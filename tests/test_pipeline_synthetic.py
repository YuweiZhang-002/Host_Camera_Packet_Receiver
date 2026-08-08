import threading
import time
import sys
import types
from ctypes import Structure, c_ulong
from types import SimpleNamespace
from pathlib import Path

import pytest

import taxi_receiver.cli as cli_module
from taxi_receiver.async_sink import AsyncCallbackDispatcher
from taxi_receiver.camera_lane import CameraLanePool
from taxi_receiver.capture import ScapyLiveCapture, SyntheticFrameSource, _packet_timestamp
from taxi_receiver.packet_format import (
    FLAG_FIRST_ROW,
    FLAG_LAST_ROW,
    ROW_BYTES,
    build_camera_row,
)
from taxi_receiver.pipeline import TaxiReceiverPipeline
from taxi_receiver.reassembler import FrameReassembler, FrameStatus
from taxi_receiver.session_audit import SessionAuditLogger
from taxi_receiver.storage import StorageAndPipeline

from .synthetic import make_camera_frame, make_fixed_frame, make_raw_frame


def test_pipeline_camera_mode_end_to_end():
    frames = [
        make_camera_frame(cam_id=1, frame_id=1, row_idx=i, row_seq=i)
        for i in range(3)
    ]
    frames.append(make_camera_frame(cam_id=1, frame_id=1, row_idx=3, row_seq=3, corrupt_crc=True))

    source = SyntheticFrameSource(frames)
    pipeline = TaxiReceiverPipeline(
        frame_source=source, mode="camera", report_interval=999, sink=lambda *_: None
    )
    pipeline.start()
    time.sleep(0.3)
    pipeline.stop()

    assert pipeline.monitor.stats.valid_packets == 3
    assert pipeline.monitor.stats.bad_crc == 1
    assert pipeline.monitor.stats.camera(1).packets == 4


def test_pipeline_with_reassembler_layer5():
    frames = [
        make_camera_frame(
            cam_id=0, frame_id=7, row_idx=0, row_seq=0,
            row_flags=FLAG_FIRST_ROW,
        ),
        make_camera_frame(
            cam_id=0, frame_id=7, row_idx=1, row_seq=1,
            row_flags=FLAG_LAST_ROW,
        ),
    ]
    completed_frames = []

    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource(frames),
        mode="camera",
        max_stage="reassemble",  # Layer 1-5; default "monitor" would stop at Layer 4
        reassembler=FrameReassembler(),
        report_interval=999,
        sink=lambda *_: None,
        on_completed_frame=completed_frames.append,
    )
    pipeline.start()
    time.sleep(0.3)
    pipeline.stop()

    assert len(completed_frames) == 1
    assert completed_frames[0].camera_id == 0
    assert completed_frames[0].frame_id == 7
    assert completed_frames[0].row_count == 2


def test_frame_output_logs_named_callback_failures():
    messages = []
    seen = []

    def storage(_frame):
        seen.append("storage")
        raise FileExistsError("output already exists")

    def image_publication(_frame):
        seen.append("image publication")
        raise ValueError("geometry mismatch")

    dispatcher = AsyncCallbackDispatcher(
        cli_module._fanout_callbacks(
            ("storage", storage),
            ("image publication", image_publication),
        ),
        queue_depth=1,
        name="test-frame-writer",
        error_sink=messages.append,
    )

    dispatcher.submit(object())
    dispatcher.close()

    assert seen == ["storage", "image publication"]
    assert dispatcher.stats.submitted == 1
    assert dispatcher.stats.processed == 0
    assert dispatcher.stats.failures == 1
    assert len(messages) == 1
    assert messages[0] == (
        "[FRAME OUTPUT ERROR] storage failed: output already exists; "
        "image publication failed: geometry mismatch"
    )


def test_fanout_callbacks_still_accept_plain_callables():
    calls = []

    def first(value):
        calls.append(("first", value))

    def second(value):
        calls.append(("second", value))

    invoke = cli_module._fanout_callbacks(first, second)
    assert invoke is not None

    marker = object()
    invoke(marker)

    assert calls == [("first", marker), ("second", marker)]


def test_final_report_includes_live_pcap_drop_stats():
    class StatsFrameSource:
        def __init__(self):
            self.frames = []

        def start(self, on_frame):
            self.frames.append("started")

        def stop(self):
            self.frames.append("stopped")

        def pcap_stats(self):
            class Stats:
                ps_recv = 12
                ps_drop = 3
                ps_ifdrop = 1

            return Stats()

    lines = []
    pipeline = TaxiReceiverPipeline(
        frame_source=StatsFrameSource(),
        mode="camera",
        report_interval=999,
        sink=lines.append,
    )
    pipeline.print_final_report()

    joined = "\n".join(lines)
    assert "LIVE PCAP STATS" in joined
    assert "ps_recv" in joined
    assert "ps_drop" in joined
    assert "ps_ifdrop" in joined


def test_scapy_live_capture_caches_pcap_stats_across_stop(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.pcap_fd = SimpleNamespace(pcap=object())
            self.closed = False

        def close(self):
            self.closed = True

    class FakeSniffer:
        def __init__(self, opened_socket=None, prn=None, store=None):
            self.opened_socket = opened_socket
            self.prn = prn
            self.store = store
            self.running = False

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    class FakeStat(Structure):
        _fields_ = [
            ("ps_recv", c_ulong),
            ("ps_drop", c_ulong),
            ("ps_ifdrop", c_ulong),
        ]

    fake_socket = FakeSocket()

    def fake_pcap_stats(_handle, stat_ptr):
        stat = stat_ptr._obj
        stat.ps_recv = 42
        stat.ps_drop = 7
        stat.ps_ifdrop = 1
        return 0

    scapy_module = types.ModuleType("scapy")
    scapy_libs_module = types.ModuleType("scapy.libs")
    winpcapy_module = types.ModuleType("scapy.libs.winpcapy")
    winpcapy_module.pcap_stat = FakeStat
    winpcapy_module.pcap_stats = fake_pcap_stats
    scapy_libs_module.winpcapy = winpcapy_module
    scapy_module.libs = scapy_libs_module
    monkeypatch.setitem(sys.modules, "scapy", scapy_module)
    monkeypatch.setitem(sys.modules, "scapy.libs", scapy_libs_module)
    monkeypatch.setitem(sys.modules, "scapy.libs.winpcapy", winpcapy_module)

    capture = ScapyLiveCapture.__new__(ScapyLiveCapture)
    capture.interface = "fake0"
    capture.ether_type = 0x88B5
    capture.include_raw = True
    capture._sniffer = FakeSniffer()
    capture._socket = fake_socket
    capture._pcap_stats_snapshot = None

    before_stop = capture.pcap_stats()
    assert before_stop is not None
    assert before_stop.ps_drop == 7

    capture.stop()

    after_stop = capture.pcap_stats()
    assert after_stop is not None
    assert after_stop.ps_drop == 7
    assert fake_socket.closed


def test_scapy_live_capture_uses_pcap_buffer_size_before_activation(monkeypatch):
    calls = []
    fake_handle = object()

    def record(name, return_value=0):
        def _inner(*args, **kwargs):
            calls.append((name, args, kwargs))
            return return_value

        return _inner

    def fake_next_packet(self, _winpcapy, _handle):
        raise RuntimeError("stop thread")

    fake_winpcapy = types.ModuleType("scapy.libs.winpcapy")
    fake_winpcapy.PCAP_ERRBUF_SIZE = 256
    class FakeBpfProgram(Structure):
        _fields_ = []

    fake_winpcapy.bpf_program = FakeBpfProgram
    fake_winpcapy.pcap_create = record("pcap_create", fake_handle)
    fake_winpcapy.pcap_set_snaplen = record("pcap_set_snaplen")
    fake_winpcapy.pcap_set_promisc = record("pcap_set_promisc")
    fake_winpcapy.pcap_set_timeout = record("pcap_set_timeout")
    fake_winpcapy.pcap_set_buffer_size = record("pcap_set_buffer_size")
    fake_winpcapy.pcap_activate = record("pcap_activate")
    fake_winpcapy.pcap_compile = record("pcap_compile")
    fake_winpcapy.pcap_setfilter = record("pcap_setfilter")
    fake_winpcapy.pcap_freecode = record("pcap_freecode")
    fake_winpcapy.pcap_setmintocopy = record("pcap_setmintocopy")
    class FakeStat(Structure):
        _fields_ = [
            ("ps_recv", c_ulong),
            ("ps_drop", c_ulong),
            ("ps_ifdrop", c_ulong),
        ]

    fake_winpcapy.pcap_stat = FakeStat
    fake_winpcapy.pcap_stats = lambda _handle, _stat_ptr: 0
    fake_winpcapy.pcap_geterr = lambda _handle: b"fake error"
    fake_winpcapy.pcap_breakloop = record("pcap_breakloop")
    fake_winpcapy.pcap_close = record("pcap_close")

    fake_layers_l2 = types.ModuleType("scapy.layers.l2")

    fake_scapy_all = types.ModuleType("scapy.all")
    fake_scapy_all.conf = SimpleNamespace(use_npcap=True)

    fake_scapy_libs = types.ModuleType("scapy.libs")
    fake_scapy_libs.winpcapy = fake_winpcapy
    fake_scapy = types.ModuleType("scapy")
    fake_scapy.all = fake_scapy_all
    fake_scapy.libs = fake_scapy_libs
    fake_scapy.layers = types.ModuleType("scapy.layers")
    fake_scapy.layers.l2 = fake_layers_l2

    monkeypatch.setitem(sys.modules, "scapy", fake_scapy)
    monkeypatch.setitem(sys.modules, "scapy.all", fake_scapy_all)
    monkeypatch.setitem(sys.modules, "scapy.libs", fake_scapy_libs)
    monkeypatch.setitem(sys.modules, "scapy.libs.winpcapy", fake_winpcapy)
    monkeypatch.setitem(sys.modules, "scapy.layers", fake_scapy.layers)
    monkeypatch.setitem(sys.modules, "scapy.layers.l2", fake_layers_l2)
    monkeypatch.setattr(ScapyLiveCapture, "_next_packet", fake_next_packet)

    capture = ScapyLiveCapture(
        "fake0",
        ether_type=0x88B5,
        include_raw=True,
        pcap_buffer_size=2 * 1024 * 1024,
        read_timeout_ms=250,
    )
    received = []

    capture.start(received.append)
    time.sleep(0.1)
    capture.stop()

    names = [name for name, *_ in calls]
    assert names[0] == "pcap_create"
    assert names.index("pcap_set_buffer_size") < names.index("pcap_activate")
    assert names.index("pcap_activate") < names.index("pcap_setfilter")
    assert names.index("pcap_setfilter") < names.index("pcap_setmintocopy")
    assert names.index("pcap_setmintocopy") < names.index("pcap_close")


def test_pipeline_fixed_mode():
    frames = [make_fixed_frame(), make_fixed_frame(corrupt=True)]
    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource(frames), mode="fixed",
        report_interval=999, sink=lambda *_: None,
    )
    pipeline.start()
    time.sleep(0.3)
    pipeline.stop()

    assert pipeline.monitor.stats.valid_packets == 1
    assert pipeline.monitor.stats.bad_fixed_payload == 1


def test_bad_sync_is_counted_but_cannot_create_image_session():
    payload = build_camera_row(
        cam_id=0,
        frame_id=24618,
        row_idx=0,
        row_flags=FLAG_LAST_ROW,
        row_seq=1,
        payload=bytes(ROW_BYTES),
        sync0=0x1111,
        sync1=0x2222,
    )
    completed_frames = []
    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource([make_raw_frame(payload)]),
        mode="camera",
        max_stage="reassemble",
        reassembler=FrameReassembler(expected_rows=1),
        report_interval=999,
        sink=lambda *_: None,
        on_completed_frame=completed_frames.append,
    )

    pipeline.start()
    pipeline.stop()

    assert pipeline.monitor.stats.valid_packets == 0
    assert pipeline.monitor.stats.camera(0).packets == 1
    assert pipeline.monitor.stats.camera(0).last_row_packets == 1
    # Monitoring/session-audit retain the packet error, but Layer 5 must not
    # trust frame_id=24618 from a packet whose sync words are invalid.
    assert completed_frames == []


def test_slow_frame_storage_is_decoupled_from_capture_queue():
    """A slow image/archive callback must not stall the capture consumer."""

    frames = [
        make_camera_frame(
            cam_id=0,
            frame_id=index,
            row_idx=0,
            row_seq=index,
            row_flags=FLAG_FIRST_ROW | FLAG_LAST_ROW,
        )
        for index in range(100)
    ]

    def slow_store(_frame):
        time.sleep(0.002)

    class PacedFrameSource:
        def start(self, on_frame):
            for frame in frames:
                on_frame(frame)
                time.sleep(0.0005)

        def stop(self):
            pass

    direct = TaxiReceiverPipeline(
        frame_source=PacedFrameSource(),
        mode="camera",
        max_stage="reassemble",
        reassembler=FrameReassembler(expected_rows=1),
        queue_depth=2,
        report_interval=999,
        sink=lambda *_: None,
        on_completed_frame=slow_store,
    )
    direct.start()
    direct.stop()
    assert direct.monitor.stats.dropped_capture_queue > 0

    dispatcher = AsyncCallbackDispatcher(
        slow_store,
        queue_depth=len(frames),
        name="test-frame-writer",
        error_sink=lambda _message: None,
    )
    asynchronous = TaxiReceiverPipeline(
        frame_source=PacedFrameSource(),
        mode="camera",
        max_stage="reassemble",
        reassembler=FrameReassembler(expected_rows=1),
        queue_depth=2,
        report_interval=999,
        sink=lambda *_: None,
        on_completed_frame=dispatcher.submit,
    )
    asynchronous.start()
    asynchronous.stop()
    dispatcher.close()

    assert asynchronous.monitor.stats.dropped_capture_queue == 0
    assert dispatcher.stats.submitted == len(frames)
    assert dispatcher.stats.processed == len(frames)
    assert dispatcher.stats.failures == 0
    assert dispatcher.stats.queue_peak > 0


def test_split_by_camera_routes_each_camera_into_its_own_lane(tmp_path):
    frames = [
        make_camera_frame(
            cam_id=0,
            frame_id=10,
            row_idx=0,
            row_seq=0,
            row_flags=FLAG_FIRST_ROW | FLAG_LAST_ROW,
        ),
        make_camera_frame(
            cam_id=1,
            frame_id=20,
            row_idx=0,
            row_seq=0,
            row_flags=FLAG_FIRST_ROW | FLAG_LAST_ROW,
        ),
    ]

    archive_root = tmp_path / "archive"
    images_root = tmp_path / "images"
    storage = StorageAndPipeline(archive_root)
    session_audit = SessionAuditLogger(archive_root)

    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource(frames),
        mode="camera",
        max_stage="reassemble",
        reassembler=FrameReassembler(expected_rows=1),
        queue_depth=8,
        report_interval=999,
        sink=lambda *_: None,
        split_by_camera=True,
    )
    lane_pool = CameraLanePool(
        monitor=pipeline.monitor,
        output_root=archive_root,
        images_root=images_root,
        enable_row_csv=True,
        expected_rows=1,
        bit_order="msb_first",
        image_policy="strict",
        max_missing_rows=0,
        max_consecutive_missing=1,
        report_interval=999,
        csv_queue_depth=8,
        csv_backpressure="drop",
        frame_output_queue_depth=8,
        lane_queue_depth=4,
        session_audit=session_audit,
        storage=storage,
        report_sink=lambda *_: None,
        error_sink=lambda *_: None,
    )
    pipeline.on_frame_processed = lane_pool.submit

    pipeline.start()
    pipeline.stop()
    lane_pool.close()
    session_audit.close()
    storage.close()

    assert pipeline.monitor.stats.camera(0).packets == 1
    assert pipeline.monitor.stats.camera(1).packets == 1
    assert pipeline.monitor.stats.camera(0).capture_queue_drops == 0
    assert pipeline.monitor.stats.camera(1).capture_queue_drops == 0
    assert (images_root / "cam0" / "10.pgm").is_file()
    assert (images_root / "cam1" / "20.pgm").is_file()
    report_lines = lane_pool.report_lines()
    assert any("CAMERA LANE 0" in line for line in report_lines)
    assert any("CAMERA LANE 1" in line for line in report_lines)


def test_packet_timestamp_prefers_captured_packet_time():
    class DummyPacket:
        time = 123.456

    assert _packet_timestamp(DummyPacket()) == 123.456


def test_setting_on_completed_frame_after_construction_reaches_the_stage():
    # ReassemblyStage captures this callback at construction.  Assigning the
    # plain attribute afterwards left the stage holding None, so frames
    # completed during the run were silently unpublished and only the
    # stop()-time flush reached the sink.
    frames = [
        make_camera_frame(
            cam_id=0, frame_id=1, row_idx=0, row_seq=0, row_flags=FLAG_FIRST_ROW
        ),
        make_camera_frame(
            cam_id=0, frame_id=1, row_idx=1, row_seq=1, row_flags=FLAG_LAST_ROW
        ),
        make_camera_frame(
            cam_id=0, frame_id=2, row_idx=0, row_seq=2, row_flags=FLAG_FIRST_ROW
        ),
        make_camera_frame(
            cam_id=0, frame_id=2, row_idx=1, row_seq=3, row_flags=FLAG_LAST_ROW
        ),
    ]
    published = []
    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource(frames),
        mode="camera",
        max_stage="reassemble",
        reassembler=FrameReassembler(expected_rows=2),
        queue_depth=8,
        lossless_input=True,
        report_interval=999,
        sink=lambda *_: None,
    )
    pipeline.on_completed_frame = published.append

    pipeline.start()
    pipeline.stop()

    assert [frame.frame_id for frame in published] == [1, 2]
    assert all(
        stage.on_completed_frame is not None
        for stage in pipeline._reassembly_stages
    )


def test_dispatcher_records_how_long_submit_was_blocked():
    release = threading.Event()

    def slow(_item):
        release.wait(2.0)

    dispatcher = AsyncCallbackDispatcher(
        slow, queue_depth=1, error_sink=lambda *_: None
    )
    try:
        dispatcher.submit("first")   # taken by the worker
        dispatcher.submit("second")  # fills the queue
        blocker = threading.Thread(target=dispatcher.submit, args=("third",))
        blocker.start()
        time.sleep(0.25)
        release.set()
        blocker.join(timeout=5.0)
        assert not blocker.is_alive()
    finally:
        release.set()
        dispatcher.close()

    assert dispatcher.stats.submit_blocked_count >= 1
    assert dispatcher.stats.submit_blocked_seconds > 0.0
    assert any(
        "submit blocked" in line for line in dispatcher.report_lines()
    )


def test_dispatcher_disables_itself_after_repeated_failures():
    # 2767 identical tracebacks is not evidence; it is a second bottleneck.
    errors = []

    def always_fails(_item):
        raise RuntimeError("sink is broken")

    dispatcher = AsyncCallbackDispatcher(
        always_fails,
        queue_depth=32,
        max_consecutive_failures=5,
        error_report_limit=3,
        error_sink=errors.append,
    )
    try:
        for index in range(40):
            dispatcher.submit(index)
            if dispatcher.stats.disabled:
                break
        for index in range(10):
            dispatcher.submit(f"after-{index}")
    finally:
        dispatcher.close()

    assert dispatcher.stats.disabled
    # The breaker stops new submissions; whatever the producer already queued
    # still drains through the broken sink, so failures is bounded by the
    # backlog rather than exactly the threshold.
    assert dispatcher.stats.failures >= 5
    assert dispatcher.stats.dropped_after_disable == 10
    assert sum("FRAME OUTPUT ERROR" in message for message in errors) == 3
    assert any("FRAME OUTPUT DISABLED" in message for message in errors)
