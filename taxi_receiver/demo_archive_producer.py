from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
import tempfile
import time
from pathlib import Path


@dataclass(frozen=True)
class DemoFrameSpec:
    frame_id: int
    status: str
    timestamp: float
    missing_rows: tuple[int, ...]


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Produce a temporary demo archive for the camera viewer.")
    parser.add_argument("--root", type=Path, default=None, help="Demo archive root. Defaults to a new temp directory.")
    parser.add_argument("--attempt", default="attempt1")
    parser.add_argument("--camera", default="cam0")
    parser.add_argument("--fps", type=float, default=16.0)
    parser.add_argument("--duration-seconds", type=float, default=5.0)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    return parser


def produce_demo_archive(
    root: str | Path | None = None,
    *,
    attempt: str = "attempt1",
    camera: str = "cam0",
    fps: float = 16.0,
    duration_seconds: float = 5.0,
    width: int = 640,
    height: int = 480,
) -> Path:
    if root is None:
        root = Path(tempfile.mkdtemp(prefix="taxi_receiver_demo_archive_"))
    root = Path(root)
    attempt_dir = root / attempt
    camera_dir = attempt_dir / camera
    recovered_root = camera_dir / "recovered"
    camera_dir.mkdir(parents=True, exist_ok=True)
    recovered_root.mkdir(parents=True, exist_ok=True)

    frame_count = max(1, int(round(duration_seconds * fps)))
    period = 1.0 / fps if fps > 0 else 0.0
    for index in range(frame_count):
        status = "COMPLETE" if index % 2 == 0 else "RECOVERED"
        spec = DemoFrameSpec(
            frame_id=index,
            status=status,
            timestamp=time.time(),
            missing_rows=(index % max(2, min(12, height // 20)),) if status == "RECOVERED" else (),
        )
        _write_demo_frame(camera_dir, recovered_root, spec, width=width, height=height)
        if period > 0:
            time.sleep(period)
    return root


def _write_demo_frame(
    camera_dir: Path,
    recovered_root: Path,
    spec: DemoFrameSpec,
    *,
    width: int,
    height: int,
) -> None:
    pixels = _build_pixels(spec.frame_id, width, height, spec.missing_rows)
    if spec.status == "COMPLETE":
        pgm_path = camera_dir / f"{spec.frame_id}.pgm"
        raw_path = camera_dir / f"{spec.frame_id}.raw"
        json_path = camera_dir / f"{spec.frame_id}.json"
        metadata = {
            "cam_id": 0,
            "frame_id": spec.frame_id,
            "status": "COMPLETE",
            "timestamp": spec.timestamp,
            "width": width,
            "height": height,
            "missing_rows": [],
            "missing_count": 0,
            "fill_policy": "none",
            "expected_rows": height,
        }
        pgm_path.write_bytes(_pgm_bytes(width, height, pixels))
        raw_path.write_bytes(pixels)
        json_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return

    frame_dir = recovered_root / f"frame_{spec.frame_id}"
    frame_dir.mkdir(parents=True, exist_ok=True)
    pgm_path = frame_dir / "image.pgm"
    raw_path = frame_dir / "image.raw"
    json_path = frame_dir / "metadata.json"
    metadata = {
        "cam_id": 0,
        "frame_id": spec.frame_id,
        "status": "RECOVERED",
        "timestamp": spec.timestamp,
        "width": width,
        "height": height,
        "missing_rows": list(spec.missing_rows),
        "missing_count": len(spec.missing_rows),
        "fill_policy": "zero",
        "expected_rows": height,
    }
    pgm_path.write_bytes(_pgm_bytes(width, height, pixels))
    raw_path.write_bytes(pixels)
    json_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_pixels(frame_id: int, width: int, height: int, missing_rows: tuple[int, ...]) -> bytes:
    pixels = bytearray(width * height)
    for row in range(height):
        start = row * width
        if row in missing_rows:
            continue
        value = (frame_id * 7 + row) % 256
        pixels[start:start + width] = bytes([(value + column) % 256 for column in range(width)])
    return bytes(pixels)


def _pgm_bytes(width: int, height: int, pixels: bytes) -> bytes:
    return f"P5\n{width} {height}\n255\n".encode("ascii") + pixels


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    root = produce_demo_archive(
        args.root,
        attempt=args.attempt,
        camera=args.camera,
        fps=args.fps,
        duration_seconds=args.duration_seconds,
        width=args.width,
        height=args.height,
    )
    print(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
