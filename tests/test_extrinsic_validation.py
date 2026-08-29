from pathlib import Path

import cv2
import numpy as np
import pytest

from taxi_receiver.binary_calibration import asymmetric_object_points
from taxi_receiver.extrinsic_validation import (
    _require_publishable_extrinsics,
    rectified_vertical_metrics,
    select_point_order_by_pnp,
    stereo_rectification,
    validate_holdout_isolation,
)


def test_holdout_refuses_extrinsics_that_failed_their_own_gate() -> None:
    document = {"quality": {"status": "limited", "failures": ["tz drifts with board depth"]}}

    with pytest.raises(ValueError, match="not 'acceptable'"):
        _require_publishable_extrinsics(
            document, Path("cam0_to_cam1_extrinsics.json"), allow_limited=False
        )


def test_holdout_accepts_extrinsics_that_cleared_their_gate() -> None:
    document = {"quality": {"status": "acceptable", "failures": []}}

    _require_publishable_extrinsics(
        document, Path("cam0_to_cam1_extrinsics.json"), allow_limited=False
    )


def test_holdout_override_admits_a_limited_solve() -> None:
    document = {"quality": {"status": "limited", "failures": []}}

    _require_publishable_extrinsics(
        document, Path("cam0_to_cam1_extrinsics.json"), allow_limited=True
    )


def test_correct_fisheye_rectification_has_zero_vertical_disparity() -> None:
    object_points = asymmetric_object_points(4, 11, 20.0).astype(np.float64)
    K0 = np.asarray([[430.0, 0.0, 320.0], [0.0, 432.0, 240.0], [0.0, 0.0, 1.0]])
    D0 = np.asarray([0.03, -0.01, 0.002, -0.0002]).reshape(-1, 1)
    K1 = np.asarray([[440.0, 0.0, 319.0], [0.0, 438.0, 238.0], [0.0, 0.0, 1.0]])
    D1 = np.asarray([0.02, -0.008, 0.001, -0.0001]).reshape(-1, 1)
    rotation, _ = cv2.Rodrigues(np.asarray([0.01, -0.025, 0.004]))
    translation = np.asarray([-110.0, 2.0, 4.0]).reshape(3, 1)
    rvec0 = np.asarray([0.1, -0.1, 0.02]).reshape(3, 1)
    rotation0, _ = cv2.Rodrigues(rvec0)
    translation0 = np.asarray([-60.0, -90.0, 600.0]).reshape(3, 1)
    rotation1 = rotation @ rotation0
    translation1 = rotation @ translation0 + translation
    rvec1, _ = cv2.Rodrigues(rotation1)
    points0, _ = cv2.fisheye.projectPoints(
        object_points.reshape(1, -1, 3), rvec0, translation0, K0, D0
    )
    points1, _ = cv2.fisheye.projectPoints(
        object_points.reshape(1, -1, 3), rvec1, translation1, K1, D1
    )
    rectification = stereo_rectification(
        K0, D0, K1, D1, (640, 480), rotation, translation, 0.0
    )

    metrics = rectified_vertical_metrics(
        points0.reshape(-1, 2), points1.reshape(-1, 2), K0, D0, K1, D1, rectification
    )

    assert metrics["maximum_px"] < 1e-8


def test_holdout_point_order_is_selected_only_by_single_camera_pnp() -> None:
    object_points = asymmetric_object_points(4, 11, 20.0).astype(np.float64)
    K = np.asarray([[430.0, 0.0, 320.0], [0.0, 432.0, 240.0], [0.0, 0.0, 1.0]])
    D = np.asarray([0.03, -0.01, 0.002, -0.0002]).reshape(-1, 1)
    rvec = np.asarray([0.1, -0.1, 0.02]).reshape(3, 1)
    translation = np.asarray([-60.0, -90.0, 600.0]).reshape(3, 1)
    points, _ = cv2.fisheye.projectPoints(
        object_points.reshape(1, -1, 3), rvec, translation, K, D
    )

    selected = select_point_order_by_pnp(
        object_points,
        points.reshape(-1, 2),
        K,
        D,
        "opencv_fisheye",
        maximum_rmse_px=0.1,
    )

    assert selected is not None
    orientation, selected_points, pose = selected
    assert orientation == "normal"
    np.testing.assert_array_equal(selected_points, points.reshape(-1, 2))
    assert pose.rmse_px < 1e-8


def test_holdout_rejects_training_manifest_and_capture_session(tmp_path: Path) -> None:
    extrinsics = {
        "pairing": {
            "manifest_sha256": "A" * 64,
            "dataset_root": str(tmp_path / "training_capture"),
        }
    }
    independent = {"dataset_root": str(tmp_path / "holdout_capture")}

    with pytest.raises(ValueError, match="identical to the training manifest"):
        validate_holdout_isolation(extrinsics, independent, "A" * 64)

    with pytest.raises(ValueError, match="same capture session"):
        validate_holdout_isolation(
            extrinsics,
            {"dataset_root": str(tmp_path / "training_capture")},
            "B" * 64,
        )

    assert validate_holdout_isolation(extrinsics, independent, "B" * 64)[0] == "A" * 64
