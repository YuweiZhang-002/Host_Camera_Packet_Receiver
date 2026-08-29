"""Offline intrinsic calibration from Sobel-threshold binary images.

The detector is deliberately tailored to the RP2350 payload.  A printed disk
arrives as a thick, sometimes broken ring.  We fit ellipses to ring boundaries,
merge concentric inner/outer fits, and use ``findCirclesGrid`` only to order the
resulting centers.  No grayscale reconstruction is attempted.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import glob
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Sequence

import cv2
import numpy as np

from .calibration_config import (
    CALIBRATION_SCHEMA,
    validate_camera_calibration,
)


PINHOLE_DISTORTION_NAMES = (
    "k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6",
    "s1", "s2", "s3", "s4", "tau_x", "tau_y",
)
FISHEYE_DISTORTION_NAMES = ("k1", "k2", "k3", "k4")
SUPPORTED_IMAGE_SUFFIXES = frozenset({".pgm", ".raw"})

# Each outlier iteration re-solves the full bundle.  Without a ceiling a
# several-hundred-view capture would trigger hundreds of fisheye solves and
# never finish; rejections beyond this point indicate bad data, not outliers.
MAX_OUTLIER_ITERATIONS = 20


def _fisheye_flag(name: str, fallback: int) -> int:
    """Bridge OpenCV 4.x and 5.0 Python constant exposure.

    OpenCV 5.0.0's Python wheel exposes ``cv2.fisheye.calibrate`` but omits
    the namespace constants.  Their public bit values remain part of the C++
    API, so use those values only when the binding does not publish a name.
    """

    return int(getattr(cv2.fisheye, name, fallback))


@dataclass(slots=True)
class DetectorSettings:
    pattern: str = "asymmetric"
    columns: int = 4
    rows: int = 11
    min_dot_diameter_px: float = 6.0
    max_dot_diameter_px: float = 120.0
    min_axis_ratio: float = 0.24
    min_arc_coverage: float = 0.42
    max_ellipse_residual: float = 0.30
    close_kernel: int = 3
    min_grid_spacing_px: float = 10.0

    @property
    def point_count(self) -> int:
        return self.columns * self.rows


@dataclass(slots=True)
class EllipseCandidate:
    center: np.ndarray
    diameter: float
    axis_ratio: float
    arc_coverage: float
    residual: float
    weight: float
    center_spread: float = 0.0
    members: int = 1


@dataclass(slots=True)
class DetectionResult:
    found: bool
    reason: str
    centers: np.ndarray | None
    candidates: list[EllipseCandidate]
    selected: list[EllipseCandidate] = field(default_factory=list)
    metrics: dict[str, float | int] = field(default_factory=dict)


@dataclass(slots=True)
class ViewRecord:
    path: Path
    image: np.ndarray | None = None
    detection: DetectionResult | None = None
    accepted: bool = False
    reason: str = "not_processed"
    reprojection_rmse_px: float | None = None
    initial_reprojection_rmse_px: float | None = None


@dataclass(slots=True)
class CalibrationFit:
    rms_px: float
    K: np.ndarray
    D: np.ndarray
    rvecs: list[np.ndarray]
    tvecs: list[np.ndarray]
    per_view_rmse_px: list[float]
    std_intrinsics: np.ndarray | None = None


def asymmetric_object_points(
    columns: int, rows: int, spacing: float
) -> np.ndarray:
    """OpenCV asymmetric-grid coordinates.

    ``spacing`` is the base pitch: adjacent rows are one pitch apart and
    same-row neighbors are two pitches apart.
    """

    points = np.zeros((columns * rows, 3), np.float64)
    index = 0
    for row in range(rows):
        for column in range(columns):
            points[index, :2] = (
                (2 * column + (row & 1)) * spacing,
                row * spacing,
            )
            index += 1
    return points


def symmetric_object_points(
    columns: int, rows: int, spacing: float
) -> np.ndarray:
    points = np.zeros((columns * rows, 3), np.float64)
    points[:, :2] = np.asarray(
        [(column * spacing, row * spacing) for row in range(rows) for column in range(columns)],
        dtype=np.float64,
    )
    return points


def make_object_points(settings: DetectorSettings, spacing: float) -> np.ndarray:
    if settings.pattern == "asymmetric":
        return asymmetric_object_points(settings.columns, settings.rows, spacing)
    return symmetric_object_points(settings.columns, settings.rows, spacing)


def _ellipse_statistics(
    contour: np.ndarray,
    center: tuple[float, float],
    axes: tuple[float, float],
    angle_degrees: float,
) -> tuple[float, float]:
    points = contour.reshape(-1, 2).astype(np.float64)
    theta = math.radians(angle_degrees)
    cosine = math.cos(theta)
    sine = math.sin(theta)
    delta = points - np.asarray(center, dtype=np.float64)
    x_rot = cosine * delta[:, 0] + sine * delta[:, 1]
    y_rot = -sine * delta[:, 0] + cosine * delta[:, 1]
    half_x = max(float(axes[0]) * 0.5, 1e-6)
    half_y = max(float(axes[1]) * 0.5, 1e-6)
    normalized_radius = np.sqrt((x_rot / half_x) ** 2 + (y_rot / half_y) ** 2)
    residual = float(np.median(np.abs(normalized_radius - 1.0)))
    phase = np.arctan2(y_rot / half_y, x_rot / half_x)
    bins = np.floor((phase + math.pi) * (36.0 / (2.0 * math.pi))).astype(int)
    coverage = float(len(np.unique(np.clip(bins, 0, 35))) / 36.0)
    return residual, coverage


def _raw_ellipse_candidates(
    binary: np.ndarray, settings: DetectorSettings
) -> list[EllipseCandidate]:
    working = binary
    if settings.close_kernel > 1:
        kernel_size = settings.close_kernel | 1
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)
        )
        working = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)

    contours, _hierarchy = cv2.findContours(
        working, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE
    )
    candidates: list[EllipseCandidate] = []
    height, width = binary.shape
    for contour in contours:
        if len(contour) < 12:
            continue
        try:
            if hasattr(cv2, "fitEllipseAMS"):
                center, axes, angle = cv2.fitEllipseAMS(contour)
            else:
                center, axes, angle = cv2.fitEllipse(contour)
        except cv2.error:
            continue
        major = max(float(axes[0]), float(axes[1]))
        minor = min(float(axes[0]), float(axes[1]))
        diameter = math.sqrt(max(major * minor, 0.0))
        if not settings.min_dot_diameter_px <= diameter <= settings.max_dot_diameter_px:
            continue
        axis_ratio = minor / max(major, 1e-9)
        if axis_ratio < settings.min_axis_ratio:
            continue
        cx, cy = float(center[0]), float(center[1])
        if not (1.0 <= cx < width - 1.0 and 1.0 <= cy < height - 1.0):
            continue
        residual, coverage = _ellipse_statistics(contour, center, axes, angle)
        if residual > settings.max_ellipse_residual:
            continue
        if coverage < settings.min_arc_coverage:
            continue
        weight = coverage * math.sqrt(float(len(contour))) / max(residual + 0.025, 0.025)
        candidates.append(
            EllipseCandidate(
                center=np.asarray((cx, cy), dtype=np.float64),
                diameter=diameter,
                axis_ratio=axis_ratio,
                arc_coverage=coverage,
                residual=residual,
                weight=weight,
            )
        )

    # Bound pathological background clutter before concentric merging.  The
    # best complete ellipses survive; partial/noisy contours have lower weight.
    limit = max(settings.point_count * 24, 512)
    candidates.sort(key=lambda item: item.weight, reverse=True)
    return candidates[:limit]


def _merge_concentric_candidates(
    raw_candidates: Sequence[EllipseCandidate],
) -> list[EllipseCandidate]:
    clusters: list[list[EllipseCandidate]] = []
    for candidate in raw_candidates:
        best_index: int | None = None
        best_distance = math.inf
        for index, cluster in enumerate(clusters):
            representative = cluster[0]
            diameter_ratio = max(candidate.diameter, representative.diameter) / max(
                min(candidate.diameter, representative.diameter), 1e-9
            )
            if diameter_ratio > 1.75:
                continue
            distance = float(np.linalg.norm(candidate.center - representative.center))
            tolerance = max(2.0, 0.20 * min(candidate.diameter, representative.diameter))
            if distance <= tolerance and distance < best_distance:
                best_index = index
                best_distance = distance
        if best_index is None:
            clusters.append([candidate])
        else:
            clusters[best_index].append(candidate)

    merged: list[EllipseCandidate] = []
    for cluster in clusters:
        weights = np.asarray([item.weight for item in cluster], dtype=np.float64)
        centers = np.asarray([item.center for item in cluster], dtype=np.float64)
        total_weight = float(weights.sum())
        center = (centers * weights[:, None]).sum(axis=0) / max(total_weight, 1e-9)
        spread = float(np.max(np.linalg.norm(centers - center, axis=1)))
        merged.append(
            EllipseCandidate(
                center=center,
                diameter=float(np.average([item.diameter for item in cluster], weights=weights)),
                axis_ratio=float(np.average([item.axis_ratio for item in cluster], weights=weights)),
                arc_coverage=float(np.average([item.arc_coverage for item in cluster], weights=weights)),
                residual=float(np.average([item.residual for item in cluster], weights=weights)),
                weight=total_weight,
                center_spread=spread,
                members=len(cluster),
            )
        )
    merged.sort(key=lambda item: item.weight, reverse=True)
    return merged


def extract_circle_candidates(
    image: np.ndarray, settings: DetectorSettings
) -> list[EllipseCandidate]:
    binary = np.where(image > 0, 255, 0).astype(np.uint8)
    return _merge_concentric_candidates(_raw_ellipse_candidates(binary, settings))


def _blob_detector() -> cv2.SimpleBlobDetector:
    parameters = cv2.SimpleBlobDetector_Params()
    parameters.minThreshold = 1
    parameters.maxThreshold = 256
    parameters.thresholdStep = 16
    parameters.minRepeatability = 1
    parameters.filterByColor = True
    parameters.blobColor = 255
    parameters.filterByArea = True
    parameters.minArea = 8
    parameters.maxArea = 256
    parameters.filterByCircularity = False
    parameters.filterByConvexity = False
    parameters.filterByInertia = False
    return cv2.SimpleBlobDetector_create(parameters)


def _order_candidates(
    candidates: Sequence[EllipseCandidate],
    image_shape: tuple[int, int],
    settings: DetectorSettings,
) -> tuple[np.ndarray | None, list[EllipseCandidate]]:
    height, width = image_shape
    synthetic = np.zeros((height, width), dtype=np.uint8)
    for candidate in candidates:
        center = tuple(int(round(value)) for value in candidate.center)
        cv2.circle(synthetic, center, 3, 255, -1, lineType=cv2.LINE_8)

    base_flag = (
        cv2.CALIB_CB_ASYMMETRIC_GRID
        if settings.pattern == "asymmetric"
        else cv2.CALIB_CB_SYMMETRIC_GRID
    )
    detector = _blob_detector()
    ordered: np.ndarray | None = None
    for flags in (base_flag | cv2.CALIB_CB_CLUSTERING, base_flag):
        found, centers = cv2.findCirclesGrid(
            synthetic,
            (settings.columns, settings.rows),
            flags=flags,
            blobDetector=detector,
        )
        if found:
            ordered = centers.reshape(-1, 2).astype(np.float64)
            break
    if ordered is None:
        return None, []

    selected: list[EllipseCandidate] = []
    available = set(range(len(candidates)))
    refined = np.empty_like(ordered)
    for point_index, point in enumerate(ordered):
        if not available:
            return None, []
        candidate_index = min(
            available,
            key=lambda index: float(np.linalg.norm(candidates[index].center - point)),
        )
        candidate = candidates[candidate_index]
        if float(np.linalg.norm(candidate.center - point)) > 4.0:
            return None, []
        available.remove(candidate_index)
        selected.append(candidate)
        refined[point_index] = candidate.center
    return refined.astype(np.float32), selected


def _nearest_neighbor_spacing(points: np.ndarray) -> float:
    delta = points[:, None, :] - points[None, :, :]
    distances = np.linalg.norm(delta, axis=2)
    distances[distances == 0.0] = np.inf
    return float(np.median(np.min(distances, axis=1)))


def detect_circle_grid(
    image: np.ndarray, settings: DetectorSettings
) -> DetectionResult:
    candidates = extract_circle_candidates(image, settings)
    if len(candidates) < settings.point_count:
        return DetectionResult(
            False,
            f"only_{len(candidates)}_ellipse_candidates_need_{settings.point_count}",
            None,
            candidates,
            metrics={"candidate_count": len(candidates)},
        )
    centers, selected = _order_candidates(candidates, image.shape, settings)
    if centers is None:
        return DetectionResult(
            False,
            "grid_ordering_failed",
            None,
            candidates,
            metrics={"candidate_count": len(candidates)},
        )

    spacing = _nearest_neighbor_spacing(centers)
    x_span = float(np.ptp(centers[:, 0]) / image.shape[1])
    y_span = float(np.ptp(centers[:, 1]) / image.shape[0])
    metrics: dict[str, float | int] = {
        "candidate_count": len(candidates),
        "point_count": len(centers),
        "nearest_neighbor_spacing_px": spacing,
        "board_x_span_fraction": x_span,
        "board_y_span_fraction": y_span,
        "median_ring_diameter_px": float(np.median([item.diameter for item in selected])),
        "median_axis_ratio": float(np.median([item.axis_ratio for item in selected])),
        "median_arc_coverage": float(np.median([item.arc_coverage for item in selected])),
        "median_ellipse_residual": float(np.median([item.residual for item in selected])),
        "max_concentric_center_spread_px": float(max(item.center_spread for item in selected)),
    }
    if spacing < settings.min_grid_spacing_px:
        return DetectionResult(
            False,
            f"grid_spacing_{spacing:.2f}px_below_{settings.min_grid_spacing_px:.2f}px",
            centers,
            candidates,
            selected,
            metrics,
        )
    return DetectionResult(True, "accepted_for_calibration", centers, candidates, selected, metrics)


def _metadata_for_image(path: Path) -> dict[str, Any] | None:
    metadata_path = (
        path.with_suffix(".json") if path.stem != "image" else path.with_name("metadata.json")
    )
    if not metadata_path.is_file():
        return None
    try:
        document = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _preflight_rejection(path: Path, allow_recovered: bool) -> str | None:
    metadata = _metadata_for_image(path)
    if metadata is None:
        return None
    if not allow_recovered and metadata.get("status") == "RECOVERED":
        return "recovered_frame_not_allowed"
    if metadata.get("had_overflow") is True:
        return "frame_overflow"
    if metadata.get("had_crc_error") is True:
        return "frame_crc_error"
    if metadata.get("missing_count", 0):
        return "frame_has_missing_rows"
    return None


def load_binary_image(path: str | Path, width: int = 640, height: int = 480) -> np.ndarray:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".pgm":
        image = cv2.imread(str(source), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError(f"cannot decode PGM: {source}")
    elif suffix == ".raw":
        payload = source.read_bytes()
        expected = width * height
        if len(payload) != expected:
            raise ValueError(
                f"RAW size mismatch for {source}: got {len(payload)}, expected {expected} "
                "bytes of unpacked threshold_u8_0_255"
            )
        image = np.frombuffer(payload, dtype=np.uint8).reshape(height, width).copy()
    else:
        raise ValueError(f"unsupported image suffix {suffix!r}: {source}")
    if image.shape != (height, width):
        raise ValueError(
            f"image geometry mismatch for {source}: got {image.shape[1]}x{image.shape[0]}, "
            f"expected {width}x{height}"
        )
    return np.where(image > 0, 255, 0).astype(np.uint8)


def expand_input_paths(specifications: Sequence[str]) -> list[Path]:
    paths: list[Path] = []
    for specification in specifications:
        expanded = [Path(item) for item in glob.glob(specification, recursive=True)]
        if not expanded:
            expanded = [Path(specification)]
        for item in expanded:
            if item.is_dir():
                paths.extend(
                    path
                    for path in item.rglob("*")
                    if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
                )
            elif item.is_file() and item.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES:
                paths.append(item)

    # The image pipeline writes identical .pgm/.raw pairs.  Count each frame
    # once, preferring the self-describing PGM when both were selected.
    paired: dict[Path, Path] = {}
    for path in sorted({path.resolve() for path in paths}):
        key = path.with_suffix("")
        existing = paired.get(key)
        if existing is None or (path.suffix.lower() == ".pgm" and existing.suffix.lower() == ".raw"):
            paired[key] = path
    return sorted(paired.values())


def _initial_camera_matrix(
    image_size: tuple[int, int], model: str, fov_degrees: float
) -> np.ndarray:
    width, height = image_size
    half_fov = math.radians(fov_degrees * 0.5)
    if model == "fisheye":
        focal = (width * 0.5) / max(half_fov, 1e-6)
    else:
        focal = (width * 0.5) / max(math.tan(half_fov), 1e-6)
    return np.asarray(
        [[focal, 0.0, width * 0.5], [0.0, focal, height * 0.5], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def _project_points(
    object_points: np.ndarray,
    rvec: np.ndarray,
    tvec: np.ndarray,
    K: np.ndarray,
    D: np.ndarray,
    model: str,
) -> np.ndarray:
    if model == "fisheye":
        projected, _ = cv2.fisheye.projectPoints(
            object_points.reshape(1, -1, 3), rvec, tvec, K, D
        )
    else:
        projected, _ = cv2.projectPoints(object_points, rvec, tvec, K, D)
    return projected.reshape(-1, 2)


def _per_view_errors(
    object_points: np.ndarray,
    image_points: Sequence[np.ndarray],
    rvecs: Sequence[np.ndarray],
    tvecs: Sequence[np.ndarray],
    K: np.ndarray,
    D: np.ndarray,
    model: str,
) -> list[float]:
    errors: list[float] = []
    for observed, rvec, tvec in zip(image_points, rvecs, tvecs, strict=True):
        projected = _project_points(object_points, rvec, tvec, K, D, model)
        residual = projected - observed.reshape(-1, 2)
        errors.append(float(np.sqrt(np.mean(np.sum(residual * residual, axis=1)))))
    return errors


def fit_calibration(
    image_points: Sequence[np.ndarray],
    object_points: np.ndarray,
    image_size: tuple[int, int],
    model: str,
    fov_degrees: float = 120.0,
    fisheye_fix_k3_k4: bool = False,
    fisheye_fix_k4: bool = False,
) -> CalibrationFit:
    if fisheye_fix_k3_k4 and fisheye_fix_k4:
        raise ValueError("choose only one fisheye distortion constraint")
    if (fisheye_fix_k3_k4 or fisheye_fix_k4) and model != "fisheye":
        raise ValueError("fisheye k3/k4 constraints require the fisheye model")
    K0 = _initial_camera_matrix(image_size, model, fov_degrees)
    criteria = (
        cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS,
        200,
        1e-10,
    )
    if model == "fisheye":
        objects = [object_points.reshape(-1, 1, 3).astype(np.float64) for _ in image_points]
        images = [points.reshape(-1, 1, 2).astype(np.float64) for points in image_points]
        flags = (
            _fisheye_flag("CALIB_USE_INTRINSIC_GUESS", 1 << 0)
            | _fisheye_flag("CALIB_RECOMPUTE_EXTRINSIC", 1 << 1)
            | _fisheye_flag("CALIB_CHECK_COND", 1 << 2)
            | _fisheye_flag("CALIB_FIX_SKEW", 1 << 3)
        )
        if fisheye_fix_k3_k4:
            flags |= _fisheye_flag("CALIB_FIX_K3", 1 << 6)
            flags |= _fisheye_flag("CALIB_FIX_K4", 1 << 7)
        elif fisheye_fix_k4:
            flags |= _fisheye_flag("CALIB_FIX_K4", 1 << 7)
        rms, K, D, rvecs, tvecs = cv2.fisheye.calibrate(
            objects,
            images,
            image_size,
            K0,
            np.zeros((4, 1), dtype=np.float64),
            flags=flags,
            criteria=criteria,
        )
        fixed_values = (
            np.asarray(D, dtype=np.float64).reshape(-1)[2:4]
            if fisheye_fix_k3_k4
            else np.asarray(D, dtype=np.float64).reshape(-1)[3:4]
        )
        if (fisheye_fix_k3_k4 or fisheye_fix_k4) and not np.allclose(
            fixed_values,
            0.0,
            rtol=0.0,
            atol=1e-12,
        ):
            raise ValueError("OpenCV did not preserve fixed fisheye coefficients at zero")
        std_intrinsics = None
    else:
        objects = [object_points.astype(np.float32) for _ in image_points]
        images = [points.reshape(-1, 2).astype(np.float32) for points in image_points]
        flags = cv2.CALIB_USE_INTRINSIC_GUESS | cv2.CALIB_RATIONAL_MODEL
        result = cv2.calibrateCameraExtended(
            objects,
            images,
            image_size,
            K0,
            np.zeros((8, 1), dtype=np.float64),
            flags=flags,
            criteria=criteria,
        )
        rms, K, D, rvecs, tvecs, std_intrinsics, _std_extrinsics, _opencv_errors = result
    rvec_list = list(rvecs)
    tvec_list = list(tvecs)
    errors = _per_view_errors(
        object_points, image_points, rvec_list, tvec_list, K, D, model
    )
    return CalibrationFit(
        rms_px=float(rms),
        K=np.asarray(K, dtype=np.float64),
        D=np.asarray(D, dtype=np.float64).reshape(-1, 1),
        rvecs=rvec_list,
        tvecs=tvec_list,
        per_view_rmse_px=errors,
        std_intrinsics=(
            None if std_intrinsics is None else np.asarray(std_intrinsics, dtype=np.float64).reshape(-1)
        ),
    )


def _robust_view_threshold(errors: Sequence[float], hard_limit: float) -> float:
    values = np.asarray(errors, dtype=np.float64)
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    robust_limit = max(median + 0.15, median + 3.0 * 1.4826 * mad)
    return min(hard_limit, robust_limit)


def calibrate_with_outlier_rejection(
    records: Sequence[ViewRecord],
    object_points: np.ndarray,
    image_size: tuple[int, int],
    model: str,
    min_views: int,
    max_view_rmse_px: float,
    fov_degrees: float,
    fisheye_fix_k3_k4: bool = False,
    fisheye_fix_k4: bool = False,
) -> tuple[CalibrationFit, list[int], list[dict[str, float | str]]]:
    active = [
        index
        for index, record in enumerate(records)
        if record.detection is not None and record.detection.found
    ]
    if len(active) < min_views:
        raise ValueError(f"only {len(active)} usable detections; at least {min_views} are required")
    rejected: list[dict[str, float | str]] = []

    iterations = min(MAX_OUTLIER_ITERATIONS, max(1, len(active) - min_views + 1))
    for _iteration in range(iterations):
        fit = fit_calibration(
            [records[index].detection.centers for index in active],  # type: ignore[union-attr]
            object_points,
            image_size,
            model,
            fov_degrees,
            fisheye_fix_k3_k4,
            fisheye_fix_k4,
        )
        for index, error in zip(active, fit.per_view_rmse_px, strict=True):
            record = records[index]
            if record.initial_reprojection_rmse_px is None:
                record.initial_reprojection_rmse_px = error
            record.reprojection_rmse_px = error
        threshold = _robust_view_threshold(fit.per_view_rmse_px, max_view_rmse_px)
        worst_position = int(np.argmax(fit.per_view_rmse_px))
        worst_error = fit.per_view_rmse_px[worst_position]
        if worst_error <= threshold or len(active) <= min_views:
            # ``fit`` was solved on exactly this ``active`` set and nothing has
            # been dropped since, so it is already the answer.
            break
        record_index = active.pop(worst_position)
        record = records[record_index]
        record.accepted = False
        record.reason = f"reprojection_outlier_gt_{threshold:.3f}px"
        rejected.append(
            {
                "path": str(record.path),
                "rmse_px_at_rejection": worst_error,
                "threshold_px": threshold,
            }
        )
    else:
        # Only reached when the loop ran out of iterations immediately after a
        # rejection, which leaves ``fit`` stale with respect to ``active``.
        fit = fit_calibration(
            [records[index].detection.centers for index in active],  # type: ignore[union-attr]
            object_points,
            image_size,
            model,
            fov_degrees,
            fisheye_fix_k3_k4,
            fisheye_fix_k4,
        )

    for index, error in zip(active, fit.per_view_rmse_px, strict=True):
        records[index].accepted = True
        records[index].reason = "accepted"
        records[index].reprojection_rmse_px = error
    return fit, active, rejected


def _pose_and_coverage_metrics(
    records: Sequence[ViewRecord], active: Sequence[int], fit: CalibrationFit, image_size: tuple[int, int]
) -> dict[str, float | int]:
    width, height = image_size
    all_points = np.concatenate(
        [records[index].detection.centers for index in active], axis=0  # type: ignore[union-attr]
    )
    spacings = [
        float(records[index].detection.metrics["nearest_neighbor_spacing_px"])  # type: ignore[union-attr]
        for index in active
    ]
    normals: list[np.ndarray] = []
    tilts: list[float] = []
    for rvec in fit.rvecs:
        rotation, _ = cv2.Rodrigues(rvec)
        normal = rotation[:, 2]
        if normal[2] < 0:
            normal = -normal
        normals.append(normal)
        tilts.append(math.degrees(math.acos(float(np.clip(normal[2], -1.0, 1.0)))))
    normal_array = np.asarray(normals)
    return {
        "point_x_coverage_fraction": float(np.ptp(all_points[:, 0]) / width),
        "point_y_coverage_fraction": float(np.ptp(all_points[:, 1]) / height),
        "minimum_edge_margin_px": float(
            min(
                np.min(all_points[:, 0]),
                np.min(all_points[:, 1]),
                width - 1 - np.max(all_points[:, 0]),
                height - 1 - np.max(all_points[:, 1]),
            )
        ),
        "grid_spacing_min_px": float(min(spacings)),
        "grid_spacing_max_px": float(max(spacings)),
        "grid_spacing_scale_ratio": float(max(spacings) / max(min(spacings), 1e-9)),
        "maximum_board_tilt_deg": float(max(tilts)),
        "views_tilted_over_20_deg": int(sum(value >= 20.0 for value in tilts)),
        "board_normal_x_span": float(np.ptp(normal_array[:, 0])),
        "board_normal_y_span": float(np.ptp(normal_array[:, 1])),
    }


def _quality_status(
    fit: CalibrationFit,
    coverage: dict[str, float | int],
    accepted_count: int,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    max_view = max(fit.per_view_rmse_px)
    if fit.rms_px > 0.8:
        warnings.append(f"overall RMS {fit.rms_px:.3f}px exceeds the 0.8px binary-calibration target")
    if max_view > 1.2:
        warnings.append(f"maximum accepted view RMS {max_view:.3f}px exceeds 1.2px")
    if float(coverage["point_x_coverage_fraction"]) < 0.75:
        warnings.append("detected points cover less than 75% of image width")
    if float(coverage["point_y_coverage_fraction"]) < 0.70:
        warnings.append("detected points cover less than 70% of image height")
    if float(coverage["grid_spacing_scale_ratio"]) < 1.4:
        warnings.append("board-distance/scale diversity is below 1.4x")
    if int(coverage["views_tilted_over_20_deg"]) < max(4, accepted_count // 5):
        warnings.append("too few views have board tilt above 20 degrees")
    if float(coverage["board_normal_x_span"]) < 0.45:
        warnings.append("left/right tilt diversity is weak")
    if float(coverage["board_normal_y_span"]) < 0.45:
        warnings.append("up/down tilt diversity is weak")
    if fit.rms_px > 1.2 or max_view > 1.5:
        return "unacceptable", warnings
    if warnings:
        return "limited", warnings
    return "acceptable", warnings


def _view_report(record: ViewRecord) -> dict[str, Any]:
    detection = record.detection
    report: dict[str, Any] = {
        "path": str(record.path),
        "accepted": record.accepted,
        "reason": record.reason,
        "reprojection_rmse_px": record.reprojection_rmse_px,
        "initial_reprojection_rmse_px": record.initial_reprojection_rmse_px,
    }
    if detection is not None:
        report["detection_found"] = detection.found
        report["detection_reason"] = detection.reason
        report.update(detection.metrics)
    else:
        report["detection_found"] = False
        report["detection_reason"] = record.reason
    return report


def build_calibration_document(
    fit: CalibrationFit,
    records: Sequence[ViewRecord],
    active: Sequence[int],
    outliers: Sequence[dict[str, float | str]],
    settings: DetectorSettings,
    image_size: tuple[int, int],
    model: str,
    camera_id: int | None,
    spacing_mm: float,
    dot_diameter_mm: float,
    fisheye_fix_k3_k4: bool = False,
    fisheye_fix_k4: bool = False,
) -> dict[str, Any]:
    if fisheye_fix_k3_k4 and fisheye_fix_k4:
        raise ValueError("choose only one fisheye distortion constraint")
    fixed_distortion_coefficients = (
        ["k3", "k4"]
        if model == "fisheye" and fisheye_fix_k3_k4
        else ["k4"]
        if model == "fisheye" and fisheye_fix_k4
        else []
    )
    coverage = _pose_and_coverage_metrics(records, active, fit, image_size)
    status, warnings = _quality_status(fit, coverage, len(active))
    distortion_names = (
        FISHEYE_DISTORTION_NAMES
        if model == "fisheye"
        else PINHOLE_DISTORTION_NAMES[: len(fit.D)]
    )
    errors = np.asarray(fit.per_view_rmse_px, dtype=np.float64)
    document: dict[str, Any] = {
        "schema": CALIBRATION_SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "camera_id": camera_id,
        "model": "opencv_fisheye" if model == "fisheye" else "opencv_pinhole_rational",
        "image_size": {"width": image_size[0], "height": image_size[1]},
        "K": fit.K.tolist(),
        "dist_coeffs": fit.D.reshape(-1).tolist(),
        "dist_coeff_order": list(distortion_names),
        "solver_constraints": {
            "fixed_distortion_coefficients": fixed_distortion_coefficients,
            "free_distortion_coefficients": [
                name for name in distortion_names
                if name not in fixed_distortion_coefficients
            ],
        },
        "pattern": {
            "type": f"{settings.pattern}_circles",
            "columns": settings.columns,
            "rows": settings.rows,
            "used_point_count": settings.point_count,
            "excluded_point_indices": [],
            "base_spacing_mm": spacing_mm,
            "dot_diameter_mm": dot_diameter_mm,
            "asymmetric_coordinate_rule": (
                "x=(2*column+(row%2))*base_spacing_mm; y=row*base_spacing_mm"
                if settings.pattern == "asymmetric"
                else None
            ),
        },
        "quality": {
            "status": status,
            "rms_px": fit.rms_px,
            "mean_view_rmse_px": float(np.mean(errors)),
            "median_view_rmse_px": float(np.median(errors)),
            "max_view_rmse_px": float(np.max(errors)),
            "accepted_images": len(active),
            "rejected_images": len(records) - len(active),
            "calibration_outliers": list(outliers),
            "coverage": coverage,
            "warnings": warnings,
            "std_deviations_intrinsics": (
                None if fit.std_intrinsics is None else fit.std_intrinsics.tolist()
            ),
            "views": [_view_report(record) for record in records],
        },
    }
    validate_camera_calibration(document)
    return document


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_view_report(path: Path, records: Sequence[ViewRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [_view_report(record) for record in records]
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_detection_diagnostic(path: Path, record: ViewRecord) -> None:
    if record.image is None:
        return
    canvas = cv2.cvtColor(record.image, cv2.COLOR_GRAY2BGR)
    detection = record.detection
    if detection is not None:
        for candidate in detection.candidates:
            center = tuple(int(round(value)) for value in candidate.center)
            radius = max(2, int(round(candidate.diameter * 0.5)))
            cv2.circle(canvas, center, radius, (0, 180, 255), 1, cv2.LINE_AA)
        if detection.centers is not None:
            for index, point in enumerate(detection.centers):
                center = tuple(int(round(value)) for value in point)
                cv2.circle(canvas, center, 3, (0, 255, 0), -1, cv2.LINE_AA)
                if index % max(1, len(detection.centers) // 12) == 0:
                    cv2.putText(
                        canvas,
                        str(index),
                        (center[0] + 4, center[1] - 4),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.35,
                        (255, 100, 0),
                        1,
                        cv2.LINE_AA,
                    )
    color = (0, 200, 0) if record.accepted else (0, 0, 255)
    label = record.reason
    if record.reprojection_rmse_px is not None:
        label += f"  RMSE={record.reprojection_rmse_px:.3f}px"
    cv2.putText(canvas, label, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), canvas)


def _write_all_diagnostics(
    args: argparse.Namespace, records: Sequence[ViewRecord]
) -> None:
    if args.no_diagnostics:
        return
    diagnostics = args.diagnostics_dir or args.output.with_name(
        f"{args.output.stem}_diagnostics"
    )
    for index, record in enumerate(records):
        safe_stem = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in record.path.stem
        )
        write_detection_diagnostic(
            diagnostics / f"{index:03d}_{safe_stem}.png", record
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline intrinsic calibration from image_pipeline binary PGM/RAW frames."
    )
    parser.add_argument("inputs", nargs="+", help="PGM/RAW files, directories, or glob patterns")
    parser.add_argument("--output", type=Path, default=Path("camera_calibration.json"))
    parser.add_argument("--report", type=Path, help="per-view CSV; default: <output>.views.csv")
    parser.add_argument("--diagnostics-dir", type=Path, help="annotated detections directory")
    parser.add_argument("--no-diagnostics", action="store_true")
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
    parser.add_argument("--fov-deg", type=float, default=120.0, help="initialization only")
    parser.add_argument("--min-views", type=int, default=15)
    parser.add_argument("--max-view-rmse-px", type=float, default=1.5)
    parser.add_argument("--min-dot-diameter-px", type=float, default=6.0)
    parser.add_argument("--max-dot-diameter-px", type=float, default=120.0)
    parser.add_argument("--min-grid-spacing-px", type=float, default=10.0)
    parser.add_argument("--min-axis-ratio", type=float, default=0.24)
    parser.add_argument("--min-arc-coverage", type=float, default=0.42)
    parser.add_argument("--max-ellipse-residual", type=float, default=0.30)
    parser.add_argument("--close-kernel", type=int, default=3)
    parser.add_argument(
        "--allow-recovered",
        action="store_true",
        help="allow zero-filled RECOVERED frames (not recommended for calibration)",
    )
    return parser


def _validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.width <= 0 or args.height <= 0:
        parser.error("--width and --height must be positive")
    if args.columns < 2 or args.rows < 2:
        parser.error("--columns and --rows must both be at least 2")
    if args.spacing_mm <= 0 or args.dot_diameter_mm <= 0:
        parser.error("physical dimensions must be positive")
    if args.min_views < 6:
        parser.error("--min-views must be at least 6")
    if not 60.0 <= args.fov_deg < 179.0:
        parser.error("--fov-deg must be in [60, 179)")
    if args.close_kernel < 1:
        parser.error("--close-kernel must be positive")
    if (args.fisheye_fix_k3_k4 or args.fisheye_fix_k4) and args.model != "fisheye":
        parser.error("fisheye distortion constraints require --model fisheye")


def run(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any]]:
    paths = expand_input_paths(args.inputs)
    if not paths:
        raise ValueError("no .pgm or .raw inputs found")
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
    records: list[ViewRecord] = []
    for path in paths:
        record = ViewRecord(path=path)
        records.append(record)
        preflight_reason = _preflight_rejection(path, args.allow_recovered)
        if preflight_reason is not None:
            record.reason = preflight_reason
            continue
        try:
            record.image = load_binary_image(path, args.width, args.height)
            record.detection = detect_circle_grid(record.image, settings)
        except (OSError, ValueError, cv2.error) as exc:
            record.reason = f"load_or_detection_error:{exc}"
            continue
        record.reason = record.detection.reason

    report_path = args.report or args.output.with_suffix(".views.csv")
    object_points = make_object_points(settings, args.spacing_mm)
    try:
        fit, active, outliers = calibrate_with_outlier_rejection(
            records,
            object_points,
            (args.width, args.height),
            args.model,
            args.min_views,
            args.max_view_rmse_px,
            args.fov_deg,
            args.fisheye_fix_k3_k4,
            args.fisheye_fix_k4,
        )
    except (ValueError, cv2.error):
        # Detection failures need to remain actionable even when there are too
        # few valid views to solve intrinsics.
        write_view_report(report_path, records)
        _write_all_diagnostics(args, records)
        raise
    document = build_calibration_document(
        fit,
        records,
        active,
        outliers,
        settings,
        (args.width, args.height),
        args.model,
        args.camera_id,
        args.spacing_mm,
        args.dot_diameter_mm,
        args.fisheye_fix_k3_k4,
        args.fisheye_fix_k4,
    )
    _write_json_atomic(args.output, document)
    write_view_report(report_path, records)
    _write_all_diagnostics(args, records)
    return args.output, report_path, document


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_arguments(parser, args)
    try:
        output_path, report_path, document = run(args)
    except (ValueError, cv2.error) as exc:
        print(f"calibration failed: {exc}", file=sys.stderr)
        return 2
    quality = document["quality"]
    print(f"calibration: {quality['status']}")
    print(
        f"accepted/rejected: {quality['accepted_images']}/{quality['rejected_images']}  "
        f"RMS: {quality['rms_px']:.4f}px  max-view: {quality['max_view_rmse_px']:.4f}px"
    )
    print(f"config: {output_path}")
    print(f"per-view report: {report_path}")
    for warning in quality["warnings"]:
        print(f"warning: {warning}")
    return 0 if quality["status"] != "unacceptable" else 3


if __name__ == "__main__":
    raise SystemExit(main())
