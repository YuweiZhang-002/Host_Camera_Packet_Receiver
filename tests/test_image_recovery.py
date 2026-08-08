import json

from taxi_receiver.image_pipeline import CameraImagePipeline, ImagePolicy
from taxi_receiver.packet_format import (
    FLAG_FIRST_ROW,
    FLAG_FRAME_OVERFLOW,
    FLAG_LAST_ROW,
    ROW_BYTES,
)
from taxi_receiver.reassembler import CompletedFrame, FrameStatus, PacketRecord
from taxi_receiver.threshold_recover import ROW_PIXELS


EXPECTED_ROWS = 480


def _frame(
    *,
    frame_id=100,
    missing=(),
    last_seen=True,
    overflow=False,
    packet_error=None,
    conflict=False,
    out_of_range=False,
    sequence_override=None,
):
    missing_set = set(missing)
    rows = {
        row_idx: bytes([row_idx & 0xFF]) * ROW_BYTES
        for row_idx in range(EXPECTED_ROWS)
        if row_idx not in missing_set
    }
    records = []
    for row_idx in sorted(rows):
        flags = 0
        if row_idx == 0:
            flags |= FLAG_FIRST_ROW
        if row_idx == EXPECTED_ROWS - 1 and last_seen:
            flags |= FLAG_LAST_ROW
        row_seq = (
            sequence_override
            if sequence_override is not None and row_idx == 200
            else row_idx
        )
        records.append(
            PacketRecord(
                packet_index=len(records),
                capture_timestamp=float(row_idx),
                row_idx=row_idx,
                row_seq=row_seq,
                payload_len=ROW_BYTES,
                row_flags=flags,
                accepted=True,
            )
        )

    errors = []
    if packet_error is not None:
        rejected_row = min(missing_set) if missing_set else 10
        records.append(
            PacketRecord(
                packet_index=len(records),
                capture_timestamp=999.0,
                row_idx=rejected_row,
                row_seq=rejected_row,
                payload_len=ROW_BYTES,
                row_flags=0,
                accepted=False,
                errors=(packet_error,),
            )
        )
        errors.append(
            {
                "kind": "packet_validation",
                "row_idx": rejected_row,
                "errors": [packet_error],
            }
        )
    if conflict:
        records.append(
            PacketRecord(
                packet_index=len(records),
                capture_timestamp=1000.0,
                row_idx=20,
                row_seq=20,
                payload_len=ROW_BYTES,
                row_flags=0,
                accepted=False,
                duplicate=True,
                conflicting_duplicate=True,
            )
        )
        errors.append({"kind": "conflicting_duplicate", "row_idx": 20})
    if out_of_range:
        rows[480] = bytes([0xEE]) * ROW_BYTES
        records.append(
            PacketRecord(
                packet_index=len(records),
                capture_timestamp=1001.0,
                row_idx=480,
                row_seq=480,
                payload_len=ROW_BYTES,
                row_flags=0,
                accepted=True,
            )
        )

    if overflow:
        records[0].row_flags |= FLAG_FRAME_OVERFLOW

    corrupt = overflow or packet_error is not None or conflict
    complete = not missing_set and last_seen and not corrupt and not out_of_range
    return CompletedFrame(
        camera_id=0,
        frame_id=frame_id,
        row_count=len(rows),
        rows=rows,
        missing_rows=sorted(missing_set),
        had_overflow=overflow,
        status=FrameStatus.COMPLETE if complete else (
            FrameStatus.CORRUPT if corrupt else FrameStatus.PARTIAL
        ),
        close_reason="last_row" if complete else "frame_switch",
        expected_rows=EXPECTED_ROWS,
        packet_records=records,
        errors=errors,
        conflicting_duplicates=1 if conflict else 0,
        saw_first=True,
        saw_last=last_seen,
    )


def _recovering_sink(tmp_path, **kwargs):
    return CameraImagePipeline(
        tmp_path / "images",
        expected_rows=EXPECTED_ROWS,
        image_policy=ImagePolicy.RECOVER_ZERO_FILL,
        max_missing_rows=4,
        max_consecutive_missing=2,
        report_interval=999,
        report_sink=lambda _line: None,
        **kwargs,
    )


def _recovered_dir(tmp_path, frame_id=100):
    return tmp_path / "images" / "cam0" / "recovered" / f"frame_{frame_id}"


def test_complete_480_rows_remains_complete_without_fill(tmp_path):
    sink = _recovering_sink(tmp_path)
    path = sink.archive_frame(_frame())
    assert path == tmp_path / "images" / "cam0" / "100.pgm"
    assert sink.stats.images_complete == 1
    assert sink.stats.images_recovered == 0
    assert sink.stats.rows_zero_filled == 0


def test_one_missing_row_is_recovered_and_zero_filled(tmp_path):
    sink = _recovering_sink(tmp_path)
    path = sink.archive_frame(_frame(missing=(17,)))
    raw = (_recovered_dir(tmp_path) / "image.raw").read_bytes()
    assert path == _recovered_dir(tmp_path) / "image.pgm"
    assert raw[17 * ROW_PIXELS:(17 + 1) * ROW_PIXELS] == bytes(ROW_PIXELS)
    assert sink.stats.images_recovered == 1
    assert sink.stats.rows_zero_filled == 1


def test_four_nonconsecutive_missing_rows_are_recovered(tmp_path):
    missing = (1, 100, 250, 478)
    sink = _recovering_sink(tmp_path)
    sink.archive_frame(_frame(missing=missing))
    raw = (_recovered_dir(tmp_path) / "image.raw").read_bytes()
    for row_idx in missing:
        assert raw[row_idx * ROW_PIXELS:(row_idx + 1) * ROW_PIXELS] == bytes(
            ROW_PIXELS
        )
    metadata = json.loads(
        (_recovered_dir(tmp_path) / "metadata.json").read_text("utf-8")
    )
    assert metadata["status"] == "RECOVERED"
    assert metadata["missing_rows"] == list(missing)
    assert metadata["missing_count"] == 4
    assert metadata["max_consecutive_missing"] == 1
    assert metadata["fill_policy"] == "zero"
    assert metadata["row_bytes"] == 640


def test_five_missing_rows_are_rejected(tmp_path):
    sink = _recovering_sink(tmp_path)
    assert sink.archive_frame(_frame(missing=(1, 3, 5, 7, 9))) is None
    assert sink.stats.reject_reasons["too_many_missing_rows"] == 1


def test_two_consecutive_missing_rows_are_rejected(tmp_path):
    sink = _recovering_sink(tmp_path)
    assert sink.archive_frame(_frame(missing=(20, 21))) is None
    assert sink.stats.reject_reasons["consecutive_missing_rows"] == 1


def test_overflow_is_rejected(tmp_path):
    sink = _recovering_sink(tmp_path)
    assert sink.archive_frame(_frame(missing=(20,), overflow=True)) is None
    assert sink.stats.reject_reasons["overflow"] == 1


def test_bad_sync_is_rejected(tmp_path):
    sink = _recovering_sink(tmp_path)
    assert sink.archive_frame(
        _frame(missing=(20,), packet_error="bad_sync")
    ) is None
    assert sink.stats.reject_reasons["bad_sync"] == 1


def test_crc_error_is_rejected(tmp_path):
    sink = _recovering_sink(tmp_path)
    assert sink.archive_frame(
        _frame(missing=(20,), packet_error="crc_error")
    ) is None
    assert sink.stats.reject_reasons["crc_error"] == 1


def test_conflicting_duplicate_is_rejected(tmp_path):
    sink = _recovering_sink(tmp_path)
    assert sink.archive_frame(_frame(missing=(20,), conflict=True)) is None
    assert sink.stats.reject_reasons["conflicting_duplicate"] == 1


def test_out_of_range_row_is_rejected(tmp_path):
    sink = _recovering_sink(tmp_path)
    assert sink.archive_frame(
        _frame(missing=(20,), out_of_range=True)
    ) is None
    assert sink.stats.reject_reasons["row_idx_out_of_range"] == 1


def test_missing_reliable_last_row_is_rejected(tmp_path):
    sink = _recovering_sink(tmp_path)
    assert sink.archive_frame(
        _frame(missing=(20,), last_seen=False)
    ) is None
    assert sink.stats.reject_reasons["reliable_last_row_not_seen"] == 1


def test_severe_row_sequence_discontinuity_is_rejected(tmp_path):
    sink = _recovering_sink(tmp_path)
    assert sink.archive_frame(
        _frame(missing=(20,), sequence_override=9000)
    ) is None
    assert sink.stats.reject_reasons["row_seq_discontinuity"] == 1


def test_default_strict_policy_still_does_not_publish_missing_row(tmp_path):
    sink = CameraImagePipeline(
        tmp_path / "images",
        expected_rows=EXPECTED_ROWS,
        report_interval=999,
        report_sink=lambda _line: None,
    )
    assert sink.archive_frame(_frame(missing=(20,))) is None
    assert not (tmp_path / "images" / "cam0" / "100.pgm").exists()
    assert not _recovered_dir(tmp_path).exists()
    assert not (tmp_path / "images" / "cam0" / "rejected.csv").exists()


def test_complete_raw_and_pgm_are_identical_in_both_policies(tmp_path):
    strict = CameraImagePipeline(
        tmp_path / "strict",
        expected_rows=EXPECTED_ROWS,
        report_interval=999,
        report_sink=lambda _line: None,
    )
    recovery = _recovering_sink(tmp_path / "recovery")
    strict.archive_frame(_frame())
    recovery.archive_frame(_frame())
    assert (
        tmp_path / "strict" / "cam0" / "100.raw"
    ).read_bytes() == (
        tmp_path / "recovery" / "images" / "cam0" / "100.raw"
    ).read_bytes()
    assert (
        tmp_path / "strict" / "cam0" / "100.pgm"
    ).read_bytes() == (
        tmp_path / "recovery" / "images" / "cam0" / "100.pgm"
    ).read_bytes()


def test_recovered_output_only_uses_recovered_directory(tmp_path):
    sink = _recovering_sink(tmp_path)
    sink.archive_frame(_frame(missing=(20,)))
    recovered = _recovered_dir(tmp_path)
    assert (recovered / "image.pgm").is_file()
    assert (recovered / "image.raw").is_file()
    assert (recovered / "metadata.json").is_file()
    assert not (tmp_path / "images" / "cam0" / "100.pgm").exists()


def test_publication_envelope_round_trip_preserves_missing_rows():
    # to_bytes() zero-fills absent rows, so the blob alone cannot tell "row
    # missing" from "row of zero pixels".  Without present_rows/missing_rows the
    # child process would see every frame as complete and publish zero-filled
    # rows as real data.
    from taxi_receiver.image_pipeline import (
        _envelope_to_frame,
        _frame_to_envelope,
    )

    rows = {0: bytes([0xAA]) * ROW_BYTES, 2: bytes(ROW_BYTES)}
    frame = CompletedFrame(
        camera_id=1,
        frame_id=77,
        row_count=2,
        rows=dict(rows),
        missing_rows=[1],
        had_overflow=False,
        status=FrameStatus.PARTIAL,
        close_reason="frame_switch",
        expected_rows=3,
        saw_first=True,
        saw_last=False,
    )

    restored = _envelope_to_frame(_frame_to_envelope(frame))

    assert restored.camera_id == 1
    assert restored.frame_id == 77
    assert restored.status is FrameStatus.PARTIAL
    assert restored.missing_rows == [1]
    # Row 1 stays absent; row 2 is a genuine all-zero row and must be kept.
    assert sorted(restored.rows) == [0, 2]
    assert restored.rows[0] == rows[0]
    assert restored.rows[2] == rows[2]
    assert restored.to_bytes(3) == frame.to_bytes(3)


def test_publisher_stop_sentinel_survives_pickling():
    # A bare object() sentinel unpickles into a different object in a spawned
    # child, so `item is _PUBLISHER_STOP` never matched: the worker hung until
    # two 30 s timeouts expired and the final image counters were lost.
    import pickle

    from taxi_receiver.image_pipeline import _PublisherStop

    assert isinstance(
        pickle.loads(pickle.dumps(_PublisherStop())), _PublisherStop
    )


def test_process_publication_matches_in_thread_publication(tmp_path):
    """S2 must be a placement change only, byte for byte."""
    rows = {
        index: bytes([(index * 7) & 0xFF]) * ROW_BYTES
        for index in range(4)
    }
    outputs = {}
    for mode in ("thread", "process"):
        root = tmp_path / mode
        pipeline = CameraImagePipeline(
            root,
            expected_rows=4,
            enable_row_csv=False,
            report_interval=1e9,
            publish_async=(mode == "process"),
            report_sink=lambda *_: None,
            error_sink=lambda *_: None,
        )
        try:
            pipeline.archive_frame(
                CompletedFrame(
                    camera_id=0,
                    frame_id=5,
                    row_count=4,
                    rows=dict(rows),
                    missing_rows=[],
                    had_overflow=False,
                    status=FrameStatus.COMPLETE,
                    close_reason="last_row",
                    expected_rows=4,
                    saw_first=True,
                    saw_last=True,
                )
            )
        finally:
            pipeline.close()
        if mode == "process":
            assert pipeline.publisher_stats.stats_returned
            assert pipeline.publisher_stats.published == 1
            assert pipeline.publisher_stats.failures == 0
        assert pipeline.stats.images_complete == 1
        outputs[mode] = {
            path.name: path.read_bytes()
            for path in sorted((root / "cam0").iterdir())
            if path.suffix in (".pgm", ".raw")
        }

    assert outputs["thread"] == outputs["process"]
    assert set(outputs["thread"]) == {"5.pgm", "5.raw"}
