"""Schema and immutable-intrinsics guards for stereo extrinsic calibration."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .calibration_config import validate_camera_calibration


STEREO_EXTRINSICS_SCHEMA = "taxi_receiver.stereo_extrinsics/1"
STILLNESS_SCHEMA = "taxi_receiver.stereo_stillness/1"
TRANSFORM_CONVENTION = "X_cam1 = R_cam1_from_cam0 * X_cam0 + t_cam1_from_cam0"


class ExtrinsicValidationError(ValueError):
    """Raised when a stereo configuration is structurally or geometrically unsafe."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ExtrinsicValidationError(message)


def _is_sha256(value: Any) -> bool:
    digest = str(value)
    return len(digest) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in digest
    )


def _finite_array(value: Any, shape: tuple[int, ...], name: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    _require(array.shape == shape, f"{name} must have shape {shape}, got {array.shape}")
    _require(bool(np.all(np.isfinite(array))), f"{name} contains a non-finite value")
    return array


def load_intrinsic(path: Path) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExtrinsicValidationError(f"invalid intrinsic JSON in {path}: {exc}") from exc
    validate_camera_calibration(document)
    return (
        document,
        np.asarray(document["K"], dtype=np.float64),
        np.asarray(document["dist_coeffs"], dtype=np.float64).reshape(-1, 1),
    )


def intrinsic_point_set(document: dict[str, Any]) -> frozenset[int]:
    """Return the exact object-point indices used to fit an intrinsic model."""

    pattern = document.get("pattern")
    _require(isinstance(pattern, dict), "intrinsic pattern object is missing")
    columns = pattern.get("columns")
    rows = pattern.get("rows")
    _require(
        isinstance(columns, int)
        and not isinstance(columns, bool)
        and isinstance(rows, int)
        and not isinstance(rows, bool)
        and columns > 1
        and rows > 1,
        "intrinsic pattern.columns/rows must be integers > 1",
    )
    total = columns * rows

    used_count = pattern.get("used_point_count")
    if used_count is not None:
        _require(
            isinstance(used_count, int)
            and not isinstance(used_count, bool)
            and 0 < used_count <= total,
            f"intrinsic pattern.used_point_count must be between 1 and {total}",
        )

    def parse_indices(field: str) -> frozenset[int]:
        values = pattern.get(field)
        _require(isinstance(values, list), f"intrinsic pattern.{field} must be a list")
        _require(
            all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value < total
                for value in values
            ),
            f"intrinsic pattern.{field} must contain indices in [0, {total - 1}]",
        )
        _require(
            len(set(values)) == len(values),
            f"intrinsic pattern.{field} contains duplicate indices",
        )
        return frozenset(values)

    if "used_point_indices" in pattern:
        used = parse_indices("used_point_indices")
        _require(bool(used), "intrinsic pattern.used_point_indices must not be empty")
        if used_count is not None:
            _require(
                used_count == len(used),
                "intrinsic pattern.used_point_count "
                f"is {used_count}, but used_point_indices contains {len(used)} indices",
            )
        if "excluded_point_indices" in pattern:
            excluded = parse_indices("excluded_point_indices")
            expected = frozenset(range(total)) - excluded
            _require(
                used == expected,
                "intrinsic used_point_indices conflicts with excluded_point_indices",
            )
        return used

    if "excluded_point_indices" in pattern:
        excluded = parse_indices("excluded_point_indices")
        used = frozenset(range(total)) - excluded
        _require(bool(used), "intrinsic excluded_point_indices removes every object point")
        if used_count is not None:
            _require(
                used_count == len(used),
                "intrinsic pattern.used_point_count "
                f"is {used_count}, but excluded_point_indices leaves {len(used)} indices",
            )
        return used

    if used_count == total:
        return frozenset(range(total))

    raise ExtrinsicValidationError(
        "intrinsic point set is ambiguous (点集不明确): used_point_count is smaller "
        "than the full grid or missing, but no explicit point-index field is present"
    )


def validate_intrinsic_pair(
    cam0_path: Path,
    cam1_path: Path,
) -> tuple[
    dict[str, Any], np.ndarray, np.ndarray,
    dict[str, Any], np.ndarray, np.ndarray,
]:
    doc0, K0, D0 = load_intrinsic(cam0_path)
    doc1, K1, D1 = load_intrinsic(cam1_path)
    _require(doc0.get("camera_id") == 0, f"cam0 intrinsic camera_id is {doc0.get('camera_id')!r}")
    _require(doc1.get("camera_id") == 1, f"cam1 intrinsic camera_id is {doc1.get('camera_id')!r}")
    _require(doc0["model"] == doc1["model"], "the two camera models differ")
    _require(
        doc0["model"] == "opencv_fisheye",
        "stereo extrinsic pipeline currently requires opencv_fisheye intrinsics",
    )
    _require(doc0["image_size"] == doc1["image_size"], "the two image sizes differ")
    keys = ("type", "columns", "rows", "base_spacing_mm")
    for key in keys:
        _require(
            doc0["pattern"].get(key) == doc1["pattern"].get(key),
            f"intrinsic pattern.{key} differs between cameras",
        )
    points0 = intrinsic_point_set(doc0)
    points1 = intrinsic_point_set(doc1)
    _require(
        points0 == points1,
        "intrinsic point sets differ between cameras: "
        f"cam0-only indices={sorted(points0 - points1)}, "
        f"cam1-only indices={sorted(points1 - points0)}; "
        "recalibrate both cameras with the same point set",
    )
    return doc0, K0, D0, doc1, K1, D1


def intrinsic_reference(path: Path, document: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "schema": document["schema"],
        "camera_id": document.get("camera_id"),
        "model": document["model"],
        "image_size": document["image_size"],
    }


def verify_intrinsic_reference(
    path: Path, expected: dict[str, Any], expected_camera_id: int
) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    document, K, D = load_intrinsic(path)
    _require(
        document.get("camera_id") == expected_camera_id,
        f"intrinsic {path} is camera {document.get('camera_id')}, expected {expected_camera_id}",
    )
    actual_hash = sha256_file(path)
    _require(
        actual_hash.upper() == str(expected.get("sha256", "")).upper(),
        f"intrinsic hash mismatch for camera {expected_camera_id}: {actual_hash}",
    )
    _require(document.get("schema") == expected.get("schema"), "intrinsic schema reference mismatch")
    _require(document.get("model") == expected.get("model"), "intrinsic model reference mismatch")
    _require(document.get("image_size") == expected.get("image_size"), "intrinsic image-size reference mismatch")
    return document, K, D


def fisheye_domain_report(
    document: dict[str, Any], K: np.ndarray, D: np.ndarray, *, warning_margin_deg: float = 5.0
) -> dict[str, Any]:
    """Check that the fitted radial polynomial is invertible over the sensor.

    The corner radius is measured in distorted normalized coordinates.  The
    first non-positive derivative of theta_d(theta) is the edge of the safe
    one-to-one interval.  A finite undistortPoints grid is checked separately.
    """

    width = int(document["image_size"]["width"])
    height = int(document["image_size"]["height"])
    corners = np.asarray(
        [[0.0, 0.0], [width - 1.0, 0.0], [0.0, height - 1.0], [width - 1.0, height - 1.0]],
        dtype=np.float64,
    )
    normalized = np.column_stack(
        ((corners[:, 0] - K[0, 2]) / K[0, 0], (corners[:, 1] - K[1, 2]) / K[1, 1])
    )
    required_radius = float(np.max(np.linalg.norm(normalized, axis=1)))
    coefficients = np.zeros(4, dtype=np.float64)
    flat = np.asarray(D, dtype=np.float64).reshape(-1)
    coefficients[: min(4, flat.size)] = flat[:4]
    theta = np.linspace(0.0, math.radians(89.9), 20000)
    powers2 = theta * theta
    mapped = theta * (
        1.0
        + coefficients[0] * powers2
        + coefficients[1] * powers2**2
        + coefficients[2] * powers2**3
        + coefficients[3] * powers2**4
    )
    derivative = (
        1.0
        + 3.0 * coefficients[0] * powers2
        + 5.0 * coefficients[1] * powers2**2
        + 7.0 * coefficients[2] * powers2**3
        + 9.0 * coefficients[3] * powers2**4
    )
    nonpositive = np.flatnonzero(derivative <= 0.0)
    limit_index = int(nonpositive[0]) if nonpositive.size else len(theta) - 1
    monotonic_limit = float(theta[limit_index])
    maximum_monotonic_radius = float(np.max(mapped[: limit_index + 1]))
    invertible_to_corner = required_radius <= maximum_monotonic_radius
    required_theta: float | None = None
    if invertible_to_corner:
        usable_mapped = mapped[: limit_index + 1]
        required_theta = float(np.interp(required_radius, usable_mapped, theta[: limit_index + 1]))

    xs = np.linspace(0.0, width - 1.0, 33)
    ys = np.linspace(0.0, height - 1.0, 25)
    sample = np.asarray([(x, y) for y in ys for x in xs], dtype=np.float64).reshape(-1, 1, 2)
    undistorted = cv2.fisheye.undistortPoints(sample, K, D)
    finite_grid = bool(np.all(np.isfinite(undistorted)))
    margin_deg = (
        math.degrees(monotonic_limit - required_theta)
        if required_theta is not None
        else -math.inf
    )
    warnings: list[str] = []
    failures: list[str] = []
    if not invertible_to_corner:
        failures.append("fisheye polynomial is not one-to-one through the image corners")
    if not finite_grid:
        failures.append("fisheye undistortPoints produced a non-finite sensor-grid value")
    if invertible_to_corner and margin_deg < warning_margin_deg:
        warnings.append(
            f"only {margin_deg:.3f} deg of monotonic angular margin remains beyond the corners"
        )
    return {
        "status": "fail" if failures else "warning" if warnings else "pass",
        "required_distorted_corner_radius": required_radius,
        "required_corner_theta_deg": (
            None if required_theta is None else math.degrees(required_theta)
        ),
        "monotonic_limit_theta_deg": math.degrees(monotonic_limit),
        "monotonic_margin_deg": margin_deg if math.isfinite(margin_deg) else None,
        "finite_undistort_grid": finite_grid,
        "warnings": warnings,
        "failures": failures,
    }


def validate_stereo_extrinsics(document: Any) -> dict[str, Any]:
    _require(isinstance(document, dict), "stereo extrinsics must be a JSON object")
    _require(
        document.get("schema") == STEREO_EXTRINSICS_SCHEMA,
        f"schema must be {STEREO_EXTRINSICS_SCHEMA!r}",
    )
    _require(document.get("reference_camera_id") == 0, "reference camera must be cam0")
    _require(document.get("target_camera_id") == 1, "target camera must be cam1")
    _require(
        document.get("transform_convention") == TRANSFORM_CONVENTION,
        "unknown or missing transform convention",
    )
    _require(document.get("translation_unit") == "mm", "translation_unit must be mm")
    rotation = _finite_array(document.get("R_cam1_from_cam0"), (3, 3), "R_cam1_from_cam0")
    translation = _finite_array(document.get("t_cam1_from_cam0_mm"), (3,), "t_cam1_from_cam0_mm")
    _require(
        bool(np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6)),
        "R_cam1_from_cam0 is not orthonormal",
    )
    _require(math.isclose(float(np.linalg.det(rotation)), 1.0, abs_tol=1e-6), "rotation determinant is not +1")
    _require(float(np.linalg.norm(translation)) > 0.0, "stereo baseline is zero")
    intrinsics = document.get("intrinsics")
    _require(isinstance(intrinsics, dict), "intrinsics references are missing")
    for name, camera_id in (("cam0", 0), ("cam1", 1)):
        reference = intrinsics.get(name)
        _require(isinstance(reference, dict), f"intrinsics.{name} is missing")
        _require(reference.get("camera_id") == camera_id, f"intrinsics.{name}.camera_id is wrong")
        digest = str(reference.get("sha256", ""))
        _require(_is_sha256(digest), f"intrinsics.{name}.sha256 is invalid")
        _require(reference.get("schema") == "taxi_receiver.camera_calibration/1", f"intrinsics.{name}.schema is invalid")
        _require(reference.get("model") == "opencv_fisheye", f"intrinsics.{name}.model is invalid")
        size = reference.get("image_size")
        _require(
            isinstance(size, dict)
            and isinstance(size.get("width"), int)
            and isinstance(size.get("height"), int)
            and size["width"] > 0
            and size["height"] > 0,
            f"intrinsics.{name}.image_size is invalid",
        )
    _require(
        intrinsics["cam0"]["image_size"] == intrinsics["cam1"]["image_size"],
        "referenced intrinsic image sizes differ",
    )
    pattern = document.get("pattern")
    _require(isinstance(pattern, dict), "pattern object is missing")
    pattern_type = pattern.get("type")
    _require(
        isinstance(pattern_type, str) and pattern_type.startswith("asymmetric"),
        "pattern.type must identify an asymmetric circle grid",
    )
    columns = pattern.get("columns")
    rows = pattern.get("rows")
    spacing = pattern.get("base_spacing_mm")
    _require(
        isinstance(columns, int) and columns > 1,
        "pattern.columns must be an integer > 1",
    )
    _require(
        isinstance(rows, int) and rows > 1,
        "pattern.rows must be an integer > 1",
    )
    _require(
        isinstance(spacing, (int, float))
        and math.isfinite(float(spacing))
        and float(spacing) > 0.0,
        "pattern.base_spacing_mm must be positive",
    )
    used_indices = pattern.get("used_point_indices")
    _require(
        isinstance(used_indices, list)
        and used_indices
        and all(isinstance(value, int) and value >= 0 for value in used_indices)
        and len(set(used_indices)) == len(used_indices),
        "pattern.used_point_indices must be unique non-negative integers",
    )
    _require(
        max(used_indices) < columns * rows,
        "pattern.used_point_indices exceeds the declared grid size",
    )
    solver = document.get("solver")
    _require(isinstance(solver, dict), "solver object is missing")
    _require(
        solver.get("backend") == "cv2.fisheye.stereoCalibrate",
        "solver backend is not the fixed-intrinsic fisheye solver",
    )
    _require(solver.get("intrinsics_unchanged") is True, "solver did not freeze intrinsics")
    flags = solver.get("flags")
    _require(
        isinstance(flags, list) and "CALIB_FIX_INTRINSIC" in flags,
        "solver flags do not include CALIB_FIX_INTRINSIC",
    )
    quality = document.get("quality")
    _require(isinstance(quality, dict), "quality object is missing")
    _require(quality.get("status") in {"acceptable", "limited", "unacceptable"}, "invalid quality.status")
    for key in ("accepted_pairs", "stereo_rms_px", "baseline_mm"):
        value = quality.get(key)
        if key == "accepted_pairs":
            _require(isinstance(value, int) and value >= 3, "quality.accepted_pairs must be >= 3")
        else:
            _require(
                isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) >= 0.0,
                f"quality.{key} must be finite and non-negative",
            )
    _require(
        math.isclose(float(quality["baseline_mm"]), float(np.linalg.norm(translation)), rel_tol=1e-8, abs_tol=1e-8),
        "quality.baseline_mm does not match the translation vector",
    )
    pairing = document.get("pairing")
    _require(isinstance(pairing, dict), "pairing provenance is missing")
    for field in (
        "manifest_path",
        "pairing_summary_path",
        "dataset_root",
        "stillness_config_path",
    ):
        _require(
            isinstance(pairing.get(field), str) and bool(pairing[field].strip()),
            f"pairing.{field} is missing",
        )
    for field in (
        "manifest_sha256",
        "pairing_summary_sha256",
        "stillness_config_sha256",
    ):
        _require(_is_sha256(pairing.get(field)), f"pairing.{field} is invalid")
    pose_ids = pairing.get("training_pose_ids")
    _require(
        isinstance(pose_ids, list)
        and len(pose_ids) == quality["accepted_pairs"]
        and all(isinstance(value, str) and bool(value.strip()) for value in pose_ids)
        and len(set(pose_ids)) == len(pose_ids),
        "pairing.training_pose_ids must uniquely identify every accepted pair",
    )
    image_hashes = pairing.get("training_images_sha256")
    _require(
        isinstance(image_hashes, list)
        and quality["accepted_pairs"] <= len(image_hashes) <= 2 * quality["accepted_pairs"]
        and all(_is_sha256(value) for value in image_hashes)
        and len({str(value).upper() for value in image_hashes}) == len(image_hashes),
        "pairing.training_images_sha256 must uniquely bind the accepted training images",
    )
    return document


def load_stereo_extrinsics(path: Path) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExtrinsicValidationError(f"invalid extrinsic JSON in {path}: {exc}") from exc
    validate_stereo_extrinsics(document)
    return (
        document,
        np.asarray(document["R_cam1_from_cam0"], dtype=np.float64),
        np.asarray(document["t_cam1_from_cam0_mm"], dtype=np.float64).reshape(3, 1),
    )


def validate_stillness_config(document: Any) -> dict[str, Any]:
    _require(isinstance(document, dict), "stillness configuration must be an object")
    _require(document.get("schema") == STILLNESS_SCHEMA, f"schema must be {STILLNESS_SCHEMA!r}")
    _require(
        isinstance(document.get("dataset_root"), str)
        and bool(document["dataset_root"].strip()),
        "stillness dataset_root is missing",
    )
    _require(
        isinstance(document.get("window_frames"), int)
        and document["window_frames"] >= 3
        and document["window_frames"] % 2 == 1,
        "window_frames must be an odd integer >= 3",
    )
    max_gap = document.get("max_frame_gap_ms")
    _require(
        isinstance(max_gap, (int, float))
        and math.isfinite(float(max_gap))
        and float(max_gap) > 0.0,
        "max_frame_gap_ms must be positive",
    )
    intrinsics = document.get("intrinsics")
    _require(isinstance(intrinsics, dict), "stillness intrinsic references are missing")
    for key in ("cam0", "cam1"):
        reference = intrinsics.get(key)
        _require(
            isinstance(reference, dict) and _is_sha256(reference.get("sha256")),
            f"stillness {key} intrinsic reference is invalid",
        )
    cameras = document.get("cameras")
    _require(isinstance(cameras, dict), "stillness cameras object is missing")
    for key in ("cam0", "cam1"):
        item = cameras.get(key)
        _require(isinstance(item, dict), f"stillness {key} is missing")
        _require(
            isinstance(item.get("complete_grid_frames"), int)
            and item["complete_grid_frames"] >= document["window_frames"],
            f"{key}.complete_grid_frames is insufficient",
        )
        for field in ("step_threshold_px", "window_threshold_px"):
            value = item.get(field)
            _require(isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0.0, f"{key}.{field} must be positive")
    return document
