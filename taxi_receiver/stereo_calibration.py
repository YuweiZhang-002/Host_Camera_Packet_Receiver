"""Fixed-intrinsics fisheye stereo extrinsic calibration."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from pathlib import Path
import sys
from typing import Any, Sequence

import cv2
import numpy as np

from .binary_calibration import (
    _write_json_atomic,
    detect_circle_grid,
    load_binary_image,
    make_object_points,
)
from .calibration_validation import _pose_solutions, _project_points
from .extrinsic_config import (
    STEREO_EXTRINSICS_SCHEMA,
    TRANSFORM_CONVENTION,
    fisheye_domain_report,
    intrinsic_point_set,
    intrinsic_reference,
    sha256_file,
    validate_intrinsic_pair,
    validate_stereo_extrinsics,
)
from .stereo_pairs import (
    detector_settings,
    load_pairing_provenance,
    read_accepted_pair_manifest,
    stereo_preflight_rejection,
    verify_frozen_stillness_provenance,
    verify_pair_manifest_images,
    write_csv_rows,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class PoseEstimate:
    rvec: np.ndarray
    tvec: np.ndarray
    rotation: np.ndarray
    rmse_px: float
    maximum_px: float


@dataclass(slots=True)
class TransformCandidate:
    points0: np.ndarray
    points1: np.ndarray
    pose0: PoseEstimate
    pose1: PoseEstimate
    rotation: np.ndarray
    translation: np.ndarray
    orientation: str


@dataclass(slots=True)
class StereoPairRecord:
    pose_id: str
    cam0_path: Path
    cam1_path: Path
    manifest_row: dict[str, str] = field(default_factory=dict)
    reason: str = "not_processed"
    points0: np.ndarray | None = None
    points1: np.ndarray | None = None
    candidates: list[TransformCandidate] = field(default_factory=list)
    selected: TransformCandidate | None = None
    accepted: bool = False
    rotation_residual_deg: float | None = None
    translation_residual_mm: float | None = None
    cross_cam0_to_cam1_rmse_px: float | None = None
    cross_cam1_to_cam0_rmse_px: float | None = None
    cross_bidirectional_rmse_px: float | None = None
    cross_maximum_px: float | None = None


def solve_fixed_pose(
    object_points: np.ndarray,
    image_points: np.ndarray,
    K: np.ndarray,
    D: np.ndarray,
    model: str = "opencv_fisheye",
) -> PoseEstimate | None:
    best: PoseEstimate | None = None
    for rvec, tvec in _pose_solutions(object_points, image_points, K, D, model):
        rotation, _ = cv2.Rodrigues(rvec)
        camera_points = (rotation @ object_points.T + tvec.reshape(3, 1)).T
        if np.min(camera_points[:, 2]) <= 0.0:
            continue
        projected = _project_points(object_points, rvec, tvec, K, D, model)
        errors = np.linalg.norm(projected - image_points.reshape(-1, 2), axis=1)
        estimate = PoseEstimate(
            np.asarray(rvec, dtype=np.float64).reshape(3, 1),
            np.asarray(tvec, dtype=np.float64).reshape(3, 1),
            rotation,
            float(np.sqrt(np.mean(errors * errors))),
            float(np.max(errors)),
        )
        if best is None or estimate.rmse_px < best.rmse_px:
            best = estimate
    return best


def relative_transform(pose0: PoseEstimate, pose1: PoseEstimate) -> tuple[np.ndarray, np.ndarray]:
    rotation = pose1.rotation @ pose0.rotation.T
    translation = pose1.tvec - rotation @ pose0.tvec
    return rotation, translation


def rotation_distance_deg(first: np.ndarray, second: np.ndarray) -> float:
    relative = first @ second.T
    cosine = float(np.clip((np.trace(relative) - 1.0) * 0.5, -1.0, 1.0))
    return math.degrees(math.acos(cosine))


def mean_rotation(rotations: Sequence[np.ndarray]) -> np.ndarray:
    if not rotations:
        raise ValueError("cannot average zero rotations")
    matrix = np.sum(np.asarray(rotations, dtype=np.float64), axis=0)
    left, _singular, right_t = np.linalg.svd(matrix)
    result = left @ right_t
    if np.linalg.det(result) < 0.0:
        left[:, -1] *= -1.0
        result = left @ right_t
    return result


def _candidate_distance(
    candidate: TransformCandidate,
    rotation: np.ndarray,
    translation: np.ndarray,
    rotation_scale_deg: float,
    translation_scale_mm: float,
) -> tuple[float, float, float]:
    rotation_error = rotation_distance_deg(candidate.rotation, rotation)
    translation_error = float(np.linalg.norm(candidate.translation - translation))
    score = rotation_error / rotation_scale_deg + translation_error / translation_scale_mm
    return score, rotation_error, translation_error


def choose_orientation_consensus(
    records: Sequence[StereoPairRecord],
    *,
    seed_rotation_limit_deg: float,
    seed_translation_limit_mm: float,
) -> None:
    all_candidates = [candidate for record in records for candidate in record.candidates]
    if not all_candidates:
        raise ValueError("no per-pair transform candidates were solved")
    best: tuple[int, float, TransformCandidate] | None = None
    for seed in all_candidates:
        count = 0
        cost = 0.0
        for record in records:
            distances = [
                _candidate_distance(
                    candidate,
                    seed.rotation,
                    seed.translation,
                    seed_rotation_limit_deg,
                    seed_translation_limit_mm,
                )
                for candidate in record.candidates
            ]
            if not distances:
                continue
            selected = min(distances, key=lambda item: item[0])
            if selected[1] <= seed_rotation_limit_deg and selected[2] <= seed_translation_limit_mm:
                count += 1
                cost += selected[0]
        score = (count, -cost)
        if best is None or score > (best[0], -best[1]):
            best = (count, cost, seed)
    assert best is not None
    seed = best[2]
    for record in records:
        if not record.candidates:
            continue
        record.selected = min(
            record.candidates,
            key=lambda candidate: _candidate_distance(
                candidate,
                seed.rotation,
                seed.translation,
                seed_rotation_limit_deg,
                seed_translation_limit_mm,
            )[0],
        )


def robust_transform_inliers(
    records: Sequence[StereoPairRecord],
    *,
    maximum_rotation_residual_deg: float,
    maximum_translation_residual_mm: float,
    minimum_pairs: int,
) -> tuple[np.ndarray, np.ndarray, list[StereoPairRecord]]:
    active = [record for record in records if record.selected is not None]
    if len(active) < minimum_pairs:
        raise ValueError(f"only {len(active)} solvable stereo pairs; need {minimum_pairs}")
    for _iteration in range(10):
        rotation = mean_rotation([record.selected.rotation for record in active])
        translation = np.median(
            np.asarray([record.selected.translation.reshape(3) for record in active]),
            axis=0,
        ).reshape(3, 1)
        rotation_errors = np.asarray(
            [rotation_distance_deg(record.selected.rotation, rotation) for record in active]
        )
        translation_errors = np.asarray(
            [float(np.linalg.norm(record.selected.translation - translation)) for record in active]
        )
        rot_median = float(np.median(rotation_errors))
        trans_median = float(np.median(translation_errors))
        rot_sigma = 1.4826 * float(np.median(np.abs(rotation_errors - rot_median)))
        trans_sigma = 1.4826 * float(np.median(np.abs(translation_errors - trans_median)))
        rot_limit = min(maximum_rotation_residual_deg, max(0.10, rot_median + 3.0 * rot_sigma))
        trans_limit = min(maximum_translation_residual_mm, max(0.50, trans_median + 3.0 * trans_sigma))
        kept = [
            record
            for record, rot_error, trans_error in zip(active, rotation_errors, translation_errors, strict=True)
            if rot_error <= rot_limit and trans_error <= trans_limit
        ]
        if len(kept) < minimum_pairs:
            # Do not silently over-prune a noisy dataset.  Keep the best
            # minimum_pairs for diagnosis; final quality gates will expose it.
            ranked = sorted(
                zip(active, rotation_errors, translation_errors, strict=True),
                key=lambda item: (
                    item[1] / maximum_rotation_residual_deg
                    + item[2] / maximum_translation_residual_mm
                ),
            )
            kept = [item[0] for item in ranked[:minimum_pairs]]
        if len(kept) == len(active):
            break
        active = kept
    rotation = mean_rotation([record.selected.rotation for record in active])
    translation = np.median(
        np.asarray([record.selected.translation.reshape(3) for record in active]), axis=0
    ).reshape(3, 1)
    active_ids = {id(record) for record in active}
    for record in records:
        if record.selected is None:
            continue
        record.rotation_residual_deg = rotation_distance_deg(record.selected.rotation, rotation)
        record.translation_residual_mm = float(
            np.linalg.norm(record.selected.translation - translation)
        )
        record.accepted = id(record) in active_ids
        record.reason = "accepted_for_stereo" if record.accepted else "relative_transform_outlier"
    return rotation, translation, active


def stereo_calibrate_fixed(
    object_points: np.ndarray,
    records: Sequence[StereoPairRecord],
    K0: np.ndarray,
    D0: np.ndarray,
    K1: np.ndarray,
    D1: np.ndarray,
    image_size: tuple[int, int],
    *,
    maximum_iterations: int = 200,
    epsilon: float = 1e-10,
) -> tuple[float, np.ndarray, np.ndarray]:
    objects = [object_points.reshape(-1, 1, 3).astype(np.float64) for _ in records]
    points0 = [record.selected.points0.reshape(-1, 1, 2).astype(np.float64) for record in records]
    points1 = [record.selected.points1.reshape(-1, 1, 2).astype(np.float64) for record in records]
    input_matrices = (K0.copy(), D0.copy(), K1.copy(), D1.copy())
    flags = cv2.fisheye.CALIB_FIX_INTRINSIC
    if hasattr(cv2.fisheye, "CALIB_CHECK_COND"):
        flags |= cv2.fisheye.CALIB_CHECK_COND
    result = cv2.fisheye.stereoCalibrate(
        objects,
        points0,
        points1,
        input_matrices[0],
        input_matrices[1],
        input_matrices[2],
        input_matrices[3],
        image_size,
        flags=flags,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, maximum_iterations, epsilon),
    )
    if len(result) not in {7, 9}:
        raise ValueError(f"unexpected fisheye.stereoCalibrate result length: {len(result)}")
    rms, solved_K0, solved_D0, solved_K1, solved_D1, rotation, translation = result[:7]
    frozen = (K0, D0, K1, D1)
    solved = (solved_K0, solved_D0, solved_K1, solved_D1)
    for name, before, after in zip(("K0", "D0", "K1", "D1"), frozen, solved, strict=True):
        if not np.allclose(before, after, rtol=0.0, atol=1e-12):
            raise ValueError(f"CALIB_FIX_INTRINSIC changed frozen {name}")
    rotation = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    translation = np.asarray(translation, dtype=np.float64).reshape(3, 1)
    if not np.all(np.isfinite(rotation)) or not np.all(np.isfinite(translation)):
        raise ValueError("stereoCalibrate returned a non-finite transform")
    return float(rms), rotation, translation


def _project_composed(
    object_points: np.ndarray,
    board_pose: PoseEstimate,
    inter_camera_R: np.ndarray,
    inter_camera_t: np.ndarray,
    K: np.ndarray,
    D: np.ndarray,
) -> np.ndarray:
    rotation = inter_camera_R @ board_pose.rotation
    translation = inter_camera_R @ board_pose.tvec + inter_camera_t
    rvec, _ = cv2.Rodrigues(rotation)
    return _project_points(object_points, rvec, translation, K, D, "opencv_fisheye")


def cross_reprojection_metrics(
    object_points: np.ndarray,
    points0: np.ndarray,
    points1: np.ndarray,
    K0: np.ndarray,
    D0: np.ndarray,
    K1: np.ndarray,
    D1: np.ndarray,
    rotation10: np.ndarray,
    translation10: np.ndarray,
) -> dict[str, float] | None:
    pose0 = solve_fixed_pose(object_points, points0, K0, D0)
    pose1 = solve_fixed_pose(object_points, points1, K1, D1)
    if pose0 is None or pose1 is None:
        return None
    predicted1 = _project_composed(
        object_points, pose0, rotation10, translation10, K1, D1
    )
    rotation01 = rotation10.T
    translation01 = -rotation01 @ translation10
    predicted0 = _project_composed(
        object_points, pose1, rotation01, translation01, K0, D0
    )
    errors01 = np.linalg.norm(predicted1 - points1.reshape(-1, 2), axis=1)
    errors10 = np.linalg.norm(predicted0 - points0.reshape(-1, 2), axis=1)
    combined = np.concatenate((errors01, errors10))
    return {
        "cam0_to_cam1_rmse_px": float(np.sqrt(np.mean(errors01 * errors01))),
        "cam1_to_cam0_rmse_px": float(np.sqrt(np.mean(errors10 * errors10))),
        "bidirectional_rmse_px": float(np.sqrt(np.mean(combined * combined))),
        "maximum_px": float(np.max(combined)),
    }


DEPTH_DRIFT_AXES = ("x", "y", "z")


def depth_drift_report(
    records: Sequence[StereoPairRecord],
    *,
    correlation_limit: float,
    slope_limit_mm_per_mm: float,
    rule: str = "and",
) -> dict[str, Any]:
    """Regress each per-pose relative-translation axis against board depth.

    Two rigidly mounted cameras have one relative transform.  It cannot depend
    on how far away the operator happens to hold the target, so the regression
    slope must be zero within noise.  A non-zero slope is proof of a model
    error -- biased intrinsics, an exhausted distortion domain, or a focal
    scale mismatch between the two cameras -- and it is invisible to
    per-camera PnP RMSE, which stays sub-pixel while the *pose* it produces
    drifts.  attempt17 shipped with tz/z0 correlation -0.868 and slope
    -0.0283 mm/mm and still reported 0.556px training cross-reprojection.

    ``rule='and'`` (the default) requires both a meaningful effect size and a
    correlation before failing.  Correlation alone is not a usable gate: under
    the null hypothesis its standard error is 1/sqrt(n-3), which is 0.21 at
    n=26, so a bare |r| > 0.3 threshold fires on roughly 14% of clean runs per
    axis -- 36% across three axes.  ``rule='or'`` restores the stricter
    literal form for operators who want it.
    """
    if rule not in ("and", "or"):
        raise ValueError(f"rule must be 'and' or 'or', got {rule!r}")

    usable = [
        record
        for record in records
        if record.selected is not None and record.accepted
    ]
    axes: dict[str, Any] = {}
    failures: list[str] = []
    if len(usable) < 3:
        return {
            "status": "insufficient_pairs",
            "evaluated_pairs": len(usable),
            "rule": rule,
            "thresholds": {
                "absolute_correlation": correlation_limit,
                "absolute_slope_mm_per_mm": slope_limit_mm_per_mm,
            },
            "axes": axes,
            "failures": failures,
        }

    depth = np.asarray(
        [float(record.selected.pose0.tvec.reshape(3)[2]) for record in usable],
        dtype=np.float64,
    )
    translations = np.asarray(
        [record.selected.translation.reshape(3) for record in usable],
        dtype=np.float64,
    )
    depth_span = float(np.ptp(depth))
    # A degenerate depth spread makes the slope meaningless rather than good.
    degenerate = depth_span < 1.0 or float(np.std(depth)) <= 0.0

    for index, axis in enumerate(DEPTH_DRIFT_AXES):
        values = translations[:, index]
        if degenerate or float(np.std(values)) <= 0.0:
            correlation = 0.0
            slope = 0.0
        else:
            correlation = float(np.corrcoef(depth, values)[0, 1])
            slope = float(np.polyfit(depth, values, 1)[0])
        over_correlation = abs(correlation) > correlation_limit
        over_slope = abs(slope) > slope_limit_mm_per_mm
        drifting = (
            (over_correlation or over_slope)
            if rule == "or"
            else (over_correlation and over_slope)
        )
        axes[axis] = {
            "correlation": correlation,
            "slope_mm_per_mm": slope,
            "predicted_drift_over_depth_span_mm": slope * depth_span,
            "exceeds_correlation_limit": over_correlation,
            "exceeds_slope_limit": over_slope,
            "drifting": drifting,
        }
        if drifting:
            failures.append(
                f"t{axis} drifts with board depth: correlation {correlation:+.3f} "
                f"(limit {correlation_limit:.3f}), slope {slope:+.4f} mm/mm "
                f"(limit {slope_limit_mm_per_mm:.4f}); the rig is rigid, so this "
                f"is an intrinsics/distortion model error, not pose noise"
            )

    if degenerate:
        status = "degenerate_depth_span"
    elif failures:
        status = "fail"
    else:
        status = "pass"
    return {
        "status": status,
        "evaluated_pairs": len(usable),
        "rule": rule,
        "board_depth_mm": {
            "minimum": float(np.min(depth)),
            "median": float(np.median(depth)),
            "maximum": float(np.max(depth)),
            "span": depth_span,
        },
        "thresholds": {
            "absolute_correlation": correlation_limit,
            "absolute_slope_mm_per_mm": slope_limit_mm_per_mm,
        },
        "axes": axes,
        "failures": failures,
    }


def _read_manifest(path: Path) -> list[StereoPairRecord]:
    return _records_from_manifest_rows(read_accepted_pair_manifest(path))


def _records_from_manifest_rows(
    rows: Sequence[dict[str, str]],
) -> list[StereoPairRecord]:
    return [
        StereoPairRecord(
            pose_id=row["pose_id"],
            cam0_path=Path(row["cam0_path"]),
            cam1_path=Path(row["cam1_path"]),
            manifest_row=dict(row),
        )
        for row in rows
    ]


def analyse_pairs(
    records: Sequence[StereoPairRecord],
    object_points: np.ndarray,
    point_indices: Sequence[int],
    doc0: dict[str, Any],
    K0: np.ndarray,
    D0: np.ndarray,
    doc1: dict[str, Any],
    K1: np.ndarray,
    D1: np.ndarray,
    *,
    maximum_pnp_rmse_px: float,
) -> None:
    settings = detector_settings(doc0)
    width = int(doc0["image_size"]["width"])
    height = int(doc0["image_size"]["height"])
    for index, record in enumerate(records, start=1):
        rejection0 = stereo_preflight_rejection(record.cam0_path)
        rejection1 = stereo_preflight_rejection(record.cam1_path)
        if rejection0 or rejection1:
            record.reason = f"preflight:cam0={rejection0};cam1={rejection1}"
            continue
        expected_hash0 = record.manifest_row.get("cam0_pixels_sha256")
        expected_hash1 = record.manifest_row.get("cam1_pixels_sha256")
        if expected_hash0 and sha256_file(record.cam0_path) != expected_hash0.upper():
            record.reason = "cam0_manifest_image_hash_mismatch"
            continue
        if expected_hash1 and sha256_file(record.cam1_path) != expected_hash1.upper():
            record.reason = "cam1_manifest_image_hash_mismatch"
            continue
        try:
            image0 = load_binary_image(record.cam0_path, width, height)
            image1 = load_binary_image(record.cam1_path, width, height)
            detection0 = detect_circle_grid(image0, settings)
            detection1 = detect_circle_grid(image1, settings)
        except (OSError, ValueError, cv2.error) as exc:
            record.reason = f"load_or_detection_error:{exc}"
            continue
        if not detection0.found or detection0.centers is None:
            record.reason = f"cam0_{detection0.reason}"
            continue
        if not detection1.found or detection1.centers is None:
            record.reason = f"cam1_{detection1.reason}"
            continue
        record.points0 = detection0.centers.reshape(-1, 2).astype(np.float64)
        record.points1 = detection1.centers.reshape(-1, 2).astype(np.float64)
        orientations = (
            (False, False, "normal-normal"),
            (False, True, "normal-reversed"),
            (True, False, "reversed-normal"),
            (True, True, "reversed-reversed"),
        )
        for reverse0, reverse1, name in orientations:
            ordered0 = record.points0[::-1] if reverse0 else record.points0
            ordered1 = record.points1[::-1] if reverse1 else record.points1
            points0 = ordered0[list(point_indices)].copy()
            points1 = ordered1[list(point_indices)].copy()
            pose0 = solve_fixed_pose(object_points, points0, K0, D0, doc0["model"])
            pose1 = solve_fixed_pose(object_points, points1, K1, D1, doc1["model"])
            if pose0 is None or pose1 is None:
                continue
            if max(pose0.rmse_px, pose1.rmse_px) > maximum_pnp_rmse_px:
                continue
            rotation, translation = relative_transform(pose0, pose1)
            record.candidates.append(
                TransformCandidate(points0, points1, pose0, pose1, rotation, translation, name)
            )
        record.reason = "pose_candidates_ready" if record.candidates else "no_acceptable_pose_candidate"
        if index % 25 == 0 or index == len(records):
            solved = sum(bool(item.candidates) for item in records[:index])
            print(f"  stereo pose scan {index}/{len(records)}, solvable={solved}", flush=True)


def _board_depth_mm(pose: PoseEstimate | None) -> float | None:
    if pose is None:
        return None
    return float(pose.tvec.reshape(3)[2])


def _record_row(record: StereoPairRecord) -> dict[str, Any]:
    selected = record.selected
    # depth_independence in the solve JSON only reports the three regression
    # fits; the per-pose scatter behind them never reaches disk.  Emitting the
    # depths and the relative-translation components here is what lets a failed
    # run be read as "only the far tail", "only near the corners", "continuous
    # linear drift" or "one dwell segment jumped".
    depth0 = _board_depth_mm(None if selected is None else selected.pose0)
    depth1 = _board_depth_mm(None if selected is None else selected.pose1)
    translation = (
        None if selected is None else selected.translation.reshape(3)
    )
    return {
        "pose_id": record.pose_id,
        "cam0_path": str(record.cam0_path),
        "cam1_path": str(record.cam1_path),
        "accepted": record.accepted,
        "reason": record.reason,
        "orientation": None if selected is None else selected.orientation,
        "cam0_pnp_rmse_px": None if selected is None else selected.pose0.rmse_px,
        "cam1_pnp_rmse_px": None if selected is None else selected.pose1.rmse_px,
        "cam0_pnp_max_px": None if selected is None else selected.pose0.maximum_px,
        "cam1_pnp_max_px": None if selected is None else selected.pose1.maximum_px,
        "board_depth_cam0_mm": depth0,
        "board_depth_cam1_mm": depth1,
        # Each camera measures the same board, so this ratio is an
        # extrinsic-free probe of whether the two focal lengths agree.  It
        # separates an intrinsic scale error from an extrinsic solve error.
        "depth_ratio_cam1_over_cam0": (
            None
            if depth0 is None or depth1 is None or depth0 == 0.0
            else depth1 / depth0
        ),
        "relative_tx_mm": None if translation is None else float(translation[0]),
        "relative_ty_mm": None if translation is None else float(translation[1]),
        "relative_tz_mm": None if translation is None else float(translation[2]),
        "rotation_residual_deg": record.rotation_residual_deg,
        "translation_residual_mm": record.translation_residual_mm,
        "cross_cam0_to_cam1_rmse_px": record.cross_cam0_to_cam1_rmse_px,
        "cross_cam1_to_cam0_rmse_px": record.cross_cam1_to_cam0_rmse_px,
        "cross_bidirectional_rmse_px": record.cross_bidirectional_rmse_px,
        "cross_maximum_px": record.cross_maximum_px,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calibrate cam0->cam1 fisheye extrinsics with frozen intrinsics."
    )
    parser.add_argument("pairs", type=Path, help="accepted stationary pair manifest CSV")
    parser.add_argument("--cam0-intrinsics", type=Path, required=True)
    parser.add_argument("--cam1-intrinsics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--pairing-summary",
        type=Path,
        help="pairing_summary.json (defaults to the manifest sibling)",
    )
    parser.add_argument("--stillness-config", type=Path)
    parser.add_argument("--min-pairs", type=int, default=15)
    parser.add_argument("--max-pnp-rmse-px", type=float, default=1.5)
    parser.add_argument("--seed-rotation-limit-deg", type=float, default=5.0)
    parser.add_argument("--seed-translation-limit-mm", type=float, default=50.0)
    parser.add_argument("--max-rotation-residual-deg", type=float, default=2.0)
    parser.add_argument("--max-translation-residual-mm", type=float, default=10.0)
    parser.add_argument("--stereo-rms-target-px", type=float, default=1.0)
    parser.add_argument("--rotation-dispersion-target-deg", type=float, default=0.5)
    parser.add_argument("--translation-dispersion-fraction", type=float, default=0.01)
    parser.add_argument("--depth-drift-correlation", type=float, default=0.3)
    parser.add_argument("--depth-drift-slope", type=float, default=0.005)
    parser.add_argument(
        "--depth-drift-rule",
        choices=("and", "or"),
        default="and",
        help=(
            "'and' fails only when both the correlation and the slope limits "
            "are exceeded; 'or' fails on either.  Correlation alone has a "
            "1/sqrt(n-3) null standard error, so 'or' misfires on clean data."
        ),
    )
    parser.add_argument(
        "--allow-limited",
        action="store_true",
        help=(
            "write the extrinsics file even when quality.status is 'limited'.  "
            "Off by default: attempt17 promoted a solve whose translation "
            "dispersion was 10x its target because dispersion was only a "
            "warning.  The exit code stays non-zero when this is used."
        ),
    )
    return parser


def _validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.min_pairs < 3:
        parser.error("--min-pairs must be at least 3")
    numeric = (
        args.max_pnp_rmse_px,
        args.seed_rotation_limit_deg,
        args.seed_translation_limit_mm,
        args.max_rotation_residual_deg,
        args.max_translation_residual_mm,
        args.stereo_rms_target_px,
        args.rotation_dispersion_target_deg,
        args.translation_dispersion_fraction,
        args.depth_drift_correlation,
        args.depth_drift_slope,
    )
    if min(numeric) <= 0.0:
        parser.error("all calibration thresholds must be positive")
    if args.depth_drift_correlation >= 1.0:
        parser.error("--depth-drift-correlation must be below 1.0")


def run(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any]]:
    doc0, K0, D0, doc1, K1, D1 = validate_intrinsic_pair(
        args.cam0_intrinsics, args.cam1_intrinsics
    )
    references = {
        "cam0": intrinsic_reference(args.cam0_intrinsics, doc0),
        "cam1": intrinsic_reference(args.cam1_intrinsics, doc1),
    }
    manifest_rows = read_accepted_pair_manifest(args.pairs)
    pairing_summary_path = getattr(args, "pairing_summary", None) or args.pairs.with_name(
        "pairing_summary.json"
    )
    pairing_summary = load_pairing_provenance(
        pairing_summary_path, args.pairs, manifest_rows, references
    )
    verify_pair_manifest_images(manifest_rows)
    records = _records_from_manifest_rows(manifest_rows)
    if len(records) < args.min_pairs:
        raise ValueError(f"pair manifest has only {len(records)} accepted pairs; need {args.min_pairs}")

    stillness_path, _stillness_document = verify_frozen_stillness_provenance(
        pairing_summary,
        references,
        getattr(args, "stillness_config", None),
    )
    settings = detector_settings(doc0)
    point_indices = tuple(sorted(intrinsic_point_set(doc0)))
    all_object_points = make_object_points(
        settings, float(doc0["pattern"]["base_spacing_mm"])
    )
    object_points = all_object_points[list(point_indices)]
    analyse_pairs(
        records,
        object_points,
        point_indices,
        doc0,
        K0,
        D0,
        doc1,
        K1,
        D1,
        maximum_pnp_rmse_px=args.max_pnp_rmse_px,
    )
    solvable = [record for record in records if record.candidates]
    choose_orientation_consensus(
        solvable,
        seed_rotation_limit_deg=args.seed_rotation_limit_deg,
        seed_translation_limit_mm=args.seed_translation_limit_mm,
    )
    _initial_R, _initial_t, active = robust_transform_inliers(
        solvable,
        maximum_rotation_residual_deg=args.max_rotation_residual_deg,
        maximum_translation_residual_mm=args.max_translation_residual_mm,
        minimum_pairs=args.min_pairs,
    )
    image_size = (int(doc0["image_size"]["width"]), int(doc0["image_size"]["height"]))
    stereo_rms, rotation, translation = stereo_calibrate_fixed(
        object_points, active, K0, D0, K1, D1, image_size
    )
    for record in active:
        metrics = cross_reprojection_metrics(
            object_points,
            record.selected.points0,
            record.selected.points1,
            K0,
            D0,
            K1,
            D1,
            rotation,
            translation,
        )
        if metrics is None:
            record.accepted = False
            record.reason = "final_cross_reprojection_pose_failed"
            continue
        record.cross_cam0_to_cam1_rmse_px = metrics["cam0_to_cam1_rmse_px"]
        record.cross_cam1_to_cam0_rmse_px = metrics["cam1_to_cam0_rmse_px"]
        record.cross_bidirectional_rmse_px = metrics["bidirectional_rmse_px"]
        record.cross_maximum_px = metrics["maximum_px"]

    accepted = [record for record in active if record.accepted]
    rotation_residuals = np.asarray([record.rotation_residual_deg for record in accepted], dtype=np.float64)
    translation_residuals = np.asarray([record.translation_residual_mm for record in accepted], dtype=np.float64)
    cross_errors = np.asarray([record.cross_bidirectional_rmse_px for record in accepted], dtype=np.float64)
    baseline = float(np.linalg.norm(translation))
    rotation_median = float(np.median(rotation_residuals))
    translation_median = float(np.median(translation_residuals))
    translation_target = max(0.5, args.translation_dispersion_fraction * baseline)
    warnings: list[str] = []
    failures: list[str] = []
    if len(accepted) < args.min_pairs:
        failures.append(f"only {len(accepted)} final pairs; need {args.min_pairs}")
    if stereo_rms > 1.5 * args.stereo_rms_target_px:
        failures.append(f"stereo RMS {stereo_rms:.4f}px is grossly above target")
    elif stereo_rms > args.stereo_rms_target_px:
        warnings.append(f"stereo RMS {stereo_rms:.4f}px exceeds target {args.stereo_rms_target_px:.4f}px")
    if rotation_median > args.rotation_dispersion_target_deg:
        warnings.append(
            f"rotation dispersion {rotation_median:.4f}deg exceeds provisional target "
            f"{args.rotation_dispersion_target_deg:.4f}deg"
        )
    if translation_median > translation_target:
        warnings.append(
            f"translation dispersion {translation_median:.4f}mm exceeds provisional target "
            f"{translation_target:.4f}mm"
        )
    depth_drift = depth_drift_report(
        accepted,
        correlation_limit=args.depth_drift_correlation,
        slope_limit_mm_per_mm=args.depth_drift_slope,
        rule=args.depth_drift_rule,
    )
    # A depth-dependent extrinsic is a model error, never a tolerance issue:
    # it goes straight to failures rather than warnings.
    failures.extend(depth_drift["failures"])
    if depth_drift["status"] == "degenerate_depth_span":
        warnings.append(
            "board depth span is too small to test depth independence; vary "
            "the target distance across poses"
        )
    domain = {
        "cam0": fisheye_domain_report(doc0, K0, D0),
        "cam1": fisheye_domain_report(doc1, K1, D1),
    }
    if domain["cam0"]["status"] == "warning":
        warnings.extend(f"cam0: {item}" for item in domain["cam0"]["warnings"])
    if domain["cam1"]["status"] == "warning":
        warnings.extend(f"cam1: {item}" for item in domain["cam1"]["warnings"])
    status = "unacceptable" if failures else "limited" if warnings else "acceptable"
    pairing: dict[str, Any] = {
        "manifest_path": str(args.pairs.resolve()),
        "manifest_sha256": sha256_file(args.pairs),
        "pairing_summary_path": str(pairing_summary_path.resolve()),
        "pairing_summary_sha256": sha256_file(pairing_summary_path),
        "dataset_root": str(pairing_summary["dataset_root"]),
        "stillness_config_path": str(stillness_path.resolve()),
        "stillness_config_sha256": sha256_file(stillness_path),
        "training_pose_ids": [record.pose_id for record in accepted],
        "training_images_sha256": sorted(
            {
                sha256_file(record.cam0_path)
                for record in accepted
            }
            | {
                sha256_file(record.cam1_path)
                for record in accepted
            }
        ),
    }
    document = {
        "schema": STEREO_EXTRINSICS_SCHEMA,
        "created_utc": _utc_now(),
        "reference_camera_id": 0,
        "target_camera_id": 1,
        "transform_convention": TRANSFORM_CONVENTION,
        "translation_unit": "mm",
        "R_cam1_from_cam0": rotation.tolist(),
        "t_cam1_from_cam0_mm": translation.reshape(3).tolist(),
        "intrinsics": references,
        "pattern": {
            "type": doc0["pattern"]["type"],
            "columns": int(doc0["pattern"]["columns"]),
            "rows": int(doc0["pattern"]["rows"]),
            "base_spacing_mm": float(doc0["pattern"]["base_spacing_mm"]),
            "used_point_indices": list(point_indices),
        },
        "pairing": pairing,
        "solver": {
            "backend": "cv2.fisheye.stereoCalibrate",
            "opencv_version": cv2.__version__,
            "flags": ["CALIB_FIX_INTRINSIC", "CALIB_CHECK_COND"],
            "maximum_iterations": 200,
            "epsilon": 1e-10,
            "intrinsics_unchanged": True,
        },
        "intrinsic_domain": domain,
        "quality": {
            "status": status,
            "input_pairs": len(records),
            "solvable_pairs": len(solvable),
            "accepted_pairs": len(accepted),
            "rejected_pairs": len(records) - len(accepted),
            "stereo_rms_px": stereo_rms,
            "baseline_mm": baseline,
            "rotation_dispersion_median_deg": rotation_median,
            "rotation_dispersion_p95_deg": float(np.percentile(rotation_residuals, 95)),
            "translation_dispersion_median_mm": translation_median,
            "translation_dispersion_p95_mm": float(np.percentile(translation_residuals, 95)),
            "training_cross_reprojection_median_px": float(np.median(cross_errors)),
            "training_cross_reprojection_p95_px": float(np.percentile(cross_errors, 95)),
            "training_cross_reprojection_maximum_px": float(np.max(cross_errors)),
            "provisional_thresholds": {
                "minimum_pairs": args.min_pairs,
                "stereo_rms_target_px": args.stereo_rms_target_px,
                "rotation_dispersion_median_deg": args.rotation_dispersion_target_deg,
                "translation_dispersion_fraction_of_baseline": args.translation_dispersion_fraction,
                "translation_dispersion_effective_mm": translation_target,
                "depth_drift_correlation": args.depth_drift_correlation,
                "depth_drift_slope_mm_per_mm": args.depth_drift_slope,
                "depth_drift_rule": args.depth_drift_rule,
            },
            "depth_independence": depth_drift,
            "gate": {
                "publishable": status == "acceptable",
                "override_used": bool(args.allow_limited) and status == "limited",
                "policy": (
                    "extrinsics are written only when quality.status is "
                    "'acceptable'; --allow-limited overrides 'limited' but "
                    "never 'unacceptable'"
                ),
            },
            "warnings": warnings,
            "failures": failures,
        },
    }
    validate_stereo_extrinsics(document)
    report = args.report or args.output.with_suffix(".pairs.csv")
    write_csv_rows(report, [_record_row(record) for record in records])
    # The pair report is always written -- it is the diagnostic evidence.  The
    # extrinsics file is the artefact every downstream stage consumes, so a
    # solve that did not clear the gate must not exist on disk at all.
    if status == "acceptable" or (status == "limited" and args.allow_limited):
        _write_json_atomic(args.output, document)
    else:
        # Suppressing the extrinsics file also suppressed every other field it
        # carried: quality.depth_independence (including the board depth span),
        # the intrinsics paths and hashes, and the gate reason.  attempt18 and
        # attempt19 both left nothing but a .pairs.csv behind, so which
        # intrinsics were used had to be recovered by hash-matching the pair
        # manifest.  Keep the same document under a name nothing downstream
        # consumes.
        _write_json_atomic(args.output.with_suffix(".rejected.json"), document)
    return args.output, report, document


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_arguments(parser, args)
    try:
        output, report, document = run(args)
    except (OSError, ValueError, cv2.error) as exc:
        print(f"stereo calibration failed: {exc}", file=sys.stderr)
        return 2
    quality = document["quality"]
    gate = quality["gate"]
    # attempt19 silently ran on the stale full44/mask26 pair because a fresh
    # shell restored the runbook defaults.  The 2.333deg cam0 margin was the
    # only tell, and it was buried in the pairing summary.  Lead with it.
    domain = document["intrinsic_domain"]
    for camera in ("cam0", "cam1"):
        print(
            f"{camera} intrinsics: "
            f"{Path(document['intrinsics'][camera]['path']).name}  "
            f"monotonic margin {domain[camera]['monotonic_margin_deg']:.3f}deg "
            f"({domain[camera]['status']})"
        )
    print(
        f"stereo calibration: {quality['status']}  pairs={quality['accepted_pairs']} "
        f"RMS={quality['stereo_rms_px']:.4f}px baseline={quality['baseline_mm']:.3f}mm"
    )
    drift = quality["depth_independence"]
    print(f"depth independence: {drift['status']} (rule={drift['rule']})")
    depth = drift.get("board_depth_mm")
    if depth is not None:
        print(
            f"  board depth: {depth['minimum']:.0f}..{depth['maximum']:.0f} mm "
            f"(span {depth['span']:.0f}, ratio {depth['maximum'] / max(depth['minimum'], 1e-9):.2f}x)"
        )
    for axis, values in drift["axes"].items():
        print(
            f"  t{axis}: correlation {values['correlation']:+.3f} "
            f"slope {values['slope_mm_per_mm']:+.4f} mm/mm "
            f"drift-over-span {values['predicted_drift_over_depth_span_mm']:+.3f} mm"
        )
    print(f"pair report: {report}")
    for warning in quality["warnings"]:
        print(f"warning: {warning}")
    for failure in quality["failures"]:
        print(f"failure: {failure}")
    if gate["publishable"]:
        print(f"extrinsics: {output}")
        return 0
    if gate["override_used"]:
        print(f"extrinsics: {output}  (WRITTEN UNDER --allow-limited)")
        print(
            "warning: this solve did not clear the quality gate; do not "
            "promote it to calibration_configs",
            file=sys.stderr,
        )
        return 4
    print(f"diagnostics: {output.with_suffix('.rejected.json')}")
    print(
        f"refusing to write extrinsics ({quality['status']}): {output}",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
