"""P0-P3 regression tests for the rows.csv recorder.

attempt2 left one question unanswerable from the archive: when a row_seq is
missing, did the packet never arrive, or did the recorder drop it?  These tests
pin the columns and the queue accounting that make that question answerable,
and the A/B pair proves the recorder does not change what gets reassembled.
"""
import csv
import dataclasses
import queue
import threading
import time

from taxi_receiver.capture import SyntheticFrameSource
from taxi_receiver.image_pipeline import (
    CameraImagePipeline,
    CsvBackpressure,
)
from taxi_receiver.packet_format import (
    FLAG_FIRST_ROW,
    FLAG_LAST_ROW,
    build_camera_row,
)
from taxi_receiver.pipeline import TaxiReceiverPipeline
from taxi_receiver.reassembler import FrameReassembler

from .synthetic import make_raw_frame


def _frame(*, cam_id=0, frame_id=1, row_idx=0, row_seq=0, row_flags=0,
           payload=None, corrupt_crc=False):
    return make_raw_frame(
        build_camera_row(
            cam_id=cam_id,
            frame_id=frame_id,
            row_idx=row_idx,
            row_flags=row_flags,
            row_seq=row_seq,
            payload=bytes(80) if payload is None else payload,
            corrupt_crc=corrupt_crc,
        )
    )


def _sequence(frame_id, rows, *, cam_id=0, last_flag_on=None):
    """One frame of ``rows`` rows; ``last_flag_on`` forces the LAST bit."""
    last_row = rows - 1 if last_flag_on is None else last_flag_on
    return [
        _frame(
            cam_id=cam_id,
            frame_id=frame_id,
            row_idx=index,
            row_seq=index,
            row_flags=(
                (FLAG_FIRST_ROW if index == 0 else 0)
                | (FLAG_LAST_ROW if index == last_row else 0)
            ),
        )
        for index in range(rows)
    ]


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
    pipeline.stop()
    assert sink.flush_rows(timeout=10.0)
    return pipeline


def _read_rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(row for row in handle if row.strip()))


# ---- P0: the new columns ------------------------------------------------


def test_capture_index_and_csv_sequence_are_both_contiguous_when_nothing_drops(
    tmp_path,
):
    sink = CameraImagePipeline(tmp_path / "images", expected_rows=4)
    _run(_sequence(1, 4), sink, expected_rows=4)

    rows = _read_rows(tmp_path / "images" / "cam0" / "rows.csv")
    assert [int(row["capture_index"]) for row in rows] == [0, 1, 2, 3]
    assert [int(row["csv_sequence"]) for row in rows] == [1, 2, 3, 4]
    assert sink.csv_stats.rows_submitted == 4
    assert sink.csv_stats.rows_written == 4
    assert sink.csv_stats.rows_dropped == 0
    assert sink.csv_stats.writer_failures == 0
    sink.close()


def test_reliable_first_and_last_require_layer3_validity_and_position(tmp_path):
    sink = CameraImagePipeline(tmp_path / "images", expected_rows=4)
    _run(_sequence(1, 4), sink, expected_rows=4)

    rows = _read_rows(tmp_path / "images" / "cam0" / "rows.csv")
    assert [row["reliable_first"] for row in rows] == ["1", "0", "0", "0"]
    assert [row["reliable_last"] for row in rows] == ["0", "0", "0", "1"]
    assert all(row["layer3_valid"] == "1" for row in rows)
    assert all(row["row_accepted"] == "1" for row in rows)
    sink.close()


def test_last_bit_on_the_wrong_row_does_not_close_the_csv_group(tmp_path):
    """The attempt2 0xFF byte-bleed case, reduced to four rows.

    Row 1 carries a LAST bit it has no right to.  ``last_row`` keeps recording
    the raw evidence, but ``reliable_last`` refuses it and no blank line is
    emitted, so counting blank lines cannot over-count frame ends.
    """

    sink = CameraImagePipeline(tmp_path / "images", expected_rows=4)
    frames = _sequence(1, 4)
    frames[1] = _frame(frame_id=1, row_idx=1, row_seq=1, row_flags=FLAG_LAST_ROW)
    _run(frames, sink, expected_rows=4)

    path = tmp_path / "images" / "cam0" / "rows.csv"
    rows = _read_rows(path)
    assert rows[1]["last_row"] == "1"          # raw evidence preserved
    assert rows[1]["frame_end"] == "1"         # raw evidence preserved
    assert rows[1]["reliable_last"] == "0"     # but not counted
    assert rows[3]["reliable_last"] == "1"
    # Exactly one human-readable group terminator, at the real frame end.
    assert path.read_text("utf-8").count("\n\n") == 1
    sink.close()


def test_layer3_failure_makes_a_correctly_placed_last_bit_unreliable(tmp_path):
    sink = CameraImagePipeline(tmp_path / "images", expected_rows=2)
    _run(
        [
            _frame(frame_id=5, row_idx=0, row_seq=0, row_flags=FLAG_FIRST_ROW),
            _frame(
                frame_id=5,
                row_idx=1,
                row_seq=1,
                row_flags=FLAG_LAST_ROW,
                corrupt_crc=True,
            ),
        ],
        sink,
        expected_rows=2,
    )

    path = tmp_path / "images" / "cam0" / "rows.csv"
    rows = _read_rows(path)
    assert rows[1]["last_row"] == "1"
    assert rows[1]["layer3_valid"] == "0"
    assert rows[1]["row_accepted"] == "0"
    assert rows[1]["reliable_last"] == "0"
    assert "\n\n" not in path.read_text("utf-8")
    sink.close()


def test_duplicate_row_is_recorded_as_evidence_but_not_accepted(tmp_path):
    sink = CameraImagePipeline(tmp_path / "images", expected_rows=2)
    duplicate = _frame(frame_id=3, row_idx=0, row_seq=0, row_flags=FLAG_FIRST_ROW)
    _run(
        [
            duplicate,
            duplicate,
            _frame(frame_id=3, row_idx=1, row_seq=1, row_flags=FLAG_LAST_ROW),
        ],
        sink,
        expected_rows=2,
    )

    rows = _read_rows(tmp_path / "images" / "cam0" / "rows.csv")
    assert len(rows) == 3
    assert [row["row_accepted"] for row in rows] == ["1", "0", "1"]
    assert all(row["layer3_valid"] == "1" for row in rows)
    sink.close()


def test_frame_switch_resets_the_duplicate_mirror(tmp_path):
    sink = CameraImagePipeline(tmp_path / "images", expected_rows=2)
    _run(_sequence(1, 2) + _sequence(2, 2), sink, expected_rows=2)

    rows = _read_rows(tmp_path / "images" / "cam0" / "rows.csv")
    # Row 0 of frame 2 repeats an index already seen in frame 1; a session-less
    # mirror would have called it a duplicate.
    assert [row["row_accepted"] for row in rows] == ["1", "1", "1", "1"]
    sink.close()


# ---- P2: the recorder is off the packet-consumer hot path ---------------


def test_rows_are_formatted_and_written_on_the_dedicated_writer_thread(tmp_path):
    seen: list[str] = []

    class ThreadNamingPipeline(CameraImagePipeline):
        def _write_row_event(self, event):
            seen.append(threading.current_thread().name)
            super()._write_row_event(event)

    sink = ThreadNamingPipeline(tmp_path / "images", expected_rows=2)
    caller = threading.current_thread().name
    _run(_sequence(1, 2), sink, expected_rows=2)

    assert seen, "writer thread never ran"
    assert set(seen) == {"taxi-rows-csv"}
    assert caller not in seen
    assert "taxi-worker" not in seen
    sink.close()


def test_full_csv_queue_drops_are_counted_and_leave_a_capture_index_gap(
    tmp_path,
):
    # The queue is deliberately roomy: the only drop in this run is the
    # injected one, so the counter and the capture_index hole must both be
    # exactly one.
    sink = CameraImagePipeline(
        tmp_path / "images",
        expected_rows=4,
        csv_queue_depth=4096,
        csv_backpressure=CsvBackpressure.DROP,
    )
    real_put_nowait = sink._csv_queue.put_nowait
    reject_next = {"count": 1}

    def flaky_put_nowait(item):
        if reject_next["count"] > 0:
            reject_next["count"] -= 1
            raise queue.Full
        real_put_nowait(item)

    sink._csv_queue.put_nowait = flaky_put_nowait  # type: ignore[method-assign]
    _run(_sequence(1, 4), sink, expected_rows=4)

    assert sink.csv_stats.rows_submitted == 4
    assert sink.csv_stats.rows_dropped == 1
    assert sink.csv_stats.rows_written == 3

    rows = _read_rows(tmp_path / "images" / "cam0" / "rows.csv")
    # capture_index shows the hole; csv_sequence stays contiguous.  That pair
    # is what tells a future investigation the loss was ours, not the FPGA's.
    assert [int(row["capture_index"]) for row in rows] == [1, 2, 3]
    assert [int(row["csv_sequence"]) for row in rows] == [1, 2, 3]
    sink.close()


def test_block_backpressure_never_drops_even_with_a_one_slot_queue(tmp_path):
    sink = CameraImagePipeline(
        tmp_path / "images",
        expected_rows=8,
        csv_queue_depth=1,
        csv_backpressure=CsvBackpressure.BLOCK,
    )
    _run(_sequence(1, 8), sink, expected_rows=8)

    assert sink.csv_stats.rows_dropped == 0
    assert sink.csv_stats.rows_written == 8
    assert len(_read_rows(tmp_path / "images" / "cam0" / "rows.csv")) == 8
    sink.close()


def test_disabling_rows_csv_writes_no_file_and_starts_no_thread(tmp_path):
    sink = CameraImagePipeline(
        tmp_path / "images", expected_rows=2, enable_row_csv=False
    )
    assert sink._csv_thread is None
    _run(_sequence(1, 2), sink, expected_rows=2)

    assert not (tmp_path / "images" / "cam0" / "rows.csv").exists()
    assert sink.csv_stats.rows_submitted == 0
    # The image path is untouched by the recorder being off.
    assert (tmp_path / "images" / "cam0" / "1.pgm").is_file()
    sink.close()


def test_close_is_idempotent(tmp_path):
    sink = CameraImagePipeline(tmp_path / "images", expected_rows=2)
    _run(_sequence(1, 2), sink, expected_rows=2)
    sink.close()
    sink.close()
    assert len(_read_rows(tmp_path / "images" / "cam0" / "rows.csv")) == 2


# ---- P3: A/B -- the recorder must not change reassembly -----------------


# Wall-clock and queue-high-water fields are excluded on purpose: they are
# timing observations, not reassembly outcomes, and comparing them would make
# the A/B assertion flaky without making it stronger.
_MONITOR_OUTCOME_FIELDS = (
    "total_ethernet_frames",
    "matching_frames",
    "valid_packets",
    "bad_ethernet_length",
    "bad_fixed_payload",
    "bad_crc",
    "parser_errors",
    "processing_errors",
    "dropped_capture_queue",
    "ethernet_validation_failures",
    "total_payload_bytes",
    "cameras",
)


def _ab_observation(images_root, frames, *, enable_row_csv, expected_rows):
    sink = CameraImagePipeline(
        images_root, expected_rows=expected_rows, enable_row_csv=enable_row_csv
    )
    pipeline = _run(frames, sink, expected_rows=expected_rows)
    reassembler = pipeline._reassembly_stages[0].reassembler
    monitor = dataclasses.asdict(pipeline.monitor.stats)
    observation = {
        "monitor": {name: monitor[name] for name in _MONITOR_OUTCOME_FIELDS},
        "reassembly": dataclasses.asdict(reassembler.stats),
        "images": dataclasses.asdict(sink.stats),
        "pgm": sorted(
            (path.name, path.read_bytes())
            for path in images_root.rglob("*.pgm")
        ),
    }
    sink.close()
    return observation


def test_reassembly_is_bit_identical_with_and_without_rows_csv(tmp_path):
    """Replay the same frames twice; only the recorder differs.

    A difference here would mean the recorder is not a passive observer.  Note
    what this does *not* prove: identical offline results say nothing about
    whether the recorder costs live-capture throughput.  That is the
    ``csv_queue_peak``/``csv_rows_dropped``/``capture queue drops`` comparison
    on a real NIC run, not this test.
    """

    frames = (
        _sequence(1, 4)
        + _sequence(2, 4)
        # A corrupt row and a duplicate, so both A and B exercise the
        # rejection paths rather than only the happy path.
        + [_frame(frame_id=3, row_idx=0, row_seq=0, corrupt_crc=True)]
        + _sequence(4, 4)
        + [_frame(frame_id=4, row_idx=0, row_seq=0, row_flags=FLAG_FIRST_ROW)]
    )

    without = _ab_observation(
        tmp_path / "a_no_csv", frames, enable_row_csv=False, expected_rows=4
    )
    with_csv = _ab_observation(
        tmp_path / "b_csv", frames, enable_row_csv=True, expected_rows=4
    )

    assert without["monitor"] == with_csv["monitor"]
    assert without["reassembly"] == with_csv["reassembly"]
    assert without["images"] == with_csv["images"]
    assert without["pgm"] == with_csv["pgm"]
    assert without["pgm"], "A/B compared two empty runs"

    # And the B half really did record something.
    rows = _read_rows(tmp_path / "b_csv" / "cam0" / "rows.csv")
    assert len(rows) == len(frames)
    assert not (tmp_path / "a_no_csv" / "cam0" / "rows.csv").exists()


def test_slow_csv_flush_does_not_stall_the_packet_consumer(tmp_path):
    """The P2 property, measured rather than asserted structurally."""

    release = threading.Event()

    class StallingPipeline(CameraImagePipeline):
        def _emit_batch(self, sink, now):
            release.wait(timeout=5.0)
            super()._emit_batch(sink, now)

    # csv_flush_rows=1 makes the writer block on the very first row, so the
    # consumer is measured against a writer that is genuinely stuck.
    sink = StallingPipeline(
        tmp_path / "images",
        expected_rows=64,
        csv_queue_depth=4096,
        csv_flush_rows=1,
    )
    frames = _sequence(1, 64)
    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource(frames),
        mode="camera",
        max_stage="reassemble",
        reassembler=FrameReassembler(expected_rows=64),
        on_completed_frame=sink.archive_frame,
        on_frame_processed=sink.record_packet,
        report_interval=999,
        sink=lambda *_: None,
    )
    started = time.monotonic()
    pipeline.start()
    # Wait for the packet worker specifically, not for the CSV writer.
    pipeline._queue.join()
    consumer_elapsed = time.monotonic() - started

    # The consumer finished handing every packet over while the writer is
    # still blocked inside its first flush.
    assert sink.csv_stats.rows_submitted == 64
    assert consumer_elapsed < 2.0

    release.set()
    pipeline.stop()
    assert sink.flush_rows(timeout=10.0)
    assert sink.csv_stats.rows_dropped == 0
    assert len(_read_rows(tmp_path / "images" / "cam0" / "rows.csv")) == 64
    sink.close()
