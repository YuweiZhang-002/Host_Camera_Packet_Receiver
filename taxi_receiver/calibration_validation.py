"""Fixed-intrinsics validation on images that did not train the model."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Sequence

import cv2
import numpy as np

from .binary_calibration import (
    DetectorSettings,
    DetectionResult,
    _preflight_rejection,
    _write_json_atomic,
    detect_circle_grid,
    expand_input_paths,
    load_binary_image,
    make_object_points,
)
from .calibration_config import validate_camera_calibration
from .calibration_refill import natural_path_key


VALIDATION_SCHEMA = "taxi_receiver.camera_calibration_validation/1"


@dataclass(slots=True)
class ValidationRecord:
    path: Path
    excluded: bool = False
    detection: DetectionResult | None = None
    reason: str = "not_processed"
    sampled: bool = False
    reprojection_rmse_px: float | None = None
    mean_point_error_px: float | None = None
    max_point_error_px: float | None = None
    rvec: np.ndarray | None = None
    tvec: np.ndarray | None = None
    board_tilt_deg: float | None = None
    board_normal_x: float | None = None
    board_normal_y: float | None = None
    metrics: dict[str, float | int] = field(default_factory=dict)


def _original_basename(path: str | Path) -> str:
    name = Path(path).name.lower()
    match = re.match(r"pose\d+_(?:frame)?(.+)", name)
    return match.group(1) if match is not None else name


def read_excluded_views(paths: Sequence[Path]) -> set[str]:
    excluded: set[str] = set()
    for path in paths:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or "path" not in reader.fieldnames:
                raise ValueError(f"exclude report has no path column: {path}")
            for row in reader:
                value = row.get("path")
                if value:
                    excluded.add(Path(value).name.lower())
                    excluded.add(_original_basename(value))
    return excluded


def load_calibration(path: Path) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid calibration JSON in {path}: {exc}") from exc
    validate_camera_calibration(document)
    K = np.asarray(document["K"], dtype=np.float64)
    D = np.asarray(document["dist_coeffs"], dtype=np.float64).reshape(-1, 1)
    return document, K, D


def _project_points(
    object_points: np.ndarray,
    rvec: np.ndarray,
    tvec: np.ndarray,
    K: np.ndarray,
    D: np.ndarray,
    model: str,
) -> np.ndarray:
    if model == "opencv_fisheye":
        projected, _ = cv2.fisheye.projectPoints(
            object_points.reshape(1, -1, 3), rvec, tvec, K, D
        )
    else:
        projected, _ = cv2.projectPoints(object_points, rvec, tvec, K, D)
    return projected.reshape(-1, 2)


def _pose_solutions(
    object_points: np.ndarray,
    image_points: np.ndarray,
    K: np.ndarray,
    D: np.ndarray,
    model: str,
) -> list[tuple[np.ndarray, np.ndarray]]:
    if model == "opencv_fisheye":
        normalized = cv2.fisheye.undistortPoints(
            image_points.reshape(-1, 1, 2).astype(np.float64), K, D
        )
        pose_K = np.eye(3, dtype=np.float64)
        pose_D = np.zeros((4, 1), dtype=np.float64)
        pose_points = normalized
    else:
        pose_K = K
        pose_D = D
        pose_points = image_points.reshape(-1, 1, 2).astype(np.float64)

    solutions: list[tuple[np.ndarray, np.ndarray]] = []
    ok, rvec, tvec = cv2.solvePnP(
        object_points,
        pose_points,
        pose_K,
        pose_D,
        flags=cv2.SOLVEPNP_ITERATIVE,
    )
    if ok:
        solutions.append((rvec, tvec))
    generic = cv2.solvePnPGeneric(
        object_points,
        pose_points,
        pose_K,
        pose_D,
        flags=cv2.SOLVEPNP_IPPE,
    )
    if generic[0]:
        for rvec, tvec in zip(generic[1], generic[2], strict=True):
            try:
                rvec, tvec = cv2.solvePnPRefineLM(
                    object_points, pose_points, pose_K, pose_D, rvec, tvec
                )
            except cv2.error:
                pass
            solutions.append((rvec, tvec))
    return solutions


def evaluate_fixed_calibration(
    record: ValidationRecord,
    object_points: np.ndarray,
    K: np.ndarray,
    D: np.ndarray,
    model: str,
) -> None:
    detection = record.detection
    if detection is None or not detection.found or detection.centers is None:
        return
    observed = detection.centers.reshape(-1, 2).astype(np.float64)
    solutions = _pose_solutions(object_points, observed, K, D, model)
    if not solutions:
        record.reason = "pose_solution_failed"
        return

    best: tuple[float, np.ndarray, np.ndarray, np.ndarray] | None = None
    for rvec, tvec in solutions:
        projected = _project_points(object_points, rvec, tvec, K, D, model)
        residual = projected - observed
        rmse = float(np.sqrt(np.mean(np.sum(residual * residual, axis=1))))
        if best is None or rmse < best[0]:
            best = (rmse, rvec, tvec, residual)
    assert best is not None
    rmse, rvec, tvec, residual = best
    magnitudes = np.linalg.norm(residual, axis=1)
    rotation, _ = cv2.Rodrigues(rvec)
    normal = rotation[:, 2]
    if normal[2] < 0:
        normal = -normal
    record.reprojection_rmse_px = rmse
    record.mean_point_error_px = float(np.mean(magnitudes))
    record.max_point_error_px = float(np.max(magnitudes))
    record.rvec = rvec
    record.tvec = tvec
    record.board_tilt_deg = math.degrees(
        math.acos(float(np.clip(normal[2], -1.0, 1.0)))
    )
    record.board_normal_x = float(normal[0])
    record.board_normal_y = float(normal[1])
    record.reason = "evaluated"


def _diversity_features(
    record: ValidationRecord, width: int, height: int
) -> np.ndarray:
    detection = record.detection
    assert detection is not None and detection.centers is not None
    points = np.asarray(detection.centers, dtype=np.float64)
    spacing = float(detection.metrics["nearest_neighbor_spacing_px"])
    return np.asarray(
        [
            float(np.mean(points[:, 0])) / width,
            float(np.mean(points[:, 1])) / height,
            math.log(max(spacing, 1e-9)),
            float(record.board_normal_x),
            float(record.board_normal_y),
        ],
        dtype=np.float64,
    )


def select_diverse_records(
    records: Sequence[ValidationRecord],
    sample_count: int,
    width: int,
    height: int,
) -> list[ValidationRecord]:
    usable = sorted(
        (record for record in records if record.reprojection_rmse_px is not None),
        key=lambda record: natural_path_key(record.path),
    )
    if sample_count <= 0 or len(usable) <= sample_count:
        for record in usable:
            record.sampled = True
        return usable
    matrix = np.asarray(
        [_diversity_features(record, width, height) for record in usable]
    )
    lower = np.min(matrix, axis=0)
    span = np.maximum(np.max(matrix, axis=0) - lower, 1e-9)
    normalized = (matrix - lower) / span
    chosen = [int(np.argmax(np.linalg.norm(normalized - np.mean(normalized, axis=0), axis=1)))]
    while len(chosen) < sample_count:
        distance = np.min(
            np.linalg.norm(
                normalized[:, None, :] - normalized[chosen][None, :, :], axis=2
            ),
            axis=1,
        )
        distance[chosen] = -1.0
        chosen.append(int(np.argmax(distance)))
    selected = [usable[index] for index in chosen]
    for record in selected:
        record.sampled = True
    return selected


def _record_report(record: ValidationRecord) -> dict[str, Any]:
    row: dict[str, Any] = {
        "path": str(record.path),
        "excluded": record.excluded,
        "grid_found": bool(record.detection is not None and record.detection.found),
        "reason": record.reason,
        "sampled": record.sampled,
        "reprojection_rmse_px": record.reprojection_rmse_px,
        "mean_point_error_px": record.mean_point_error_px,
        "max_point_error_px": record.max_point_error_px,
        "board_tilt_deg": record.board_tilt_deg,
        "board_normal_x": record.board_normal_x,
        "board_normal_y": record.board_normal_y,
    }
    row.update(record.metrics)
    return row


def write_validation_report(path: Path, records: Sequence[ValidationRecord]) -> None:
    rows = [_record_report(record) for record in records]
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_validation_summary(
    calibration_path: Path,
    calibration_document: dict[str, Any],
    records: Sequence[ValidationRecord],
    selected: Sequence[ValidationRecord],
    *,
    min_holdout_views: int,
    median_limit: float,
    p95_limit: float,
    maximum_limit: float,
    required_pass_fraction: float,
) -> dict[str, Any]:
    errors = np.asarray(
        [record.reprojection_rmse_px for record in selected], dtype=np.float64
    )
    if errors.size:
        median = float(np.median(errors))
        p95 = float(np.percentile(errors, 95))
        maximum = float(np.max(errors))
        pass_fraction = float(np.mean(errors <= p95_limit))
    else:
        median = p95 = maximum = math.inf
        pass_fraction = 0.0
    failures: list[str] = []
    if len(selected) < min_holdout_views:
        failures.append(
            f"only {len(selected)} holdout views; need at least {min_holdout_views}"
        )
    if median > median_limit:
        failures.append(f"median RMSE {median:.3f}px exceeds {median_limit:.3f}px")
    if p95 > p95_limit:
        failures.append(f"P95 RMSE {p95:.3f}px exceeds {p95_limit:.3f}px")
    if maximum > maximum_limit:
        failures.append(f"maximum RMSE {maximum:.3f}px exceeds {maximum_limit:.3f}px")
    if pass_fraction < required_pass_fraction:
        failures.append(
            f"fraction <= {p95_limit:.3f}px is {pass_fraction:.1%}; "
            f"need {required_pass_fraction:.1%}"
        )
    digest = hashlib.sha256(calibration_path.read_bytes()).hexdigest()
    return {
        "schema": VALIDATION_SCHEMA,
        "status": "pass" if not failures else "fail",
        "calibration": {
            "path": str(calibration_path),
            "sha256": digest,
            "schema": calibration_document["schema"],
            "model": calibration_document["model"],
            "camera_id": calibration_document.get("camera_id"),
            "image_size": calibration_document["image_size"],
        },
        "counts": {
            "input_frames": len(records),
            "excluded_frames": sum(record.excluded for record in records),
            "complete_grids": sum(
                record.detection is not None and record.detection.found
                for record in records
            ),
            "evaluated_frames": sum(
                record.reprojection_rmse_px is not None for record in records
            ),
            "sampled_holdout_views": len(selected),
        },
        "quality": {
            "median_rmse_px": median,
            "p95_rmse_px": p95,
            "maximum_rmse_px": maximum,
            "fraction_at_or_below_p95_limit": pass_fraction,
            "thresholds": {
                "minimum_holdout_views": min_holdout_views,
                "median_rmse_px": median_limit,
                "p95_rmse_px": p95_limit,
                "maximum_rmse_px": maximum_limit,
                "required_pass_fraction": required_pass_fraction,
            },
            "failures": failures,
        },
        "sampled_views": [_record_report(record) for record in selected],
    }


def _undistort_maps(
    document: dict[str, Any], K: np.ndarray, D: np.ndarray, balance: float
) -> tuple[np.ndarray, np.ndarray]:
    width = int(document["image_size"]["width"])
    height = int(document["image_size"]["height"])
    size = (width, height)
    if document["model"] == "opencv_fisheye":
        new_K = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
            K, D, size, np.eye(3), balance=balance
        )
        return cv2.fisheye.initUndistortRectifyMap(
            K, D, np.eye(3), new_K, size, cv2.CV_32FC1
        )
    new_K, _roi = cv2.getOptimalNewCameraMatrix(K, D, size, 1.0 - balance, size)
    return cv2.initUndistortRectifyMap(
        K, D, None, new_K, size, cv2.CV_32FC1
    )


def write_validation_images(
    output_root: Path,
    selected: Sequence[ValidationRecord],
    document: dict[str, Any],
    K: np.ndarray,
    D: np.ndarray,
    balance: float,
) -> None:
    undistorted_root = output_root / "undistorted"
    comparison_root = output_root / "comparisons"
    undistorted_root.mkdir(parents=True, exist_ok=True)
    comparison_root.mkdir(parents=True, exist_ok=True)
    map_x, map_y = _undistort_maps(document, K, D, balance)
    tiles: list[np.ndarray] = []
    for index, record in enumerate(selected):
        source = load_binary_image(
            record.path,
            int(document["image_size"]["width"]),
            int(document["image_size"]["height"]),
        )
        corrected = cv2.remap(
            source,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
        )
        stem = f"{index:03d}_{record.path.stem}"
        cv2.imwrite(str(undistorted_root / f"{stem}.png"), corrected)
        left = cv2.cvtColor(source, cv2.COLOR_GRAY2BGR)
        right = cv2.cvtColor(corrected, cv2.COLOR_GRAY2BGR)
        cv2.putText(left, "ORIGINAL", (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 220, 255), 1, cv2.LINE_AA)
        cv2.putText(right, "UNDISTORTED", (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 220, 0), 1, cv2.LINE_AA)
        cv2.putText(
            right,
            f"holdout RMSE={record.reprojection_rmse_px:.3f}px",
            (8, 42),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (255, 200, 0),
            1,
            cv2.LINE_AA,
        )
        comparison = np.hstack((left, right))
        cv2.imwrite(str(comparison_root / f"{stem}.png"), comparison)
        tiles.append(cv2.resize(comparison, (640, 240), interpolation=cv2.INTER_AREA))
    if not tiles:
        return
    columns = 3
    while len(tiles) % columns:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [
        np.hstack(tiles[index : index + columns])
        for index in range(0, len(tiles), columns)
    ]
    cv2.imwrite(str(output_root / "comparison_montage.png"), np.vstack(rows))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a fixed camera calibration on held-out binary frames."
    )
    parser.add_argument("calibration", type=Path, help="existing intrinsic JSON")
    parser.add_argument("inputs", nargs="+", help="holdout PGM/RAW files, directories, or globs")
    parser.add_argument("--exclude-views", type=Path, action="append", default=[])
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--sample-count", type=int, default=30)
    parser.add_argument("--min-holdout-views", type=int, default=15)
    parser.add_argument("--median-rmse-px", type=float, default=0.8)
    parser.add_argument("--p95-rmse-px", type=float, default=1.2)
    parser.add_argument("--maximum-rmse-px", type=float, default=1.5)
    parser.add_argument("--required-pass-fraction", type=float, default=0.90)
    parser.add_argument("--balance", type=float, default=0.2)
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument("--allow-recovered", action="store_true")
    parser.add_argument("--min-dot-diameter-px", type=float, default=6.0)
    parser.add_argument("--max-dot-diameter-px", type=float, default=120.0)
    parser.add_argument("--min-grid-spacing-px", type=float, default=10.0)
    parser.add_argument("--min-axis-ratio", type=float, default=0.24)
    parser.add_argument("--min-arc-coverage", type=float, default=0.42)
    parser.add_argument("--max-ellipse-residual", type=float, default=0.30)
    parser.add_argument("--close-kernel", type=int, default=3)
    return parser


def _validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.sample_count < 0 or args.min_holdout_views < 1:
        parser.error("sample counts must be non-negative and minimum views positive")
    if not 0.0 <= args.required_pass_fraction <= 1.0:
        parser.error("--required-pass-fraction must be in [0, 1]")
    if not 0.0 <= args.balance <= 1.0:
        parser.error("--balance must be in [0, 1]")
    if min(args.median_rmse_px, args.p95_rmse_px, args.maximum_rmse_px) <= 0:
        parser.error("RMSE thresholds must be positive")


def run(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any]]:
    if args.output_root.exists() and any(args.output_root.iterdir()):
        raise ValueError(f"output root is not empty: {args.output_root}")
    args.output_root.mkdir(parents=True, exist_ok=True)
    document, K, D = load_calibration(args.calibration)
    width = int(document["image_size"]["width"])
    height = int(document["image_size"]["height"])
    pattern = document["pattern"]
    pattern_type = str(pattern["type"])
    settings = DetectorSettings(
        pattern="asymmetric" if pattern_type.startswith("asymmetric") else "symmetric",
        columns=int(pattern["columns"]),
        rows=int(pattern["rows"]),
        min_dot_diameter_px=args.min_dot_diameter_px,
        max_dot_diameter_px=args.max_dot_diameter_px,
        min_axis_ratio=args.min_axis_ratio,
        min_arc_coverage=args.min_arc_coverage,
        max_ellipse_residual=args.max_ellipse_residual,
        close_kernel=args.close_kernel,
        min_grid_spacing_px=args.min_grid_spacing_px,
    )
    spacing = float(pattern["base_spacing_mm"])
    object_points = make_object_points(settings, spacing)
    excluded = read_excluded_views(args.exclude_views)
    paths = sorted(expand_input_paths(args.inputs), key=natural_path_key)
    if not paths:
        raise ValueError("no .pgm or .raw inputs found")
    records: list[ValidationRecord] = []
    for index, path in enumerate(paths, start=1):
        record = ValidationRecord(path=path)
        records.append(record)
        if path.name.lower() in excluded or _original_basename(path) in excluded:
            record.excluded = True
            record.reason = "calibration_input_excluded"
            continue
        rejection = _preflight_rejection(path, args.allow_recovered)
        if rejection is not None:
            record.reason = rejection
            continue
        try:
            image = load_binary_image(path, width, height)
            record.detection = detect_circle_grid(image, settings)
            record.reason = record.detection.reason
            record.metrics = dict(record.detection.metrics)
            evaluate_fixed_calibration(
                record, object_points, K, D, document["model"]
            )
        except (OSError, ValueError, cv2.error) as exc:
            record.reason = f"validation_error:{exc}"
        if index % 100 == 0 or index == len(paths):
            evaluated = sum(item.reprojection_rmse_px is not None for item in records)
            print(
                f"  validation {index}/{len(paths)} frames, {evaluated} evaluated",
                flush=True,
            )

    selected = select_diverse_records(records, args.sample_count, width, height)
    summary = build_validation_summary(
        args.calibration,
        document,
        records,
        selected,
        min_holdout_views=args.min_holdout_views,
        median_limit=args.median_rmse_px,
        p95_limit=args.p95_rmse_px,
        maximum_limit=args.maximum_rmse_px,
        required_pass_fraction=args.required_pass_fraction,
    )
    report_path = args.output_root / "holdout_views.csv"
    summary_path = args.output_root / "holdout_summary.json"
    write_validation_report(report_path, records)
    _write_json_atomic(summary_path, summary)
    if not args.no_images:
        write_validation_images(
            args.output_root, selected, document, K, D, args.balance
        )
    return summary_path, report_path, summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_arguments(parser, args)
    try:
        summary_path, report_path, summary = run(args)
    except (OSError, ValueError, cv2.error) as exc:
        print(f"calibration validation failed: {exc}", file=sys.stderr)
        return 2
    quality = summary["quality"]
    print(
        f"holdout validation: {summary['status']}  "
        f"views={summary['counts']['sampled_holdout_views']}  "
        f"median={quality['median_rmse_px']:.4f}px  "
        f"P95={quality['p95_rmse_px']:.4f}px  "
        f"max={quality['maximum_rmse_px']:.4f}px  "
        f"pass-fraction={quality['fraction_at_or_below_p95_limit']:.1%}"
    )
    print(f"summary: {summary_path}")
    print(f"per-view report: {report_path}")
    for failure in quality["failures"]:
        print(f"failure: {failure}")
    return 0 if summary["status"] == "pass" else 3


if __name__ == "__main__":
    raise SystemExit(main())
