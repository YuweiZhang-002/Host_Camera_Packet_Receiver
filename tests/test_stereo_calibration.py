from pathlib import Path

import cv2
import numpy as np

import pytest

from taxi_receiver.binary_calibration import asymmetric_object_points
from taxi_receiver.stereo_calibration import (
    PoseEstimate,
    StereoPairRecord,
    TransformCandidate,
    _record_row,
    cross_reprojection_metrics,
    depth_drift_report,
    rotation_distance_deg,
    stereo_calibrate_fixed,
)


def _synthetic_dataset():
    object_points = asymmetric_object_points(4, 11, 20.0).astype(np.float64)
    K0 = np.asarray([[430.0, 0.0, 320.0], [0.0, 432.0, 240.0], [0.0, 0.0, 1.0]])
    D0 = np.asarray([0.03, -0.01, 0.002, -0.0002]).reshape(-1, 1)
    K1 = np.asarray([[440.0, 0.0, 319.0], [0.0, 438.0, 238.0], [0.0, 0.0, 1.0]])
    D1 = np.asarray([0.02, -0.008, 0.001, -0.0001]).reshape(-1, 1)
    expected_rvec = np.asarray([0.01, -0.025, 0.004]).reshape(3, 1)
    expected_rotation, _ = cv2.Rodrigues(expected_rvec)
    expected_translation = np.asarray([-110.0, 2.0, 4.0]).reshape(3, 1)
    records = []
    for index in range(18):
        rvec0 = np.asarray(
            [0.04 + 0.01 * (index % 4), -0.12 + 0.02 * (index % 5), -0.08 + 0.015 * index]
        ).reshape(3, 1)
        rotation0, _ = cv2.Rodrigues(rvec0)
        translation0 = np.asarray(
            [-65.0 + (index % 6) * 8.0, -95.0 + (index % 3) * 10.0, 520.0 + index * 12.0]
        ).reshape(3, 1)
        rotation1 = expected_rotation @ rotation0
        translation1 = expected_rotation @ translation0 + expected_translation
        rvec1, _ = cv2.Rodrigues(rotation1)
        points0, _ = cv2.fisheye.projectPoints(
            object_points.reshape(1, -1, 3), rvec0, translation0, K0, D0
        )
        points1, _ = cv2.fisheye.projectPoints(
            object_points.reshape(1, -1, 3), rvec1, translation1, K1, D1
        )
        pose0 = PoseEstimate(rvec0, translation0, rotation0, 0.0, 0.0)
        pose1 = PoseEstimate(rvec1, translation1, rotation1, 0.0, 0.0)
        candidate = TransformCandidate(
            points0.reshape(-1, 2),
            points1.reshape(-1, 2),
            pose0,
            pose1,
            expected_rotation,
            expected_translation,
            "normal-normal",
        )
        records.append(
            StereoPairRecord(
                str(index), Path("cam0.pgm"), Path("cam1.pgm"), selected=candidate, accepted=True
            )
        )
    return object_points, K0, D0, K1, D1, expected_rotation, expected_translation, records


def test_fixed_intrinsic_stereo_recovers_cam0_to_cam1_transform() -> None:
    obj, K0, D0, K1, D1, expected_R, expected_T, records = _synthetic_dataset()
    frozen = [array.copy() for array in (K0, D0, K1, D1)]

    rms, rotation, translation = stereo_calibrate_fixed(
        obj, records, K0, D0, K1, D1, (640, 480)
    )

    assert rms < 1e-8
    assert rotation_distance_deg(rotation, expected_R) < 1e-7
    assert np.linalg.norm(translation - expected_T) < 1e-7
    for before, after in zip(frozen, (K0, D0, K1, D1), strict=True):
        np.testing.assert_array_equal(before, after)


def test_cross_reprojection_is_zero_for_correct_direction() -> None:
    obj, K0, D0, K1, D1, rotation, translation, records = _synthetic_dataset()
    first = records[0].selected

    metrics = cross_reprojection_metrics(
        obj, first.points0, first.points1, K0, D0, K1, D1, rotation, translation
    )

    assert metrics is not None
    assert metrics["bidirectional_rmse_px"] < 1e-8


def _drifted_records(slope_mm_per_mm: float):
    """Rebuild the synthetic set with tz proportional to board depth.

    A rigid rig cannot do this; only a model error (biased intrinsics, an
    exhausted distortion domain, a focal scale mismatch) can.
    """
    *_, records = _synthetic_dataset()
    depths = [float(record.selected.pose0.tvec.reshape(3)[2]) for record in records]
    origin = sum(depths) / len(depths)
    for record, depth in zip(records, depths, strict=True):
        drift = np.zeros((3, 1))
        drift[2, 0] = slope_mm_per_mm * (depth - origin)
        record.selected.translation = record.selected.translation + drift
    return records


def test_depth_drift_passes_for_a_rigid_rig() -> None:
    *_, records = _synthetic_dataset()

    report = depth_drift_report(
        records, correlation_limit=0.3, slope_limit_mm_per_mm=0.005
    )

    assert report["status"] == "pass"
    assert report["failures"] == []
    assert report["board_depth_mm"]["span"] > 1.0
    for axis in report["axes"].values():
        assert abs(axis["slope_mm_per_mm"]) < 1e-9


def test_depth_drift_fails_when_translation_tracks_board_depth() -> None:
    records = _drifted_records(0.03)

    report = depth_drift_report(
        records, correlation_limit=0.3, slope_limit_mm_per_mm=0.005
    )

    assert report["status"] == "fail"
    assert report["axes"]["z"]["drifting"] is True
    assert report["axes"]["z"]["slope_mm_per_mm"] == pytest.approx(0.03, abs=1e-9)
    assert abs(report["axes"]["z"]["correlation"]) > 0.99
    assert any("tz drifts with board depth" in item for item in report["failures"])


def test_depth_drift_and_rule_ignores_a_correlated_but_negligible_slope() -> None:
    # Correlation alone is not evidence: its null standard error is
    # 1/sqrt(n-3), so a bare |r| > 0.3 test misfires on clean data.  A perfect
    # correlation with a 0.001 mm/mm slope is 0.2mm of drift over the whole
    # depth span and must not fail the default rule.
    records = _drifted_records(0.001)

    strict = depth_drift_report(
        records, correlation_limit=0.3, slope_limit_mm_per_mm=0.005, rule="or"
    )
    default = depth_drift_report(
        records, correlation_limit=0.3, slope_limit_mm_per_mm=0.005, rule="and"
    )

    assert abs(strict["axes"]["z"]["correlation"]) > 0.99
    assert strict["status"] == "fail"
    assert default["status"] == "pass"


def test_depth_drift_reports_a_degenerate_depth_span() -> None:
    *_, records = _synthetic_dataset()
    for record in records:
        record.selected.pose0.tvec[2, 0] = 600.0

    report = depth_drift_report(
        records, correlation_limit=0.3, slope_limit_mm_per_mm=0.005
    )

    assert report["status"] == "degenerate_depth_span"
    assert report["failures"] == []


def test_depth_drift_needs_at_least_three_pairs() -> None:
    *_, records = _synthetic_dataset()

    report = depth_drift_report(
        records[:2], correlation_limit=0.3, slope_limit_mm_per_mm=0.005
    )

    assert report["status"] == "insufficient_pairs"
    assert report["failures"] == []


def test_depth_drift_rejects_an_unknown_rule() -> None:
    *_, records = _synthetic_dataset()

    with pytest.raises(ValueError, match="rule must be"):
        depth_drift_report(
            records, correlation_limit=0.3, slope_limit_mm_per_mm=0.005, rule="xor"
        )


DEPTH_DIAGNOSTIC_COLUMNS = (
    "board_depth_cam0_mm",
    "board_depth_cam1_mm",
    "depth_ratio_cam1_over_cam0",
    "relative_tx_mm",
    "relative_ty_mm",
    "relative_tz_mm",
)


def test_pair_row_carries_board_depth_and_relative_translation() -> None:
    *_, expected_translation, records = _synthetic_dataset()
    record = records[0]
    candidate = record.selected

    row = _record_row(record)

    assert row["board_depth_cam0_mm"] == pytest.approx(
        float(candidate.pose0.tvec.reshape(3)[2])
    )
    assert row["board_depth_cam1_mm"] == pytest.approx(
        float(candidate.pose1.tvec.reshape(3)[2])
    )
    assert row["depth_ratio_cam1_over_cam0"] == pytest.approx(
        row["board_depth_cam1_mm"] / row["board_depth_cam0_mm"]
    )
    for index, axis in enumerate("xyz"):
        assert row[f"relative_t{axis}_mm"] == pytest.approx(
            float(expected_translation.reshape(3)[index])
        )


def test_pair_row_emits_every_depth_column_even_without_a_pose() -> None:
    # _record_row runs over the whole manifest, including preflight rejects and
    # detection failures whose `selected` stayed None.  write_csv_rows uses a
    # DictWriter, so a row that omits a key raises instead of writing a blank.
    unsolved = StereoPairRecord("pose_x", Path("cam0.pgm"), Path("cam1.pgm"))
    assert unsolved.selected is None

    row = _record_row(unsolved)

    for column in DEPTH_DIAGNOSTIC_COLUMNS:
        assert column in row
        assert row[column] is None


def test_pair_row_column_set_is_identical_with_and_without_a_pose() -> None:
    *_, records = _synthetic_dataset()
    unsolved = StereoPairRecord("pose_x", Path("cam0.pgm"), Path("cam1.pgm"))

    assert _record_row(records[0]).keys() == _record_row(unsolved).keys()
