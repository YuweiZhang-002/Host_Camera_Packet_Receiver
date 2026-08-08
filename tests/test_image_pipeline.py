import csv
import json
import time

from taxi_receiver.capture import SyntheticFrameSource
from taxi_receiver.image_pipeline import CameraImagePipeline
from taxi_receiver.packet_format import (
    FLAG_FIRST_ROW,
    FLAG_LAST_ROW,
    ROW_BYTES,
    build_camera_row,
)
from taxi_receiver.pipeline import TaxiReceiverPipeline
from taxi_receiver.reassembler import (
    CompletedFrame,
    FrameReassembler,
    FrameStatus,
    PacketRecord,
)

from .synthetic import make_raw_frame


def _camera_frame(
    *,
    cam_id,
    frame_id,
    row_idx,
    row_seq,
    row_flags,
    payload,
    corrupt_crc=False,
    m00=0,
    xc_q4=0,
    yc_q4=0,
    vx_q8=0,
    vy_q8=0,
):
    return make_raw_frame(
        build_camera_row(
            cam_id=cam_id,
            frame_id=frame_id,
            row_idx=row_idx,
            row_flags=row_flags,
            row_seq=row_seq,
            payload=payload,
            corrupt_crc=corrupt_crc,
            m00=m00,
            xc_q4=xc_q4,
            yc_q4=yc_q4,
            vx_q8=vx_q8,
            vy_q8=vy_q8,
        )
    )


def _run(frames, sink, expected_rows):
    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource(frames),
        mode="camera",
        max_stage="reassemble",
        reassembler=FrameReassembler(expected_rows=expected_rows),
        on_completed_frame=sink.archive_frame,
        on_frame_processed=sink.record_packet,
        report_interval=999,
        sink=lambda *_: None,
    )
    pipeline.start()
    time.sleep(0.1)
    pipeline.stop()
    # rows.csv is written by its own thread now, so the test has to wait for
    # that thread rather than for the packet worker.
    assert sink.flush_rows(timeout=10.0)
    return pipeline


def _completed_frame(*, frame_id, missing_rows=(), camera_id=0):
    missing = set(missing_rows)
    rows = {
        row_idx: bytes([row_idx & 0xFF]) * ROW_BYTES
        for row_idx in range(480)
        if row_idx not in missing
    }
    packet_records = []
    for row_idx in sorted(rows):
        flags = 0
        if row_idx == 0:
            flags |= FLAG_FIRST_ROW
        if row_idx == 479:
            flags |= FLAG_LAST_ROW
        packet_records.append(
            PacketRecord(
                packet_index=len(packet_records),
                capture_timestamp=float(row_idx),
                row_idx=row_idx,
                row_seq=row_idx,
                payload_len=ROW_BYTES,
                row_flags=flags,
                accepted=True,
            )
        )
    return CompletedFrame(
        camera_id=camera_id,
        frame_id=frame_id,
        row_count=len(rows),
        rows=rows,
        missing_rows=sorted(missing),
        had_overflow=False,
        status=(
            FrameStatus.COMPLETE if not missing else FrameStatus.PARTIAL
        ),
        close_reason="last_row" if not missing else "frame_switch",
        expected_rows=480,
        packet_records=packet_records,
        errors=[],
        conflicting_duplicates=0,
        saw_first=True,
        saw_last=True,
    )


def test_complete_cam0_frame_writes_numeric_image_and_row_csv(tmp_path):
    images = tmp_path / "images"
    sink = CameraImagePipeline(images, expected_rows=2)
    frames = [
        _camera_frame(
            cam_id=0,
            frame_id=42,
            row_idx=0,
            row_seq=100,
            row_flags=FLAG_FIRST_ROW,
            payload=bytes([0x80]) + bytes(79),
            m00=1234,
            xc_q4=160,
            yc_q4=320,
            vx_q8=256,
            vy_q8=-128,
        ),
        _camera_frame(
            cam_id=0,
            frame_id=42,
            row_idx=1,
            row_seq=101,
            row_flags=FLAG_LAST_ROW,
            payload=bytes([0x01]) + bytes(79),
        ),
    ]

    _run(frames, sink, expected_rows=2)

    cam0 = images / "cam0"
    assert (images / "cam1").is_dir()
    pgm = (cam0 / "42.pgm").read_bytes()
    header = b"P5\n640 2\n255\n"
    assert pgm.startswith(header)
    pixels = pgm[len(header):]
    assert len(pixels) == 640 * 2
    assert pixels[:8] == bytes([0xFF, 0, 0, 0, 0, 0, 0, 0])
    assert pixels[640:648] == bytes([0, 0, 0, 0, 0, 0, 0, 0xFF])
    assert (cam0 / "42.raw").read_bytes() == pixels

    metadata = json.loads((cam0 / "42.json").read_text("utf-8"))
    assert metadata["cam_id"] == 0
    assert metadata["frame_id"] == 42
    assert metadata["width"] == 640
    assert metadata["height"] == 2

    csv_text = (cam0 / "rows.csv").read_text("utf-8")
    assert csv_text.endswith("\n\n")
    with (cam0 / "rows.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["frame_id"] == "42"
    assert rows[0]["row_idx"] == "0"
    assert rows[0]["row_flags"] == "0x04"
    assert rows[0]["m00"] == "1234"
    assert rows[0]["x"] == "10.0000"
    assert rows[0]["y"] == "20.0000"
    assert rows[0]["vx"] == "1.000000"
    assert rows[0]["vy"] == "-0.500000"
    assert rows[0]["crc_ok"] == "1"
    assert rows[1]["frame_end"] == "1"
    assert rows[1]["row_flags"] == "0x02"
    assert sink.stats.completed_frames_seen == 1
    assert sink.stats.pgm_write_attempts == 1
    assert sink.stats.pgm_write_success == 1
    assert sink.stats.pgm_write_failures == 0
    assert sink.stats.raw_write_attempts == 1
    assert sink.stats.raw_write_success == 1
    assert sink.stats.raw_write_failures == 0


def test_complete_cam0_frame_is_idempotent_when_republished(tmp_path):
    images = tmp_path / "images"
    sink = CameraImagePipeline(images, expected_rows=480)
    frame = _completed_frame(frame_id=43)

    first = sink.archive_frame(frame)
    second = sink.archive_frame(frame)

    assert first == second
    assert sink.stats.images_complete == 1
    assert sink.stats.pgm_write_attempts == 1
    assert sink.stats.pgm_write_success == 1
    assert sink.stats.pgm_write_failures == 0
    assert sink.stats.raw_write_attempts == 1
    assert sink.stats.raw_write_success == 1
    assert sink.stats.raw_write_failures == 0


def test_cam1_is_isolated_and_last_bit_is_masked_not_compared_whole(tmp_path):
    images = tmp_path / "images"
    sink = CameraImagePipeline(images, expected_rows=1)
    _run(
        [
            _camera_frame(
                cam_id=1,
                frame_id=7,
                row_idx=0,
                row_seq=8,
                # FIRST+LAST is 0x06, not exactly 0x02; successful publication
                # therefore still proves the end test masks the LAST bit.
                row_flags=FLAG_FIRST_ROW | FLAG_LAST_ROW,
                payload=bytes(80),
            )
        ],
        sink,
        expected_rows=1,
    )

    assert (images / "cam1" / "7.pgm").is_file()
    assert not (images / "cam0" / "7.pgm").exists()
    text = (images / "cam1" / "rows.csv").read_text("utf-8")
    assert text.endswith("\n\n")
    with (images / "cam1" / "rows.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        row = next(csv.DictReader(handle))
    assert row["row_flags"] == "0x06"
    assert row["last_row"] == "1"
    assert row["frame_end"] == "1"


def test_bad_crc_is_logged_but_never_published_as_image(tmp_path):
    images = tmp_path / "images"
    sink = CameraImagePipeline(images, expected_rows=1)
    pipeline = _run(
        [
            _camera_frame(
                cam_id=0,
                frame_id=9,
                row_idx=0,
                row_seq=0,
                row_flags=FLAG_FIRST_ROW | FLAG_LAST_ROW,
                payload=bytes(80),
                corrupt_crc=True,
            )
        ],
        sink,
        expected_rows=1,
    )

    assert not (images / "cam0" / "9.pgm").exists()
    with (images / "cam0" / "rows.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        row = next(csv.DictReader(handle))
    assert row["crc_ok"] == "0"
    assert row["parse_ok"] == "0"
    assert row["errors"] == "crc_error"
    assert pipeline.monitor.stats.parser_errors == 0


def test_missing_row_does_not_masquerade_as_complete_photo(tmp_path):
    images = tmp_path / "images"
    sink = CameraImagePipeline(images, expected_rows=3)
    _run(
        [
            _camera_frame(
                cam_id=0,
                frame_id=11,
                row_idx=0,
                row_seq=0,
                row_flags=FLAG_FIRST_ROW,
                payload=bytes(80),
            ),
            _camera_frame(
                cam_id=0,
                frame_id=11,
                row_idx=2,
                row_seq=2,
                row_flags=FLAG_LAST_ROW,
                payload=bytes(80),
            ),
        ],
        sink,
        expected_rows=3,
    )

    assert not (images / "cam0" / "11.pgm").exists()
    with (images / "cam0" / "rows.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        rows = list(csv.DictReader(handle))
    assert [row["row_idx"] for row in rows] == ["0", "2"]
    assert rows[-1]["frame_end"] == "1"


def test_recovered_cam0_frame_is_idempotent_when_republished(tmp_path):
    sink = CameraImagePipeline(
        tmp_path / "images",
        expected_rows=480,
        image_policy="recover-zero-fill",
        max_missing_rows=4,
        max_consecutive_missing=2,
    )
    frame = _completed_frame(frame_id=44, missing_rows=(17,))

    first = sink.archive_frame(frame)
    second = sink.archive_frame(frame)

    assert first == second
    assert sink.stats.images_recovered == 1
    assert sink.stats.rows_zero_filled == 1
    assert sink.stats.pgm_write_attempts == 1
    assert sink.stats.raw_write_attempts == 1
