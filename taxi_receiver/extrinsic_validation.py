"""Independent holdout validation for frozen cam0->cam1 extrinsics."""
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
from .extrinsic_config import (
    ExtrinsicValidationError,
    intrinsic_point_set,
    load_stereo_extrinsics,
    sha256_file,
    verify_intrinsic_reference,
)
from .stereo_calibration import (
    PoseEstimate,
    cross_reprojection_metrics,
    relative_transform,
    rotation_distance_deg,
    solve_fixed_pose,
)
from .stereo_pairs import detector_settings, stereo_preflight_rejection, write_csv_rows
from .stereo_pairs import (
    load_pairing_provenance,
    read_accepted_pair_manifest,
    verify_frozen_stillness_provenance,
    verify_pair_manifest_images,
)


EXTRINSIC_VALIDATION_SCHEMA = "taxi_receiver.stereo_extrinsics_validation/1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class HoldoutRecord:
    pose_id: str
    cam0_path: Path
    cam1_path: Path
    manifest_row: dict[str, str] = field(default_factory=dict)
    reason: str = "not_processed"
    accepted: bool = False
    training_overlap: bool = False
    orientation: str | None = None
    cam0_pnp_rmse_px: float | None = None
    cam1_pnp_rmse_px: float | None = None
    bidirectional_rmse_px: float | None = None
    cross_maximum_px: float | None = None
    rectified_vertical_rmse_px: float | None = None
    rectified_vertical_p95_px: float | None = None
    rectified_vertical_maximum_px: float | None = None
    rotation_residual_deg: float | None = None
    translation_residual_mm: float | None = None
    points0: np.ndarray | None = None
    points1: np.ndarray | None = None


def _read_manifest(path: Path) -> list[HoldoutRecord]:
    return _records_from_manifest_rows(read_accepted_pair_manifest(path))


def _records_from_manifest_rows(
    rows: Sequence[dict[str, str]],
) -> list[HoldoutRecord]:
    return [
        HoldoutRecord(
            row["pose_id"],
            Path(row["cam0_path"]),
            Path(row["cam1_path"]),
            manifest_row=dict(row),
        )
        for row in rows
    ]


def select_point_order_by_pnp(
    object_points: np.ndarray,
    raw_points: np.ndarray,
    K: np.ndarray,
    D: np.ndarray,
    model: str,
    *,
    maximum_rmse_px: float,
    point_indices: Sequence[int] | None = None,
) -> tuple[str, np.ndarray, PoseEstimate] | None:
    """Resolve detector order using only this camera's fixed-intrinsic PnP fit.

    The extrinsic under test is intentionally absent from this API, preventing
    a holdout label/order choice from being optimized against that transform.
    """

    candidates: list[tuple[float, str, np.ndarray, PoseEstimate]] = []
    indices = (
        list(range(len(raw_points)))
        if point_indices is None
        else list(point_indices)
    )
    for name, ordered_points in (
        ("normal", raw_points),
        ("reversed", raw_points[::-1]),
    ):
        points = ordered_points[indices].copy()
        pose = solve_fixed_pose(object_points, points, K, D, model)
        if pose is None or not math.isfinite(pose.rmse_px):
            continue
        if pose.rmse_px <= maximum_rmse_px:
            candidates.append((pose.rmse_px, name, points, pose))
    if not candidates:
        return None
    best_score = min(item[0] for item in candidates)
    # A 180-degree reversal of a planar asymmetric grid can be PnP-equivalent.
    # Preserve the detector's frozen order when fits are numerically tied.
    eligible = [item for item in candidates if item[0] <= best_score + 1e-6]
    _score, name, points, pose = min(
        eligible, key=lambda item: (item[1] != "normal", item[0])
    )
    return name, points, pose


def validate_holdout_isolation(
    extrinsic_document: dict[str, Any],
    pairing_summary: dict[str, Any],
    holdout_manifest_hash: str,
) -> tuple[str, str]:
    """Reject reuse of either the training manifest or its capture session."""

    training_pairing = extrinsic_document.get("pairing")
    if not isinstance(training_pairing, dict):
        raise ValueError("extrinsic training pairing provenance is missing")
    training_manifest_hash = str(training_pairing.get("manifest_sha256", "")).upper()
    if holdout_manifest_hash.upper() == training_manifest_hash:
        raise ValueError("holdout pair manifest is identical to the training manifest")
    training_dataset = training_pairing.get("dataset_root")
    holdout_dataset = pairing_summary.get("dataset_root")
    if not isinstance(training_dataset, str) or not isinstance(holdout_dataset, str):
        raise ValueError("training/holdout dataset provenance is missing")
    training_dataset_root = str(Path(training_dataset).resolve()).casefold()
    holdout_dataset_root = str(Path(holdout_dataset).resolve()).casefold()
    if holdout_dataset_root == training_dataset_root:
        raise ValueError("holdout dataset_root is the same capture session as training")
    return training_manifest_hash, holdout_dataset_root


def stereo_rectification(
    K0: np.ndarray,
    D0: np.ndarray,
    K1: np.ndarray,
    D1: np.ndarray,
    image_size: tuple[int, int],
    rotation: np.ndarray,
    translation: np.ndarray,
    balance: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    return cv2.fisheye.stereoRectify(
        K0,
        D0,
        K1,
        D1,
        image_size,
        rotation,
        translation,
        flags=cv2.CALIB_ZERO_DISPARITY,
        balance=balance,
        fov_scale=1.0,
    )


def rectified_vertical_metrics(
    points0: np.ndarray,
    points1: np.ndarray,
    K0: np.ndarray,
    D0: np.ndarray,
    K1: np.ndarray,
    D1: np.ndarray,
    rectification: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
) -> dict[str, float]:
    R0, R1, P0, P1, _Q = rectification
    corrected0 = cv2.fisheye.undistortPoints(
        points0.reshape(-1, 1, 2), K0, D0, R=R0, P=P0
    ).reshape(-1, 2)
    corrected1 = cv2.fisheye.undistortPoints(
        points1.reshape(-1, 1, 2), K1, D1, R=R1, P=P1
    ).reshape(-1, 2)
    errors = np.abs(corrected0[:, 1] - corrected1[:, 1])
    return {
        "rmse_px": float(np.sqrt(np.mean(errors * errors))),
        "p95_px": float(np.percentile(errors, 95)),
        "maximum_px": float(np.max(errors)),
    }


def analyse_holdout(
    records: Sequence[HoldoutRecord],
    object_points: np.ndarray,
    point_indices: Sequence[int],
    doc0: dict[str, Any],
    K0: np.ndarray,
    D0: np.ndarray,
    doc1: dict[str, Any],
    K1: np.ndarray,
    D1: np.ndarray,
    rotation: np.ndarray,
    translation: np.ndarray,
    rectification: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    training_hashes: set[str],
    *,
    maximum_pnp_rmse_px: float,
) -> None:
    settings = detector_settings(doc0)
    width = int(doc0["image_size"]["width"])
    height = int(doc0["image_size"]["height"])
    for index, record in enumerate(records, start=1):
        if not record.cam0_path.is_file() or not record.cam1_path.is_file():
            record.reason = "missing_holdout_image"
            continue
        hash0 = sha256_file(record.cam0_path)
        hash1 = sha256_file(record.cam1_path)
        if hash0 != record.manifest_row["cam0_pixels_sha256"]:
            record.reason = "cam0_manifest_image_hash_mismatch"
            continue
        if hash1 != record.manifest_row["cam1_pixels_sha256"]:
            record.reason = "cam1_manifest_image_hash_mismatch"
            continue
        if hash0 in training_hashes or hash1 in training_hashes:
            record.training_overlap = True
            record.reason = "training_image_reused_in_holdout"
            continue
        rejection0 = stereo_preflight_rejection(record.cam0_path)
        rejection1 = stereo_preflight_rejection(record.cam1_path)
        if rejection0 or rejection1:
            record.reason = f"preflight:cam0={rejection0};cam1={rejection1}"
            continue
        try:
            detection0 = detect_circle_grid(
                load_binary_image(record.cam0_path, width, height), settings
            )
            detection1 = detect_circle_grid(
                load_binary_image(record.cam1_path, width, height), settings
            )
        except (OSError, ValueError, cv2.error) as exc:
            record.reason = f"load_or_detection_error:{exc}"
            continue
        if not detection0.found or detection0.centers is None:
            record.reason = f"cam0_{detection0.reason}"
            continue
        if not detection1.found or detection1.centers is None:
            record.reason = f"cam1_{detection1.reason}"
            continue
        raw0 = detection0.centers.reshape(-1, 2).astype(np.float64)
        raw1 = detection1.centers.reshape(-1, 2).astype(np.float64)
        selected0 = select_point_order_by_pnp(
            object_points,
            raw0,
            K0,
            D0,
            doc0["model"],
            maximum_rmse_px=maximum_pnp_rmse_px,
            point_indices=point_indices,
        )
        selected1 = select_point_order_by_pnp(
            object_points,
            raw1,
            K1,
            D1,
            doc1["model"],
            maximum_rmse_px=maximum_pnp_rmse_px,
            point_indices=point_indices,
        )
        if selected0 is None or selected1 is None:
            record.reason = "no_valid_orientation_or_pose"
            continue
        name0, points0, pose0 = selected0
        name1, points1, pose1 = selected1
        metrics = cross_reprojection_metrics(
            object_points, points0, points1, K0, D0, K1, D1, rotation, translation
        )
        if metrics is None:
            record.reason = "cross_reprojection_pose_failed"
            continue
        observed_rotation, observed_translation = relative_transform(pose0, pose1)
        vertical = rectified_vertical_metrics(
            points0, points1, K0, D0, K1, D1, rectification
        )
        record.orientation = f"{name0}-{name1}"
        record.points0 = points0
        record.points1 = points1
        record.cam0_pnp_rmse_px = pose0.rmse_px
        record.cam1_pnp_rmse_px = pose1.rmse_px
        record.bidirectional_rmse_px = metrics["bidirectional_rmse_px"]
        record.cross_maximum_px = metrics["maximum_px"]
        record.rectified_vertical_rmse_px = vertical["rmse_px"]
        record.rectified_vertical_p95_px = vertical["p95_px"]
        record.rectified_vertical_maximum_px = vertical["maximum_px"]
        record.rotation_residual_deg = rotation_distance_deg(observed_rotation, rotation)
        record.translation_residual_mm = float(
            np.linalg.norm(observed_translation - translation)
        )
        record.accepted = True
        record.reason = "evaluated"
        if index % 25 == 0 or index == len(records):
            evaluated = sum(item.accepted for item in records[:index])
            print(f"  extrinsic holdout {index}/{len(records)}, evaluated={evaluated}", flush=True)


def _record_row(record: HoldoutRecord) -> dict[str, Any]:
    return {
        "pose_id": record.pose_id,
        "cam0_path": str(record.cam0_path),
        "cam1_path": str(record.cam1_path),
        "accepted": record.accepted,
        "reason": record.reason,
        "training_overlap": record.training_overlap,
        "orientation": record.orientation,
        "cam0_pnp_rmse_px": record.cam0_pnp_rmse_px,
        "cam1_pnp_rmse_px": record.cam1_pnp_rmse_px,
        "bidirectional_rmse_px": record.bidirectional_rmse_px,
        "cross_maximum_px": record.cross_maximum_px,
        "rectified_vertical_rmse_px": record.rectified_vertical_rmse_px,
        "rectified_vertical_p95_px": record.rectified_vertical_p95_px,
        "rectified_vertical_maximum_px": record.rectified_vertical_maximum_px,
        "rotation_residual_deg": record.rotation_residual_deg,
        "translation_residual_mm": record.translation_residual_mm,
    }


def write_rectified_diagnostics(
    output_root: Path,
    records: Sequence[HoldoutRecord],
    doc0: dict[str, Any],
    K0: np.ndarray,
    D0: np.ndarray,
    K1: np.ndarray,
    D1: np.ndarray,
    rectification: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    maximum_pairs: int = 12,
) -> None:
    width = int(doc0["image_size"]["width"])
    height = int(doc0["image_size"]["height"])
    size = (width, height)
    R0, R1, P0, P1, _Q = rectification
    map00, map01 = cv2.fisheye.initUndistortRectifyMap(K0, D0, R0, P0, size, cv2.CV_32FC1)
    map10, map11 = cv2.fisheye.initUndistortRectifyMap(K1, D1, R1, P1, size, cv2.CV_32FC1)
    root = output_root / "rectified"
    root.mkdir(parents=True, exist_ok=True)
    tiles: list[np.ndarray] = []
    for record in [item for item in records if item.accepted][:maximum_pairs]:
        image0 = load_binary_image(record.cam0_path, width, height)
        image1 = load_binary_image(record.cam1_path, width, height)
        corrected0 = cv2.remap(image0, map00, map01, cv2.INTER_LINEAR)
        corrected1 = cv2.remap(image1, map10, map11, cv2.INTER_LINEAR)
        combined = cv2.cvtColor(np.hstack((corrected0, corrected1)), cv2.COLOR_GRAY2BGR)
        for y in range(20, height, 40):
            cv2.line(combined, (0, y), (2 * width - 1, y), (0, 180, 0), 1)
        cv2.putText(
            combined,
            f"{record.pose_id} cross={record.bidirectional_rmse_px:.3f}px yP95={record.rectified_vertical_p95_px:.3f}px",
            (8, 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (0, 180, 255),
            1,
            cv2.LINE_AA,
        )
        cv2.imwrite(str(root / f"{record.pose_id}.png"), combined)
        tiles.append(cv2.resize(combined, (640, 240), interpolation=cv2.INTER_AREA))
    if tiles:
        while len(tiles) % 2:
            tiles.append(np.zeros_like(tiles[0]))
        montage = np.vstack([np.hstack(tiles[index:index + 2]) for index in range(0, len(tiles), 2)])
        cv2.imwrite(str(output_root / "rectified_montage.png"), montage)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate frozen cam0->cam1 extrinsics on independent stationary pairs."
    )
    parser.add_argument("extrinsics", type=Path)
    parser.add_argument("pairs", type=Path)
    parser.add_argument("--cam0-intrinsics", type=Path, required=True)
    parser.add_argument("--cam1-intrinsics", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--pairing-summary",
        type=Path,
        help="holdout pairing_summary.json (defaults to the manifest sibling)",
    )
    parser.add_argument(
        "--stillness-config",
        type=Path,
        help="frozen holdout stillness config (defaults to pairing summary source)",
    )
    parser.add_argument("--min-holdout-pairs", type=int, default=15)
    parser.add_argument("--max-pnp-rmse-px", type=float, default=1.5)
    parser.add_argument("--median-rmse-px", type=float, default=0.8)
    parser.add_argument("--p95-rmse-px", type=float, default=1.2)
    parser.add_argument("--maximum-rmse-px", type=float, default=1.5)
    parser.add_argument("--required-pass-fraction", type=float, default=0.90)
    parser.add_argument("--rectified-p95-px", type=float, default=1.2)
    parser.add_argument("--rotation-dispersion-deg", type=float, default=0.5)
    parser.add_argument("--translation-dispersion-fraction", type=float, default=0.01)
    parser.add_argument("--balance", type=float, default=0.0)
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument(
        "--allow-limited-extrinsics",
        action="store_true",
        help=(
            "validate extrinsics whose own quality.status is not 'acceptable'.  "
            "Off by default so a solve that failed its training gate cannot "
            "silently consume a holdout set."
        ),
    )
    return parser


def _validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.min_holdout_pairs < 1:
        parser.error("--min-holdout-pairs must be positive")
    if not 0.0 <= args.required_pass_fraction <= 1.0:
        parser.error("--required-pass-fraction must be in [0,1]")
    if not 0.0 <= args.balance <= 1.0:
        parser.error("--balance must be in [0,1]")
    if min(
        args.median_rmse_px,
        args.p95_rmse_px,
        args.maximum_rmse_px,
        args.max_pnp_rmse_px,
        args.rectified_p95_px,
        args.rotation_dispersion_deg,
        args.translation_dispersion_fraction,
    ) <= 0.0:
        parser.error("validation thresholds must be positive")


def _require_publishable_extrinsics(
    extrinsic_doc: dict[str, Any],
    path: Path,
    *,
    allow_limited: bool,
) -> None:
    """Refuse to spend a holdout set on a solve that failed its own gate.

    Holdout sets are single-use: once validated against, the poses are burnt
    and a fresh capture is required.  attempt17 spent one on a solve whose
    training translation dispersion was already 10x its target, so the 17.4px
    holdout result measured a known-bad transform rather than the rig.
    """
    quality = extrinsic_doc.get("quality")
    if not isinstance(quality, dict):
        return
    status = quality.get("status")
    if status is None or status == "acceptable":
        return
    detail = "; ".join(str(item) for item in quality.get("failures", ())) or "none"
    message = (
        f"extrinsics quality.status is {status!r}, not 'acceptable': {path}. "
        f"Recorded failures: {detail}. Re-solve before spending a holdout set, "
        f"or pass --allow-limited-extrinsics to override."
    )
    if not allow_limited:
        raise ValueError(message)
    print(f"warning: {message}", file=sys.stderr)


def _validated_point_indices(
    extrinsic_doc: dict[str, Any],
    doc0: dict[str, Any],
    doc1: dict[str, Any],
) -> tuple[int, ...]:
    points0 = intrinsic_point_set(doc0)
    points1 = intrinsic_point_set(doc1)
    if points0 != points1:
        raise ExtrinsicValidationError(
            "intrinsic point sets differ between cameras: "
            f"cam0-only indices={sorted(points0 - points1)}, "
            f"cam1-only indices={sorted(points1 - points0)}"
        )
    recorded_indices = tuple(extrinsic_doc["pattern"]["used_point_indices"])
    expected_indices = tuple(sorted(points0))
    if recorded_indices != expected_indices:
        recorded_points = frozenset(recorded_indices)
        raise ExtrinsicValidationError(
            "extrinsic pattern.used_point_indices does not match the intrinsic point set: "
            f"extrinsics-only indices={sorted(recorded_points - points0)}, "
            f"intrinsics-only indices={sorted(points0 - recorded_points)}, "
            f"recorded order={list(recorded_indices)}, "
            f"expected order={list(expected_indices)}"
        )
    return expected_indices


def run(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any]]:
    if args.output_root.exists() and any(args.output_root.iterdir()):
        raise ValueError(f"output root is not empty: {args.output_root}")
    args.output_root.mkdir(parents=True, exist_ok=True)
    extrinsic_doc, rotation, translation = load_stereo_extrinsics(args.extrinsics)
    _require_publishable_extrinsics(
        extrinsic_doc,
        args.extrinsics,
        allow_limited=bool(getattr(args, "allow_limited_extrinsics", False)),
    )
    doc0, K0, D0 = verify_intrinsic_reference(
        args.cam0_intrinsics, extrinsic_doc["intrinsics"]["cam0"], 0
    )
    doc1, K1, D1 = verify_intrinsic_reference(
        args.cam1_intrinsics, extrinsic_doc["intrinsics"]["cam1"], 1
    )
    if doc0["image_size"] != doc1["image_size"]:
        raise ValueError("intrinsic image sizes differ")
    point_indices = _validated_point_indices(extrinsic_doc, doc0, doc1)
    settings = detector_settings(doc0)
    all_object_points = make_object_points(
        settings, float(doc0["pattern"]["base_spacing_mm"])
    )
    object_points = all_object_points[list(point_indices)]
    image_size = (int(doc0["image_size"]["width"]), int(doc0["image_size"]["height"]))
    rectification = stereo_rectification(
        K0, D0, K1, D1, image_size, rotation, translation, args.balance
    )
    manifest_rows = read_accepted_pair_manifest(args.pairs)
    pairing_summary_path = getattr(args, "pairing_summary", None) or args.pairs.with_name(
        "pairing_summary.json"
    )
    pairing_summary = load_pairing_provenance(
        pairing_summary_path,
        args.pairs,
        manifest_rows,
        extrinsic_doc["intrinsics"],
    )
    holdout_stillness_path, _holdout_stillness = verify_frozen_stillness_provenance(
        pairing_summary,
        extrinsic_doc["intrinsics"],
        getattr(args, "stillness_config", None),
    )
    verify_pair_manifest_images(manifest_rows)
    holdout_manifest_hash = sha256_file(args.pairs)
    training_manifest_hash, _holdout_dataset_root = validate_holdout_isolation(
        extrinsic_doc, pairing_summary, holdout_manifest_hash
    )
    records = _records_from_manifest_rows(manifest_rows)
    training_hashes = {
        str(value).upper()
        for value in extrinsic_doc.get("pairing", {}).get("training_images_sha256", [])
    }
    analyse_holdout(
        records,
        object_points,
        point_indices,
        doc0,
        K0,
        D0,
        doc1,
        K1,
        D1,
        rotation,
        translation,
        rectification,
        training_hashes,
        maximum_pnp_rmse_px=args.max_pnp_rmse_px,
    )
    evaluated = [record for record in records if record.accepted]
    errors = np.asarray([record.bidirectional_rmse_px for record in evaluated], dtype=np.float64)
    rectified_p95 = np.asarray([record.rectified_vertical_p95_px for record in evaluated], dtype=np.float64)
    rotation_errors = np.asarray([record.rotation_residual_deg for record in evaluated], dtype=np.float64)
    translation_errors = np.asarray([record.translation_residual_mm for record in evaluated], dtype=np.float64)
    if errors.size:
        median = float(np.median(errors))
        p95 = float(np.percentile(errors, 95))
        maximum = float(np.max(errors))
        pass_fraction = float(np.mean(errors <= args.p95_rmse_px))
        vertical_p95 = float(np.percentile(rectified_p95, 95))
        rotation_median = float(np.median(rotation_errors))
        translation_median = float(np.median(translation_errors))
    else:
        median = p95 = maximum = vertical_p95 = rotation_median = translation_median = math.inf
        pass_fraction = 0.0
    baseline = float(np.linalg.norm(translation))
    translation_limit = max(0.5, args.translation_dispersion_fraction * baseline)
    failures: list[str] = []
    if len(evaluated) < args.min_holdout_pairs:
        failures.append(f"only {len(evaluated)} evaluated holdout pairs; need {args.min_holdout_pairs}")
    if any(record.training_overlap for record in records):
        failures.append("one or more holdout images were reused from training")
    if median > args.median_rmse_px:
        failures.append(f"median cross RMSE {median:.3f}px exceeds {args.median_rmse_px:.3f}px")
    if p95 > args.p95_rmse_px:
        failures.append(f"P95 cross RMSE {p95:.3f}px exceeds {args.p95_rmse_px:.3f}px")
    if maximum > args.maximum_rmse_px:
        failures.append(f"maximum cross RMSE {maximum:.3f}px exceeds {args.maximum_rmse_px:.3f}px")
    if pass_fraction < args.required_pass_fraction:
        failures.append(
            f"fraction <= {args.p95_rmse_px:.3f}px is {pass_fraction:.1%}; need {args.required_pass_fraction:.1%}"
        )
    if vertical_p95 > args.rectified_p95_px:
        failures.append(
            f"holdout P95 of per-pose rectified vertical P95 {vertical_p95:.3f}px "
            f"exceeds {args.rectified_p95_px:.3f}px"
        )
    if rotation_median > args.rotation_dispersion_deg:
        failures.append(
            f"rotation dispersion {rotation_median:.3f}deg exceeds {args.rotation_dispersion_deg:.3f}deg"
        )
    if translation_median > translation_limit:
        failures.append(
            f"translation dispersion {translation_median:.3f}mm exceeds {translation_limit:.3f}mm"
        )
    report_path = args.output_root / "holdout_pairs.csv"
    write_csv_rows(report_path, [_record_row(record) for record in records])
    summary = {
        "schema": EXTRINSIC_VALIDATION_SCHEMA,
        "created_utc": _utc_now(),
        "status": "pass" if not failures else "fail",
        "extrinsics": {
            "path": str(args.extrinsics.resolve()),
            "sha256": sha256_file(args.extrinsics),
            "schema": extrinsic_doc["schema"],
            "transform_convention": extrinsic_doc["transform_convention"],
            "baseline_mm": baseline,
        },
        "pair_manifest": {
            "path": str(args.pairs.resolve()),
            "sha256": holdout_manifest_hash,
            "dataset_root": str(pairing_summary["dataset_root"]),
            "pairing_summary_path": str(pairing_summary_path.resolve()),
            "pairing_summary_sha256": sha256_file(pairing_summary_path),
            "stillness_config_path": str(holdout_stillness_path),
            "stillness_config_sha256": sha256_file(holdout_stillness_path),
            "training_manifest_sha256": training_manifest_hash,
        },
        "evaluation_protocol": {
            "point_order_policy": "per_camera_fixed_intrinsic_pnp_with_detector_order_tiebreak",
            "point_order_uses_tested_extrinsics": False,
            "maximum_single_camera_pnp_rmse_px": args.max_pnp_rmse_px,
        },
        "counts": {
            "input_pairs": len(records),
            "evaluated_pairs": len(evaluated),
            "training_overlap_pairs": sum(record.training_overlap for record in records),
        },
        "quality": {
            "median_cross_rmse_px": median,
            "p95_cross_rmse_px": p95,
            "maximum_cross_rmse_px": maximum,
            "fraction_at_or_below_p95_limit": pass_fraction,
            "holdout_p95_of_rectified_vertical_p95_px": vertical_p95,
            "rotation_dispersion_median_deg": rotation_median,
            "translation_dispersion_median_mm": translation_median,
            "thresholds": {
                "minimum_holdout_pairs": args.min_holdout_pairs,
                "median_cross_rmse_px": args.median_rmse_px,
                "p95_cross_rmse_px": args.p95_rmse_px,
                "maximum_cross_rmse_px": args.maximum_rmse_px,
                "required_pass_fraction": args.required_pass_fraction,
                "rectified_vertical_p95_px": args.rectified_p95_px,
                "rotation_dispersion_median_deg": args.rotation_dispersion_deg,
                "translation_dispersion_fraction_of_baseline": args.translation_dispersion_fraction,
                "translation_dispersion_effective_mm": translation_limit,
            },
            "failures": failures,
        },
    }
    summary_path = args.output_root / "holdout_summary.json"
    _write_json_atomic(summary_path, summary)
    if not args.no_images:
        write_rectified_diagnostics(
            args.output_root, records, doc0, K0, D0, K1, D1, rectification
        )
    return summary_path, report_path, summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_arguments(parser, args)
    try:
        summary_path, report_path, summary = run(args)
    except (OSError, ValueError, cv2.error) as exc:
        print(f"extrinsic validation failed: {exc}", file=sys.stderr)
        return 2
    quality = summary["quality"]
    print(
        f"extrinsic validation: {summary['status']}  pairs={summary['counts']['evaluated_pairs']} "
        f"median={quality['median_cross_rmse_px']:.4f}px "
        f"P95={quality['p95_cross_rmse_px']:.4f}px max={quality['maximum_cross_rmse_px']:.4f}px"
    )
    print(f"summary: {summary_path}")
    print(f"pair report: {report_path}")
    for failure in quality["failures"]:
        print(f"failure: {failure}")
    return 0 if summary["status"] == "pass" else 3


if __name__ == "__main__":
    raise SystemExit(main())
