"""Schema identity and structural validation for camera calibration documents.

Rewritten: the original file was lost from the working tree and was never
tracked by git. Only its two exports were known (``CALIBRATION_SCHEMA`` and
``validate_camera_calibration``), so the checks below are a fresh
reconstruction. They validate the document that
``binary_calibration.build_calibration_document`` emits and deliberately do not
touch the calibration mathematics.
"""
from __future__ import annotations

import math
from typing import Any

CALIBRATION_SCHEMA = "taxi_receiver.camera_calibration/1"

SUPPORTED_MODELS = frozenset({"opencv_fisheye", "opencv_pinhole_rational"})
EXPECTED_DISTORTION_COUNT = {"opencv_fisheye": 4}


class CalibrationValidationError(ValueError):
    """Raised when a calibration document is structurally unusable."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CalibrationValidationError(message)


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _validate_camera_matrix(K: Any) -> None:
    _require(isinstance(K, list) and len(K) == 3, "K must be a 3x3 nested list")
    for row in K:
        _require(isinstance(row, list) and len(row) == 3, "K must be a 3x3 nested list")
        _require(all(_finite_number(value) for value in row), "K contains a non-finite entry")

    fx, fy = float(K[0][0]), float(K[1][1])
    cx, cy = float(K[0][2]), float(K[1][2])
    _require(fx > 0.0 and fy > 0.0, f"focal lengths must be positive, got fx={fx}, fy={fy}")
    _require(
        math.isclose(float(K[2][0]), 0.0) and math.isclose(float(K[2][1]), 0.0)
        and math.isclose(float(K[2][2]), 1.0),
        "K bottom row must be [0, 0, 1]",
    )
    # A principal point outside the sensor means the solve diverged even if the
    # optimiser reported convergence.
    _require(math.isfinite(cx) and math.isfinite(cy), "principal point is not finite")


def _validate_image_size(document: dict[str, Any]) -> tuple[int, int]:
    size = document.get("image_size")
    _require(isinstance(size, dict), "image_size must be an object")
    width, height = size.get("width"), size.get("height")
    _require(
        isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0,
        "image_size.width/height must be positive integers",
    )
    return int(width), int(height)


def _validate_distortion(document: dict[str, Any], model: str) -> None:
    coefficients = document.get("dist_coeffs")
    order = document.get("dist_coeff_order")
    _require(isinstance(coefficients, list) and coefficients, "dist_coeffs must be a non-empty list")
    _require(
        all(_finite_number(value) for value in coefficients),
        "dist_coeffs contains a non-finite entry",
    )
    _require(isinstance(order, list), "dist_coeff_order must be a list")
    _require(
        len(order) == len(coefficients),
        f"dist_coeff_order has {len(order)} names for {len(coefficients)} coefficients",
    )
    expected = EXPECTED_DISTORTION_COUNT.get(model)
    _require(
        expected is None or len(coefficients) == expected,
        f"model {model} expects {expected} distortion coefficients, got {len(coefficients)}",
    )


def _validate_quality(document: dict[str, Any]) -> None:
    quality = document.get("quality")
    _require(isinstance(quality, dict), "quality must be an object")
    for key in ("status", "rms_px", "accepted_images", "views"):
        _require(key in quality, f"quality.{key} is missing")
    _require(
        quality["status"] in {"acceptable", "limited", "unacceptable"},
        f"unknown quality.status {quality['status']!r}",
    )
    _require(_finite_number(quality["rms_px"]), "quality.rms_px is not finite")
    _require(
        isinstance(quality["accepted_images"], int) and quality["accepted_images"] > 0,
        "quality.accepted_images must be a positive integer",
    )
    _require(isinstance(quality["views"], list), "quality.views must be a list")


def validate_camera_calibration(document: Any) -> dict[str, Any]:
    """Validate a calibration document in place and return it.

    Raises ``CalibrationValidationError`` (a ``ValueError``) describing the
    first structural problem found.
    """

    _require(isinstance(document, dict), "calibration document must be an object")
    _require(
        document.get("schema") == CALIBRATION_SCHEMA,
        f"schema must be {CALIBRATION_SCHEMA!r}, got {document.get('schema')!r}",
    )

    model = document.get("model")
    _require(model in SUPPORTED_MODELS, f"unsupported model {model!r}")

    camera_id = document.get("camera_id")
    _require(
        camera_id is None or (isinstance(camera_id, int) and camera_id >= 0),
        "camera_id must be a non-negative integer or null",
    )

    _validate_image_size(document)
    _validate_camera_matrix(document.get("K"))
    _validate_distortion(document, str(model))

    pattern = document.get("pattern")
    _require(isinstance(pattern, dict), "pattern must be an object")
    for key in ("type", "columns", "rows", "base_spacing_mm"):
        _require(key in pattern, f"pattern.{key} is missing")
    _require(
        isinstance(pattern["columns"], int) and isinstance(pattern["rows"], int)
        and pattern["columns"] >= 2 and pattern["rows"] >= 2,
        "pattern.columns/rows must be integers >= 2",
    )
    _require(
        _finite_number(pattern["base_spacing_mm"]) and float(pattern["base_spacing_mm"]) > 0.0,
        "pattern.base_spacing_mm must be a positive number",
    )

    _validate_quality(document)
    return document
