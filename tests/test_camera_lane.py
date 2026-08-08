"""S1 per-camera lane behaviour.

These cover the properties that make the split safe rather than merely fast:
routing cannot be steered by corrupt wire bytes, offline replay stays lossless
across *both* queues, and the two queues report separately.
"""
from __future__ import annotations

import threading
import time

import pytest

from taxi_receiver.camera_lane import (
    CameraLanePool,
    PublishPolicy,
)
from taxi_receiver.capture import SyntheticFrameSource
from taxi_receiver.packet_format import FLAG_FIRST_ROW, FLAG_LAST_ROW
from taxi_receiver.pipeline import TaxiReceiverPipeline
from taxi_receiver.reassembler import (
    CompletedFrame,
    FrameReassembler,
    FrameStatus,
)
from taxi_receiver.stages import FrameContext
from taxi_receiver.stream_monitor import StreamMonitor

from .synthetic import make_camera_frame


def build_pool(monitor, **overrides):
    kwargs = dict(
        monitor=monitor,
        output_root=None,
        images_root=None,
        enable_row_csv=False,
        expected_rows=1,
        bit_order="msb_first",
        image_policy="strict",
        max_missing_rows=0,
        max_consecutive_missing=1,
        report_interval=1e9,
        csv_queue_depth=8,
        csv_backpressure="drop",
        frame_output_queue_depth=8,
        lane_queue_depth=8,
        session_audit=None,
        storage=None,
        report_sink=lambda *_: None,
        error_sink=lambda *_: None,
    )
    kwargs.update(overrides)
    return CameraLanePool(**kwargs)


def quiet_monitor() -> StreamMonitor:
    return StreamMonitor(report_interval=1e9, sink=lambda *_: None)


def test_untrusted_cam_id_does_not_mint_a_lane():
    # A stuck data bit or bad-sync storm puts arbitrary values in the offset-18
    # routing byte.  Creating a lane per value would spawn threads, CSV writers
    # and camN directories without bound.
    monitor = quiet_monitor()
    pool = build_pool(monitor, camera_ids=(0, 1))
    threads_before = threading.active_count()
    try:
        for bogus in (2, 7, 0x8C, 0xFF):
            frame = make_camera_frame(cam_id=0, frame_id=1, row_idx=0, row_seq=0)
            frame.camera_id = bogus
            # No parsed packet, so routing must fall back to the raw peek.
            pool.submit(FrameContext(frame=frame, mode="camera"))

        assert pool._lanes == {}
        assert threading.active_count() == threads_before
        assert monitor.stats.unroutable_camera_packets == 4
    finally:
        pool.close()


def test_parsed_header_outranks_the_raw_peek_for_routing():
    monitor = quiet_monitor()
    pool = build_pool(monitor, camera_ids=(0, 1))
    try:
        frame = make_camera_frame(cam_id=1, frame_id=1, row_idx=0, row_seq=0)
        frame.camera_id = 0  # deliberately disagrees with the header
        ctx = FrameContext(frame=frame, mode="camera")
        from taxi_receiver.camera_parser import parse_camera_mode

        ctx.camera_result = parse_camera_mode(frame.payload)
        pool.submit(ctx)
        pool.close()
        assert sorted(pool._lanes) == [1]
    finally:
        pool.close()


def test_lossless_lane_queue_blocks_instead_of_dropping():
    # The capture queue already honours this contract for offline replay.  A
    # lane queue that silently dropped voided it: a lossless replay reported
    # zero capture drops while 63% of packets never reached Layer 5.
    frames = [
        make_camera_frame(
            cam_id=0,
            frame_id=index,
            row_idx=0,
            row_seq=index,
            row_flags=FLAG_FIRST_ROW | FLAG_LAST_ROW,
        )
        for index in range(200)
    ]
    monitor = quiet_monitor()
    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource(frames),
        mode="camera",
        max_stage="reassemble",
        reassembler=FrameReassembler(expected_rows=1),
        queue_depth=8,
        lossless_input=True,
        split_by_camera=True,
        report_interval=1e9,
        sink=lambda *_: None,
    )
    published: list[CompletedFrame] = []

    class SlowSink:
        def __call__(self, frame):
            time.sleep(0.001)
            published.append(frame)

    pool = build_pool(
        pipeline.monitor,
        lane_queue_depth=2,
        frame_output_queue_depth=2,
        lossless=True,
        publish_policy=PublishPolicy.ALL,
        storage=SlowSink(),
    )
    pipeline.on_frame_processed = pool.submit
    pipeline.start()
    pipeline.stop()
    pool.close()

    assert pipeline.monitor.stats.lane_queue_drops == 0
    assert pipeline.monitor.stats.dropped_capture_queue == 0
    assert pipeline.monitor.stats.camera(0).packets == len(frames)
    assert len(published) == len(frames)


def test_lane_and_capture_queue_counters_stay_separate():
    monitor = quiet_monitor()
    monitor.record_capture_queue_depth(11, camera_id=0)
    monitor.record_dropped_capture(0)
    monitor.record_lane_queue_depth(5, 0)
    monitor.record_lane_drop(0)

    camera = monitor.stats.camera(0)
    assert (camera.capture_queue_peak, camera.capture_queue_drops) == (11, 1)
    assert (camera.lane_queue_peak, camera.lane_queue_drops) == (5, 1)
    assert monitor.stats.capture_queue_peak == 11
    assert monitor.stats.lane_queue_drops == 1


def make_completed(status: FrameStatus, row_count: int) -> CompletedFrame:
    return CompletedFrame(
        camera_id=0,
        frame_id=1,
        row_count=row_count,
        rows={index: b"" for index in range(row_count)},
        missing_rows=[],
        had_overflow=False,
        status=status,
        close_reason="test",
        expected_rows=480,
    )


@pytest.mark.parametrize(
    "policy,status,row_count,expected",
    [
        (PublishPolicy.COMPLETE, FrameStatus.COMPLETE, 480, True),
        (PublishPolicy.COMPLETE, FrameStatus.PARTIAL, 300, False),
        (PublishPolicy.COMPLETE, FrameStatus.TIMEOUT, 300, False),
        (PublishPolicy.ELIGIBLE, FrameStatus.PARTIAL, 300, True),
        (PublishPolicy.ELIGIBLE, FrameStatus.PARTIAL, 0, False),
        (PublishPolicy.ALL, FrameStatus.PARTIAL, 0, True),
    ],
)
def test_publish_policy_gate(policy, status, row_count, expected):
    assert policy.accepts(make_completed(status, row_count)) is expected


def test_partial_frames_are_not_handed_to_the_sinks():
    # A partial frame costs a full to_bytes(), a sha256 and a recovery
    # assessment in every sink before being rejected anyway.
    frames = [
        # row 0 only, no LAST flag -> the session closes partial on frame switch
        make_camera_frame(cam_id=0, frame_id=1, row_idx=0, row_seq=0),
        make_camera_frame(
            cam_id=0,
            frame_id=2,
            row_idx=0,
            row_seq=1,
            row_flags=FLAG_FIRST_ROW | FLAG_LAST_ROW,
        ),
    ]
    monitor = quiet_monitor()
    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource(frames),
        mode="camera",
        max_stage="reassemble",
        reassembler=FrameReassembler(expected_rows=1),
        queue_depth=8,
        lossless_input=True,
        split_by_camera=True,
        report_interval=1e9,
        sink=lambda *_: None,
    )
    published: list[CompletedFrame] = []
    pool = build_pool(
        pipeline.monitor,
        lossless=True,
        publish_policy=PublishPolicy.COMPLETE,
        storage=published.append,
    )
    pipeline.on_frame_processed = pool.submit
    pipeline.start()
    pipeline.stop()
    pool.close()

    lane = pool.lanes[0]
    assert [frame.frame_id for frame in published] == [2]
    assert lane.frames_not_published == 1
    assert any(
        "frames not published: 1" in line for line in lane.report_lines()
    )


def test_a_broken_sink_does_not_disable_a_healthy_one(tmp_path):
    # Storage and image publication used to share one dispatcher behind a
    # fanout, so a permanently failing storage sink disabled image publication
    # with it and the reported blocked-time could not say which sink was slow.
    frames = [
        make_camera_frame(
            cam_id=0,
            frame_id=index,
            row_idx=0,
            row_seq=index,
            row_flags=FLAG_FIRST_ROW | FLAG_LAST_ROW,
        )
        for index in range(30)
    ]
    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource(frames),
        mode="camera",
        max_stage="reassemble",
        reassembler=FrameReassembler(expected_rows=1),
        queue_depth=64,
        lossless_input=True,
        split_by_camera=True,
        report_interval=1e9,
        sink=lambda *_: None,
    )

    class AlwaysFails:
        def __call__(self, _frame):
            raise RuntimeError("storage is broken")

    pool = build_pool(
        pipeline.monitor,
        images_root=tmp_path / "images",
        expected_rows=1,
        lossless=True,
        publish_policy=PublishPolicy.ALL,
        storage=AlwaysFails(),
        error_sink=lambda *_: None,
    )
    pipeline.on_frame_processed = pool.submit
    pipeline.start()
    pipeline.stop()
    pool.close()

    lane = pool.lanes[0]
    storage_sink = lane.frame_outputs["storage"]
    image_sink = lane.frame_outputs["images"]

    assert storage_sink.stats.failures == len(frames)
    assert storage_sink.stats.processed == 0
    # The healthy sink is untouched and actually produced images.
    assert image_sink.stats.failures == 0
    assert image_sink.stats.processed == len(frames)
    assert not image_sink.stats.disabled
    assert len(list((tmp_path / "images" / "cam0").glob("*.pgm"))) == len(frames)
