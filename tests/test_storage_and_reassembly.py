import csv
import json
import time

import pytest

import taxi_receiver.storage as storage_module
from taxi_receiver.camera_parser import parse_camera_mode
from taxi_receiver.capture import SyntheticFrameSource
from taxi_receiver.packet_format import (
    FLAG_FIRST_ROW,
    FLAG_LAST_ROW,
    build_camera_row,
    parse_camera_row,
)
from taxi_receiver.pipeline import TaxiReceiverPipeline
from taxi_receiver.reassembler import FrameReassembler, FrameStatus
from taxi_receiver.storage import StorageAndPipeline

from .synthetic import make_camera_frame


def _packet(
    cam_id,
    frame_id,
    row_idx,
    *,
    flags=0,
    value=None,
    row_seq=None,
    corrupt_crc=False,
):
    if value is None:
        value = row_idx & 0xFF
    if row_seq is None:
        row_seq = row_idx
    return parse_camera_row(
        build_camera_row(
            cam_id=cam_id,
            frame_id=frame_id,
            row_idx=row_idx,
            row_flags=flags,
            row_seq=row_seq,
            payload=bytes([value]) * 80,
            corrupt_crc=corrupt_crc,
        )
    )


def test_out_of_order_rows_reconstruct_and_archive_atomically(tmp_path):
    reassembler = FrameReassembler()
    storage = StorageAndPipeline(tmp_path)

    assert reassembler.on_row(_packet(0, 7, 1)) is None
    assert reassembler.on_row(
        _packet(0, 7, 0, flags=FLAG_FIRST_ROW)
    ) is None
    completed = reassembler.on_row(
        _packet(0, 7, 2, flags=FLAG_LAST_ROW)
    )

    assert completed.status is FrameStatus.COMPLETE
    assert completed.missing_rows == []
    output = storage.archive(completed)

    assert output == tmp_path / "cam_0" / "frame_7"
    assert (output / "image.raw").read_bytes() == (
        bytes([0]) * 80 + bytes([1]) * 80 + bytes([2]) * 80
    )
    assert (output / "metadata.json").is_file()
    assert (output / "packets.csv").is_file()
    assert (output / "errors.json").is_file()
    assert not (output / "image.png").exists()
    assert not (output / "image.pgm").exists()
    assert not list((tmp_path / "cam_0").glob(".*.tmp"))

    metadata = json.loads((output / "metadata.json").read_text("utf-8"))
    assert metadata["status"] == "COMPLETE"
    assert metadata["pixel_format"] is None
    assert metadata["raw_size_bytes"] == 240

    with (tmp_path / "summary.csv").open(newline="", encoding="utf-8") as f:
        summary = list(csv.DictReader(f))
    assert len(summary) == 1
    assert summary[0]["status"] == "COMPLETE"
    assert summary[0]["missing_rows"] == "0"


def test_windows_transient_directory_lock_is_retried(
    tmp_path,
    monkeypatch,
):
    reassembler = FrameReassembler()
    reassembler.on_row(
        _packet(0, 70, 0, flags=FLAG_FIRST_ROW)
    )
    completed = reassembler.on_row(
        _packet(0, 70, 1, flags=FLAG_LAST_ROW)
    )
    storage = StorageAndPipeline(tmp_path)

    real_replace = storage_module.os.replace
    directory_attempts = 0

    def transient_replace(source, destination):
        nonlocal directory_attempts
        if source.is_dir():
            directory_attempts += 1
            if directory_attempts <= 2:
                error = PermissionError("transient directory lock")
                error.winerror = 5
                raise error
        return real_replace(source, destination)

    monkeypatch.setattr(storage_module.os, "name", "nt")
    monkeypatch.setattr(storage_module.os, "replace", transient_replace)
    monkeypatch.setattr(storage_module.time, "sleep", lambda _delay: None)

    output = storage.archive(completed)

    assert directory_attempts == 3
    assert output == tmp_path / "cam_0" / "frame_70"
    assert output.is_dir()


def test_two_cameras_do_not_overwrite_each_other():
    reassembler = FrameReassembler()
    reassembler.on_row(_packet(0, 1, 0, flags=FLAG_FIRST_ROW))
    reassembler.on_row(_packet(1, 1, 0, flags=FLAG_FIRST_ROW))

    cam0 = reassembler.on_row(
        _packet(0, 1, 1, flags=FLAG_LAST_ROW)
    )
    cam1 = reassembler.on_row(
        _packet(1, 1, 1, flags=FLAG_LAST_ROW)
    )

    assert cam0.camera_id == 0
    assert cam1.camera_id == 1
    assert cam0.rows[0] == bytes([0]) * 80
    assert cam1.rows[0] == bytes([0]) * 80
    assert cam0.status is FrameStatus.COMPLETE
    assert cam1.status is FrameStatus.COMPLETE


def test_identical_archive_is_idempotent(tmp_path):
    reassembler = FrameReassembler()
    storage = StorageAndPipeline(tmp_path)
    reassembler.on_row(_packet(0, 8, 0, flags=FLAG_FIRST_ROW))
    completed = reassembler.on_row(_packet(0, 8, 1, flags=FLAG_LAST_ROW))

    first = storage.archive(completed)
    second = storage.archive(completed)

    assert first == second
    with (tmp_path / "summary.csv").open(newline="", encoding="utf-8") as f:
        summary = list(csv.DictReader(f))
    assert len(summary) == 1
    assert summary[0]["frame_id"] == "8"


def test_rewriting_only_timing_metadata_is_idempotent(tmp_path):
    reassembler = FrameReassembler()
    storage = StorageAndPipeline(tmp_path)
    reassembler.on_row(_packet(0, 9, 0, flags=FLAG_FIRST_ROW))
    completed = reassembler.on_row(_packet(0, 9, 1, flags=FLAG_LAST_ROW))

    first = storage.archive(completed)
    metadata_path = first / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["started_at_monotonic"] = metadata["started_at_monotonic"] + 100.0
    metadata["ended_at_monotonic"] = metadata["ended_at_monotonic"] + 100.0
    metadata["duration_seconds"] = metadata["duration_seconds"] + 1.0
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    second = storage.archive(completed)

    assert second == first
    with (tmp_path / "summary.csv").open(newline="", encoding="utf-8") as f:
        summary = list(csv.DictReader(f))
    assert len(summary) == 1


def test_identical_duplicate_is_deduplicated():
    reassembler = FrameReassembler()
    row0 = _packet(0, 2, 0, flags=FLAG_FIRST_ROW)
    reassembler.on_row(row0)
    reassembler.on_row(row0)
    completed = reassembler.on_row(
        _packet(0, 2, 1, flags=FLAG_LAST_ROW)
    )

    assert completed.status is FrameStatus.COMPLETE
    assert completed.row_count == 2
    assert completed.duplicate_packets == 1
    assert completed.conflicting_duplicates == 0
    assert len(completed.packet_records) == 3


def test_conflicting_duplicate_marks_frame_corrupt_and_keeps_first():
    reassembler = FrameReassembler()
    reassembler.on_row(
        _packet(0, 3, 0, flags=FLAG_FIRST_ROW, value=0x11)
    )
    reassembler.on_row(_packet(0, 3, 0, value=0x22))
    completed = reassembler.on_row(
        _packet(0, 3, 1, flags=FLAG_LAST_ROW)
    )

    assert completed.status is FrameStatus.CORRUPT
    assert completed.conflicting_duplicates == 1
    assert completed.rows[0] == bytes([0x11]) * 80
    assert completed.errors[0]["kind"] == "conflicting_duplicate"


def test_missing_row_closes_partial():
    reassembler = FrameReassembler()
    reassembler.on_row(_packet(0, 4, 0, flags=FLAG_FIRST_ROW))
    assert reassembler.on_row(
        _packet(0, 4, 2, flags=FLAG_LAST_ROW)
    ) is None
    completed = reassembler.flush()[0]

    assert completed.status is FrameStatus.PARTIAL
    assert completed.missing_rows == [1]
    assert completed.to_bytes()[80:160] == bytes(80)


def test_crc_error_is_rejected_before_image_session_creation():
    reassembler = FrameReassembler()
    corrupt = _packet(
        0,
        5,
        0,
        flags=FLAG_FIRST_ROW | FLAG_LAST_ROW,
        corrupt_crc=True,
    )
    completed = reassembler.on_row(corrupt, errors=("crc_error",))

    assert completed is None
    assert reassembler.flush() == []
    assert reassembler.stats.sessions_created == 0
    assert reassembler.stats.rows_rejected == 1


def test_frame_id_wrap_closes_old_frame_without_cross_camera_ordering():
    reassembler = FrameReassembler()
    reassembler.on_row(
        _packet(0, 0xFFFF, 0, flags=FLAG_FIRST_ROW),
        now=10.0,
    )
    old = reassembler.on_row(
        _packet(0, 0, 0, flags=FLAG_FIRST_ROW | FLAG_LAST_ROW),
        now=10.1,
    )
    new = reassembler.drain_completed()

    assert old.frame_id == 0xFFFF
    assert old.status is FrameStatus.PARTIAL
    assert old.close_reason == "frame_switch"
    assert len(new) == 1
    assert new[0].frame_id == 0
    assert new[0].status is FrameStatus.COMPLETE


def test_timeout_closes_in_progress_frame():
    reassembler = FrameReassembler(timeout_seconds=1.0)
    reassembler.on_row(
        _packet(0, 6, 0, flags=FLAG_FIRST_ROW),
        now=20.0,
    )

    assert reassembler.expire(20.9) == []
    expired = reassembler.expire(21.0)
    assert len(expired) == 1
    assert expired[0].status is FrameStatus.TIMEOUT
    assert expired[0].close_reason == "timeout"


def test_pipeline_archives_completed_frame(tmp_path):
    frames = [
        make_camera_frame(
            cam_id=2,
            frame_id=9,
            row_idx=0,
            row_seq=0,
            row_flags=FLAG_FIRST_ROW,
        ),
        make_camera_frame(
            cam_id=2,
            frame_id=9,
            row_idx=1,
            row_seq=1,
            row_flags=FLAG_LAST_ROW,
        ),
    ]
    storage = StorageAndPipeline(tmp_path)
    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource(frames),
        mode="camera",
        max_stage="reassemble",
        reassembler=FrameReassembler(),
        on_completed_frame=storage,
        report_interval=999,
        sink=lambda *_: None,
    )

    pipeline.start()
    time.sleep(0.3)
    pipeline.stop()

    output = tmp_path / "cam_2" / "frame_9"
    assert output.is_dir()
    assert (output / "image.raw").stat().st_size == 160


def test_differing_frame_collision_is_published_as_a_duplicate(tmp_path):
    # frame_id restarts near zero at every board power-on.  Raising here used
    # to happen inside the bounded output worker, which turned a naming
    # collision into a total publication outage (2767 submitted / 0 processed).
    storage = StorageAndPipeline(tmp_path)
    first_reassembler = FrameReassembler()
    first_reassembler.on_row(_packet(0, 21, 0, flags=FLAG_FIRST_ROW, value=0x11))
    first = first_reassembler.on_row(_packet(0, 21, 1, flags=FLAG_LAST_ROW, value=0x11))

    second_reassembler = FrameReassembler()
    second_reassembler.on_row(_packet(0, 21, 0, flags=FLAG_FIRST_ROW, value=0x22))
    second = second_reassembler.on_row(
        _packet(0, 21, 1, flags=FLAG_LAST_ROW, value=0x22)
    )

    first_path = storage.archive(first)
    second_path = storage.archive(second)

    assert first_path.name == "frame_21"
    assert second_path.name == "frame_21.dup1"
    assert storage.frames_archived == 2
    assert storage.frames_renamed_on_collision == 1
    assert first_path.joinpath("image.raw").read_bytes() != (
        second_path.joinpath("image.raw").read_bytes()
    )


def test_error_collision_policy_still_raises(tmp_path):
    storage = StorageAndPipeline(tmp_path, collision_policy="error")
    first_reassembler = FrameReassembler()
    first_reassembler.on_row(_packet(0, 21, 0, flags=FLAG_FIRST_ROW, value=0x11))
    first = first_reassembler.on_row(_packet(0, 21, 1, flags=FLAG_LAST_ROW, value=0x11))
    second_reassembler = FrameReassembler()
    second_reassembler.on_row(_packet(0, 21, 0, flags=FLAG_FIRST_ROW, value=0x22))
    second = second_reassembler.on_row(
        _packet(0, 21, 1, flags=FLAG_LAST_ROW, value=0x22)
    )

    storage.archive(first)
    with pytest.raises(FileExistsError):
        storage.archive(second)


def test_run_subdir_policy_isolates_every_run(tmp_path):
    first = storage_module.resolve_archive_root(
        tmp_path, policy="run-subdir", run_id="20260805_120000"
    )
    second = storage_module.resolve_archive_root(
        tmp_path, policy="run-subdir", run_id="20260805_130000"
    )
    assert first != second
    assert first.parent == tmp_path


def test_require_empty_policy_refuses_a_root_that_already_has_frames(tmp_path):
    assert not storage_module.archive_root_has_frames(tmp_path)
    (tmp_path / "cam_0" / "frame_21").mkdir(parents=True)
    assert storage_module.archive_root_has_frames(tmp_path)

    with pytest.raises(storage_module.StaleArchiveRootError):
        storage_module.resolve_archive_root(
            tmp_path, policy="require-empty", run_id="ignored"
        )
    # reuse is still available for callers that really want the old behaviour.
    assert (
        storage_module.resolve_archive_root(
            tmp_path, policy="reuse", run_id="ignored"
        )
        == tmp_path
    )


def test_require_empty_sees_frames_nested_under_a_previous_run_subdir(tmp_path):
    # A root populated by an earlier run-subdir run keeps its frames at
    # <root>/run_*/cam_*/frame_*.  A single-level check called that root empty.
    (tmp_path / "run_20260805_120000" / "cam_1" / "frame_22").mkdir(parents=True)
    assert storage_module.archive_root_has_frames(tmp_path)
    with pytest.raises(storage_module.StaleArchiveRootError):
        storage_module.resolve_archive_root(
            tmp_path, policy="require-empty", run_id="ignored"
        )
