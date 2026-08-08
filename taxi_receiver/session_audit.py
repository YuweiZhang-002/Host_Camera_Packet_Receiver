"""Per-packet, session-scoped CSV audit logging.

This module is deliberately a side-band observer.  It consumes the
``FrameContext`` exposed by ``TaxiReceiverPipeline.on_frame_processed`` and
does not alter Layer-3 validation, reassembly, or storage decisions.
"""
from __future__ import annotations

import csv
from pathlib import Path
import threading
import time
from typing import Optional, TextIO

from .stages import FrameContext


AUDIT_FIELDS = (
    "timestamp",
    "cam_id",
    "frame_id",
    "row_idx",
    "row_flags_raw",
    "row_flags_effective",
    "fpga_status",
    "header_check",
    "payload_len",
    "row_seq",
    "crc_ok",
    "m00",
    "xc_q4",
    "yc_q4",
    "vx_q8",
    "vy_q8",
    "validation_status",
    "reject_reason",
)

_COUNTER_MAX = 0xFFFF
_WRAP_HIGH = 0xF000
_WRAP_LOW = 0x0FFF


class SessionAuditLogger:
    """Write one CSV row for every processed capture record.

    ``row_flags_effective`` is an audit-only compatibility view: it propagates
    FPGA LENGTH_ERROR (fpga_status bit 3) from the first contaminated row through
    the remainder of the same ``(cam_id, frame_id)``. Raw MCU flags are never
    modified and fpga_status is recorded in its own column.

    A large, non-wrap rollback of either frame_id or row_seq is treated as a
    new power-on session.  The CSV is then reopened with ``"w"`` so evidence
    from different board sessions cannot be accidentally merged.
    """

    def __init__(
        self,
        output_root: str | Path,
        *,
        rollback_threshold: int = 1024,
        flush_every_rows: int = 256,
        flush_interval_seconds: float = 0.5,
    ) -> None:
        if not 1 <= rollback_threshold <= _COUNTER_MAX:
            raise ValueError("rollback_threshold must be in 1..65535")
        if flush_every_rows <= 0:
            raise ValueError("flush_every_rows must be positive")
        if flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be positive")

        self.output_root = Path(output_root)
        self.path = self.output_root / "session_audit.csv"
        self.rollback_threshold = rollback_threshold
        self.flush_every_rows = flush_every_rows
        self.flush_interval_seconds = flush_interval_seconds
        self._lock = threading.Lock()
        self._handle: Optional[TextIO] = None
        self._writer: Optional[csv.DictWriter] = None
        self._max_frame_id: dict[int, int] = {}
        self._max_row_seq: dict[int, int] = {}
        self._active_frame_id: dict[int, int] = {}
        self._contaminated: dict[int, bool] = {}
        self._pending_rows = 0
        self._last_flush = time.monotonic()
        self.reset_count = 0
        self._start_new_session(initial=True)

    def __call__(self, ctx: FrameContext) -> None:
        self.log_context(ctx)

    def log_context(self, ctx: FrameContext) -> None:
        """Record a processed frame, including Layer-3 failures."""
        result = ctx.camera_result
        packet = result.packet if result is not None else None

        with self._lock:
            if packet is None:
                self._write_row(
                    {
                        "timestamp": _format_timestamp(ctx.frame.timestamp),
                        **{name: "" for name in AUDIT_FIELDS[1:]},
                    }
                )
                return

            header = packet.header
            trailer = packet.trailer
            cam_id = header.cam_id
            frame_id = header.frame_id
            row_seq = header.row_seq

            # A malformed header can contain arbitrary frame/sequence values.
            # It is still written below, but it must not erase the current
            # session's CSV.  Only a Layer-3-valid packet is a "known value"
            # for power-session rollback detection.
            trusted_for_session = result is not None and result.ok
            if trusted_for_session:
                if self._is_new_power_session(cam_id, frame_id, row_seq):
                    self._start_new_session(initial=False)
                self._update_counter_maxima(cam_id, frame_id, row_seq)

            if self._active_frame_id.get(cam_id) != frame_id:
                self._active_frame_id[cam_id] = frame_id
                self._contaminated[cam_id] = False

            row_flags_raw = header.row_flags
            if packet.length_error:
                self._contaminated[cam_id] = True
            row_flags_effective = (
                row_flags_raw | 0x08
                if self._contaminated.get(cam_id, False)
                else row_flags_raw
            )

            self._write_row(
                {
                    "timestamp": _format_timestamp(ctx.frame.timestamp),
                    "cam_id": cam_id,
                    "frame_id": frame_id,
                    "row_idx": header.row_idx,
                    "row_flags_raw": f"0x{row_flags_raw:02X}",
                    "row_flags_effective": f"0x{row_flags_effective:02X}",
                    "fpga_status": f"0x{header.fpga_status:02X}",
                    "header_check": f"0x{header.header_check:02X}",
                    "payload_len": header.payload_len,
                    "row_seq": row_seq,
                    "crc_ok": int(packet.crc_ok),
                    "m00": trailer.m00,
                    "xc_q4": trailer.xc_q4,
                    "yc_q4": trailer.yc_q4,
                    "vx_q8": trailer.vx_q8,
                    "vy_q8": trailer.vy_q8,
                    "validation_status": (
                        "PASS" if result is not None and result.ok else "FAIL"
                    ),
                    "reject_reason": (
                        ""
                        if result is None or result.ok
                        else ";".join(result.errors)
                    ),
                }
            )

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.flush()
                self._handle.close()
                self._handle = None
                self._writer = None

    def _start_new_session(self, *, initial: bool) -> None:
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()

        self.output_root.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._handle, fieldnames=AUDIT_FIELDS)
        self._writer.writeheader()
        self._handle.flush()
        self._pending_rows = 0
        self._last_flush = time.monotonic()

        self._max_frame_id.clear()
        self._max_row_seq.clear()
        self._active_frame_id.clear()
        self._contaminated.clear()
        if not initial:
            self.reset_count += 1

    def _is_new_power_session(
        self,
        cam_id: int,
        frame_id: int,
        row_seq: int,
    ) -> bool:
        previous_frame = self._max_frame_id.get(cam_id)
        previous_row_seq = self._max_row_seq.get(cam_id)
        return (
            previous_frame is not None
            and _is_illegal_rollback(
                previous_frame,
                frame_id,
                self.rollback_threshold,
            )
        ) or (
            previous_row_seq is not None
            and _is_illegal_rollback(
                previous_row_seq,
                row_seq,
                self.rollback_threshold,
            )
        )

    def _update_counter_maxima(
        self,
        cam_id: int,
        frame_id: int,
        row_seq: int,
    ) -> None:
        self._max_frame_id[cam_id] = _updated_max(
            self._max_frame_id.get(cam_id), frame_id
        )
        self._max_row_seq[cam_id] = _updated_max(
            self._max_row_seq.get(cam_id), row_seq
        )

    def _write_row(self, values: dict[str, object]) -> None:
        if self._writer is None or self._handle is None:
            raise RuntimeError("session audit logger is closed")
        self._writer.writerow(values)
        self._pending_rows += 1
        now = time.monotonic()
        if (
            self._pending_rows >= self.flush_every_rows
            or now - self._last_flush >= self.flush_interval_seconds
        ):
            self._handle.flush()
            self._pending_rows = 0
            self._last_flush = now


def _is_illegal_rollback(previous: int, current: int, threshold: int) -> bool:
    if current >= previous:
        return False
    if previous >= _WRAP_HIGH and current <= _WRAP_LOW:
        return False
    return previous - current >= threshold


def _updated_max(previous: Optional[int], current: int) -> int:
    if previous is None:
        return current
    if previous >= _WRAP_HIGH and current <= _WRAP_LOW:
        return current
    return max(previous, current)


def _format_timestamp(timestamp: float) -> str:
    return f"{timestamp:.9f}"
