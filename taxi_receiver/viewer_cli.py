from __future__ import annotations

import argparse
from pathlib import Path

from .camera_viewer import CameraViewerApp, DEFAULT_ARCHIVE_ROOT


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open a live, read-only viewer for taxi_receiver camera archives.")
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=DEFAULT_ARCHIVE_ROOT,
        help="Root archive directory that contains attemptN folders.",
    )
    parser.add_argument(
        "--attempt",
        default="attempt1",
        help="Attempt selector; accepts 3 or attempt3.",
    )
    parser.add_argument(
        "--camera",
        default="",
        help="Optional initial camera name such as cam0.",
    )
    parser.add_argument(
        "--poll-interval-ms",
        type=int,
        default=50,
        help="Directory polling interval for the background monitor.",
    )
    parser.add_argument(
        "--refresh-interval-ms",
        type=int,
        default=50,
        help="UI refresh interval in milliseconds.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    app = CameraViewerApp(
        archive_root=args.archive_root,
        attempt=args.attempt,
        camera=args.camera or None,
        poll_interval_ms=args.poll_interval_ms,
        refresh_interval_ms=args.refresh_interval_ms,
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
