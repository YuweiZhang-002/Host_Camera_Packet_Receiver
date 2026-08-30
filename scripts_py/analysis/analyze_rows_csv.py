"""Stream a large rows.csv and explain frame/flag integrity without pandas."""
from __future__ import annotations

import argparse
import collections
import csv
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from taxi_receiver.packet_format import (
    FLAG_FIRST_ROW,
    FLAG_FRAME_OVERFLOW,
    FLAG_LAST_ROW,
    FLAG_LENGTH_ERROR,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--expected-rows", type=int, default=480)
    args = parser.parse_args()

    flag_counts: collections.Counter[int] = collections.Counter()
    error_counts: collections.Counter[str] = collections.Counter()
    sessions: dict[tuple[int, int], dict[str, object]] = {}
    shifted_header_samples: list[dict[str, str]] = []
    row_count = 0

    with args.csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(row for row in handle if row.strip())
        for row in reader:
            row_count += 1
            flags = int(row["row_flags"], 16)
            flag_counts[flags] += 1
            error_counts[row["errors"] or "<none>"] += 1

            key = (int(row["cam_id"]), int(row["frame_id"]))
            session = sessions.setdefault(
                key,
                {
                    "rows": set(),
                    "bad": 0,
                    "first": 0,
                    "last": 0,
                    "overflow": 0,
                },
            )
            session["rows"].add(int(row["row_idx"]))
            recorded_errors = [
                value for value in row["errors"].split(";") if value
            ]
            # Older rows.csv files were produced while Python incorrectly
            # decoded 0x04 as overflow. Re-evaluate that one historical label
            # from the raw flag byte; retain every other recorded failure.
            corrected_errors = [
                value
                for value in recorded_errors
                if value != "frame_overflow"
            ]
            if flags & FLAG_FRAME_OVERFLOW:
                corrected_errors.append("frame_overflow")
            session["bad"] += bool(corrected_errors)
            session["first"] += bool(flags & FLAG_FIRST_ROW)
            session["last"] += bool(flags & FLAG_LAST_ROW)
            session["overflow"] += bool(flags & FLAG_FRAME_OVERFLOW)

            # A short packet that lost offset 9 has payload_len (0x50) shifted
            # into row_flags. Byte_Replacer ORs 0x08, producing 0x58; row_seq is
            # then observed as its low byte followed by zero padding.
            if (
                flags == (0x50 | FLAG_LENGTH_ERROR)
                and len(shifted_header_samples) < 20
            ):
                shifted_header_samples.append(
                    {
                        name: row[name]
                        for name in (
                            "timestamp",
                            "frame_id",
                            "row_idx",
                            "row_seq",
                            "row_flags",
                            "payload_len",
                            "crc_ok",
                            "errors",
                        )
                    }
                )

    expected = set(range(args.expected_rows))
    complete = [
        {"cam_id": key[0], "frame_id": key[1]}
        for key, session in sessions.items()
        if session["rows"] == expected
        and session["bad"] == 0
        and session["last"]
        and not session["overflow"]
    ]
    result = {
        "csv": str(args.csv_path.resolve()),
        "rows": row_count,
        "sessions": len(sessions),
        "strict_complete_sessions": len(complete),
        "first_complete_sessions": complete[:20],
        "flag_counts": {
            f"0x{flag:02X}": count for flag, count in flag_counts.most_common()
        },
        "error_counts": dict(error_counts.most_common()),
        "shifted_header_0x58_samples": shifted_header_samples,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
