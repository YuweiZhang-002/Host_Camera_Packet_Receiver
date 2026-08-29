import copy

import numpy as np
import pytest

from taxi_receiver.extrinsic_config import (
    ExtrinsicValidationError,
    STEREO_EXTRINSICS_SCHEMA,
    TRANSFORM_CONVENTION,
    fisheye_domain_report,
    validate_stereo_extrinsics,
)


def _document() -> dict:
    return {
        "schema": STEREO_EXTRINSICS_SCHEMA,
        "reference_camera_id": 0,
        "target_camera_id": 1,
        "transform_convention": TRANSFORM_CONVENTION,
        "translation_unit": "mm",
        "R_cam1_from_cam0": np.eye(3).tolist(),
        "t_cam1_from_cam0_mm": [-100.0, 0.0, 0.0],
        "intrinsics": {
            "cam0": {
                "camera_id": 0,
                "sha256": "A" * 64,
                "schema": "taxi_receiver.camera_calibration/1",
                "model": "opencv_fisheye",
                "image_size": {"width": 640, "height": 480},
            },
            "cam1": {
                "camera_id": 1,
                "sha256": "B" * 64,
                "schema": "taxi_receiver.camera_calibration/1",
                "model": "opencv_fisheye",
                "image_size": {"width": 640, "height": 480},
            },
        },
        "pattern": {
            "type": "asymmetric_circles",
            "columns": 4,
            "rows": 11,
            "base_spacing_mm": 20.0,
            "used_point_indices": list(range(44)),
        },
        "pairing": {
            "manifest_path": "training/pairs.csv",
            "manifest_sha256": "C" * 64,
            "pairing_summary_path": "training/pairing_summary.json",
            "pairing_summary_sha256": "D" * 64,
            "dataset_root": "training/capture",
            "stillness_config_path": "training/stillness_config.json",
            "stillness_config_sha256": "E" * 64,
            "training_pose_ids": [f"pose_{index:03d}" for index in range(20)],
            "training_images_sha256": [f"{index + 1:064X}" for index in range(40)],
        },
        "solver": {
            "backend": "cv2.fisheye.stereoCalibrate",
            "flags": ["CALIB_FIX_INTRINSIC", "CALIB_CHECK_COND"],
            "intrinsics_unchanged": True,
        },
        "quality": {
            "status": "acceptable",
            "accepted_pairs": 20,
            "stereo_rms_px": 0.4,
            "baseline_mm": 100.0,
        },
    }


def test_extrinsic_schema_accepts_rigid_cam0_to_cam1_transform() -> None:
    assert validate_stereo_extrinsics(_document())["translation_unit"] == "mm"


@pytest.mark.parametrize(
    "mutation", ["reflection", "zero_baseline", "wrong_convention", "missing_pairing"]
)
def test_extrinsic_schema_rejects_unsafe_transform(mutation: str) -> None:
    document = copy.deepcopy(_document())
    if mutation == "reflection":
        document["R_cam1_from_cam0"][0][0] = -1.0
    elif mutation == "zero_baseline":
        document["t_cam1_from_cam0_mm"] = [0.0, 0.0, 0.0]
    elif mutation == "wrong_convention":
        document["transform_convention"] = "ambiguous"
    else:
        document.pop("pairing")
    with pytest.raises(ExtrinsicValidationError):
        validate_stereo_extrinsics(document)


def test_cam0_release_coefficients_report_small_monotonic_margin() -> None:
    document = {"image_size": {"width": 640, "height": 480}}
    K = np.asarray(
        [[945.3020361536, 0.0, 296.4320214626], [0.0, 940.2659220333, 243.2537145454], [0.0, 0.0, 1.0]]
    )
    D = np.asarray(
        [-0.06456621017, -0.63557032371, 10.80030435400, -50.87206047800]
    ).reshape(-1, 1)

    report = fisheye_domain_report(document, K, D)

    assert report["status"] == "warning"
    assert 2.0 < report["monotonic_margin_deg"] < 3.0
    assert report["finite_undistort_grid"]
