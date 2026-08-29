from pathlib import Path

import cv2
import numpy as np

from taxi_receiver.binary_calibration import CalibrationFit, DetectionResult, ViewRecord
from taxi_receiver.calibration_refill import build_pose_clusters, refill_rejected_poses


def _record(name: str, center_x: float, *, found: bool = True) -> ViewRecord:
    grid = np.asarray(
        [[center_x + (index % 4) * 12.0, 80.0 + (index // 4) * 12.0]
         for index in range(44)],
        dtype=np.float32,
    )
    detection = DetectionResult(
        found=found,
        reason="accepted_for_calibration" if found else "grid_not_found",
        centers=grid if found else None,
        candidates=[],
        metrics={
            "candidate_count": 44,
            "nearest_neighbor_spacing_px": 12.0,
            "max_concentric_center_spread_px": 0.1,
            "median_ellipse_residual": 0.05,
            "median_arc_coverage": 0.95,
        },
    )
    return ViewRecord(path=Path(name), detection=detection, reason=detection.reason)


def _fit(count: int) -> CalibrationFit:
    return CalibrationFit(
        rms_px=0.2,
        K=np.eye(3, dtype=np.float64),
        D=np.zeros((4, 1), dtype=np.float64),
        rvecs=[np.zeros((3, 1), dtype=np.float64) for _ in range(count)],
        tvecs=[np.zeros((3, 1), dtype=np.float64) for _ in range(count)],
        per_view_rmse_px=[0.2] * count,
    )


def test_pose_clusters_keep_backups_and_use_natural_frame_order() -> None:
    records = [
        _record("frame10.pgm", 40.0),
        _record("frame2.pgm", 40.0),
        _record("frame20.pgm", 140.0),
        _record("frame21.pgm", 140.0, found=False),
    ]

    clusters = build_pose_clusters(records, 640, 480)

    assert len(clusters) == 2
    assert [item.path.name for item in clusters[0].candidates] == [
        "frame2.pgm",
        "frame10.pgm",
    ]
    assert [item.path.name for item in clusters[1].candidates] == ["frame20.pgm"]


def test_rejected_pose_is_refilled_and_never_enters_final_records() -> None:
    records = [
        _record("frame1.pgm", 40.0),
        _record("frame2.pgm", 40.0),
        _record("frame3.pgm", 140.0),
        _record("frame4.pgm", 240.0),
    ]
    clusters = build_pose_clusters(records, 640, 480)
    calls = 0

    def solve(selected, *_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            selected[0].reason = "reprojection_outlier"
            selected[0].accepted = False
            for record in selected[1:]:
                record.accepted = True
                record.reason = "accepted"
            return _fit(len(selected)), list(range(1, len(selected))), []
        for record in selected:
            record.accepted = True
            record.reason = "accepted"
            record.reprojection_rmse_px = 0.2
        return _fit(len(selected)), list(range(len(selected))), []

    result = refill_rejected_poses(
        clusters,
        np.zeros((44, 3), dtype=np.float32),
        (640, 480),
        "opencv_fisheye",
        min_views=3,
        min_final_poses=3,
        max_view_rmse_px=1.2,
        fov_degrees=180.0,
        max_refill_rounds=3,
        max_candidates_per_pose=2,
        solve=solve,
    )

    assert result.rounds == 2
    assert [record.path.name for record in result.records] == [
        "frame2.pgm",
        "frame3.pgm",
        "frame4.pgm",
    ]
    assert all(record.accepted for record in result.records)
    assert any(
        row["path"].endswith("frame1.pgm") and not row["accepted_this_round"]
        for row in result.attempts
    )


def test_ill_conditioned_solver_error_advances_only_named_input() -> None:
    records = [
        _record("frame1.pgm", 40.0),
        _record("frame2.pgm", 40.0),
        _record("frame3.pgm", 140.0),
        _record("frame4.pgm", 140.0),
        _record("frame5.pgm", 240.0),
        _record("frame6.pgm", 240.0),
    ]
    clusters = build_pose_clusters(records, 640, 480)
    calls = 0

    def solve(selected, *_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise cv2.error("calibrate: ill-conditioned input array 1")
        for record in selected:
            record.accepted = True
            record.reason = "accepted"
        return _fit(len(selected)), list(range(len(selected))), []

    result = refill_rejected_poses(
        clusters,
        np.zeros((44, 3), dtype=np.float32),
        (640, 480),
        "opencv_fisheye",
        min_views=3,
        min_final_poses=3,
        max_view_rmse_px=1.2,
        fov_degrees=180.0,
        max_refill_rounds=3,
        max_candidates_per_pose=2,
        solve=solve,
    )

    assert [cluster.cursor for cluster in result.clusters] == [0, 1, 0]
    assert [record.path.name for record in result.records] == [
        "frame1.pgm",
        "frame4.pgm",
        "frame5.pgm",
    ]
    first_round = [row for row in result.attempts if row["round"] == 1]
    assert [row["accepted_this_round"] for row in first_round] == [None, False, None]
