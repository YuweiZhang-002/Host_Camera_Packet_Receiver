import copy
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from taxi_receiver.binary_calibration import (
    CalibrationFit,
    DetectionResult,
    DetectorSettings,
    ViewRecord,
    asymmetric_object_points,
    build_calibration_document,
    fit_calibration,
)
from taxi_receiver.extrinsic_config import (
    ExtrinsicValidationError,
    intrinsic_point_set,
    validate_intrinsic_pair,
)
from taxi_receiver.extrinsic_validation import (
    _validated_point_indices,
    select_point_order_by_pnp,
)
from taxi_receiver import stereo_calibration
from taxi_receiver.stereo_calibration import StereoPairRecord, analyse_pairs


def _intrinsic(camera_id: int) -> dict:
    return {
        "schema": "taxi_receiver.camera_calibration/1",
        "camera_id": camera_id,
        "model": "opencv_fisheye",
        "image_size": {"width": 640, "height": 480},
        "K": [[430.0, 0.0, 320.0], [0.0, 432.0, 240.0], [0.0, 0.0, 1.0]],
        "dist_coeffs": [0.03, -0.01, 0.002, -0.0002],
        "dist_coeff_order": ["k1", "k2", "k3", "k4"],
        "pattern": {
            "type": "asymmetric_circles",
            "columns": 4,
            "rows": 11,
            "base_spacing_mm": 20.0,
            "used_point_count": 44,
            "excluded_point_indices": [],
        },
        "quality": {
            "status": "acceptable",
            "rms_px": 0.4,
            "accepted_images": 20,
            "views": [],
        },
    }


def _write_intrinsic(path, document: dict) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def test_t1_identical_full44_intrinsic_point_sets_pass(tmp_path) -> None:
    cam0 = _intrinsic(0)
    cam1 = _intrinsic(1)
    cam0_path = tmp_path / "cam0.json"
    cam1_path = tmp_path / "cam1.json"
    _write_intrinsic(cam0_path, cam0)
    _write_intrinsic(cam1_path, cam1)

    loaded0, *_middle, loaded1, _K1, _D1 = validate_intrinsic_pair(
        cam0_path, cam1_path
    )

    assert intrinsic_point_set(loaded0) == frozenset(range(44))
    assert intrinsic_point_set(loaded1) == frozenset(range(44))


def test_t2_different_intrinsic_point_sets_list_the_difference(tmp_path) -> None:
    cam0 = _intrinsic(0)
    cam1 = _intrinsic(1)
    cam1["pattern"]["used_point_count"] = 43
    cam1["pattern"]["excluded_point_indices"] = [26]
    cam0_path = tmp_path / "cam0.json"
    cam1_path = tmp_path / "cam1.json"
    _write_intrinsic(cam0_path, cam0)
    _write_intrinsic(cam1_path, cam1)

    with pytest.raises(
        ExtrinsicValidationError,
        match=r"cam0-only indices=\[26\].*cam1-only indices=\[\]",
    ):
        validate_intrinsic_pair(cam0_path, cam1_path)


def test_t3_used_count_must_match_explicit_used_indices() -> None:
    document = _intrinsic(0)
    document["pattern"].pop("excluded_point_indices")
    document["pattern"]["used_point_count"] = 43
    document["pattern"]["used_point_indices"] = list(range(44))

    with pytest.raises(ExtrinsicValidationError, match="contains 44 indices"):
        intrinsic_point_set(document)


def test_t4_partial_count_without_indices_is_ambiguous() -> None:
    document = copy.deepcopy(_intrinsic(0))
    document["pattern"].pop("excluded_point_indices")
    document["pattern"]["used_point_count"] = 43

    with pytest.raises(ExtrinsicValidationError, match="点集不明确"):
        intrinsic_point_set(document)


def test_full_count_without_index_fields_means_the_full_grid() -> None:
    document = _intrinsic(0)
    document["pattern"].pop("excluded_point_indices")

    assert intrinsic_point_set(document) == frozenset(range(44))


def test_generated_intrinsic_declares_the_full_point_set() -> None:
    centers = np.asarray(
        [
            [80.0 + 60.0 * (index % 4), 40.0 + 35.0 * (index // 4)]
            for index in range(44)
        ],
        dtype=np.float32,
    )
    detection = DetectionResult(
        found=True,
        reason="accepted_for_calibration",
        centers=centers,
        candidates=[],
        metrics={"nearest_neighbor_spacing_px": 35.0},
    )
    record = ViewRecord(
        path=Path("frame1.pgm"),
        detection=detection,
        accepted=True,
        reason="accepted",
        reprojection_rmse_px=0.2,
    )
    fit = CalibrationFit(
        rms_px=0.2,
        K=np.asarray(
            [[430.0, 0.0, 320.0], [0.0, 432.0, 240.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        ),
        D=np.zeros((4, 1), dtype=np.float64),
        rvecs=[np.zeros((3, 1), dtype=np.float64)],
        tvecs=[np.asarray([[0.0], [0.0], [500.0]], dtype=np.float64)],
        per_view_rmse_px=[0.2],
    )

    document = build_calibration_document(
        fit,
        [record],
        [0],
        [],
        DetectorSettings(columns=4, rows=11),
        (640, 480),
        "fisheye",
        1,
        20.0,
        10.0,
        True,
    )

    assert document["pattern"]["used_point_count"] == 44
    assert document["pattern"]["excluded_point_indices"] == []
    assert intrinsic_point_set(document) == frozenset(range(44))
    assert document["solver_constraints"] == {
        "fixed_distortion_coefficients": ["k3", "k4"],
        "free_distortion_coefficients": ["k1", "k2"],
    }


@pytest.mark.parametrize(
    ("constraint", "expect_k3"),
    [
        ({"fisheye_fix_k3_k4": True}, True),
        ({"fisheye_fix_k4": True}, False),
    ],
)
def test_constrained_fisheye_passes_fix_flags_to_opencv(
    monkeypatch, constraint, expect_k3
) -> None:
    objects = asymmetric_object_points(4, 11, 20.0)
    K = np.asarray(
        [[430.0, 0.0, 320.0], [0.0, 432.0, 240.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    D = np.zeros((4, 1), dtype=np.float64)
    rvec = np.asarray([[0.08], [-0.12], [0.02]], dtype=np.float64)
    tvec = np.asarray([[-60.0], [-90.0], [600.0]], dtype=np.float64)
    points, _ = cv2.fisheye.projectPoints(
        objects.reshape(1, -1, 3), rvec, tvec, K, D
    )
    observed_flags: list[int] = []

    def fake_calibrate(object_sets, image_sets, image_size, K0, D0, *, flags, criteria):
        observed_flags.append(flags)
        return 0.0, K.copy(), D.copy(), [rvec.copy()], [tvec.copy()]

    monkeypatch.setattr(cv2.fisheye, "calibrate", fake_calibrate)
    fit = fit_calibration(
        [points.reshape(-1, 2)],
        objects,
        (640, 480),
        "fisheye",
        **constraint,
    )

    fix_k3 = int(getattr(cv2.fisheye, "CALIB_FIX_K3", 1 << 6))
    fix_k4 = int(getattr(cv2.fisheye, "CALIB_FIX_K4", 1 << 7))
    assert bool(observed_flags[0] & fix_k3) is expect_k3
    assert observed_flags[0] & fix_k4
    assert fit.D.reshape(-1)[2:].tolist() == [0.0, 0.0]


def test_holdout_rejects_recorded_point_set_that_differs_from_intrinsics() -> None:
    doc0 = _intrinsic(0)
    doc1 = _intrinsic(1)
    extrinsics = {"pattern": {"used_point_indices": list(range(43))}}

    with pytest.raises(
        ExtrinsicValidationError,
        match=r"intrinsics-only indices=\[43\]",
    ):
        _validated_point_indices(extrinsics, doc0, doc1)


def test_holdout_rejects_recorded_point_indices_in_a_different_order() -> None:
    doc0 = _intrinsic(0)
    doc1 = _intrinsic(1)
    recorded = list(range(44))
    recorded[0], recorded[1] = recorded[1], recorded[0]
    extrinsics = {"pattern": {"used_point_indices": recorded}}

    with pytest.raises(ExtrinsicValidationError, match="recorded order"):
        _validated_point_indices(extrinsics, doc0, doc1)


def test_holdout_applies_identical_43_point_subset_after_ordering() -> None:
    all_objects = asymmetric_object_points(4, 11, 20.0).astype(np.float64)
    indices = tuple(index for index in range(44) if index != 26)
    objects = all_objects[list(indices)]
    K = np.asarray([[430.0, 0.0, 320.0], [0.0, 432.0, 240.0], [0.0, 0.0, 1.0]])
    D = np.asarray([0.03, -0.01, 0.002, -0.0002]).reshape(-1, 1)
    rvec = np.asarray([0.1, -0.1, 0.02]).reshape(3, 1)
    translation = np.asarray([-60.0, -90.0, 600.0]).reshape(3, 1)
    all_points, _ = cv2.fisheye.projectPoints(
        all_objects.reshape(1, -1, 3), rvec, translation, K, D
    )
    raw = all_points.reshape(-1, 2)

    selected = select_point_order_by_pnp(
        objects,
        raw,
        K,
        D,
        "opencv_fisheye",
        maximum_rmse_px=0.1,
        point_indices=indices,
    )

    assert selected is not None
    orientation, selected_points, pose = selected
    assert orientation == "normal"
    np.testing.assert_array_equal(selected_points, raw[list(indices)])
    assert pose.rmse_px < 1e-8


def test_stereo_analysis_applies_identical_43_point_subset_after_ordering(
    monkeypatch,
) -> None:
    all_objects = asymmetric_object_points(4, 11, 20.0).astype(np.float64)
    indices = tuple(index for index in range(44) if index != 26)
    objects = all_objects[list(indices)]
    K = np.asarray([[430.0, 0.0, 320.0], [0.0, 432.0, 240.0], [0.0, 0.0, 1.0]])
    D = np.asarray([0.03, -0.01, 0.002, -0.0002]).reshape(-1, 1)
    rvec = np.asarray([0.1, -0.1, 0.02]).reshape(3, 1)
    translation = np.asarray([-60.0, -90.0, 600.0]).reshape(3, 1)
    all_points, _ = cv2.fisheye.projectPoints(
        all_objects.reshape(1, -1, 3), rvec, translation, K, D
    )
    raw = all_points.reshape(-1, 2).astype(np.float32)
    detections = iter(
        (
            SimpleNamespace(found=True, centers=raw, reason="ok"),
            SimpleNamespace(found=True, centers=raw, reason="ok"),
        )
    )
    monkeypatch.setattr(stereo_calibration, "stereo_preflight_rejection", lambda _path: None)
    monkeypatch.setattr(
        stereo_calibration,
        "load_binary_image",
        lambda _path, width, height: np.zeros((height, width), dtype=np.uint8),
    )
    monkeypatch.setattr(
        stereo_calibration,
        "detect_circle_grid",
        lambda _image, _settings: next(detections),
    )
    record = StereoPairRecord("pose_000", Path("cam0.pgm"), Path("cam1.pgm"))
    doc0 = _intrinsic(0)
    doc1 = _intrinsic(1)

    analyse_pairs(
        [record],
        objects,
        indices,
        doc0,
        K,
        D,
        doc1,
        K,
        D,
        maximum_pnp_rmse_px=0.1,
    )

    candidate = next(
        item for item in record.candidates if item.orientation == "normal-normal"
    )
    np.testing.assert_array_equal(candidate.points0, raw[list(indices)])
    np.testing.assert_array_equal(candidate.points1, raw[list(indices)])
