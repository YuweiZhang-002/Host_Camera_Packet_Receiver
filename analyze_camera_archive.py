"""Diagnose rows.csv/rejected.csv and published Camera image artifacts."""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
from pathlib import Path


ROW_PIXELS = 640
EXPECTED_ROWS = 480
IMAGE_BYTES = ROW_PIXELS * EXPECTED_ROWS


def analyze_csv(cam_dir: Path) -> dict[str, object]:
    rows_path = cam_dir / "rows.csv"
    rejected_path = cam_dir / "rejected.csv"
    flags = collections.Counter()
    error_names = collections.Counter()
    length_flags = collections.Counter()
    length_payload_lens = collections.Counter()
    length_row_indices = collections.Counter()
    length_frames = collections.Counter()
    length_time_bins = collections.Counter()
    length_first = 0
    length_last = 0
    length_out_of_range = 0
    total_rows = 0
    length_errors = 0
    first_timestamp: float | None = None

    with rows_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(row for row in handle if row.strip())
        for row in reader:
            total_rows += 1
            timestamp = float(row["timestamp"])
            if first_timestamp is None:
                first_timestamp = timestamp
            flag = int(row["row_flags"], 16)
            flags[f"0x{flag:02X}"] += 1
            names = tuple(value for value in row["errors"].split(";") if value)
            for name in names:
                error_names[name] += 1
            if "length_error" not in names:
                continue

            length_errors += 1
            row_idx = int(row["row_idx"])
            length_flags[f"0x{flag:02X}"] += 1
            length_payload_lens[int(row["payload_len"])] += 1
            length_row_indices[row_idx] += 1
            length_frames[int(row["frame_id"])] += 1
            length_time_bins[int((timestamp - first_timestamp) // 10)] += 1
            length_first += bool(flag & 0x04)
            length_last += bool(flag & 0x02)
            length_out_of_range += not 0 <= row_idx < EXPECTED_ROWS

    rejected_reasons = collections.Counter()
    rejected_missing_counts = collections.Counter()
    rejected_consecutive = collections.Counter()
    rejected_rows = 0
    if rejected_path.exists():
        with rejected_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                rejected_rows += 1
                for reason in row["reject_reason"].split(";"):
                    if reason:
                        rejected_reasons[reason] += 1
                rejected_missing_counts[int(row["missing_count"])] += 1
                rejected_consecutive[
                    int(row["max_consecutive_missing"])
                ] += 1

    return {
        "rows_csv": str(rows_path.resolve()),
        "rejected_csv": str(rejected_path.resolve()),
        "total_rows": total_rows,
        "length_errors": length_errors,
        "flag_counts": dict(flags.most_common()),
        "error_counts": dict(error_names.most_common()),
        "length_error_flag_counts": dict(length_flags.most_common()),
        # payload_len is a parsed header field, not Camera_Capture byte_count.
        "length_error_payload_len_field_distribution": dict(
            length_payload_lens.most_common()
        ),
        "physical_href_byte_count_distribution": "NOT_RECORDED_IN_CSV",
        "length_error_first_row_packets": length_first,
        "length_error_last_row_packets": length_last,
        "length_error_row_idx_out_of_range": length_out_of_range,
        "length_error_top_row_idx": length_row_indices.most_common(20),
        "length_error_top_frames": length_frames.most_common(20),
        "length_error_10_second_bins": dict(sorted(length_time_bins.items())),
        "rejected_rows": rejected_rows,
        "reject_reason_counts": dict(rejected_reasons.most_common()),
        "reject_missing_count_distribution": dict(
            rejected_missing_counts.most_common()
        ),
        "reject_max_consecutive_distribution": dict(
            rejected_consecutive.most_common()
        ),
    }


def analyze_images(
    cam_dir: Path,
    *,
    adjacent_pair_samples: int = 12,
) -> dict[str, object]:
    artifacts: dict[int, tuple[Path, Path, Path, dict[str, object]]] = {}
    for metadata_path in cam_dir.glob("*.json"):
        metadata = json.loads(metadata_path.read_text("utf-8"))
        frame_id = int(metadata["frame_id"])
        artifacts[frame_id] = (
            metadata_path.with_suffix(".raw"),
            metadata_path.with_suffix(".pgm"),
            metadata_path,
            metadata,
        )
    for metadata_path in (cam_dir / "recovered").glob(
        "frame_*/metadata.json"
    ):
        metadata = json.loads(metadata_path.read_text("utf-8"))
        frame_id = int(metadata["frame_id"])
        artifacts[frame_id] = (
            metadata_path.parent / "image.raw",
            metadata_path.parent / "image.pgm",
            metadata_path,
            metadata,
        )

    invalid_raw_size: list[int] = []
    pgm_raw_mismatch: list[int] = []
    zero_fill_failures: dict[int, list[int]] = {}
    raw_cache: dict[int, bytes] = {}
    for frame_id, (raw_path, pgm_path, _metadata_path, metadata) in artifacts.items():
        raw = raw_path.read_bytes()
        if len(raw) != IMAGE_BYTES:
            invalid_raw_size.append(frame_id)
            continue
        pgm = pgm_path.read_bytes()
        marker = b"\n255\n"
        split = pgm.find(marker)
        pixels = pgm[split + len(marker):] if split >= 0 else b""
        if pixels != raw:
            pgm_raw_mismatch.append(frame_id)

        bad_missing = []
        for row_idx in metadata.get("missing_rows", []):
            start = int(row_idx) * ROW_PIXELS
            if raw[start:start + ROW_PIXELS] != bytes(ROW_PIXELS):
                bad_missing.append(int(row_idx))
        if bad_missing:
            zero_fill_failures[frame_id] = bad_missing
        raw_cache[frame_id] = raw

    ordered_ids = sorted(raw_cache)
    if len(ordered_ids) > adjacent_pair_samples + 1:
        indices = {
            round(
                index
                * (len(ordered_ids) - 2)
                / max(adjacent_pair_samples - 1, 1)
            )
            for index in range(adjacent_pair_samples)
        }
    else:
        indices = set(range(max(0, len(ordered_ids) - 1)))

    pairs = []
    for index in sorted(indices):
        previous_id = ordered_ids[index]
        current_id = ordered_ids[index + 1]
        previous = raw_cache[previous_id]
        current = raw_cache[current_id]
        previous_rows = _row_hashes(previous)
        current_rows = _row_hashes(current)
        current_missing = set(
            int(row)
            for row in artifacts[current_id][3].get("missing_rows", [])
        )
        same_positions = [
            row
            for row in range(EXPECTED_ROWS)
            if previous_rows[row] == current_rows[row]
        ]
        same_nonmissing = [
            row for row in same_positions if row not in current_missing
        ]
        pairs.append(
            {
                "previous_frame_id": previous_id,
                "current_frame_id": current_id,
                "current_missing_rows": sorted(current_missing),
                "same_position_row_hash_count": len(same_positions),
                "same_nonmissing_row_hash_count": len(same_nonmissing),
                "same_nonmissing_row_samples": same_nonmissing[:20],
            }
        )

    return {
        "published_images": len(artifacts),
        "complete_images": sum(
            metadata[3].get("status") == "COMPLETE"
            for metadata in artifacts.values()
        ),
        "recovered_images": sum(
            metadata[3].get("status") == "RECOVERED"
            for metadata in artifacts.values()
        ),
        "invalid_raw_size_frame_ids": sorted(invalid_raw_size),
        "pgm_raw_mismatch_frame_ids": sorted(pgm_raw_mismatch),
        "zero_fill_failures": zero_fill_failures,
        "adjacent_frame_samples": pairs,
        "cross_frame_hash_note": (
            "Equal row hashes are evidence only; a static scene can produce "
            "legitimate equality and does not by itself prove stale buffers."
        ),
    }


def _row_hashes(raw: bytes) -> list[str]:
    return [
        hashlib.sha256(
            raw[row * ROW_PIXELS:(row + 1) * ROW_PIXELS]
        ).hexdigest()
        for row in range(EXPECTED_ROWS)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cam_dir", type=Path)
    parser.add_argument("--adjacent-pairs", type=int, default=12)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = {
        "csv": analyze_csv(args.cam_dir),
        "images": analyze_images(
            args.cam_dir,
            adjacent_pair_samples=args.adjacent_pairs,
        ),
    }
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
