from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from pathlib import Path


ATTEMPT_NAME_RE = re.compile(r"^attempt(?P<number>\d+)$", re.IGNORECASE)
CAMERA_NAME_RE = re.compile(r"^cam[_-]?(?P<number>\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class CameraDirectoryInfo:
    name: str
    path: Path
    camera_id: int | None
    complete_count: int
    recovered_count: int
    file_count: int

    @property
    def has_archive(self) -> bool:
        return self.complete_count > 0 or self.recovered_count > 0


@dataclass(frozen=True)
class AttemptDirectoryInfo:
    archive_root: Path
    attempt_name: str
    attempt_path: Path
    exists: bool
    camera_dirs: tuple[CameraDirectoryInfo, ...]

    @property
    def has_camera_archive(self) -> bool:
        return any(camera.has_archive for camera in self.camera_dirs)


@dataclass(frozen=True)
class FrameArtifactRef:
    generation: int
    stream: str
    archive_root: Path
    attempt_name: str
    camera_name: str
    camera_id: int | None
    frame_id: int
    status: str
    timestamp: float
    file_name: str
    pgm_path: Path
    json_path: Path
    raw_path: Path | None
    signature: tuple[int, int, int, str]


def normalize_attempt_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("attempt must not be empty")
    if cleaned.isdigit():
        return f"attempt{cleaned}"
    match = ATTEMPT_NAME_RE.fullmatch(cleaned)
    if match is None:
        raise ValueError(f"invalid attempt value: {value!r}")
    return f"attempt{match.group('number')}"


def resolve_attempt_path(archive_root: str | Path, attempt: str) -> Path:
    return Path(archive_root).resolve() / normalize_attempt_name(attempt)


def discover_attempt_info(
    archive_root: str | Path,
    attempt: str,
) -> AttemptDirectoryInfo:
    root = Path(archive_root).resolve()
    attempt_name = normalize_attempt_name(attempt)
    attempt_path = root / attempt_name
    if not attempt_path.is_dir():
        return AttemptDirectoryInfo(
            archive_root=root,
            attempt_name=attempt_name,
            attempt_path=attempt_path,
            exists=False,
            camera_dirs=(),
        )

    camera_dirs = tuple(
        sorted(
            (
                camera_info
                for child in _iter_directories(attempt_path)
                if (camera_info := _scan_camera_directory(child)) is not None
            ),
            key=lambda camera: (camera.camera_id is None, camera.camera_id, camera.name),
        )
    )
    return AttemptDirectoryInfo(
        archive_root=root,
        attempt_name=attempt_name,
        attempt_path=attempt_path,
        exists=True,
        camera_dirs=camera_dirs,
    )


def select_camera_directory(
    attempt_info: AttemptDirectoryInfo,
    camera_name: str | None,
) -> CameraDirectoryInfo | None:
    if not attempt_info.exists or not attempt_info.has_camera_archive:
        return None
    if camera_name is not None:
        for camera in attempt_info.camera_dirs:
            if camera.name == camera_name and camera.has_archive:
                return camera
        return None
    for camera in attempt_info.camera_dirs:
        if camera.has_archive:
            return camera
    return None


def iter_complete_candidates(
    camera_dir: Path,
    *,
    attempt_name: str,
    camera_name: str,
    camera_id: int | None,
    generation: int,
) -> list[FrameArtifactRef]:
    candidates: list[FrameArtifactRef] = []
    archive_root = camera_dir.parents[1] if len(camera_dir.parents) > 1 else camera_dir.parent
    candidates.extend(
        _scan_complete_container(
            camera_dir,
            attempt_name=attempt_name,
            camera_name=camera_name,
            camera_id=camera_id,
            generation=generation,
            archive_root=archive_root,
        )
    )
    complete_dir = camera_dir / "complete"
    if complete_dir.is_dir():
        candidates.extend(
            _scan_complete_container(
                complete_dir,
                attempt_name=attempt_name,
                camera_name=camera_name,
                camera_id=camera_id,
                generation=generation,
                archive_root=archive_root,
            )
        )
    return candidates


def iter_recovered_candidates(
    camera_dir: Path,
    *,
    attempt_name: str,
    camera_name: str,
    camera_id: int | None,
    generation: int,
) -> list[FrameArtifactRef]:
    candidates: list[FrameArtifactRef] = []
    archive_root = camera_dir.parents[1] if len(camera_dir.parents) > 1 else camera_dir.parent
    candidates.extend(
        _scan_recovered_container(
            camera_dir / "recovered",
            attempt_name=attempt_name,
            camera_name=camera_name,
            camera_id=camera_id,
            generation=generation,
            archive_root=archive_root,
        )
    )
    candidates.extend(
        _scan_recovered_container(
            camera_dir,
            attempt_name=attempt_name,
            camera_name=camera_name,
            camera_id=camera_id,
            generation=generation,
            archive_root=archive_root,
        )
    )
    return candidates


def choose_latest_candidate(candidates: list[FrameArtifactRef]) -> FrameArtifactRef | None:
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.signature)


def _scan_camera_directory(path: Path) -> CameraDirectoryInfo | None:
    match = CAMERA_NAME_RE.fullmatch(path.name)
    if match is None:
        return None

    complete_count = 0
    recovered_count = 0
    file_count = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                file_count += 1
                if entry.is_dir(follow_symlinks=False) and entry.name in {"complete", "recovered"}:
                    continue
    except FileNotFoundError:
        return None

    complete_count = len(
        iter_complete_candidates(
            path,
            attempt_name=path.parent.name,
            camera_name=path.name,
            camera_id=int(match.group("number")),
            generation=0,
        )
    )
    recovered_count = len(
        iter_recovered_candidates(
            path,
            attempt_name=path.parent.name,
            camera_name=path.name,
            camera_id=int(match.group("number")),
            generation=0,
        )
    )

    return CameraDirectoryInfo(
        name=path.name,
        path=path,
        camera_id=int(match.group("number")),
        complete_count=complete_count,
        recovered_count=recovered_count,
        file_count=file_count,
    )


def _count_recovered_frames(recovered_root: Path) -> int:
    count = 0
    try:
        with os.scandir(recovered_root) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                metadata_path = Path(entry.path) / "metadata.json"
                pgm_path = Path(entry.path) / "image.pgm"
                raw_path = Path(entry.path) / "image.raw"
                if metadata_path.is_file() and pgm_path.is_file() and raw_path.is_file():
                    count += 1
    except FileNotFoundError:
        return 0
    return count


def _scan_complete_container(
    container: Path,
    *,
    attempt_name: str,
    camera_name: str,
    camera_id: int | None,
    generation: int,
    archive_root: Path,
) -> list[FrameArtifactRef]:
    candidates: list[FrameArtifactRef] = []
    try:
        with os.scandir(container) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not entry.name.lower().endswith(".json"):
                    continue
                stem = Path(entry.name).stem
                if not stem.isdigit():
                    continue
                pgm_path = container / f"{stem}.pgm"
                raw_path = container / f"{stem}.raw"
                if not pgm_path.is_file() or not raw_path.is_file():
                    continue
                try:
                    metadata = _read_json(entry.path)
                except (OSError, json.JSONDecodeError):
                    continue
                if str(metadata.get("status")) != "COMPLETE":
                    continue
                frame_id = int(metadata.get("frame_id", stem))
                candidates.append(
                    FrameArtifactRef(
                        generation=generation,
                        stream="COMPLETE",
                        archive_root=archive_root,
                        attempt_name=attempt_name,
                        camera_name=camera_name,
                        camera_id=camera_id,
                        frame_id=frame_id,
                        status="COMPLETE",
                        timestamp=_metadata_timestamp(metadata, pgm_path),
                        file_name=pgm_path.name,
                        pgm_path=pgm_path,
                        json_path=Path(entry.path),
                        raw_path=raw_path,
                        signature=_signature(Path(entry.path), pgm_path, raw_path),
                    )
                )
    except FileNotFoundError:
        return []
    return candidates


def _scan_recovered_container(
    container: Path,
    *,
    attempt_name: str,
    camera_name: str,
    camera_id: int | None,
    generation: int,
    archive_root: Path,
) -> list[FrameArtifactRef]:
    candidates: list[FrameArtifactRef] = []
    try:
        with os.scandir(container) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    metadata_path = Path(entry.path) / "metadata.json"
                    pgm_path = Path(entry.path) / "image.pgm"
                    raw_path = Path(entry.path) / "image.raw"
                    if not metadata_path.is_file() or not pgm_path.is_file() or not raw_path.is_file():
                        continue
                    try:
                        metadata = _read_json(metadata_path)
                    except (OSError, json.JSONDecodeError):
                        continue
                    if str(metadata.get("status")) != "RECOVERED":
                        continue
                    frame_id = int(metadata.get("frame_id", _frame_id_from_dir(entry.name)))
                    candidates.append(
                        FrameArtifactRef(
                            generation=generation,
                            stream="RECOVERED",
                            archive_root=archive_root,
                            attempt_name=attempt_name,
                            camera_name=camera_name,
                            camera_id=camera_id,
                            frame_id=frame_id,
                            status="RECOVERED",
                            timestamp=_metadata_timestamp(metadata, pgm_path),
                            file_name=pgm_path.name,
                            pgm_path=pgm_path,
                            json_path=metadata_path,
                            raw_path=raw_path,
                            signature=_signature(metadata_path, pgm_path, raw_path),
                        )
                    )
                    continue
                if not entry.is_file(follow_symlinks=False) or not entry.name.lower().endswith(".json"):
                    continue
                stem = Path(entry.name).stem
                if not stem.isdigit():
                    continue
                pgm_path = container / f"{stem}.pgm"
                raw_path = container / f"{stem}.raw"
                if not pgm_path.is_file() or not raw_path.is_file():
                    continue
                try:
                    metadata = _read_json(entry.path)
                except (OSError, json.JSONDecodeError):
                    continue
                if str(metadata.get("status")) != "RECOVERED":
                    continue
                frame_id = int(metadata.get("frame_id", stem))
                candidates.append(
                    FrameArtifactRef(
                        generation=generation,
                        stream="RECOVERED",
                        archive_root=archive_root,
                        attempt_name=attempt_name,
                        camera_name=camera_name,
                        camera_id=camera_id,
                        frame_id=frame_id,
                        status="RECOVERED",
                        timestamp=_metadata_timestamp(metadata, pgm_path),
                        file_name=pgm_path.name,
                        pgm_path=pgm_path,
                        json_path=Path(entry.path),
                        raw_path=raw_path,
                        signature=_signature(Path(entry.path), pgm_path, raw_path),
                    )
                )
    except FileNotFoundError:
        return []
    return candidates


def _iter_directories(path: Path):
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    yield Path(entry.path)
    except FileNotFoundError:
        return


def _read_json(path: str | Path) -> dict[str, object]:
    return json.loads(Path(path).read_text("utf-8"))


def _metadata_timestamp(metadata: dict[str, object], fallback_path: Path) -> float:
    timestamp = metadata.get("timestamp")
    if isinstance(timestamp, (int, float)):
        return float(timestamp)
    return fallback_path.stat().st_mtime_ns / 1_000_000_000.0


def _signature(*paths: Path) -> tuple[int, int, int, str]:
    latest_mtime = 0
    total_size = 0
    for path in paths:
        stat = path.stat()
        latest_mtime = max(latest_mtime, stat.st_mtime_ns)
        total_size += stat.st_size
    return latest_mtime, total_size, len(paths), str(paths[0])


def _frame_id_from_dir(name: str) -> int:
    match = re.search(r"(\d+)$", name)
    return int(match.group(1)) if match else 0
