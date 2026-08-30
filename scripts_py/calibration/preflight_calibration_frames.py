"""Pre-flight gate for binary circle-grid calibration captures.

Answers one question before you spend a run on ``calibrate_binary_camera.py``:
*how many frames actually show a complete 4x11 ring pattern, and do they cover
enough distinct poses?*

The detector, image loader and metadata pre-checks are the exact ones the
calibrator uses, so a GO verdict here means the calibrator will see the same
detections.

Output is ASCII-only: the Windows console this ships against is not UTF-8.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
import sys
import time
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import cv2
import numpy as np

from taxi_receiver.binary_calibration import (
    DetectorSettings,
    _preflight_rejection,
    detect_circle_grid,
    expand_input_paths,
    load_binary_image,
)


# A frame is a fresh pose when its board centre moves by more than this
# fraction of the image diagonal, or its apparent scale changes by this ratio.
POSE_SHIFT_FRACTION = 0.08
POSE_SCALE_RATIO = 1.15

# Edge-density band. Sobel + adaptive T_sq on a clean board scene lands well
# inside this; outside it the threshold is starved or saturated.
EDGE_DENSITY_LOW = 0.005
EDGE_DENSITY_HIGH = 0.15


@dataclass(slots=True)
class FrameStat:
    path: Path
    ok: bool = False
    reason: str = "not_processed"
    candidates: int = 0
    edge_density: float = 0.0
    centroid: tuple[float, float] | None = None
    spacing_px: float | None = None
    image: np.ndarray | None = None
    centers: np.ndarray | None = None
    candidate_centers: list[np.ndarray] | None = None


def analyse_frame(
    path: Path,
    settings: DetectorSettings,
    width: int,
    height: int,
    allow_recovered: bool,
    keep_image: bool,
) -> FrameStat:
    stat = FrameStat(path=path)

    rejection = _preflight_rejection(path, allow_recovered)
    if rejection is not None:
        stat.reason = rejection
        return stat

    try:
        image = load_binary_image(path, width, height)
    except (OSError, ValueError) as exc:
        stat.reason = f"load_error:{exc}"
        return stat

    stat.edge_density = float((image > 0).mean())

    try:
        detection = detect_circle_grid(image, settings)
    except cv2.error as exc:
        stat.reason = f"detect_error:{exc}"
        return stat

    stat.candidates = len(detection.candidates)
    stat.ok = detection.found
    stat.reason = detection.reason

    if detection.found and detection.centers is not None:
        centers = np.asarray(detection.centers, dtype=np.float64)
        stat.centroid = (float(centers[:, 0].mean()), float(centers[:, 1].mean()))
        stat.spacing_px = float(detection.metrics["nearest_neighbor_spacing_px"])
        # Always retained: 44 points per view is cheap and drives the zone map.
        stat.centers = detection.centers

    if keep_image:
        stat.image = image
        stat.candidate_centers = [item.center for item in detection.candidates]

    return stat


def count_distinct_poses(
    stats: Sequence[FrameStat], width: int, height: int
) -> list[FrameStat]:
    """Greedy pick of frames that differ in board placement or distance."""

    diagonal = float(np.hypot(width, height))
    keep: list[FrameStat] = []
    for stat in stats:
        if not stat.ok or stat.centroid is None or stat.spacing_px is None:
            continue
        novel = True
        for chosen in keep:
            shift = float(
                np.hypot(
                    stat.centroid[0] - chosen.centroid[0],
                    stat.centroid[1] - chosen.centroid[1],
                )
            )
            scale = max(stat.spacing_px, chosen.spacing_px) / max(
                min(stat.spacing_px, chosen.spacing_px), 1e-9
            )
            if shift < POSE_SHIFT_FRACTION * diagonal and scale < POSE_SCALE_RATIO:
                novel = False
                break
        if novel:
            keep.append(stat)
    return keep


def _dominant_failure(stats: Sequence[FrameStat], point_count: int) -> str:
    candidates = [stat.candidates for stat in stats]
    if not candidates:
        return "no frames were processed at all"
    median = float(np.median(candidates))
    ordering_failures = sum(stat.reason == "grid_ordering_failed" for stat in stats)

    if median < point_count * 0.5:
        return (
            f"median {median:.0f} ellipse candidates vs {point_count} needed -- the board is "
            "almost certainly NOT in frame (or is too small / out of focus). Check the montage."
        )
    if ordering_failures > len(stats) * 0.3:
        return (
            "enough candidates but grid ordering keeps failing -- background clutter or broken "
            "rings. Clear the background so the board is the only strong edge content."
        )
    return (
        f"median {median:.0f} candidates vs {point_count} needed -- rings are partially resolved. "
        "Increase board size in frame, improve focus, or reduce competing scene edges."
    )


def export_selected(destination: Path, poses: Sequence[FrameStat]) -> int:
    """Copy one representative frame per distinct pose into ``destination``.

    The calibrator re-solves the whole active set once per outlier iteration, so
    feeding it 200 near-identical views is both slow and statistically useless.
    One frame per pose is what actually constrains the intrinsics.
    """

    import shutil

    destination.mkdir(parents=True, exist_ok=True)
    exported = 0
    for index, stat in enumerate(poses):
        for suffix in (stat.path.suffix, ".json"):
            source = stat.path.with_suffix(suffix)
            if source.is_file():
                shutil.copy2(source, destination / f"pose{index:03d}_{source.name}")
        exported += 1
    return exported


def write_csv(path: Path, stats: Sequence[FrameStat]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["path", "grid_found", "reason", "candidate_count", "edge_density",
             "centroid_x", "centroid_y", "spacing_px"]
        )
        for stat in stats:
            writer.writerow(
                [
                    str(stat.path),
                    stat.ok,
                    stat.reason,
                    stat.candidates,
                    f"{stat.edge_density:.5f}",
                    "" if stat.centroid is None else f"{stat.centroid[0]:.2f}",
                    "" if stat.centroid is None else f"{stat.centroid[1]:.2f}",
                    "" if stat.spacing_px is None else f"{stat.spacing_px:.2f}",
                ]
            )


def write_montage(path: Path, stats: Sequence[FrameStat], columns: int = 4) -> None:
    tiles: list[np.ndarray] = []
    for stat in stats:
        if stat.image is None:
            continue
        canvas = cv2.cvtColor(stat.image, cv2.COLOR_GRAY2BGR)
        for center in stat.candidate_centers or []:
            point = tuple(int(round(value)) for value in center)
            cv2.circle(canvas, point, 8, (0, 170, 255), 1, cv2.LINE_AA)
        for center in stat.centers if stat.centers is not None else []:
            point = tuple(int(round(value)) for value in center)
            cv2.circle(canvas, point, 3, (0, 255, 0), -1, cv2.LINE_AA)
        colour = (0, 220, 0) if stat.ok else (0, 0, 255)
        cv2.putText(
            canvas,
            f"{stat.path.name}  cand={stat.candidates}  {'GRID OK' if stat.ok else stat.reason[:38]}",
            (6, 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            colour,
            1,
            cv2.LINE_AA,
        )
        tiles.append(cv2.resize(canvas, (480, 360), interpolation=cv2.INTER_AREA))

    if not tiles:
        return
    while len(tiles) % columns:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.hstack(tiles[i : i + columns]) for i in range(0, len(tiles), columns)]
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.vstack(rows))


def write_zone_map(stats: Sequence[FrameStat], point_count: int) -> None:
    """Print a 3x4 occupancy grid of where detected points have ever landed.

    Unobserved zones are where distortion coefficients get extrapolated rather
    than fitted, which is exactly where an unchecked calibration goes wrong.
    """

    occupancy = np.zeros((3, 4), dtype=int)
    for stat in stats:
        if stat.centers is None:
            continue
        for x, y in np.asarray(stat.centers, dtype=np.float64):
            occupancy[min(2, int(y // 160)), min(3, int(x // 160))] += 1

    print()
    print("point coverage by image zone:")
    print("             x:0-160  160-320  320-480  480-640")
    for row in range(3):
        cells = "".join(f"{occupancy[row, col]:9d}" for col in range(4))
        print(f"  y:{row * 160:3d}-{(row + 1) * 160:3d} {cells}")
    empty = int((occupancy == 0).sum())
    print(f"  zones touched: {12 - empty}/12", end="")
    print("  -- move the board into the empty zones" if empty else "  -- full coverage")


def report(args: argparse.Namespace, stats: list[FrameStat]) -> int:
    point_count = args.columns * args.rows
    usable = [stat for stat in stats if stat.ok]
    poses = count_distinct_poses(stats, args.width, args.height)
    densities = [stat.edge_density for stat in stats if stat.edge_density > 0]
    candidates = [stat.candidates for stat in stats]

    print()
    print("=" * 68)
    print(f"frames inspected      : {len(stats)}")
    print(f"complete {point_count}-ring grids : {len(usable)}")
    print(f"distinct poses        : {len(poses)}  (need >= {args.min_poses})")
    if candidates:
        print(
            f"candidates per frame  : min={min(candidates)} "
            f"median={float(np.median(candidates)):.0f} max={max(candidates)}"
        )
    if densities:
        print(
            f"edge density          : min={min(densities):.3f} "
            f"median={float(np.median(densities)):.3f} max={max(densities):.3f}"
        )
        median_density = float(np.median(densities))
        if median_density < EDGE_DENSITY_LOW:
            print("  NOTE: very sparse edges -- T_sq is starved, or the scene is nearly blank.")
        elif median_density > EDGE_DENSITY_HIGH:
            print("  NOTE: very busy edges -- scene clutter is driving the adaptive T_sq up.")
    print("=" * 68)

    if len(poses) >= args.min_poses:
        print(f"VERDICT: GO -- {len(poses)} distinct poses is enough to run the calibrator.")
        verdict = 0
    else:
        print(f"VERDICT: NO-GO -- only {len(poses)} distinct poses.")
        if not usable:
            print(f"  cause: {_dominant_failure(stats, point_count)}")
        else:
            print(
                "  cause: grids are detected but the board barely moves between frames. "
                "Vary distance, translation and 20-45 deg tilt between shots."
            )
        verdict = 1

    if args.zone_map:
        write_zone_map(stats, point_count)

    if args.export_selected:
        count = export_selected(Path(args.export_selected), poses)
        print(f"selected: {count} frames -> {args.export_selected}")

    if args.montage:
        ranked = sorted(stats, key=lambda item: (item.ok, item.candidates), reverse=True)
        write_montage(Path(args.montage), ranked[: args.montage_count])
        print(f"montage : {args.montage}")
    if args.report:
        write_csv(Path(args.report), stats)
        print(f"csv     : {args.report}")
    return verdict


def run_batch(args: argparse.Namespace, settings: DetectorSettings) -> int:
    paths = expand_input_paths(args.inputs)
    if not paths:
        print("no .pgm or .raw inputs found", file=sys.stderr)
        return 2

    keep_image = bool(args.montage)
    stats: list[FrameStat] = []
    for index, path in enumerate(paths, start=1):
        stats.append(
            analyse_frame(path, settings, args.width, args.height, args.allow_recovered, keep_image)
        )
        if index % 50 == 0 or index == len(paths):
            found = sum(stat.ok for stat in stats)
            print(f"  ...{index}/{len(paths)} frames, {found} complete grids", flush=True)

    # Only the montage candidates need pixels retained.
    if keep_image:
        ranked = sorted(stats, key=lambda item: (item.ok, item.candidates), reverse=True)
        for stat in ranked[args.montage_count :]:
            stat.image = None
    return report(args, stats)


def run_watch(args: argparse.Namespace, settings: DetectorSettings) -> int:
    """Live gate: poll the capture directory and score frames as they land."""

    print(f"watching {args.inputs} -- Ctrl+C to stop and print the summary")
    seen: set[Path] = set()
    stats: list[FrameStat] = []
    try:
        while True:
            fresh = [path for path in expand_input_paths(args.inputs) if path not in seen]
            for path in fresh:
                seen.add(path)
                stat = analyse_frame(
                    path, settings, args.width, args.height, args.allow_recovered, False
                )
                stats.append(stat)
                poses = len(count_distinct_poses(stats, args.width, args.height))
                valid_frames = sum(item.ok for item in stats)
                marker = "ok" if stat.ok else "--"
                detail = "GRID OK" if stat.ok else f"cand={stat.candidates} {stat.reason[:40]}"
                print(
                    f"[{marker}] {path.name:<20} {detail:<52} "
                    f"valid_frames={valid_frames}/{len(stats)} "
                    f"poses={poses}/{args.min_poses}",
                    flush=True,
                )
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        print()
    return report(args, stats)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check whether a capture contains enough complete circle-grid frames "
        "to be worth calibrating."
    )
    parser.add_argument("inputs", nargs="+", help="PGM/RAW files, directories, or globs")
    parser.add_argument("--watch", action="store_true", help="poll the directory live during capture")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--report", type=Path, help="per-frame CSV")
    parser.add_argument("--montage", type=Path, help="annotated PNG of the best frames")
    parser.add_argument("--montage-count", type=int, default=12)
    parser.add_argument("--zone-map", action="store_true", help="print point coverage per image zone")
    parser.add_argument(
        "--export-selected",
        type=Path,
        help="copy one frame per distinct pose here; feed THIS directory to the calibrator",
    )
    parser.add_argument("--min-poses", type=int, default=15)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--pattern", choices=("asymmetric", "symmetric"), default="asymmetric")
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--rows", type=int, default=11)
    parser.add_argument("--min-dot-diameter-px", type=float, default=6.0)
    parser.add_argument("--max-dot-diameter-px", type=float, default=120.0)
    parser.add_argument("--min-grid-spacing-px", type=float, default=10.0)
    parser.add_argument("--min-axis-ratio", type=float, default=0.24)
    parser.add_argument("--min-arc-coverage", type=float, default=0.42)
    parser.add_argument("--max-ellipse-residual", type=float, default=0.30)
    parser.add_argument("--close-kernel", type=int, default=3)
    parser.add_argument("--allow-recovered", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = DetectorSettings(
        pattern=args.pattern,
        columns=args.columns,
        rows=args.rows,
        min_dot_diameter_px=args.min_dot_diameter_px,
        max_dot_diameter_px=args.max_dot_diameter_px,
        min_axis_ratio=args.min_axis_ratio,
        min_arc_coverage=args.min_arc_coverage,
        max_ellipse_residual=args.max_ellipse_residual,
        close_kernel=args.close_kernel,
        min_grid_spacing_px=args.min_grid_spacing_px,
    )
    return run_watch(args, settings) if args.watch else run_batch(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
