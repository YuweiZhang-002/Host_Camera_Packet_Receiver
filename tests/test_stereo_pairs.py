import csv
import json
from pathlib import Path

import numpy as np
import pytest

from taxi_receiver.extrinsic_config import sha256_file
from taxi_receiver.stereo_pairs import (
    discover_observations,
    FrameObservation,
    FrameTiming,
    load_pairing_provenance,
    mark_motion_rates,
    mark_still_frames,
    match_quasi_static_frames,
    match_still_frames,
    read_rows_timings,
    read_accepted_pair_manifest,
    select_pose_episodes,
    select_quasi_static_episode_minima,
    select_quasi_static_local_minima,
    select_distinct_pose_episodes,
    verify_frozen_stillness_provenance,
    write_csv_rows,
)


def _observation(cam: int, frame: int, timestamp: float, shift: float) -> FrameObservation:
    points = np.asarray(
        [[20.0 + column * 10.0 + shift, 30.0 + row * 10.0]
         for row in range(11) for column in range(4)],
        dtype=np.float64,
    )
    return FrameObservation(
        cam,
        frame,
        Path(f"cam{cam}/{frame}.pgm"),
        FrameTiming(timestamp - 0.03, timestamp + 0.03, timestamp, "test"),
        points,
        "accepted_for_calibration",
    )


def _write_rows_timing_csv(camera_root: Path, rows: list[dict[str, object]]) -> None:
    camera_root.mkdir(parents=True, exist_ok=True)
    fields = (
        "timestamp",
        "cam_id",
        "frame_id",
        "row_accepted",
        "reliable_first",
        "reliable_last",
    )
    with (camera_root / "rows_v2.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_rows_timing_fallback_rejects_nonfinite_and_nonpositive_times(
    tmp_path: Path,
) -> None:
    camera_root = tmp_path / "cam0"
    rows: list[dict[str, object]] = []
    for frame_id, invalid_timestamp in enumerate(("0", "-1", "nan", "inf")):
        rows.extend(
            (
                {
                    "timestamp": invalid_timestamp,
                    "cam_id": 0,
                    "frame_id": frame_id,
                    "row_accepted": 1,
                    "reliable_first": 1,
                    "reliable_last": 0,
                },
                {
                    "timestamp": 100.0 + frame_id,
                    "cam_id": 0,
                    "frame_id": frame_id,
                    "row_accepted": 1,
                    "reliable_first": 0,
                    "reliable_last": 1,
                },
            )
        )
    _write_rows_timing_csv(camera_root, rows)

    assert read_rows_timings(camera_root, 0) == {}


def test_discovery_uses_valid_rows_timing_when_sidecar_timing_is_missing(
    tmp_path: Path,
) -> None:
    camera_root = tmp_path / "cam0"
    (camera_root / "7.pgm").parent.mkdir(parents=True)
    (camera_root / "7.pgm").write_bytes(b"P5\n1 1\n255\n\x00")
    _write_rows_timing_csv(
        camera_root,
        [
            {
                "timestamp": 1000.0,
                "cam_id": 0,
                "frame_id": 7,
                "row_accepted": 1,
                "reliable_first": 1,
                "reliable_last": 0,
            },
            {
                "timestamp": 1000.1,
                "cam_id": 0,
                "frame_id": 7,
                "row_accepted": 1,
                "reliable_first": 0,
                "reliable_last": 1,
            },
        ],
    )

    observations = discover_observations(tmp_path, 0)

    assert len(observations) == 1
    assert observations[0].timing is not None
    assert observations[0].timing.source == "rows_v2.csv"
    assert observations[0].timing.start == 1000.0
    assert observations[0].timing.end == 1000.1


def test_discovery_does_not_parse_bad_rows_csv_when_all_sidecars_have_timing(
    tmp_path: Path,
) -> None:
    camera_root = tmp_path / "cam0"
    camera_root.mkdir()
    (camera_root / "7.pgm").write_bytes(b"P5\n1 1\n255\n\x00")
    (camera_root / "7.json").write_text(
        json.dumps(
            {
                "capture_started_at": 1000.0,
                "capture_ended_at": 1000.1,
            }
        ),
        encoding="utf-8",
    )
    (camera_root / "rows_v2.csv").write_text(
        "not,a,valid,timing,header\n", encoding="utf-8"
    )

    observations = discover_observations(tmp_path, 0)

    assert len(observations) == 1
    assert observations[0].timing is not None
    assert observations[0].timing.source == "image_metadata"


def test_discovery_parses_rows_csv_when_any_sidecar_timing_is_missing(
    tmp_path: Path,
) -> None:
    camera_root = tmp_path / "cam0"
    camera_root.mkdir()
    (camera_root / "7.pgm").write_bytes(b"P5\n1 1\n255\n\x00")
    (camera_root / "7.json").write_text(
        json.dumps(
            {
                "capture_started_at": 1000.0,
                "capture_ended_at": 1000.1,
            }
        ),
        encoding="utf-8",
    )
    (camera_root / "8.pgm").write_bytes(b"P5\n1 1\n255\n\x00")
    (camera_root / "rows_v2.csv").write_text(
        "not,a,valid,timing,header\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="missing timing columns"):
        discover_observations(tmp_path, 0)


def test_pairing_uses_time_not_equal_frame_ids_and_is_one_to_one() -> None:
    cam0 = [_observation(0, 10 + index, index * 0.067, 0.0) for index in range(7)]
    cam1 = [_observation(1, 900 + index, index * 0.067 + 0.010, 0.0) for index in range(7)]
    for item in cam0 + cam1:
        item.still = True

    pairs = match_still_frames(cam0, cam1, max_center_dt_ms=33.5)

    assert len(pairs) == 7
    assert all(pair.cam0.frame_id != pair.cam1.frame_id for pair in pairs)
    assert len({pair.cam1.frame_id for pair in pairs}) == 7


def test_pairing_is_monotonic_when_nearest_edge_greedy_would_cross() -> None:
    cam0 = [_observation(0, 0, 1.000, 0.0), _observation(0, 1, 1.009, 0.0)]
    cam1 = [_observation(1, 10, 1.008, 0.0), _observation(1, 11, 1.010, 0.0)]
    for item in cam0 + cam1:
        item.still = True

    pairs = match_still_frames(cam0, cam1, max_center_dt_ms=20.0)

    assert [(pair.cam0.frame_id, pair.cam1.frame_id) for pair in pairs] == [
        (0, 10),
        (1, 11),
    ]


def test_window_drift_rejects_slow_motion_even_when_each_step_passes() -> None:
    moving = [_observation(0, index, index * 0.067, index * 0.4) for index in range(7)]

    mark_still_frames(
        moving,
        window_frames=5,
        step_threshold_px=0.5,
        window_threshold_px=0.6,
        max_frame_gap_ms=100.0,
    )

    assert not any(item.still for item in moving)


def test_quasi_static_pairing_uses_equal_ids_and_bounded_predicted_motion() -> None:
    cam0 = [_observation(0, index, index * 0.067, index * 0.2) for index in range(7)]
    cam1 = [
        _observation(1, index, index * 0.067 + 0.010, index * 0.2)
        for index in range(7)
    ]
    mark_motion_rates(cam0, max_frame_gap_ms=100.0)
    mark_motion_rates(cam1, max_frame_gap_ms=100.0)

    pairs = match_quasi_static_frames(
        cam0,
        cam1,
        max_center_dt_ms=10.1,
        max_predicted_motion_px=0.1,
    )

    assert len(pairs) == 5
    assert all(pair.cam0.frame_id == pair.cam1.frame_id for pair in pairs)
    assert all(float(pair.predicted_motion_px) < 0.1 for pair in pairs)


def test_quasi_static_selection_prefers_low_motion_distinct_poses() -> None:
    candidates = []
    for frame, shift, score in (
        (1, 0.0, 0.5),
        (2, 1.0, 0.1),
        (3, 20.0, 0.3),
    ):
        pair = type(
            "Pair",
            (),
            {
                "cam0": _observation(0, frame, float(frame), shift),
                "cam1": _observation(1, frame, float(frame) + 0.005, shift),
                "center_dt_ms": 5.0,
                "predicted_motion_px": score,
            },
        )()
        candidates.append(pair)

    selected = select_quasi_static_local_minima(
        candidates, duplicate_pose_threshold_px=5.0
    )

    assert [pair.cam0.frame_id for pair in selected] == [2, 3]


def test_quasi_static_episode_selection_applies_rate_gate_and_one_per_island() -> None:
    candidates = []
    for frame, shift, rate, predicted in (
        (1, 0.0, 0.018, 0.010),
        (2, 1.0, 0.006, 0.004),
        (3, 2.0, 0.012, 0.006),
        (20, 25.0, 0.014, 0.007),
        (21, 26.0, 0.008, 0.003),
        (40, 50.0, 0.030, 0.002),
        (60, 75.0, 0.010, 0.005),
    ):
        item0 = _observation(0, frame, float(frame), shift)
        item1 = _observation(1, frame, float(frame) + 0.001, shift)
        item0.motion_rate_px_per_ms = rate
        item1.motion_rate_px_per_ms = rate * 0.9
        candidates.append(
            type(
                "Pair",
                (),
                {
                    "cam0": item0,
                    "cam1": item1,
                    "center_dt_ms": 1.0,
                    "predicted_motion_px": predicted,
                },
            )()
        )

    rate_candidates, episodes, selected = select_quasi_static_episode_minima(
        candidates,
        max_motion_rate_px_per_ms=0.020,
        episode_gap_frames=10,
        duplicate_pose_threshold_px=5.0,
    )

    assert [pair.cam0.frame_id for pair in rate_candidates] == [1, 2, 3, 20, 21, 60]
    assert [pair.cam0.frame_id for pair in episodes] == [2, 21, 60]
    assert [pair.cam0.frame_id for pair in selected] == [2, 21, 60]


def test_long_static_platform_emits_one_pose_episode() -> None:
    cam0 = [_observation(0, index, index * 0.067, 0.0) for index in range(9)]
    cam1 = [_observation(1, 100 + index, index * 0.067 + 0.005, 0.0) for index in range(9)]
    for item in cam0 + cam1:
        item.still = True
        item.still_window_drift_px = 0.1
    candidates = match_still_frames(cam0, cam1, max_center_dt_ms=33.5)

    selected = select_pose_episodes(
        candidates, episode_gap_ms=500.0, same_pose_max_shift_px=3.0
    )

    assert len(selected) == 1


def test_returning_to_same_pose_is_not_counted_twice() -> None:
    first = _observation(0, 1, 0.0, 0.0)
    first1 = _observation(1, 101, 0.005, 0.0)
    duplicate = _observation(0, 20, 2.0, 1.0)
    duplicate1 = _observation(1, 120, 2.005, 1.0)
    different = _observation(0, 40, 4.0, 20.0)
    different1 = _observation(1, 140, 4.005, 20.0)
    episodes = [
        type("Pair", (), {"cam0": first, "cam1": first1})(),
        type("Pair", (), {"cam0": duplicate, "cam1": duplicate1})(),
        type("Pair", (), {"cam0": different, "cam1": different1})(),
    ]

    selected = select_distinct_pose_episodes(
        episodes, duplicate_pose_threshold_px=5.0
    )

    assert len(selected) == 2


def _manifest_row(root: Path, index: int) -> dict[str, object]:
    center0 = 10.0 + index
    center1 = center0 + 0.010
    return {
        "pose_id": f"pose_{index:03d}",
        "cam0_frame_id": index,
        "cam1_frame_id": 100 + index,
        "cam0_path": str(root / "cam0" / f"{index}.pgm"),
        "cam1_path": str(root / "cam1" / f"{100 + index}.pgm"),
        "cam0_pixels_sha256": f"{index + 1:064X}",
        "cam1_pixels_sha256": f"{index + 101:064X}",
        "cam0_capture_center": center0,
        "cam1_capture_center": center1,
        "center_dt_ms": 10.0,
        "cam0_grid_found": True,
        "cam1_grid_found": True,
        "cam0_still": True,
        "cam1_still": True,
        "accepted": True,
        "reject_reason": "",
    }


def test_manifest_rejects_duplicate_or_nonstationary_accepted_rows(tmp_path: Path) -> None:
    path = tmp_path / "pairs.csv"
    rows = [_manifest_row(tmp_path, 0), _manifest_row(tmp_path, 1)]
    rows[1]["cam0_frame_id"] = rows[0]["cam0_frame_id"]
    write_csv_rows(path, rows)

    with pytest.raises(ValueError, match="repeats accepted cam0_frame_id"):
        read_accepted_pair_manifest(path)

    rows[1]["cam0_frame_id"] = 1
    rows[1]["cam1_still"] = False
    write_csv_rows(path, rows)
    with pytest.raises(ValueError, match="cam1_still is false"):
        read_accepted_pair_manifest(path)


def test_manifest_accepts_audited_quasi_static_pair(tmp_path: Path) -> None:
    path = tmp_path / "pairs.csv"
    row = _manifest_row(tmp_path, 0)
    row.update(
        {
            "cam1_frame_id": 0,
            "cam0_still": False,
            "cam1_still": False,
            "selection_mode": "quasi_static_local_minimum",
            "cam0_motion_rate_px_per_ms": 0.01,
            "cam1_motion_rate_px_per_ms": 0.02,
            "predicted_intercamera_motion_px": 0.2,
        }
    )
    write_csv_rows(path, [row])

    accepted = read_accepted_pair_manifest(path)

    assert len(accepted) == 1
    assert accepted[0]["selection_mode"] == "quasi_static_local_minimum"


def test_manifest_accepts_audited_quasi_static_episode_pair(tmp_path: Path) -> None:
    path = tmp_path / "pairs.csv"
    row = _manifest_row(tmp_path, 0)
    row.update(
        {
            "cam1_frame_id": 0,
            "cam0_still": False,
            "cam1_still": False,
            "selection_mode": "quasi_static_episode_minimum",
            "cam0_motion_rate_px_per_ms": 0.01,
            "cam1_motion_rate_px_per_ms": 0.02,
            "predicted_intercamera_motion_px": 0.01,
        }
    )
    write_csv_rows(path, [row])

    accepted = read_accepted_pair_manifest(path)

    assert accepted[0]["selection_mode"] == "quasi_static_episode_minimum"


def test_pairing_summary_binds_manifest_intrinsics_and_frozen_stillness(
    tmp_path: Path,
) -> None:
    pairs_path = tmp_path / "pairs.csv"
    write_csv_rows(pairs_path, [_manifest_row(tmp_path, 0)])
    rows = read_accepted_pair_manifest(pairs_path)
    references = {"cam0": {"sha256": "A" * 64}, "cam1": {"sha256": "B" * 64}}
    stillness = {
        "schema": "taxi_receiver.stereo_stillness/1",
        "dataset_root": str(tmp_path / "static_capture"),
        "intrinsics": references,
        "window_frames": 5,
        "max_frame_gap_ms": 150.0,
        "cameras": {
            "cam0": {
                "complete_grid_frames": 200,
                "step_threshold_px": 0.3,
                "window_threshold_px": 0.5,
            },
            "cam1": {
                "complete_grid_frames": 200,
                "step_threshold_px": 0.4,
                "window_threshold_px": 0.6,
            },
        },
    }
    stillness_path = tmp_path / "stillness_config.json"
    stillness_path.write_text(json.dumps(stillness), encoding="utf-8")
    summary = {
        "schema": "taxi_receiver.stereo_pairing/1",
        "status": "ready",
        "dataset_root": str(tmp_path / "capture"),
        "intrinsics": references,
        "stationarity": {
            "threshold_source": str(stillness_path),
            "stillness_config_sha256": sha256_file(stillness_path),
            "window_frames": 5,
            "max_frame_gap_ms": 150.0,
            "cam0_step_threshold_px": 0.3,
            "cam1_step_threshold_px": 0.4,
            "cam0_window_threshold_px": 0.5,
            "cam1_window_threshold_px": 0.6,
        },
        "pairing": {
            "algorithm": "monotonic_earliest_feasible_one_to_one",
            "max_center_dt_ms": 33.5,
        },
        "counts": {"selected_pose_pairs": 1},
        "outputs": {"pairs_csv_sha256": sha256_file(pairs_path)},
    }
    summary_path = tmp_path / "pairing_summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    assert load_pairing_provenance(
        summary_path, pairs_path, rows, references
    )["status"] == "ready"
    assert verify_frozen_stillness_provenance(summary, references)[0] == stillness_path

    summary["outputs"]["pairs_csv_sha256"] = "D" * 64
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest hash"):
        load_pairing_provenance(summary_path, pairs_path, rows, references)
