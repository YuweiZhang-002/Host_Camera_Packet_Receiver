from __future__ import annotations

import csv
import time

from taxi_receiver.camera_parser import parse_camera_mode
from taxi_receiver.capture import RawEthernetFrame, SyntheticFrameSource
from taxi_receiver.packet_format import (
    ROW_BYTES,
    SYNC0_DEFAULT,
    build_camera_row,
)
from taxi_receiver.pipeline import TaxiReceiverPipeline
from taxi_receiver.session_audit import SessionAuditLogger
from taxi_receiver.stages import FrameContext, ParsingStage


def _context(
    *,
    cam_id: int = 0,
    frame_id: int = 10,
    row_idx: int = 0,
    row_flags: int = 0,
    fpga_status: int = 0,
    row_seq: int = 100,
    timestamp: float = 1.0,
    sync0: int = SYNC0_DEFAULT,
    corrupt_crc: bool = False,
) -> FrameContext:
    payload = build_camera_row(
        cam_id=cam_id,
        frame_id=frame_id,
        row_idx=row_idx,
        row_flags=row_flags,
        fpga_status=fpga_status,
        row_seq=row_seq,
        payload=bytes([row_idx & 0xFF]) * ROW_BYTES,
        sync0=sync0,
        corrupt_crc=corrupt_crc,
        m00=1234,
        xc_q4=12,
        yc_q4=34,
        vx_q8=-5,
        vy_q8=6,
    )
    frame = RawEthernetFrame(
        src_mac="02:00:00:00:00:02",
        dst_mac="ff:ff:ff:ff:ff:ff",
        ethertype=0x88B5,
        payload=payload,
        raw_bytes=b"",
        timestamp=timestamp,
    )
    return FrameContext(
        frame=frame,
        mode="camera",
        camera_result=parse_camera_mode(payload),
    )


def _rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_length_error_propagates_without_mutating_raw_flags(tmp_path):
    audit = SessionAuditLogger(tmp_path)
    audit(_context(row_idx=0, row_flags=0x00, row_seq=100))
    audit(_context(row_idx=1, row_flags=0x00, fpga_status=0x08, row_seq=101))
    audit(_context(row_idx=2, row_flags=0x00, row_seq=102))
    audit.close()

    rows = _rows(tmp_path / "session_audit.csv")
    assert [row["row_flags_raw"] for row in rows] == [
        "0x00",
        "0x00",
        "0x00",
    ]
    assert [row["row_flags_effective"] for row in rows] == [
        "0x00",
        "0x08",
        "0x08",
    ]
    assert [row["fpga_status"] for row in rows] == [
        "0x00",
        "0x08",
        "0x00",
    ]


def test_new_frame_and_different_camera_clear_contamination(tmp_path):
    audit = SessionAuditLogger(tmp_path)
    audit(_context(
        cam_id=0, frame_id=10, row_flags=0x00,
        fpga_status=0x08, row_seq=100,
    ))
    audit(_context(cam_id=1, frame_id=10, row_flags=0x00, row_seq=200))
    audit(_context(cam_id=0, frame_id=11, row_flags=0x00, row_seq=101))
    audit.close()

    rows = _rows(tmp_path / "session_audit.csv")
    assert rows[0]["row_flags_effective"] == "0x08"
    assert rows[1]["row_flags_effective"] == "0x00"
    assert rows[2]["row_flags_effective"] == "0x00"


def test_large_non_wrap_rollback_overwrites_csv(tmp_path):
    audit = SessionAuditLogger(tmp_path, rollback_threshold=1024)
    audit(_context(frame_id=5000, row_seq=20000, timestamp=1.0))
    audit(_context(frame_id=1, row_seq=2, timestamp=2.0))
    audit.close()

    rows = _rows(tmp_path / "session_audit.csv")
    assert len(rows) == 1
    assert rows[0]["timestamp"] == "2.000000000"
    assert rows[0]["frame_id"] == "1"
    assert audit.reset_count == 1


def test_normal_16_bit_wrap_does_not_overwrite_csv(tmp_path):
    audit = SessionAuditLogger(tmp_path)
    audit(_context(frame_id=0xFFFF, row_seq=0xFFFF, timestamp=1.0))
    audit(_context(frame_id=0, row_seq=0, timestamp=2.0))
    audit.close()

    rows = _rows(tmp_path / "session_audit.csv")
    assert len(rows) == 2
    assert audit.reset_count == 0


def test_layer3_failure_is_still_audited(tmp_path):
    audit = SessionAuditLogger(tmp_path)
    bad_sync = _context(sync0=0x9095)
    assert bad_sync.camera_result is not None
    assert not bad_sync.camera_result.ok
    assert "bad_sync" in bad_sync.camera_result.errors
    audit(bad_sync)

    bad_crc = _context(row_idx=1, row_seq=101, corrupt_crc=True)
    assert bad_crc.camera_result is not None
    assert not bad_crc.camera_result.ok
    assert "crc_error" in bad_crc.camera_result.errors
    audit(bad_crc)
    audit.close()

    rows = _rows(tmp_path / "session_audit.csv")
    assert len(rows) == 2
    assert rows[0]["frame_id"] == "10"
    assert rows[0]["crc_ok"] == "1"
    assert rows[0]["validation_status"] == "FAIL"
    assert rows[0]["reject_reason"] == "bad_sync"
    assert rows[1]["crc_ok"] == "0"
    assert rows[1]["validation_status"] == "FAIL"
    assert rows[1]["reject_reason"] == "crc_error"


def test_bad_packet_metadata_does_not_trigger_session_overwrite(tmp_path):
    audit = SessionAuditLogger(tmp_path, rollback_threshold=1024)
    audit(_context(frame_id=5000, row_seq=20000, timestamp=1.0))
    bad = _context(
        frame_id=1,
        row_seq=2,
        sync0=0x9095,
        timestamp=2.0,
    )
    assert bad.camera_result is not None
    assert not bad.camera_result.ok
    audit(bad)
    audit.close()

    rows = _rows(tmp_path / "session_audit.csv")
    assert len(rows) == 2
    assert audit.reset_count == 0


def test_bad_length_without_packet_still_writes_row(tmp_path):
    frame = RawEthernetFrame(
        src_mac="02:00:00:00:00:02",
        dst_mac="ff:ff:ff:ff:ff:ff",
        ethertype=0x88B5,
        payload=b"\x00" * 127,
        raw_bytes=b"",
        timestamp=3.0,
    )
    ctx = FrameContext(
        frame=frame,
        mode="camera",
        camera_result=parse_camera_mode(frame.payload),
    )
    assert ctx.camera_result is not None
    assert ctx.camera_result.reason == "bad_length"
    assert ctx.camera_result.packet is None

    audit = SessionAuditLogger(tmp_path)
    audit(ctx)
    audit.close()

    rows = _rows(tmp_path / "session_audit.csv")
    assert len(rows) == 1
    assert rows[0]["timestamp"] == "3.000000000"
    assert rows[0]["frame_id"] == ""


def test_pipeline_audits_packet_when_later_stage_raises(tmp_path):
    class FailingStorageStage:
        name = "failing_storage"

        def process(self, ctx):
            raise RuntimeError("synthetic storage refusal")

    frame_ctx = _context(frame_id=22, row_idx=3, row_seq=400)
    audit = SessionAuditLogger(tmp_path)
    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource([frame_ctx.frame]),
        mode="camera",
        stages=[ParsingStage("camera"), FailingStorageStage()],
        report_interval=999,
        sink=lambda *_: None,
        on_frame_processed=audit,
    )
    pipeline.start()
    time.sleep(0.2)
    pipeline.stop()
    audit.close()

    rows = _rows(tmp_path / "session_audit.csv")
    assert len(rows) == 1
    assert rows[0]["frame_id"] == "22"
    assert pipeline.monitor.stats.parser_errors == 0
    assert pipeline.monitor.stats.processing_errors == 1
