"""Closed-loop pose selection for binary intrinsic calibration.

The ordinary preflight command picks the first usable frame that represents a
new board location/scale.  That is sufficient for a quick capture gate, but a
frame can only be identified as a reprojection outlier after an initial camera
model exists.  This module keeps several candidates for every pose cluster and
replaces a rejected representative with the next candidate from the same
cluster before solving again.

Only frames with a complete, ordered circle grid enter a pose cluster.  A
rejected frame remains in the attempt audit, but never contributes to the final
camera matrix or the final per-view report.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from functools import partial
import math
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Callable, Sequence

import cv2
import numpy as np

from .binary_calibration import (
    CalibrationFit,
    DetectorSettings,
    ViewRecord,
    _preflight_rejection,
    _write_json_atomic,
    build_calibration_document,
    calibrate_with_outlier_rejection,
    detect_circle_grid,
    expand_input_paths,
    load_binary_image,
    make_object_points,
    write_detection_diagnostic,
    write_view_report,
)


DEFAULT_POSE_SHIFT_FRACTION = 0.08
DEFAULT_POSE_SCALE_RATIO = 1.15


def natural_path_key(path: Path) -> tuple[Any, ...]:
    """Sort numbered frames chronologically instead of lexicographically."""

    parts = re.split(r"(\d+)", str(path).lower())
    return tuple(int(part) if part.isdigit() else part for part in parts)


@dataclass(slots=True)
class PoseCluster:
    pose_id: int
    anchor_centroid: tuple[float, float]
    anchor_spacing_px: float
    candidates: list[ViewRecord] = field(default_factory=list)
    cursor: int = 0
    exhausted: bool = False

    def current(self, maximum_candidates: int) -> ViewRecord | None:
        limit = min(len(self.candidates), maximum_candidates)
        if self.exhausted or self.cursor >= limit:
            return None
        return self.candidates[self.cursor]

    def advance(self, maximum_candidates: int) -> bool:
        self.cursor += 1
        if self.cursor >= min(len(self.candidates), maximum_candidates):
            self.exhausted = True
            return False
        return True


@dataclass(slots=True)
class RefillResult:
    fit: CalibrationFit
    records: list[ViewRecord]
    clusters: list[PoseCluster]
    rounds: int
    attempts: list[dict[str, Any]]


def _record_features(record: ViewRecord) -> tuple[tuple[float, float], float]:
    detection = record.detection
    if detection is None or not detection.found or detection.centers is None:
        raise ValueError(f"record has no ordered grid: {record.path}")
    centers = np.asarray(detection.centers, dtype=np.float64)
    centroid = (float(np.mean(centers[:, 0])), float(np.mean(centers[:, 1])))
    spacing = float(detection.metrics["nearest_neighbor_spacing_px"])
    return centroid, spacing


def _edge_margin(record: ViewRecord, width: int, height: int) -> float:
    detection = record.detection
    if detection is None or detection.centers is None:
        return -math.inf
    points = np.asarray(detection.centers, dtype=np.float64)
    return float(
        min(
            np.min(points[:, 0]),
            np.min(points[:, 1]),
            width - 1 - np.max(points[:, 0]),
            height - 1 - np.max(points[:, 1]),
        )
    )


def candidate_quality_key(
    record: ViewRecord, point_count: int, width: int, height: int
) -> tuple[Any, ...]:
    """Prefer clean, concentric, complete rings while preserving determinism."""

    detection = record.detection
    if detection is None:
        return (math.inf, math.inf, math.inf, math.inf, math.inf, natural_path_key(record.path))
    metrics = detection.metrics
    candidate_excess = abs(int(metrics.get("candidate_count", 0)) - point_count)
    spread = float(metrics.get("max_concentric_center_spread_px", math.inf))
    residual = float(metrics.get("median_ellipse_residual", math.inf))
    arc = float(metrics.get("median_arc_coverage", 0.0))
    margin = _edge_margin(record, width, height)
    return (
        candidate_excess,
        spread,
        residual,
        -arc,
        -margin,
        natural_path_key(record.path),
    )


def build_pose_clusters(
    records: Sequence[ViewRecord],
    width: int,
    height: int,
    *,
    shift_fraction: float = DEFAULT_POSE_SHIFT_FRACTION,
    scale_ratio: float = DEFAULT_POSE_SCALE_RATIO,
) -> list[PoseCluster]:
    """Greedily cluster complete grids by the established position/scale rule."""

    shift_limit = shift_fraction * float(np.hypot(width, height))
    clusters: list[PoseCluster] = []
    usable = sorted(
        (
            record
            for record in records
            if record.detection is not None and record.detection.found
        ),
        key=lambda record: natural_path_key(record.path),
    )
    for record in usable:
        centroid, spacing = _record_features(record)
        matches: list[tuple[float, PoseCluster]] = []
        for cluster in clusters:
            shift = float(
                np.hypot(
                    centroid[0] - cluster.anchor_centroid[0],
                    centroid[1] - cluster.anchor_centroid[1],
                )
            )
            scale = max(spacing, cluster.anchor_spacing_px) / max(
                min(spacing, cluster.anchor_spacing_px), 1e-9
            )
            if shift < shift_limit and scale < scale_ratio:
                normalized = shift / max(shift_limit, 1e-9)
                normalized += abs(math.log(scale)) / max(math.log(scale_ratio), 1e-9)
                matches.append((normalized, cluster))
        if matches:
            min(matches, key=lambda item: item[0])[1].candidates.append(record)
        else:
            clusters.append(
                PoseCluster(
                    pose_id=len(clusters),
                    anchor_centroid=centroid,
                    anchor_spacing_px=spacing,
                    candidates=[record],
                )
            )

    point_count = 0
    for cluster in clusters:
        if cluster.candidates:
            detection = cluster.candidates[0].detection
            point_count = len(detection.centers) if detection is not None else point_count
    for cluster in clusters:
        cluster.candidates.sort(
            key=lambda record: candidate_quality_key(record, point_count, width, height)
        )
    return clusters


def _reset_record(record: ViewRecord) -> None:
    record.accepted = False
    record.reason = (
        record.detection.reason
        if record.detection is not None
        else "not_processed"
    )
    record.reprojection_rmse_px = None
    record.initial_reprojection_rmse_px = None


def _ill_conditioned_index(error: cv2.error) -> int | None:
    match = re.search(r"input array\s+(\d+)", str(error))
    return None if match is None else int(match.group(1))


def refill_rejected_poses(
    clusters: Sequence[PoseCluster],
    object_points: np.ndarray,
    image_size: tuple[int, int],
    model: str,
    *,
    min_views: int,
    min_final_poses: int,
    max_view_rmse_px: float,
    fov_degrees: float,
    max_refill_rounds: int,
    max_candidates_per_pose: int,
    solve: Callable[..., tuple[CalibrationFit, list[int], list[dict[str, float | str]]]] = calibrate_with_outlier_rejection,
) -> RefillResult:
    """Solve, replace rejected pose representatives, and solve again."""

    attempts: list[dict[str, Any]] = []
    final_fit: CalibrationFit | None = None
    final_records: list[ViewRecord] = []

    for round_number in range(1, max_refill_rounds + 1):
        selected_pairs = [
            (cluster, cluster.current(max_candidates_per_pose)) for cluster in clusters
        ]
        selected_pairs = [
            (cluster, record)
            for cluster, record in selected_pairs
            if record is not None
        ]
        selected = [record for _cluster, record in selected_pairs]
        if len(selected) < min_views:
            raise ValueError(
                f"only {len(selected)} pose clusters still have candidates; "
                f"at least {min_views} are required"
            )
        for record in selected:
            _reset_record(record)

        try:
            fit, active, _outliers = solve(
                selected,
                object_points,
                image_size,
                model,
                min_views,
                max_view_rmse_px,
                fov_degrees,
            )
            active_set = set(active)
            forced_bad: set[int] = set()
        except cv2.error as exc:
            bad_index = _ill_conditioned_index(exc)
            if bad_index is None or not 0 <= bad_index < len(selected):
                raise
            fit = None
            active_set = set()
            forced_bad = {bad_index}
            selected[bad_index].reason = "ill_conditioned_extrinsics"

        rejected_pairs: list[tuple[PoseCluster, ViewRecord]] = []
        for index, (cluster, record) in enumerate(selected_pairs):
            if fit is None:
                # OpenCV can identify one ill-conditioned input before it has
                # produced a fit.  Only that input is known-bad; advancing every
                # pose here would throw away otherwise untested representatives.
                accepted: bool | None = None
                if index in forced_bad:
                    accepted = False
                    rejected_pairs.append((cluster, record))
                elif not record.reason:
                    record.reason = "solver_retry_pending"
            else:
                accepted = index in active_set
                if not accepted:
                    rejected_pairs.append((cluster, record))
            attempts.append(
                {
                    "round": round_number,
                    "pose_id": cluster.pose_id,
                    "candidate_rank": cluster.cursor,
                    "path": str(record.path),
                    "accepted_this_round": accepted,
                    "reason": record.reason,
                    "reprojection_rmse_px": record.reprojection_rmse_px,
                    "initial_reprojection_rmse_px": record.initial_reprojection_rmse_px,
                    "forced_solver_rejection": index in forced_bad,
                }
            )

        if fit is not None and not rejected_pairs:
            if len(selected) < min_final_poses:
                raise ValueError(
                    f"calibration converged with only {len(selected)} accepted poses; "
                    f"at least {min_final_poses} are required"
                )
            assert fit is not None
            final_fit = fit
            final_records = selected
            return RefillResult(
                fit=final_fit,
                records=final_records,
                clusters=list(clusters),
                rounds=round_number,
                attempts=attempts,
            )

        for cluster, _record in rejected_pairs:
            cluster.advance(max_candidates_per_pose)

    raise ValueError(
        f"pose refill did not converge after {max_refill_rounds} rounds"
    )


def detect_input_records(
    inputs: Sequence[str],
    settings: DetectorSettings,
    width: int,
    height: int,
    allow_recovered: bool,
) -> list[ViewRecord]:
    paths = sorted(expand_input_paths(inputs), key=natural_path_key)
    if not paths:
        raise ValueError("no .pgm or .raw inputs found")
    records: list[ViewRecord] = []
    for index, path in enumerate(paths, start=1):
        record = ViewRecord(path=path)
        records.append(record)
        rejection = _preflight_rejection(path, allow_recovered)
        if rejection is not None:
            record.reason = rejection
            continue
        try:
            image = load_binary_image(path, width, height)
            record.detection = detect_circle_grid(image, settings)
            record.reason = record.detection.reason
        except (OSError, ValueError, cv2.error) as exc:
            record.reason = f"load_or_detection_error:{exc}"
        if index % 100 == 0 or index == len(paths):
            usable = sum(
                item.detection is not None and item.detection.found for item in records
            )
            print(
                f"  detection {index}/{len(paths)} frames, {usable} complete grids",
                flush=True,
            )
    return records


def write_attempt_report(path: Path, attempts: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "round",
        "pose_id",
        "candidate_rank",
        "path",
        "accepted_this_round",
        "reason",
        "reprojection_rmse_px",
        "initial_reprojection_rmse_px",
        "forced_solver_rejection",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(attempts)


def write_cluster_report(path: Path, clusters: Sequence[PoseCluster]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "pose_id",
                "candidate_count",
                "final_candidate_rank",
                "exhausted",
                "anchor_centroid_x",
                "anchor_centroid_y",
                "anchor_spacing_px",
                "final_path",
            ]
        )
        for cluster in clusters:
            record = None if cluster.exhausted else cluster.current(len(cluster.candidates))
            writer.writerow(
                [
                    cluster.pose_id,
                    len(cluster.candidates),
                    cluster.cursor,
                    cluster.exhausted,
                    cluster.anchor_centroid[0],
                    cluster.anchor_centroid[1],
                    cluster.anchor_spacing_px,
                    "" if record is None else str(record.path),
                ]
            )


def export_final_records(destination: Path, records: Sequence[ViewRecord]) -> None:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"final selected directory is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for pose_id, record in enumerate(records):
        prefix = f"pose{pose_id:03d}_"
        for suffix in (record.path.suffix, ".json"):
            source = record.path.with_suffix(suffix)
            if source.is_file():
                shutil.copy2(source, destination / f"{prefix}{source.name}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Intrinsic calibration with automatic same-pose replacement of rejected views."
    )
    parser.add_argument("inputs", nargs="+", help="PGM/RAW files, directories, or globs")
    parser.add_argument("--output", type=Path, default=Path("camera_calibration_refilled.json"))
    parser.add_argument("--report", type=Path, help="final accepted-view CSV")
    parser.add_argument("--attempt-report", type=Path, help="all replacement attempts CSV")
    parser.add_argument("--cluster-report", type=Path, help="pose-cluster CSV")
    parser.add_argument("--selected-dir", type=Path, help="copy final accepted PGM/JSON pairs here")
    parser.add_argument("--diagnostics-dir", type=Path)
    parser.add_argument("--camera-id", type=int)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--pattern", choices=("asymmetric", "symmetric"), default="asymmetric")
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--rows", type=int, default=11)
    parser.add_argument("--spacing-mm", type=float, default=20.0)
    parser.add_argument("--dot-diameter-mm", type=float, default=10.0)
    parser.add_argument("--model", choices=("fisheye", "pinhole-rational"), default="fisheye")
    fisheye_constraints = parser.add_mutually_exclusive_group()
    fisheye_constraints.add_argument(
        "--fisheye-fix-k3-k4",
        action="store_true",
        help="fix fisheye k3 and k4 at zero; solve only k1 and k2",
    )
    fisheye_constraints.add_argument(
        "--fisheye-fix-k4",
        action="store_true",
        help="fix fisheye k4 at zero; solve k1, k2, and k3",
    )
    parser.add_argument("--fov-deg", type=float, default=120.0)
    parser.add_argument("--min-views", type=int, default=15)
    parser.add_argument("--min-final-poses", type=int, default=25)
    parser.add_argument("--max-view-rmse-px", type=float, default=1.2)
    parser.add_argument("--max-refill-rounds", type=int, default=12)
    parser.add_argument("--max-candidates-per-pose", type=int, default=8)
    parser.add_argument("--pose-shift-fraction", type=float, default=DEFAULT_POSE_SHIFT_FRACTION)
    parser.add_argument("--pose-scale-ratio", type=float, default=DEFAULT_POSE_SCALE_RATIO)
    parser.add_argument("--min-dot-diameter-px", type=float, default=6.0)
    parser.add_argument("--max-dot-diameter-px", type=float, default=120.0)
    parser.add_argument("--min-grid-spacing-px", type=float, default=10.0)
    parser.add_argument("--min-axis-ratio", type=float, default=0.24)
    parser.add_argument("--min-arc-coverage", type=float, default=0.42)
    parser.add_argument("--max-ellipse-residual", type=float, default=0.30)
    parser.add_argument("--close-kernel", type=int, default=3)
    parser.add_argument("--allow-recovered", action="store_true")
    parser.add_argument("--allow-limited", action="store_true")
    return parser


def _validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.width <= 0 or args.height <= 0:
        parser.error("--width and --height must be positive")
    if args.min_views < 6:
        parser.error("--min-views must be at least 6")
    if args.min_final_poses < args.min_views:
        parser.error("--min-final-poses must be >= --min-views")
    if args.max_view_rmse_px <= 0:
        parser.error("--max-view-rmse-px must be positive")
    if args.max_refill_rounds < 1 or args.max_candidates_per_pose < 1:
        parser.error("refill round and candidate limits must be positive")
    if args.pose_shift_fraction <= 0 or args.pose_scale_ratio <= 1.0:
        parser.error("pose thresholds must be positive and scale ratio must exceed 1")
    if (args.fisheye_fix_k3_k4 or args.fisheye_fix_k4) and args.model != "fisheye":
        parser.error("fisheye distortion constraints require --model fisheye")


def run(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any]]:
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
    detected = detect_input_records(
        args.inputs, settings, args.width, args.height, args.allow_recovered
    )
    clusters = build_pose_clusters(
        detected,
        args.width,
        args.height,
        shift_fraction=args.pose_shift_fraction,
        scale_ratio=args.pose_scale_ratio,
    )
    print(
        f"pose clusters: {len(clusters)} from "
        f"{sum(record.detection is not None and record.detection.found for record in detected)} "
        "complete grids",
        flush=True,
    )
    if len(clusters) < args.min_final_poses:
        raise ValueError(
            f"only {len(clusters)} distinct pose clusters; "
            f"at least {args.min_final_poses} are required"
        )

    solve = partial(
        calibrate_with_outlier_rejection,
        fisheye_fix_k3_k4=args.fisheye_fix_k3_k4,
        fisheye_fix_k4=args.fisheye_fix_k4,
    )
    result = refill_rejected_poses(
        clusters,
        make_object_points(settings, args.spacing_mm),
        (args.width, args.height),
        args.model,
        min_views=args.min_views,
        min_final_poses=args.min_final_poses,
        max_view_rmse_px=args.max_view_rmse_px,
        fov_degrees=args.fov_deg,
        max_refill_rounds=args.max_refill_rounds,
        max_candidates_per_pose=args.max_candidates_per_pose,
        solve=solve,
    )
    document = build_calibration_document(
        result.fit,
        result.records,
        list(range(len(result.records))),
        [],
        settings,
        (args.width, args.height),
        args.model,
        args.camera_id,
        args.spacing_mm,
        args.dot_diameter_mm,
        args.fisheye_fix_k3_k4,
        args.fisheye_fix_k4,
    )
    document["selection"] = {
        "strategy": "pose_cluster_refill_v1",
        "pose_clusters_detected": len(clusters),
        "final_accepted_poses": len(result.records),
        "refill_rounds": result.rounds,
        "max_candidates_per_pose": args.max_candidates_per_pose,
        "pose_shift_fraction": args.pose_shift_fraction,
        "pose_scale_ratio": args.pose_scale_ratio,
        "max_view_rmse_px": args.max_view_rmse_px,
    }

    report_path = args.report or args.output.with_suffix(".views.csv")
    attempt_path = args.attempt_report or args.output.with_suffix(".attempts.csv")
    cluster_path = args.cluster_report or args.output.with_suffix(".clusters.csv")
    _write_json_atomic(args.output, document)
    write_view_report(report_path, result.records)
    write_attempt_report(attempt_path, result.attempts)
    write_cluster_report(cluster_path, clusters)
    if args.selected_dir is not None:
        export_final_records(args.selected_dir, result.records)
    if args.diagnostics_dir is not None:
        for index, record in enumerate(result.records):
            # Detection intentionally does not retain all source images.  Reload
            # only the final representatives so diagnostics stay memory-bounded.
            record.image = load_binary_image(record.path, args.width, args.height)
            try:
                write_detection_diagnostic(
                    args.diagnostics_dir / f"{index:03d}_{record.path.stem}.png",
                    record,
                )
            finally:
                record.image = None
    return args.output, report_path, document


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_arguments(parser, args)
    try:
        output, report, document = run(args)
    except (OSError, ValueError, cv2.error) as exc:
        print(f"refill calibration failed: {exc}", file=sys.stderr)
        return 2
    quality = document["quality"]
    selection = document["selection"]
    print(
        f"refill calibration: {quality['status']}  "
        f"poses={selection['final_accepted_poses']}  "
        f"rounds={selection['refill_rounds']}  "
        f"RMS={quality['rms_px']:.4f}px  "
        f"max-view={quality['max_view_rmse_px']:.4f}px"
    )
    print(f"config: {output}")
    print(f"final accepted views: {report}")
    for warning in quality["warnings"]:
        print(f"warning: {warning}")
    if quality["status"] == "acceptable" or args.allow_limited:
        return 0
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
