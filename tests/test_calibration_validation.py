import json
from pathlib import Path

import cv2
import numpy as np

from taxi_receiver.binary_calibration import DetectionResult, asymmetric_object_points
from taxi_receiver.calibration_validation import (
    ValidationRecord,
    _original_basename,
    build_validation_summary,
    evaluate_fixed_calibration,
    read_excluded_views,
)


def test_read_excluded_views_maps_exported_pose_name_to_original(tmp_path: Path) -> None:
    report = tmp_path / "views.csv"
    report.write_text(
        "path,accepted\nC:/selected/pose005_1040.pgm,True\n"
        "C:/selected/pose006_frame52.pgm,True\n",
        encoding="utf-8",
    )

    excluded = read_excluded_views([report])

    assert "pose005_1040.pgm" in excluded
    assert "1040.pgm" in excluded
    assert "52.pgm" in excluded
    assert _original_basename("plain.pgm") == "plain.pgm"


def test_fixed_fisheye_calibration_recovers_projected_holdout_pose() -> None:
    object_points = asymmetric_object_points(4, 11, 20.0)
    K = np.asarray(
        [[430.0, 0.0, 320.0], [0.0, 435.0, 240.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    D = np.asarray([0.05, -0.01, 0.002, -0.0002], dtype=np.float64).reshape(-1, 1)
    rvec = np.asarray([0.12, -0.18, 0.04], dtype=np.float64).reshape(3, 1)
    tvec = np.asarray([-55.0, -95.0, 520.0], dtype=np.float64).reshape(3, 1)
    centers, _ = cv2.fisheye.projectPoints(
        object_points.reshape(1, -1, 3), rvec, tvec, K, D
    )
    detection = DetectionResult(
        found=True,
        reason="accepted_for_calibration",
        centers=centers.reshape(-1, 2).astype(np.float32),
        candidates=[],
        metrics={"nearest_neighbor_spacing_px": 15.0},
    )
    record = ValidationRecord(path=Path("holdout.pgm"), detection=detection)

    evaluate_fixed_calibration(record, object_points, K, D, "opencv_fisheye")

    assert record.reason == "evaluated"
    assert record.reprojection_rmse_px is not None
    assert record.reprojection_rmse_px < 1e-3


def test_validation_summary_gates_are_independent_of_training_status(
    tmp_path: Path,
) -> None:
    calibration_path = tmp_path / "calibration.json"
    calibration_path.write_text(json.dumps({"fixture": True}), encoding="utf-8")
    document = {
        "schema": "taxi_receiver.camera_calibration/1",
        "model": "opencv_fisheye",
        "camera_id": 1,
        "image_size": {"width": 640, "height": 480},
    }
    selected = []
    for index, error in enumerate([0.4, 0.5, 0.6, 0.7, 0.8]):
        record = ValidationRecord(path=Path(f"holdout{index}.pgm"))
        record.reprojection_rmse_px = error
        selected.append(record)

    passed = build_validation_summary(
        calibration_path,
        document,
        selected,
        selected,
        min_holdout_views=5,
        median_limit=0.8,
        p95_limit=1.2,
        maximum_limit=1.5,
        required_pass_fraction=0.9,
    )
    failed = build_validation_summary(
        calibration_path,
        document,
        selected,
        selected,
        min_holdout_views=6,
        median_limit=0.8,
        p95_limit=1.2,
        maximum_limit=1.5,
        required_pass_fraction=0.9,
    )

    assert passed["status"] == "pass"
    assert failed["status"] == "fail"
    assert "only 5 holdout views" in failed["quality"]["failures"][0]
