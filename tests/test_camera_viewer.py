from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from taxi_receiver.archive_layout import (
    choose_latest_candidate,
    discover_attempt_info,
    iter_complete_candidates,
    normalize_attempt_name,
    resolve_attempt_path,
)
from taxi_receiver.archive_monitor import ArchiveViewerBackend, LatestMailbox
from taxi_receiver.demo_archive_producer import produce_demo_archive
from taxi_receiver.image_loader import ArchiveImageLoadError, load_archive_frame


def _pgm_bytes(width: int, height: int, value: int) -> bytes:
    pixels = bytes([value % 256]) * (width * height)
    return f"P5\n{width} {height}\n255\n".encode("ascii") + pixels


def _write_complete_frame(camera_dir: Path, frame_id: int, *, width: int = 8, height: int = 4, status: str = "COMPLETE") -> None:
    camera_dir.mkdir(parents=True, exist_ok=True)
    (camera_dir / f"{frame_id}.pgm").write_bytes(_pgm_bytes(width, height, frame_id))
    (camera_dir / f"{frame_id}.raw").write_bytes(bytes([frame_id % 256]) * (width * height))
    (camera_dir / f"{frame_id}.json").write_text(
        json.dumps(
            {
                "cam_id": 0,
                "frame_id": frame_id,
                "status": status,
                "timestamp": 1234.5 + frame_id,
                "width": width,
                "height": height,
                "missing_rows": [],
                "missing_count": 0,
                "fill_policy": "none",
                "expected_rows": height,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_recovered_frame(camera_dir: Path, frame_id: int, *, width: int = 8, height: int = 4, missing_rows: tuple[int, ...] = (1,)) -> None:
    recovered = camera_dir / "recovered" / f"frame_{frame_id}"
    recovered.mkdir(parents=True, exist_ok=True)
    pixels = bytearray(bytes([frame_id % 256]) * (width * height))
    for row in missing_rows:
        start = row * width
        pixels[start:start + width] = bytes(width)
    (recovered / "image.pgm").write_bytes(_pgm_bytes(width, height, frame_id))
    (recovered / "image.raw").write_bytes(bytes(pixels))
    (recovered / "metadata.json").write_text(
        json.dumps(
            {
                "cam_id": 0,
                "frame_id": frame_id,
                "status": "RECOVERED",
                "timestamp": 5678.5 + frame_id,
                "width": width,
                "height": height,
                "missing_rows": list(missing_rows),
                "missing_count": len(missing_rows),
                "fill_policy": "zero",
                "expected_rows": height,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _wait_for(condition, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_attempt_mapping_and_layout_detection(tmp_path):
    assert normalize_attempt_name("3") == "attempt3"
    assert normalize_attempt_name("attempt3") == "attempt3"
    assert resolve_attempt_path(tmp_path, "3") == tmp_path / "attempt3"


def test_attempt_missing_locks_backend(tmp_path):
    backend = ArchiveViewerBackend(tmp_path, poll_interval_ms=20)
    backend.apply_configuration(tmp_path, "3", None)
    backend.start()
    try:
        assert _wait_for(lambda: backend.snapshot().status_message == "Attempt not found")
        snapshot = backend.snapshot()
        assert snapshot.attempt_exists is False
        assert snapshot.camera_names == ()
        assert snapshot.complete_frame is None
        assert snapshot.recovered_frame is None
    finally:
        backend.stop()


def test_attempt_exists_but_no_camera_locks_backend(tmp_path):
    (tmp_path / "attempt3" / "notes").mkdir(parents=True)
    backend = ArchiveViewerBackend(tmp_path, poll_interval_ms=20)
    backend.apply_configuration(tmp_path, "attempt3", None)
    backend.start()
    try:
        assert _wait_for(lambda: backend.snapshot().status_message == "No camera archive found")
        snapshot = backend.snapshot()
        assert snapshot.attempt_exists is True
        assert snapshot.camera_names == ()
        assert snapshot.complete_frame is None
        assert snapshot.recovered_frame is None
    finally:
        backend.stop()


def test_auto_discovers_cam0_and_cam1(tmp_path):
    _write_complete_frame(tmp_path / "attempt3" / "cam0", 1)
    _write_complete_frame(tmp_path / "attempt3" / "cam1", 2)
    info = discover_attempt_info(tmp_path, "3")
    assert info.exists is True
    assert [camera.name for camera in info.camera_dirs] == ["cam0", "cam1"]
    assert info.has_camera_archive is True


def test_latest_complete_and_recovered_are_tracked_independently(tmp_path):
    camera_dir = tmp_path / "attempt3" / "cam0"
    _write_complete_frame(camera_dir, 10)
    backend = ArchiveViewerBackend(tmp_path, poll_interval_ms=20)
    backend.apply_configuration(tmp_path, "attempt3", "cam0")
    backend.start()
    try:
        assert _wait_for(lambda: backend.snapshot().complete_frame is not None)
        _write_recovered_frame(camera_dir, 11, missing_rows=(1, 3))
        backend.refresh_now()
        assert _wait_for(lambda: backend.snapshot().recovered_frame is not None)
        snapshot = backend.snapshot()
        assert snapshot.complete_frame is not None
        assert snapshot.complete_frame.frame_id == 10
        assert snapshot.recovered_frame is not None
        assert snapshot.recovered_frame.frame_id == 11
        assert snapshot.recovered_frame.missing_rows == (1, 3)
        assert snapshot.recovered_frame.missing_count == 2
        assert snapshot.recovered_frame.fill_policy == "zero"
    finally:
        backend.stop()


def test_switching_camera_clears_old_image(tmp_path):
    _write_complete_frame(tmp_path / "attempt3" / "cam0", 10)
    _write_complete_frame(tmp_path / "attempt3" / "cam1", 20)
    backend = ArchiveViewerBackend(tmp_path, poll_interval_ms=20)
    backend.apply_configuration(tmp_path, "3", "cam0")
    backend.start()
    try:
        assert _wait_for(lambda: backend.snapshot().complete_frame is not None)
        backend.apply_configuration(tmp_path, "3", "cam1")
        snapshot = backend.snapshot()
        assert snapshot.complete_frame is None
        assert snapshot.recovered_frame is None
        assert _wait_for(lambda: backend.snapshot().complete_frame is not None and backend.snapshot().complete_frame.frame_id == 20)
    finally:
        backend.stop()


def test_incomplete_and_temp_files_are_ignored(tmp_path):
    camera_dir = tmp_path / "attempt3" / "cam0"
    camera_dir.mkdir(parents=True)
    (camera_dir / "99.json").write_text("{}\n", encoding="utf-8")
    (camera_dir / ".99.tmp").write_text("temp", encoding="utf-8")
    backend = ArchiveViewerBackend(tmp_path, poll_interval_ms=20)
    backend.apply_configuration(tmp_path, "3", "cam0")
    backend.start()
    try:
        assert _wait_for(lambda: backend.snapshot().status_message == "No camera archive found")
        snapshot = backend.snapshot()
        assert snapshot.complete_frame is None
        assert snapshot.recovered_frame is None
        assert snapshot.camera_names == ()
    finally:
        backend.stop()


def test_corrupt_pgm_does_not_crash_loader(tmp_path):
    camera_dir = tmp_path / "attempt3" / "cam0"
    camera_dir.mkdir(parents=True)
    (camera_dir / "1.pgm").write_bytes(b"P5\n8 4\n255\nshort")
    (camera_dir / "1.raw").write_bytes(b"short")
    (camera_dir / "1.json").write_text(
        json.dumps(
            {
                "cam_id": 0,
                "frame_id": 1,
                "status": "COMPLETE",
                "timestamp": 1.0,
                "width": 8,
                "height": 4,
                "missing_rows": [],
                "missing_count": 0,
                "fill_policy": "none",
                "expected_rows": 4,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    candidates = iter_complete_candidates(
        camera_dir,
        attempt_name="attempt3",
        camera_name="cam0",
        camera_id=0,
        generation=1,
    )
    candidate = choose_latest_candidate(candidates)
    assert candidate is not None
    with pytest.raises(ArchiveImageLoadError):
        load_archive_frame(candidate)


def test_latest_mailbox_keeps_only_latest_value():
    mailbox = LatestMailbox[int]()
    mailbox.put_latest(1)
    mailbox.put_latest(2)
    assert mailbox.take_latest() == 2
    assert mailbox.take_latest() is None


def test_demo_producer_writes_only_temp_root(tmp_path):
    root = produce_demo_archive(
        tmp_path / "demo",
        attempt="attempt7",
        camera="cam0",
        fps=4,
        duration_seconds=0.25,
        width=6,
        height=4,
    )
    assert root == tmp_path / "demo"
    assert (root / "attempt7" / "cam0").is_dir()


def test_archive_files_are_not_modified_by_viewer_scan(tmp_path):
    camera_dir = tmp_path / "attempt3" / "cam0"
    _write_complete_frame(camera_dir, 1)
    before = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in camera_dir.glob("**/*")
        if path.is_file()
    }
    backend = ArchiveViewerBackend(tmp_path, poll_interval_ms=20)
    backend.apply_configuration(tmp_path, "3", "cam0")
    backend.start()
    try:
        assert _wait_for(lambda: backend.snapshot().complete_frame is not None)
    finally:
        backend.stop()
    after = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in camera_dir.glob("**/*")
        if path.is_file()
    }
    assert before == after


def test_backend_stop_returns_cleanly(tmp_path):
    _write_complete_frame(tmp_path / "attempt3" / "cam0", 1)
    backend = ArchiveViewerBackend(tmp_path, poll_interval_ms=20)
    backend.apply_configuration(tmp_path, "3", "cam0")
    backend.start()
    backend.stop()
    snapshot = backend.snapshot()
    assert snapshot.attempt_name == "attempt3"
