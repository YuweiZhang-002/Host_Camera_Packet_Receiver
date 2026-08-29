"""Build stationary, timestamp-matched cam0/cam1 circle-grid pairs."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any, Sequence

import cv2
import numpy as np

from .binary_calibration import (
    DetectorSettings,
    _metadata_for_image,
    _preflight_rejection,
    _write_json_atomic,
    detect_circle_grid,
    load_binary_image,
)
from .calibration_refill import natural_path_key
from .extrinsic_config import (
    STILLNESS_SCHEMA,
    fisheye_domain_report,
    intrinsic_reference,
    sha256_file,
    validate_intrinsic_pair,
    validate_stillness_config,
)


PAIRING_SCHEMA = "taxi_receiver.stereo_pairing/1"
PAIR_MANIFEST_REQUIRED_COLUMNS = frozenset(
    {
        "pose_id",
        "cam0_frame_id",
        "cam1_frame_id",
        "cam0_path",
        "cam1_path",
        "cam0_pixels_sha256",
        "cam1_pixels_sha256",
        "cam0_capture_center",
        "cam1_capture_center",
        "center_dt_ms",
        "cam0_grid_found",
        "cam1_grid_found",
        "cam0_still",
        "cam1_still",
        "accepted",
        "reject_reason",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _valid_sha256(value: Any) -> bool:
    digest = str(value).strip()
    return len(digest) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in digest
    )


def _finite_manifest_float(row: dict[str, str], field: str, line_number: int) -> float:
    try:
        value = float(row[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"pair manifest line {line_number} has invalid {field}") from exc
    if not math.isfinite(value):
        raise ValueError(f"pair manifest line {line_number} has non-finite {field}")
    return value


def read_accepted_pair_manifest(path: Path) -> list[dict[str, str]]:
    """Read accepted rows from a complete, immutable stationary-pair manifest."""

    rows: list[dict[str, str]] = []
    seen_image_paths: set[str] = set()
    seen: dict[str, set[Any]] = {
        "pose_id": set(),
        "cam0_frame_id": set(),
        "cam1_frame_id": set(),
        "cam0_path": set(),
        "cam1_path": set(),
        "cam0_pixels_sha256": set(),
        "cam1_pixels_sha256": set(),
    }
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = PAIR_MANIFEST_REQUIRED_COLUMNS.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"pair manifest is missing columns: {sorted(missing)}")
        for line_number, source_row in enumerate(reader, start=2):
            if not source_row or not _truthy(source_row.get("accepted")):
                continue
            row = {
                str(key): "" if value is None else str(value).strip()
                for key, value in source_row.items()
            }
            if not row["pose_id"]:
                raise ValueError(f"pair manifest line {line_number} has an empty pose_id")
            selection_mode = row.get("selection_mode", "stationary") or "stationary"
            if selection_mode not in {
                "stationary",
                "quasi_static_local_minimum",
                "quasi_static_episode_minimum",
            }:
                raise ValueError(
                    f"pair manifest line {line_number} has invalid selection_mode"
                )
            for field in ("cam0_grid_found", "cam1_grid_found"):
                if not _truthy(row[field]):
                    raise ValueError(
                        f"pair manifest line {line_number} is accepted but {field} is false"
                    )
            if selection_mode == "stationary":
                for field in ("cam0_still", "cam1_still"):
                    if not _truthy(row[field]):
                        raise ValueError(
                            f"pair manifest line {line_number} is accepted but {field} is false"
                        )
            else:
                for field in (
                    "cam0_motion_rate_px_per_ms",
                    "cam1_motion_rate_px_per_ms",
                    "predicted_intercamera_motion_px",
                ):
                    if _finite_manifest_float(row, field, line_number) < 0.0:
                        raise ValueError(
                            f"pair manifest line {line_number} has negative {field}"
                        )
            if row["reject_reason"]:
                raise ValueError(
                    f"pair manifest line {line_number} is accepted but has reject_reason"
                )
            for field in ("cam0_path", "cam1_path"):
                if not row[field]:
                    raise ValueError(f"pair manifest line {line_number} has an empty {field}")
            for field in ("cam0_pixels_sha256", "cam1_pixels_sha256"):
                if not _valid_sha256(row[field]):
                    raise ValueError(f"pair manifest line {line_number} has invalid {field}")
                row[field] = row[field].upper()
            for field in ("cam0_frame_id", "cam1_frame_id"):
                try:
                    frame_id = int(row[field])
                except ValueError as exc:
                    raise ValueError(
                        f"pair manifest line {line_number} has invalid {field}"
                    ) from exc
                if frame_id < 0:
                    raise ValueError(
                        f"pair manifest line {line_number} has negative {field}"
                    )
                row[field] = str(frame_id)
            if (
                selection_mode in {
                    "quasi_static_local_minimum",
                    "quasi_static_episode_minimum",
                }
                and row["cam0_frame_id"] != row["cam1_frame_id"]
            ):
                raise ValueError(
                    f"pair manifest line {line_number} quasi-static frame IDs differ"
                )
            center0 = _finite_manifest_float(row, "cam0_capture_center", line_number)
            center1 = _finite_manifest_float(row, "cam1_capture_center", line_number)
            center_dt_ms = _finite_manifest_float(row, "center_dt_ms", line_number)
            if center0 <= 0.0 or center1 <= 0.0 or center_dt_ms < 0.0:
                raise ValueError(
                    f"pair manifest line {line_number} has invalid capture timing"
                )
            expected_dt_ms = abs(center1 - center0) * 1000.0
            if not math.isclose(
                center_dt_ms, expected_dt_ms, rel_tol=1e-9, abs_tol=1e-3
            ):
                raise ValueError(
                    f"pair manifest line {line_number} center_dt_ms does not match capture centers"
                )

            uniqueness_values: dict[str, Any] = {
                "pose_id": row["pose_id"],
                "cam0_frame_id": int(row["cam0_frame_id"]),
                "cam1_frame_id": int(row["cam1_frame_id"]),
                "cam0_path": str(Path(row["cam0_path"]).resolve()).casefold(),
                "cam1_path": str(Path(row["cam1_path"]).resolve()).casefold(),
                "cam0_pixels_sha256": row["cam0_pixels_sha256"],
                "cam1_pixels_sha256": row["cam1_pixels_sha256"],
            }
            for field, value in uniqueness_values.items():
                if value in seen[field]:
                    raise ValueError(
                        f"pair manifest line {line_number} repeats accepted {field}: {value}"
                    )
                seen[field].add(value)
            for field in ("cam0_path", "cam1_path"):
                normalized_path = str(Path(row[field]).resolve()).casefold()
                if normalized_path in seen_image_paths:
                    raise ValueError(
                        f"pair manifest line {line_number} reuses an accepted image path"
                    )
                seen_image_paths.add(normalized_path)
            rows.append(row)

    ordered = sorted(rows, key=lambda row: float(row["cam0_capture_center"]))
    cam0_times = [float(row["cam0_capture_center"]) for row in ordered]
    cam1_times = [float(row["cam1_capture_center"]) for row in ordered]
    if any(right <= left for left, right in zip(cam0_times, cam0_times[1:])) or any(
        right <= left for left, right in zip(cam1_times, cam1_times[1:])
    ):
        raise ValueError(
            "accepted pair manifest is not a strictly monotonic one-to-one time match"
        )
    return rows


def load_pairing_provenance(
    summary_path: Path,
    pairs_path: Path,
    accepted_rows: Sequence[dict[str, str]],
    expected_intrinsics: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Validate that a ready pairing summary cryptographically binds its inputs."""

    try:
        document = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid pairing summary JSON in {summary_path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema") != PAIRING_SCHEMA:
        raise ValueError(f"pairing summary schema must be {PAIRING_SCHEMA!r}")
    if document.get("status") != "ready":
        raise ValueError("pairing summary is not ready for formal calibration/validation")
    dataset_root = document.get("dataset_root")
    if not isinstance(dataset_root, str) or not dataset_root.strip():
        raise ValueError("pairing summary dataset_root is missing")
    outputs = document.get("outputs")
    if not isinstance(outputs, dict):
        raise ValueError("pairing summary outputs object is missing")
    expected_manifest_hash = outputs.get("pairs_csv_sha256")
    if not _valid_sha256(expected_manifest_hash):
        raise ValueError("pairing summary pairs_csv_sha256 is missing or invalid")
    actual_manifest_hash = sha256_file(pairs_path)
    if str(expected_manifest_hash).upper() != actual_manifest_hash:
        raise ValueError("pairing summary does not match the pair manifest hash")
    counts = document.get("counts")
    if (
        not isinstance(counts, dict)
        or not isinstance(counts.get("selected_pose_pairs"), int)
        or counts["selected_pose_pairs"] != len(accepted_rows)
    ):
        raise ValueError(
            "pairing summary selected-pair count does not match the manifest"
        )
    pairing = document.get("pairing")
    if not isinstance(pairing, dict):
        raise ValueError("pairing summary pairing object is missing")
    try:
        maximum_dt_ms = float(pairing["max_center_dt_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("pairing summary max_center_dt_ms is invalid") from exc
    if not math.isfinite(maximum_dt_ms) or maximum_dt_ms <= 0.0:
        raise ValueError("pairing summary max_center_dt_ms must be positive")
    if any(
        float(row["center_dt_ms"]) > maximum_dt_ms + 1e-6
        for row in accepted_rows
    ):
        raise ValueError(
            "pair manifest contains a pair outside the summary time tolerance"
        )
    selection_mode = pairing.get("selection_mode", "stationary")
    expected_algorithms = {
        "stationary": "monotonic_earliest_feasible_one_to_one",
        "quasi_static_local_minimum": (
            "equal_frame_id_bounded_motion_local_minimum"
        ),
        "quasi_static_episode_minimum": (
            "equal_frame_id_absolute_rate_episode_minimum"
        ),
    }
    if selection_mode not in expected_algorithms:
        raise ValueError("pairing summary selection_mode is invalid")
    if pairing.get("algorithm") != expected_algorithms[selection_mode]:
        raise ValueError(
            "pairing summary algorithm does not match its selection mode"
        )
    if selection_mode == "quasi_static_episode_minimum":
        try:
            maximum_rate = float(pairing["max_motion_rate_px_per_ms"])
            episode_gap_frames = int(pairing["quasi_episode_gap_frames"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "episode-minimum pairing limits are missing or invalid"
            ) from exc
        if not math.isfinite(maximum_rate) or maximum_rate <= 0.0:
            raise ValueError("episode-minimum maximum motion rate must be positive")
        if episode_gap_frames < 1:
            raise ValueError("episode-minimum frame gap must be at least one")
        if any(
            max(
                float(row["cam0_motion_rate_px_per_ms"]),
                float(row["cam1_motion_rate_px_per_ms"]),
            )
            > maximum_rate + 1e-12
            for row in accepted_rows
        ):
            raise ValueError(
                "pair manifest contains an accepted pair above the absolute motion-rate limit"
            )

    stationarity = document.get("stationarity")
    if not isinstance(stationarity, dict):
        raise ValueError("pairing summary stationarity object is missing")
    if stationarity.get("threshold_source") == "bootstrap_cli_values_not_frozen":
        raise ValueError(
            "formal pairing cannot use unfrozen bootstrap stillness thresholds"
        )
    if not _valid_sha256(stationarity.get("stillness_config_sha256")):
        raise ValueError(
            "pairing summary does not bind a frozen stillness configuration"
        )
    references = document.get("intrinsics")
    if not isinstance(references, dict):
        raise ValueError("pairing summary intrinsic references are missing")
    for camera in ("cam0", "cam1"):
        actual = references.get(camera)
        expected = expected_intrinsics.get(camera)
        if not isinstance(actual, dict) or not isinstance(expected, dict):
            raise ValueError(
                f"pairing summary {camera} intrinsic reference is missing"
            )
        if str(actual.get("sha256", "")).upper() != str(
            expected.get("sha256", "")
        ).upper():
            raise ValueError(f"pairing summary {camera} intrinsic hash mismatch")
    return document


def verify_pair_manifest_images(rows: Sequence[dict[str, str]]) -> None:
    """Fail closed if any accepted manifest image is missing or has changed."""

    for row in rows:
        for camera in ("cam0", "cam1"):
            path = Path(row[f"{camera}_path"])
            if not path.is_file():
                raise ValueError(
                    f"accepted pair {row['pose_id']} is missing {camera} image: {path}"
                )
            if sha256_file(path) != row[f"{camera}_pixels_sha256"]:
                raise ValueError(
                    f"accepted pair {row['pose_id']} {camera} image hash mismatch"
                )


def verify_frozen_stillness_provenance(
    pairing_summary: dict[str, Any],
    expected_intrinsics: dict[str, dict[str, Any]],
    override_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Load the exact frozen stillness file bound into a pairing summary."""

    stationarity = pairing_summary.get("stationarity")
    if not isinstance(stationarity, dict):
        raise ValueError("pairing summary stationarity object is missing")
    source = stationarity.get("threshold_source")
    if override_path is None:
        if not isinstance(source, str) or not source:
            raise ValueError("pairing summary stillness threshold source is missing")
        path = Path(source)
    else:
        path = override_path
    if not path.is_file():
        raise ValueError(f"frozen stillness configuration does not exist: {path}")
    if sha256_file(path) != str(stationarity.get("stillness_config_sha256", "")).upper():
        raise ValueError("stillness configuration hash differs from pairing summary")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid stillness configuration JSON in {path}: {exc}") from exc
    validate_stillness_config(document)
    for camera in ("cam0", "cam1"):
        actual_hash = document["intrinsics"][camera]["sha256"]
        expected_hash = expected_intrinsics[camera]["sha256"]
        if str(actual_hash).upper() != str(expected_hash).upper():
            raise ValueError(
                f"stillness configuration {camera} intrinsic hash mismatch: "
                f"downstream input {expected_intrinsics[camera].get('path')} binds "
                f"SHA256 {expected_hash}, but {path} binds SHA256 {actual_hash}; "
                "all pairing, solve, and validation stages must use the same "
                "intrinsic files"
            )
    try:
        summary_window_frames = int(stationarity["window_frames"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("pairing summary stationarity.window_frames is invalid") from exc
    if summary_window_frames != int(document["window_frames"]):
        raise ValueError("pairing summary window_frames differs from stillness configuration")
    numeric_fields = (
        ("max_frame_gap_ms", document["max_frame_gap_ms"]),
        ("cam0_step_threshold_px", document["cameras"]["cam0"]["step_threshold_px"]),
        ("cam1_step_threshold_px", document["cameras"]["cam1"]["step_threshold_px"]),
        (
            "cam0_window_threshold_px",
            document["cameras"]["cam0"]["window_threshold_px"],
        ),
        (
            "cam1_window_threshold_px",
            document["cameras"]["cam1"]["window_threshold_px"],
        ),
    )
    for field, expected in numeric_fields:
        try:
            actual = float(stationarity[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"pairing summary stationarity.{field} is invalid") from exc
        if not math.isclose(actual, float(expected), rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(
                f"pairing summary stationarity.{field} differs from stillness configuration"
            )
    return path.resolve(), document


@dataclass(slots=True)
class FrameTiming:
    start: float
    end: float
    center: float
    source: str


@dataclass(slots=True)
class FrameObservation:
    camera_id: int
    frame_id: int
    path: Path
    timing: FrameTiming | None = None
    centers: np.ndarray | None = None
    detection_reason: str = "not_processed"
    metrics: dict[str, float | int] = field(default_factory=dict)
    still: bool = False
    still_max_step_px: float | None = None
    still_window_drift_px: float | None = None
    motion_rate_px_per_ms: float | None = None


@dataclass(slots=True)
class PairCandidate:
    cam0: FrameObservation
    cam1: FrameObservation
    center_dt_ms: float
    predicted_motion_px: float | None = None


def detector_settings(document: dict[str, Any]) -> DetectorSettings:
    pattern = document["pattern"]
    return DetectorSettings(
        pattern="asymmetric" if str(pattern["type"]).startswith("asymmetric") else "symmetric",
        columns=int(pattern["columns"]),
        rows=int(pattern["rows"]),
    )


def _metadata_timing(path: Path) -> FrameTiming | None:
    metadata_path = path.with_suffix(".json")
    if not metadata_path.is_file():
        return None
    try:
        document = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    start = document.get("capture_started_at")
    end = document.get("capture_ended_at")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    if not (math.isfinite(float(start)) and math.isfinite(float(end)) and float(end) >= float(start) > 0.0):
        return None
    return FrameTiming(float(start), float(end), 0.5 * (float(start) + float(end)), "image_metadata")


def stereo_preflight_rejection(path: Path) -> str | None:
    rejection = _preflight_rejection(path, allow_recovered=False)
    if rejection is not None:
        return rejection
    metadata = _metadata_for_image(path)
    if metadata is None:
        return None
    if metadata.get("status") != "COMPLETE":
        return f"frame_status_{metadata.get('status', 'missing')}"
    for field, reason in (
        ("had_crc_error", "frame_crc_error"),
        ("had_sync_error", "frame_sync_error"),
        ("had_conflicting_duplicate", "frame_conflicting_duplicate"),
    ):
        if metadata.get(field) is True:
            return reason
    return None


def read_rows_timings(camera_root: Path, camera_id: int) -> dict[int, FrameTiming]:
    path = camera_root / "rows_v2.csv"
    if not path.is_file():
        return {}
    groups: dict[int, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", "cam_id", "frame_id", "row_accepted", "reliable_first", "reliable_last"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            missing = sorted(required.difference(reader.fieldnames or []))
            raise ValueError(f"{path} is missing timing columns: {missing}")
        for row in reader:
            if not row or not _truthy(row.get("row_accepted")):
                continue
            try:
                if int(row["cam_id"]) != camera_id:
                    continue
                frame_id = int(row["frame_id"])
                timestamp = float(row["timestamp"])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(timestamp) or timestamp <= 0.0:
                continue
            group = groups.setdefault(
                frame_id,
                {"timestamps": [], "first": False, "last": False},
            )
            group["timestamps"].append(timestamp)
            group["first"] |= _truthy(row.get("reliable_first"))
            group["last"] |= _truthy(row.get("reliable_last"))
    result: dict[int, FrameTiming] = {}
    for frame_id, group in groups.items():
        timestamps = group["timestamps"]
        if not timestamps or not group["first"] or not group["last"]:
            continue
        start = float(min(timestamps))
        end = float(max(timestamps))
        result[frame_id] = FrameTiming(start, end, 0.5 * (start + end), "rows_v2.csv")
    return result


def discover_observations(dataset_root: Path, camera_id: int) -> list[FrameObservation]:
    camera_root = dataset_root / f"cam{camera_id}"
    if not camera_root.is_dir():
        raise ValueError(f"camera directory does not exist: {camera_root}")
    observations: list[FrameObservation] = []
    for path in sorted(camera_root.glob("*.pgm"), key=natural_path_key):
        try:
            frame_id = int(path.stem)
        except ValueError:
            continue
        timing = _metadata_timing(path)
        observations.append(FrameObservation(camera_id, frame_id, path.resolve(), timing))
    if not observations:
        raise ValueError(f"no numbered PGM frames found in {camera_root}")
    if any(observation.timing is None for observation in observations):
        rows_timings = read_rows_timings(camera_root, camera_id)
        for observation in observations:
            if observation.timing is None:
                observation.timing = rows_timings.get(observation.frame_id)
    observations.sort(
        key=lambda item: (
            math.inf if item.timing is None else item.timing.center,
            item.frame_id,
        )
    )
    return observations


def analyse_observations(
    observations: Sequence[FrameObservation],
    document: dict[str, Any],
    settings: DetectorSettings,
    *,
    minimum_edge_margin_px: float = 0.0,
) -> None:
    width = int(document["image_size"]["width"])
    height = int(document["image_size"]["height"])
    total = len(observations)
    for index, observation in enumerate(observations, start=1):
        rejection = stereo_preflight_rejection(observation.path)
        if rejection is not None:
            observation.detection_reason = rejection
            continue
        if observation.timing is None:
            observation.detection_reason = "missing_complete_frame_timing"
            continue
        try:
            image = load_binary_image(observation.path, width, height)
            detection = detect_circle_grid(image, settings)
            observation.detection_reason = detection.reason
            observation.metrics = dict(detection.metrics)
            if detection.found and detection.centers is not None:
                centers = detection.centers.reshape(-1, 2).astype(np.float64)
                edge_margin = float(
                    min(
                        np.min(centers[:, 0]),
                        np.min(centers[:, 1]),
                        width - 1 - np.max(centers[:, 0]),
                        height - 1 - np.max(centers[:, 1]),
                    )
                )
                observation.metrics["grid_edge_margin_px"] = edge_margin
                if edge_margin < minimum_edge_margin_px:
                    observation.detection_reason = "grid_too_close_to_numeric_domain_edge"
                else:
                    observation.centers = centers
        except (OSError, ValueError, cv2.error) as exc:
            observation.detection_reason = f"detection_error:{exc}"
        if index % 100 == 0 or index == total:
            found = sum(item.centers is not None for item in observations[:index])
            print(
                f"  cam{observation.camera_id} pairing scan {index}/{total}, grids={found}",
                flush=True,
            )


def align_points(reference: np.ndarray, points: np.ndarray) -> np.ndarray:
    direct = float(np.max(np.linalg.norm(points - reference, axis=1)))
    reversed_points = points[::-1]
    reversed_error = float(np.max(np.linalg.norm(reversed_points - reference, axis=1)))
    return reversed_points if reversed_error < direct else points


def point_delta(first: np.ndarray, second: np.ndarray) -> float:
    aligned = align_points(first, second)
    return float(np.max(np.linalg.norm(aligned - first, axis=1)))


def _robust_threshold(values: Sequence[float], minimum: float = 0.05) -> dict[str, float]:
    array = np.asarray(values, dtype=np.float64)
    if not array.size:
        raise ValueError("no static displacement samples were available")
    median = float(np.median(array))
    mad = float(np.median(np.abs(array - median)))
    sigma = 1.4826 * mad
    return {
        "median_px": median,
        "mad_px": mad,
        "robust_sigma_px": sigma,
        "threshold_px": max(minimum, median + 3.0 * sigma),
        "p95_px": float(np.percentile(array, 95)),
        "maximum_px": float(np.max(array)),
        "samples": int(array.size),
    }


def static_noise_statistics(
    observations: Sequence[FrameObservation],
    *,
    window_frames: int,
    max_frame_gap_ms: float,
) -> tuple[dict[str, float], dict[str, float]]:
    usable = [item for item in observations if item.centers is not None and item.timing is not None]
    step_values: list[float] = []
    for first, second in zip(usable, usable[1:]):
        if (second.timing.center - first.timing.center) * 1000.0 > max_frame_gap_ms:
            continue
        step_values.append(point_delta(first.centers, second.centers))
    window_values: list[float] = []
    for start in range(0, len(usable) - window_frames + 1):
        window = usable[start : start + window_frames]
        gaps = [
            (right.timing.center - left.timing.center) * 1000.0
            for left, right in zip(window, window[1:])
        ]
        if gaps and max(gaps) > max_frame_gap_ms:
            continue
        reference = window[0].centers
        aligned = np.asarray([align_points(reference, item.centers) for item in window])
        median_points = np.median(aligned, axis=0)
        drift = float(np.max(np.linalg.norm(aligned - median_points[None, :, :], axis=2)))
        window_values.append(drift)
    return _robust_threshold(step_values), _robust_threshold(window_values)


def mark_still_frames(
    observations: Sequence[FrameObservation],
    *,
    window_frames: int,
    step_threshold_px: float,
    window_threshold_px: float,
    max_frame_gap_ms: float,
) -> None:
    if window_frames < 3:
        raise ValueError("window_frames must be at least 3")
    for start in range(0, len(observations) - window_frames + 1):
        window = observations[start : start + window_frames]
        if any(item.centers is None or item.timing is None for item in window):
            continue
        gaps = [
            (right.timing.center - left.timing.center) * 1000.0
            for left, right in zip(window, window[1:])
        ]
        if gaps and max(gaps) > max_frame_gap_ms:
            continue
        reference = window[0].centers
        aligned = [align_points(reference, item.centers) for item in window]
        steps = [
            float(np.max(np.linalg.norm(right - left, axis=1)))
            for left, right in zip(aligned, aligned[1:])
        ]
        median_points = np.median(np.asarray(aligned), axis=0)
        drift = float(
            np.max(np.linalg.norm(np.asarray(aligned) - median_points[None, :, :], axis=2))
        )
        max_step = max(steps, default=0.0)
        if max_step > step_threshold_px or drift > window_threshold_px:
            continue
        center_index = start + window_frames // 2
        item = observations[center_index]
        if not item.still or drift < float(item.still_window_drift_px):
            item.still = True
            item.still_max_step_px = max_step
            item.still_window_drift_px = drift


def mark_motion_rates(
    observations: Sequence[FrameObservation],
    *,
    max_frame_gap_ms: float,
) -> None:
    """Estimate local circle-center speed from a three-frame neighbourhood."""

    for index in range(1, len(observations) - 1):
        previous, item, following = observations[index - 1 : index + 2]
        if any(
            observation.centers is None or observation.timing is None
            for observation in (previous, item, following)
        ):
            continue
        previous_gap_ms = (item.timing.center - previous.timing.center) * 1000.0
        following_gap_ms = (following.timing.center - item.timing.center) * 1000.0
        if (
            previous_gap_ms <= 0.0
            or following_gap_ms <= 0.0
            or previous_gap_ms > max_frame_gap_ms
            or following_gap_ms > max_frame_gap_ms
        ):
            continue
        item.motion_rate_px_per_ms = max(
            point_delta(previous.centers, item.centers) / previous_gap_ms,
            point_delta(item.centers, following.centers) / following_gap_ms,
        )


def match_quasi_static_frames(
    cam0: Sequence[FrameObservation],
    cam1: Sequence[FrameObservation],
    *,
    max_center_dt_ms: float,
    max_predicted_motion_px: float,
) -> list[PairCandidate]:
    """Match equal frame IDs whose estimated exposure-offset motion is bounded."""

    if max_center_dt_ms <= 0.0 or max_predicted_motion_px <= 0.0:
        raise ValueError("quasi-static timing and motion limits must be positive")
    cam1_by_frame = {item.frame_id: item for item in cam1}
    matched: list[PairCandidate] = []
    for item0 in cam0:
        item1 = cam1_by_frame.get(item0.frame_id)
        if (
            item1 is None
            or item0.centers is None
            or item1.centers is None
            or item0.timing is None
            or item1.timing is None
            or item0.motion_rate_px_per_ms is None
            or item1.motion_rate_px_per_ms is None
        ):
            continue
        center_dt_ms = abs(item1.timing.center - item0.timing.center) * 1000.0
        if center_dt_ms > max_center_dt_ms:
            continue
        predicted_motion_px = max(
            item0.motion_rate_px_per_ms,
            item1.motion_rate_px_per_ms,
        ) * center_dt_ms
        if predicted_motion_px > max_predicted_motion_px:
            continue
        matched.append(
            PairCandidate(item0, item1, center_dt_ms, predicted_motion_px)
        )
    return matched


def select_quasi_static_local_minima(
    candidates: Sequence[PairCandidate],
    *,
    duplicate_pose_threshold_px: float,
) -> list[PairCandidate]:
    """Choose the lowest-motion representative of each distinct stereo pose."""

    ranked = sorted(
        candidates,
        key=lambda pair: (
            float(pair.predicted_motion_px),
            pair.center_dt_ms,
            pair.cam0.timing.center,
        ),
    )
    selected: list[PairCandidate] = []
    for candidate in ranked:
        duplicate = any(
            point_delta(existing.cam0.centers, candidate.cam0.centers)
            <= duplicate_pose_threshold_px
            and point_delta(existing.cam1.centers, candidate.cam1.centers)
            <= duplicate_pose_threshold_px
            for existing in selected
        )
        if not duplicate:
            selected.append(candidate)
    return sorted(selected, key=lambda pair: pair.cam0.timing.center)


def select_quasi_static_episode_minima(
    candidates: Sequence[PairCandidate],
    *,
    max_motion_rate_px_per_ms: float,
    episode_gap_frames: int,
    duplicate_pose_threshold_px: float,
) -> tuple[list[PairCandidate], list[PairCandidate], list[PairCandidate]]:
    """Collapse low-speed temporal islands to one geometrically distinct pair.

    ``predicted_motion_px`` only estimates displacement between the two camera
    exposure centres.  When that interval is tiny, a moving board can still
    pass.  This selector additionally limits absolute image motion, groups the
    remaining same-frame candidates into temporal islands, and retains the
    lowest-motion observation from each island before geometric de-duplication.
    """

    if not math.isfinite(max_motion_rate_px_per_ms) or max_motion_rate_px_per_ms <= 0.0:
        raise ValueError("maximum absolute motion rate must be finite and positive")
    if episode_gap_frames < 1:
        raise ValueError("quasi-static episode gap must be at least one frame")
    if duplicate_pose_threshold_px <= 0.0:
        raise ValueError("duplicate pose threshold must be positive")

    rate_candidates = sorted(
        (
            pair
            for pair in candidates
            if max(
                float(pair.cam0.motion_rate_px_per_ms),
                float(pair.cam1.motion_rate_px_per_ms),
            )
            <= max_motion_rate_px_per_ms
        ),
        key=lambda pair: pair.cam0.frame_id,
    )
    if not rate_candidates:
        return [], [], []

    episode_groups: list[list[PairCandidate]] = [[rate_candidates[0]]]
    for candidate in rate_candidates[1:]:
        previous = episode_groups[-1][-1]
        frame_gap = candidate.cam0.frame_id - previous.cam0.frame_id
        if frame_gap <= episode_gap_frames:
            episode_groups[-1].append(candidate)
        else:
            episode_groups.append([candidate])

    def rank(pair: PairCandidate) -> tuple[float, float, float, float]:
        absolute_rate = max(
            float(pair.cam0.motion_rate_px_per_ms),
            float(pair.cam1.motion_rate_px_per_ms),
        )
        return (
            absolute_rate,
            float(pair.predicted_motion_px),
            pair.center_dt_ms,
            pair.cam0.timing.center,
        )

    episode_representatives = [min(group, key=rank) for group in episode_groups]
    selected = select_distinct_pose_episodes(
        episode_representatives,
        duplicate_pose_threshold_px=duplicate_pose_threshold_px,
    )
    return rate_candidates, episode_representatives, selected


def match_still_frames(
    cam0: Sequence[FrameObservation],
    cam1: Sequence[FrameObservation],
    *,
    max_center_dt_ms: float,
) -> list[PairCandidate]:
    if not math.isfinite(max_center_dt_ms) or max_center_dt_ms <= 0.0:
        raise ValueError("max_center_dt_ms must be finite and positive")
    usable0 = sorted(
        (
            item
            for item in cam0
            if item.still
            and item.timing is not None
            and math.isfinite(item.timing.center)
        ),
        key=lambda item: item.timing.center,
    )
    usable1 = sorted(
        (
            item
            for item in cam1
            if item.still
            and item.timing is not None
            and math.isfinite(item.timing.center)
        ),
        key=lambda item: item.timing.center,
    )
    limit = max_center_dt_ms / 1000.0
    matched: list[PairCandidate] = []
    index0 = index1 = 0
    while index0 < len(usable0) and index1 < len(usable1):
        time0 = usable0[index0].timing.center
        time1 = usable1[index1].timing.center
        difference = time1 - time0
        if abs(difference) <= limit:
            # Earliest-feasible ordered matching has maximum cardinality for a
            # one-dimensional tolerance window and can never create crossings.
            matched.append(
                PairCandidate(
                    usable0[index0], usable1[index1], abs(difference) * 1000.0
                )
            )
            index0 += 1
            index1 += 1
        elif difference < -limit:
            index1 += 1
        else:
            index0 += 1
    return matched


def select_pose_episodes(
    candidates: Sequence[PairCandidate],
    *,
    episode_gap_ms: float,
    same_pose_max_shift_px: float,
) -> list[PairCandidate]:
    if not candidates:
        return []
    episodes: list[list[PairCandidate]] = [[candidates[0]]]
    for candidate in candidates[1:]:
        previous = episodes[-1][-1]
        previous_time = 0.5 * (previous.cam0.timing.center + previous.cam1.timing.center)
        current_time = 0.5 * (candidate.cam0.timing.center + candidate.cam1.timing.center)
        gap_ms = (current_time - previous_time) * 1000.0
        shift0 = point_delta(previous.cam0.centers, candidate.cam0.centers)
        shift1 = point_delta(previous.cam1.centers, candidate.cam1.centers)
        if gap_ms <= episode_gap_ms and max(shift0, shift1) <= same_pose_max_shift_px:
            episodes[-1].append(candidate)
        else:
            episodes.append([candidate])
    return [
        min(
            episode,
            key=lambda pair: (
                pair.center_dt_ms,
                float(pair.cam0.still_window_drift_px) + float(pair.cam1.still_window_drift_px),
            ),
        )
        for episode in episodes
    ]


def select_distinct_pose_episodes(
    episodes: Sequence[PairCandidate], *, duplicate_pose_threshold_px: float
) -> list[PairCandidate]:
    """Reject a later episode when both cameras see effectively the same pose."""

    selected: list[PairCandidate] = []
    for candidate in episodes:
        duplicate = any(
            point_delta(existing.cam0.centers, candidate.cam0.centers)
            <= duplicate_pose_threshold_px
            and point_delta(existing.cam1.centers, candidate.cam1.centers)
            <= duplicate_pose_threshold_px
            for existing in selected
        )
        if not duplicate:
            selected.append(candidate)
    return selected


def _frame_row(item: FrameObservation) -> dict[str, Any]:
    return {
        "camera_id": item.camera_id,
        "frame_id": item.frame_id,
        "path": str(item.path),
        "capture_start": None if item.timing is None else item.timing.start,
        "capture_end": None if item.timing is None else item.timing.end,
        "capture_center": None if item.timing is None else item.timing.center,
        "timing_source": None if item.timing is None else item.timing.source,
        "grid_found": item.centers is not None,
        "detection_reason": item.detection_reason,
        "still": item.still,
        "still_max_step_px": item.still_max_step_px,
        "still_window_drift_px": item.still_window_drift_px,
        "motion_rate_px_per_ms": item.motion_rate_px_per_ms,
        **item.metrics,
    }


def write_csv_rows(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or ["empty"])
        writer.writeheader()
        writer.writerows(rows)


def _prepare_output_root(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"output root is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build timestamp-matched stationary cam0/cam1 circle-grid pairs."
    )
    parser.add_argument("dataset_root", type=Path)
    parser.add_argument("--cam0-intrinsics", type=Path, required=True)
    parser.add_argument("--cam1-intrinsics", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--estimate-stillness-only", action="store_true")
    parser.add_argument("--stillness-config", type=Path)
    parser.add_argument(
        "--pairing-mode",
        choices=(
            "stationary",
            "quasi_static_local_minimum",
            "quasi_static_episode_minimum",
        ),
        default="stationary",
    )
    parser.add_argument("--max-predicted-motion-px", type=float, default=0.75)
    parser.add_argument("--max-motion-rate-px-per-ms", type=float, default=0.02)
    parser.add_argument("--quasi-episode-gap-frames", type=int, default=10)
    parser.add_argument("--window-frames", type=int, default=5)
    parser.add_argument("--still-threshold-cam0-px", type=float, default=1.0)
    parser.add_argument("--still-threshold-cam1-px", type=float, default=1.0)
    parser.add_argument("--window-threshold-cam0-px", type=float, default=1.5)
    parser.add_argument("--window-threshold-cam1-px", type=float, default=1.5)
    parser.add_argument("--max-frame-gap-ms", type=float, default=150.0)
    parser.add_argument("--max-center-dt-ms", type=float, default=33.5)
    parser.add_argument("--episode-gap-ms", type=float, default=500.0)
    parser.add_argument("--same-pose-max-shift-px", type=float, default=3.0)
    parser.add_argument("--duplicate-pose-threshold-px", type=float, default=5.0)
    parser.add_argument("--min-cam0-edge-margin-px", type=float, default=12.0)
    parser.add_argument("--min-static-frames", type=int, default=200)
    parser.add_argument("--min-pairs", type=int, default=15)
    return parser


def _validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.window_frames < 3 or args.window_frames % 2 == 0:
        parser.error("--window-frames must be an odd integer >= 3")
    positive = (
        args.still_threshold_cam0_px,
        args.still_threshold_cam1_px,
        args.window_threshold_cam0_px,
        args.window_threshold_cam1_px,
        args.max_frame_gap_ms,
        args.max_center_dt_ms,
        args.episode_gap_ms,
        args.same_pose_max_shift_px,
        args.duplicate_pose_threshold_px,
        args.max_predicted_motion_px,
        args.max_motion_rate_px_per_ms,
    )
    if min(positive) <= 0.0:
        parser.error("all thresholds and timing limits must be positive")
    if args.min_cam0_edge_margin_px < 0.0:
        parser.error("--min-cam0-edge-margin-px must be non-negative")
    if args.min_static_frames < args.window_frames or args.min_pairs < 1:
        parser.error("minimum frame/pair counts are invalid")
    if args.quasi_episode_gap_frames < 1:
        parser.error("--quasi-episode-gap-frames must be at least one")
    if args.estimate_stillness_only and args.stillness_config is not None:
        parser.error("--stillness-config is not used with --estimate-stillness-only")
    if args.estimate_stillness_only and args.pairing_mode != "stationary":
        parser.error("--pairing-mode is not used with --estimate-stillness-only")
    if (
        args.pairing_mode in {
            "quasi_static_local_minimum",
            "quasi_static_episode_minimum",
        }
        and args.stillness_config is None
    ):
        parser.error("quasi-static pairing requires --stillness-config for provenance")


def run(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    _prepare_output_root(args.output_root)
    doc0, K0, D0, doc1, K1, D1 = validate_intrinsic_pair(
        args.cam0_intrinsics, args.cam1_intrinsics
    )
    settings = detector_settings(doc0)
    observations0 = discover_observations(args.dataset_root, 0)
    observations1 = discover_observations(args.dataset_root, 1)
    analyse_observations(
        observations0,
        doc0,
        settings,
        minimum_edge_margin_px=args.min_cam0_edge_margin_px,
    )
    analyse_observations(observations1, doc1, settings)
    write_csv_rows(args.output_root / "frames_cam0.csv", [_frame_row(item) for item in observations0])
    write_csv_rows(args.output_root / "frames_cam1.csv", [_frame_row(item) for item in observations1])

    references = {
        "cam0": intrinsic_reference(args.cam0_intrinsics, doc0),
        "cam1": intrinsic_reference(args.cam1_intrinsics, doc1),
    }
    domain = {
        "cam0": fisheye_domain_report(doc0, K0, D0),
        "cam1": fisheye_domain_report(doc1, K1, D1),
    }
    failures = domain["cam0"]["failures"] + domain["cam1"]["failures"]
    if failures:
        raise ValueError("intrinsic numerical-domain check failed: " + "; ".join(failures))

    if args.estimate_stillness_only:
        usable0 = sum(item.centers is not None for item in observations0)
        usable1 = sum(item.centers is not None for item in observations1)
        if min(usable0, usable1) < args.min_static_frames:
            raise ValueError(
                f"static dataset has only cam0={usable0}, cam1={usable1} complete grids; "
                f"need at least {args.min_static_frames} each"
            )
        step0, window0 = static_noise_statistics(
            observations0,
            window_frames=args.window_frames,
            max_frame_gap_ms=args.max_frame_gap_ms,
        )
        step1, window1 = static_noise_statistics(
            observations1,
            window_frames=args.window_frames,
            max_frame_gap_ms=args.max_frame_gap_ms,
        )
        document = {
            "schema": STILLNESS_SCHEMA,
            "created_utc": _utc_now(),
            "dataset_root": str(args.dataset_root.resolve()),
            "intrinsics": references,
            "window_frames": args.window_frames,
            "max_frame_gap_ms": args.max_frame_gap_ms,
            "cameras": {
                "cam0": {
                    "complete_grid_frames": usable0,
                    "step_threshold_px": step0["threshold_px"],
                    "window_threshold_px": window0["threshold_px"],
                    "step_statistics": step0,
                    "window_statistics": window0,
                },
                "cam1": {
                    "complete_grid_frames": usable1,
                    "step_threshold_px": step1["threshold_px"],
                    "window_threshold_px": window1["threshold_px"],
                    "step_statistics": step1,
                    "window_statistics": window1,
                },
            },
            "intrinsic_domain": domain,
        }
        validate_stillness_config(document)
        output_path = args.output_root / "stillness_config.json"
        _write_json_atomic(output_path, document)
        return output_path, document

    if args.stillness_config is not None:
        stillness = json.loads(args.stillness_config.read_text(encoding="utf-8"))
        validate_stillness_config(stillness)
        for key in ("cam0", "cam1"):
            expected = references[key]["sha256"]
            actual = stillness.get("intrinsics", {}).get(key, {}).get("sha256")
            if str(actual).upper() != str(expected).upper():
                raise ValueError(
                    f"stillness configuration {key} intrinsic hash mismatch: "
                    f"pairing input {references[key]['path']} has SHA256 "
                    f"{expected}, but {args.stillness_config} binds SHA256 {actual}; "
                    "use the same intrinsic files that generated the stillness "
                    "configuration, or regenerate stillness_config.json"
                )
        window_frames = int(stillness["window_frames"])
        step0 = float(stillness["cameras"]["cam0"]["step_threshold_px"])
        step1 = float(stillness["cameras"]["cam1"]["step_threshold_px"])
        window0 = float(stillness["cameras"]["cam0"]["window_threshold_px"])
        window1 = float(stillness["cameras"]["cam1"]["window_threshold_px"])
        max_frame_gap_ms = float(stillness["max_frame_gap_ms"])
        threshold_source = str(args.stillness_config.resolve())
    else:
        window_frames = args.window_frames
        step0, step1 = args.still_threshold_cam0_px, args.still_threshold_cam1_px
        window0, window1 = args.window_threshold_cam0_px, args.window_threshold_cam1_px
        max_frame_gap_ms = args.max_frame_gap_ms
        threshold_source = "bootstrap_cli_values_not_frozen"

    mark_still_frames(
        observations0,
        window_frames=window_frames,
        step_threshold_px=step0,
        window_threshold_px=window0,
        max_frame_gap_ms=max_frame_gap_ms,
    )
    mark_still_frames(
        observations1,
        window_frames=window_frames,
        step_threshold_px=step1,
        window_threshold_px=window1,
        max_frame_gap_ms=max_frame_gap_ms,
    )
    mark_motion_rates(observations0, max_frame_gap_ms=max_frame_gap_ms)
    mark_motion_rates(observations1, max_frame_gap_ms=max_frame_gap_ms)
    # Rewrite reports now that stillness fields are populated.
    write_csv_rows(args.output_root / "frames_cam0.csv", [_frame_row(item) for item in observations0])
    write_csv_rows(args.output_root / "frames_cam1.csv", [_frame_row(item) for item in observations1])
    if args.pairing_mode == "stationary":
        candidates = match_still_frames(
            observations0, observations1, max_center_dt_ms=args.max_center_dt_ms
        )
        episodes = select_pose_episodes(
            candidates,
            episode_gap_ms=args.episode_gap_ms,
            same_pose_max_shift_px=args.same_pose_max_shift_px,
        )
        selected = select_distinct_pose_episodes(
            episodes, duplicate_pose_threshold_px=args.duplicate_pose_threshold_px
        )
        rate_candidates: list[PairCandidate] = []
    else:
        candidates = match_quasi_static_frames(
            observations0,
            observations1,
            max_center_dt_ms=args.max_center_dt_ms,
            max_predicted_motion_px=args.max_predicted_motion_px,
        )
        if args.pairing_mode == "quasi_static_episode_minimum":
            rate_candidates, episodes, selected = select_quasi_static_episode_minima(
                candidates,
                max_motion_rate_px_per_ms=args.max_motion_rate_px_per_ms,
                episode_gap_frames=args.quasi_episode_gap_frames,
                duplicate_pose_threshold_px=args.duplicate_pose_threshold_px,
            )
        else:
            selected = select_quasi_static_local_minima(
                candidates,
                duplicate_pose_threshold_px=args.duplicate_pose_threshold_px,
            )
            episodes = selected
            rate_candidates = candidates
    episode_keys = {(pair.cam0.frame_id, pair.cam1.frame_id) for pair in episodes}
    rate_candidate_keys = {
        (pair.cam0.frame_id, pair.cam1.frame_id) for pair in rate_candidates
    }
    selected_pose_ids = {
        (pair.cam0.frame_id, pair.cam1.frame_id): f"pose_{index:03d}"
        for index, pair in enumerate(selected)
    }
    rows: list[dict[str, Any]] = []
    for index, pair in enumerate(candidates):
        key = (pair.cam0.frame_id, pair.cam1.frame_id)
        accepted = key in selected_pose_ids
        rows.append(
            {
                "pose_id": selected_pose_ids.get(key, f"candidate_{index:06d}"),
                "cam0_frame_id": pair.cam0.frame_id,
                "cam1_frame_id": pair.cam1.frame_id,
                "cam0_path": str(pair.cam0.path),
                "cam1_path": str(pair.cam1.path),
                "cam0_pixels_sha256": sha256_file(pair.cam0.path),
                "cam1_pixels_sha256": sha256_file(pair.cam1.path),
                "cam0_capture_center": pair.cam0.timing.center,
                "cam1_capture_center": pair.cam1.timing.center,
                "center_dt_ms": pair.center_dt_ms,
                "cam0_grid_found": True,
                "cam1_grid_found": True,
                "cam0_still": pair.cam0.still,
                "cam1_still": pair.cam1.still,
                "selection_mode": args.pairing_mode,
                "cam0_max_step_px": pair.cam0.still_max_step_px,
                "cam1_max_step_px": pair.cam1.still_max_step_px,
                "cam0_window_drift_px": pair.cam0.still_window_drift_px,
                "cam1_window_drift_px": pair.cam1.still_window_drift_px,
                "cam0_motion_rate_px_per_ms": pair.cam0.motion_rate_px_per_ms,
                "cam1_motion_rate_px_per_ms": pair.cam1.motion_rate_px_per_ms,
                "predicted_intercamera_motion_px": pair.predicted_motion_px,
                "absolute_motion_rate_px_per_ms": max(
                    float(pair.cam0.motion_rate_px_per_ms),
                    float(pair.cam1.motion_rate_px_per_ms),
                ),
                "accepted": accepted,
                "reject_reason": (
                    ""
                    if accepted
                    else "duplicate_pose_geometry"
                    if (
                        args.pairing_mode == "quasi_static_episode_minimum"
                        and key in episode_keys
                    )
                    else "same_quasi_static_episode_not_representative"
                    if (
                        args.pairing_mode == "quasi_static_episode_minimum"
                        and key in rate_candidate_keys
                    )
                    else "absolute_motion_rate_exceeds_limit"
                    if args.pairing_mode == "quasi_static_episode_minimum"
                    else "higher_motion_duplicate_pose"
                    if args.pairing_mode == "quasi_static_local_minimum"
                    else "duplicate_pose_geometry"
                    if key in episode_keys
                    else "same_static_pose_not_representative"
                ),
            }
        )
    pairs_path = args.output_root / "pairs.csv"
    write_csv_rows(pairs_path, rows)
    accepted_count = len(selected)
    frozen_stillness = args.stillness_config is not None
    ready = accepted_count >= args.min_pairs and frozen_stillness
    readiness_failures: list[str] = []
    if accepted_count < args.min_pairs:
        readiness_failures.append(
            f"only {accepted_count} {args.pairing_mode} pose pairs; need {args.min_pairs}"
        )
    if not frozen_stillness:
        readiness_failures.append(
            "stillness thresholds are bootstrap CLI values; rerun with --stillness-config"
        )
    summary = {
        "schema": PAIRING_SCHEMA,
        "created_utc": _utc_now(),
        "status": "ready" if ready else "not_ready",
        "dataset_root": str(args.dataset_root.resolve()),
        "intrinsics": references,
        "intrinsic_domain": domain,
        "stationarity": {
            "selection_mode": args.pairing_mode,
            "threshold_source": threshold_source,
            "stillness_config_sha256": (
                None
                if args.stillness_config is None
                else sha256_file(args.stillness_config)
            ),
            "window_frames": window_frames,
            "max_frame_gap_ms": max_frame_gap_ms,
            "cam0_step_threshold_px": step0,
            "cam1_step_threshold_px": step1,
            "cam0_window_threshold_px": window0,
            "cam1_window_threshold_px": window1,
        },
        "pairing": {
            "selection_mode": args.pairing_mode,
            "algorithm": (
                "monotonic_earliest_feasible_one_to_one"
                if args.pairing_mode == "stationary"
                else "equal_frame_id_absolute_rate_episode_minimum"
                if args.pairing_mode == "quasi_static_episode_minimum"
                else "equal_frame_id_bounded_motion_local_minimum"
            ),
            "max_center_dt_ms": args.max_center_dt_ms,
            "max_predicted_motion_px": (
                None
                if args.pairing_mode == "stationary"
                else args.max_predicted_motion_px
            ),
            "episode_gap_ms": args.episode_gap_ms,
            "max_motion_rate_px_per_ms": (
                args.max_motion_rate_px_per_ms
                if args.pairing_mode == "quasi_static_episode_minimum"
                else None
            ),
            "quasi_episode_gap_frames": (
                args.quasi_episode_gap_frames
                if args.pairing_mode == "quasi_static_episode_minimum"
                else None
            ),
            "same_pose_max_shift_px": args.same_pose_max_shift_px,
            "duplicate_pose_threshold_px": args.duplicate_pose_threshold_px,
            "minimum_cam0_grid_edge_margin_px": args.min_cam0_edge_margin_px,
        },
        "counts": {
            "cam0_frames": len(observations0),
            "cam1_frames": len(observations1),
            "cam0_complete_grids": sum(item.centers is not None for item in observations0),
            "cam1_complete_grids": sum(item.centers is not None for item in observations1),
            "cam0_still_frames": sum(item.still for item in observations0),
            "cam1_still_frames": sum(item.still for item in observations1),
            "timestamp_matched_candidates": len(candidates),
            "stationary_pose_episodes": len(episodes),
            "quasi_motion_candidates": (
                len(candidates)
                if args.pairing_mode != "stationary"
                else 0
            ),
            "quasi_absolute_rate_candidates": (
                len(rate_candidates)
                if args.pairing_mode == "quasi_static_episode_minimum"
                else 0
            ),
            "quasi_temporal_episodes": (
                len(episodes)
                if args.pairing_mode == "quasi_static_episode_minimum"
                else 0
            ),
            "quasi_selected_pose_pairs": (
                accepted_count
                if args.pairing_mode != "stationary"
                else 0
            ),
            "selected_pose_pairs": accepted_count,
            "minimum_required_pairs": args.min_pairs,
        },
        "failures": readiness_failures,
        "outputs": {
            "pairs_csv": str(pairs_path.resolve()),
            "pairs_csv_sha256": sha256_file(pairs_path),
            "cam0_frames_csv": str((args.output_root / "frames_cam0.csv").resolve()),
            "cam1_frames_csv": str((args.output_root / "frames_cam1.csv").resolve()),
        },
    }
    summary_path = args.output_root / "pairing_summary.json"
    _write_json_atomic(summary_path, summary)
    return summary_path, summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_arguments(parser, args)
    # Echoed before the scan, not after: a full pairing pass costs minutes, and
    # attempt19 spent four of them on stale intrinsics without ever printing
    # which files it had been handed.  cam0 margin 2.333deg identifies full44.
    print(f"cam0 intrinsics: {args.cam0_intrinsics.name}")
    print(f"cam1 intrinsics: {args.cam1_intrinsics.name}")
    try:
        output_path, document = run(args)
    except (OSError, ValueError, cv2.error) as exc:
        print(f"stereo pair building failed: {exc}", file=sys.stderr)
        return 2
    for camera in ("cam0", "cam1"):
        domain = document.get("intrinsic_domain", {}).get(camera)
        if domain is not None:
            print(
                f"{camera} monotonic margin: "
                f"{domain['monotonic_margin_deg']:.3f}deg ({domain['status']})"
            )
    if args.estimate_stillness_only:
        print(f"stillness configuration: {output_path}")
        for key in ("cam0", "cam1"):
            item = document["cameras"][key]
            print(
                f"{key}: step={item['step_threshold_px']:.4f}px "
                f"window={item['window_threshold_px']:.4f}px"
            )
        return 0
    counts = document["counts"]
    print(
        f"stereo pairing: {document['status']}  selected={counts['selected_pose_pairs']} "
        f"candidates={counts['timestamp_matched_candidates']}"
    )
    print(f"summary: {output_path}")
    print(f"pairs: {document['outputs']['pairs_csv']}")
    for failure in document["failures"]:
        print(f"failure: {failure}")
    return 0 if document["status"] == "ready" else 3


if __name__ == "__main__":
    raise SystemExit(main())
