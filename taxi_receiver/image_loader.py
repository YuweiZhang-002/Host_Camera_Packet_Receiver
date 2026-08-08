from __future__ import annotations

from dataclasses import dataclass
import json
import time
from pathlib import Path

from .archive_layout import FrameArtifactRef


@dataclass(frozen=True)
class LoadedArchiveFrame:
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
    width: int
    height: int
    maxval: int
    image_bytes: bytes
    missing_rows: tuple[int, ...]
    missing_count: int
    fill_policy: str
    expected_rows: int | None


class ArchiveImageLoadError(RuntimeError):
    pass


def load_archive_frame(source: FrameArtifactRef, *, retries: int = 3) -> LoadedArchiveFrame:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            return _load_archive_frame_once(source)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(0.02)
    assert last_error is not None
    raise ArchiveImageLoadError(str(last_error)) from last_error


def _load_archive_frame_once(source: FrameArtifactRef) -> LoadedArchiveFrame:
    metadata = json.loads(_read_file_when_stable(source.json_path).decode("utf-8"))
    status = str(metadata.get("status", ""))
    if status != source.status:
        raise ValueError(
            f"metadata status {status!r} does not match expected {source.status!r}"
        )

    pgm_bytes = _read_file_when_stable(source.pgm_path)
    width, height, maxval, pixels = _parse_pgm(pgm_bytes)
    if maxval <= 0 or maxval > 255:
        raise ValueError(f"invalid PGM maxval: {maxval}")

    timestamp = _metadata_timestamp(metadata, source.pgm_path)
    missing_rows = tuple(int(value) for value in metadata.get("missing_rows", []))
    missing_count = int(metadata.get("missing_count", len(missing_rows)))
    fill_policy = str(metadata.get("fill_policy", ""))
    expected_rows = metadata.get("expected_rows")
    expected_rows_int = int(expected_rows) if isinstance(expected_rows, int) else None

    return LoadedArchiveFrame(
        generation=source.generation,
        stream=source.stream,
        archive_root=source.archive_root,
        attempt_name=source.attempt_name,
        camera_name=source.camera_name,
        camera_id=source.camera_id,
        frame_id=source.frame_id,
        status=status,
        timestamp=timestamp,
        file_name=source.file_name,
        pgm_path=source.pgm_path,
        json_path=source.json_path,
        width=width,
        height=height,
        maxval=maxval,
        image_bytes=pixels,
        missing_rows=missing_rows,
        missing_count=missing_count,
        fill_policy=fill_policy,
        expected_rows=expected_rows_int,
    )


def _read_file_when_stable(path: Path) -> bytes:
    first = path.stat()
    data = path.read_bytes()
    second = path.stat()
    if first.st_size != second.st_size or first.st_mtime_ns != second.st_mtime_ns:
        raise OSError(f"file changed while reading: {path}")
    return data


def _metadata_timestamp(metadata: dict[str, object], fallback_path: Path) -> float:
    timestamp = metadata.get("timestamp")
    if isinstance(timestamp, (int, float)):
        return float(timestamp)
    return fallback_path.stat().st_mtime_ns / 1_000_000_000.0


def _parse_pgm(data: bytes) -> tuple[int, int, int, bytes]:
    if not data.startswith(b"P5"):
        raise ValueError("PGM header must start with P5")

    tokens: list[bytes] = []
    index = 2
    length = len(data)
    while index < length and len(tokens) < 3:
        while index < length and data[index] in b" \t\r\n\f\v":
            index += 1
        if index >= length:
            break
        if data[index] == ord("#"):
            while index < length and data[index] not in b"\r\n":
                index += 1
            continue
        start = index
        while index < length and data[index] not in b" \t\r\n\f\v#":
            index += 1
        tokens.append(data[start:index])

    if len(tokens) != 3:
        raise ValueError("PGM header is incomplete")

    width = int(tokens[0])
    height = int(tokens[1])
    maxval = int(tokens[2])
    if width <= 0 or height <= 0:
        raise ValueError("PGM dimensions must be positive")

    if index >= length:
        raise ValueError("PGM header is missing pixel data")
    if data[index] in b"\r\n \t\f\v":
        index += 1
        if index < length and data[index - 1] == ord("\r") and data[index] == ord("\n"):
            index += 1

    pixels = data[index:]
    expected = width * height
    if len(pixels) != expected:
        raise ValueError(
            f"PGM pixel data length mismatch: expected {expected}, got {len(pixels)}"
        )
    return width, height, maxval, pixels
