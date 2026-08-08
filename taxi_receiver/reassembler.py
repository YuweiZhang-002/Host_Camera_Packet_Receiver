"""Layer 5 image-session reassembly.

The active legacy-v0 protocol carries one 80-byte row payload per
128-byte Ethernet payload.  Sessions are isolated by ``(cam_id,
frame_id)`` and accept out-of-order packets without allowing one camera
to overwrite another.  This module deliberately does not invent image
geometry or a pixel format; storage remains raw until firmware provides
those protocol fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Optional, Protocol, runtime_checkable

from .packet_format import CameraRowPacket, ROW_BYTES


class FrameStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    CORRUPT = "CORRUPT"
    TIMEOUT = "TIMEOUT"


@dataclass
class ReassemblyStatistics:
    """Cumulative Layer-5 lifecycle counters for live diagnosis."""

    sessions_created: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    frames_completed: int = 0
    frames_timed_out: int = 0
    frames_partial: int = 0
    frames_corrupt: int = 0


@dataclass
class PacketRecord:
    packet_index: int
    capture_timestamp: float
    row_idx: int
    row_seq: int
    payload_len: int
    row_flags: int
    accepted: bool
    duplicate: bool = False
    conflicting_duplicate: bool = False
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass
class CompletedFrame:
    camera_id: int
    frame_id: int
    row_count: int
    rows: dict[int, bytes]
    missing_rows: list[int] = field(default_factory=list)
    had_overflow: bool = False
    status: FrameStatus = FrameStatus.PARTIAL
    close_reason: str = "unknown"
    expected_rows: int = 0
    packet_records: list[PacketRecord] = field(default_factory=list)
    errors: list[dict[str, object]] = field(default_factory=list)
    duplicate_packets: int = 0
    conflicting_duplicates: int = 0
    started_at: float = 0.0
    ended_at: float = 0.0
    saw_first: bool = False
    saw_last: bool = False

    def to_bytes(self, expected_rows: Optional[int] = None) -> bytes:
        """Concatenate rows 0..expected_rows-1 in order; any row not
        seen is left as zeros."""
        if expected_rows is None:
            expected_rows = self.expected_rows
        out = bytearray(expected_rows * ROW_BYTES)
        for idx, row_payload in self.rows.items():
            if idx < expected_rows:
                start = idx * ROW_BYTES
                valid = row_payload[:ROW_BYTES]
                out[start:start + len(valid)] = valid
        return bytes(out)


@runtime_checkable
class RowReassembler(Protocol):
    def on_row(
        self,
        packet: CameraRowPacket,
        *,
        errors: tuple[str, ...] = (),
        warnings: tuple[str, ...] = (),
        capture_timestamp: float = 0.0,
        now: Optional[float] = None,
    ) -> Optional[CompletedFrame]:
        ...

    def flush(self) -> list[CompletedFrame]:
        ...


class NullReassembler:
    """Default Layer 5: satisfies RowReassembler, does nothing. This is
    what the pipeline uses until you explicitly opt in to a real one --
    exactly the "can be deferred" behavior asked for."""

    def on_row(
        self,
        packet: CameraRowPacket,
        **_kwargs,
    ) -> Optional[CompletedFrame]:
        return None

    def flush(self) -> list[CompletedFrame]:
        return []


@dataclass
class _FrameSession:
    camera_id: int
    frame_id: int
    started_at: float
    last_activity: float
    rows: dict[int, bytes] = field(default_factory=dict)
    packet_records: list[PacketRecord] = field(default_factory=list)
    errors: list[dict[str, object]] = field(default_factory=list)
    duplicate_packets: int = 0
    conflicting_duplicates: int = 0
    had_overflow: bool = False
    saw_first: bool = False
    saw_last: bool = False
    last_row_idx: Optional[int] = None


class FrameReassembler:
    """Reassemble and classify Camera frames without guessing geometry."""

    def __init__(
        self,
        max_open_frames_per_camera: int = 4,
        *,
        timeout_seconds: float = 2.0,
        expected_rows: Optional[int] = None,
    ):
        if max_open_frames_per_camera < 1:
            raise ValueError("max_open_frames_per_camera must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if expected_rows is not None and expected_rows < 1:
            raise ValueError("expected_rows must be positive")

        self._open: dict[tuple[int, int], _FrameSession] = {}
        self._order: dict[int, list[int]] = {}
        self._max_open = max_open_frames_per_camera
        self._timeout_seconds = timeout_seconds
        self._configured_expected_rows = expected_rows
        self._pending: list[CompletedFrame] = []
        self.stats = ReassemblyStatistics()

    def on_row(
        self,
        packet: CameraRowPacket,
        *,
        errors: tuple[str, ...] = (),
        warnings: tuple[str, ...] = (),
        capture_timestamp: float = 0.0,
        now: Optional[float] = None,
    ) -> Optional[CompletedFrame]:
        event_time = time.monotonic() if now is None else now
        self._pending.extend(self.expire(event_time))

        # Do not use identity fields from a packet whose sync or CRC failed.
        # Such packets still reach MonitoringStage and SessionAudit, so the
        # original error evidence is preserved, but an untrusted frame_id must
        # not create/rotate Layer-5 sessions.  A stream-wide bad-sync pattern
        # (for example the attempt8 0x8C stuck-bit failure) otherwise produces
        # one corrupt completed frame per changing bogus frame_id, fills the
        # bounded image-output queue, and makes graceful Ctrl+C shutdown appear
        # to hang while thousands of meaningless rejections are written.
        identity_untrusted = {"bad_sync", "crc_error"}.intersection(errors)
        if identity_untrusted:
            self.stats.rows_rejected += 1
            return self._pop_pending()

        cam_id = packet.header.cam_id
        key = (cam_id, packet.header.frame_id)

        # A new frame ID for one camera closes that camera's older sessions,
        # but never touches another camera's state.  Equality is used instead
        # of ordering so the 16-bit frame ID can wrap from 65535 to 0.
        for old_frame_id in list(self._order.get(cam_id, [])):
            if old_frame_id != packet.header.frame_id:
                closed = self._close(
                    (cam_id, old_frame_id),
                    close_reason="frame_switch",
                    ended_at=event_time,
                )
                if closed is not None:
                    self._pending.append(closed)

        if key not in self._open:
            self._evict_if_needed(cam_id, event_time)
            self._open[key] = _FrameSession(
                camera_id=cam_id,
                frame_id=packet.header.frame_id,
                started_at=event_time,
                last_activity=event_time,
            )
            self._order.setdefault(cam_id, []).append(packet.header.frame_id)
            self.stats.sessions_created += 1

        session = self._open[key]
        session.last_activity = event_time
        session.saw_first |= packet.first_row
        session.saw_last |= packet.last_row
        if packet.last_row:
            session.last_row_idx = packet.header.row_idx
        session.had_overflow |= packet.frame_overflow

        row_idx = packet.header.row_idx
        # Layer 3 normally supplies this error. Keep a local defensive guard so
        # direct callers can never create rows[65535] or another out-of-range
        # entry in a configured 480-row session.
        if (
            self._configured_expected_rows is not None
            and not 0 <= row_idx < self._configured_expected_rows
            and "row_idx_out_of_range" not in errors
        ):
            errors = (*errors, "row_idx_out_of_range")
        payload = packet.payload[: packet.header.payload_len]
        duplicate = row_idx in session.rows
        conflict = duplicate and session.rows[row_idx] != payload
        accepted = not errors and not duplicate
        if accepted:
            self.stats.rows_accepted += 1
        else:
            self.stats.rows_rejected += 1

        record = PacketRecord(
            packet_index=len(session.packet_records),
            capture_timestamp=capture_timestamp,
            row_idx=row_idx,
            row_seq=packet.header.row_seq,
            payload_len=packet.header.payload_len,
            row_flags=packet.header.row_flags,
            accepted=accepted,
            duplicate=duplicate,
            conflicting_duplicate=conflict,
            errors=errors,
            warnings=warnings,
        )
        session.packet_records.append(record)

        if errors:
            session.errors.append(
                {
                    "kind": "packet_validation",
                    "row_idx": row_idx,
                    "row_seq": packet.header.row_seq,
                    "errors": list(errors),
                }
            )
        elif duplicate:
            session.duplicate_packets += 1
            if conflict:
                session.conflicting_duplicates += 1
                session.errors.append(
                    {
                        "kind": "conflicting_duplicate",
                        "row_idx": row_idx,
                        "row_seq": packet.header.row_seq,
                    }
                )
        else:
            session.rows[row_idx] = payload

        # A last-row packet can arrive before earlier rows when PC/NIC queues
        # reorder traffic.  Close immediately only when every expected row is
        # already present; otherwise keep the session open for late rows and
        # let frame-switch, timeout, or flush classify the remainder.
        if session.saw_last:
            expected = (
                self._configured_expected_rows
                if self._configured_expected_rows is not None
                else (session.last_row_idx or 0) + 1
            )
            if all(index in session.rows for index in range(expected)):
                closed = self._close(
                    key,
                    close_reason="last_row",
                    ended_at=event_time,
                )
                if closed is not None:
                    self._pending.append(closed)
        return self._pop_pending()

    def flush(self) -> list[CompletedFrame]:
        now = time.monotonic()
        completed = self.drain_completed()
        for key in list(self._open):
            frame = self._close(key, close_reason="flush", ended_at=now)
            if frame is not None:
                completed.append(frame)
        return completed

    def expire(self, now: Optional[float] = None) -> list[CompletedFrame]:
        event_time = time.monotonic() if now is None else now
        expired: list[CompletedFrame] = []
        for key, session in list(self._open.items()):
            if event_time - session.last_activity >= self._timeout_seconds:
                frame = self._close(
                    key,
                    close_reason="timeout",
                    ended_at=event_time,
                )
                if frame is not None:
                    expired.append(frame)
        return expired

    def drain_completed(self) -> list[CompletedFrame]:
        completed = self._pending
        self._pending = []
        return completed

    def _pop_pending(self) -> Optional[CompletedFrame]:
        if not self._pending:
            return None
        return self._pending.pop(0)

    def _close(
        self,
        key: tuple[int, int],
        *,
        close_reason: str,
        ended_at: float,
    ) -> Optional[CompletedFrame]:
        session = self._open.pop(key, None)
        if session is None:
            return None
        if session.frame_id in self._order.get(session.camera_id, []):
            self._order[session.camera_id].remove(session.frame_id)

        if self._configured_expected_rows is not None:
            expected = self._configured_expected_rows
        elif session.last_row_idx is not None:
            expected = session.last_row_idx + 1
        else:
            expected = max(session.rows, default=-1) + 1
        missing = [i for i in range(expected) if i not in session.rows]

        corrupt = bool(session.errors) or session.had_overflow
        if corrupt:
            status = FrameStatus.CORRUPT
        elif close_reason == "timeout":
            status = FrameStatus.TIMEOUT
        elif session.saw_last and not missing:
            status = FrameStatus.COMPLETE
        else:
            status = FrameStatus.PARTIAL

        completed = CompletedFrame(
            camera_id=session.camera_id,
            frame_id=session.frame_id,
            row_count=len(session.rows),
            rows=dict(session.rows),
            missing_rows=missing,
            had_overflow=session.had_overflow,
            status=status,
            close_reason=close_reason,
            expected_rows=expected,
            packet_records=list(session.packet_records),
            errors=list(session.errors),
            duplicate_packets=session.duplicate_packets,
            conflicting_duplicates=session.conflicting_duplicates,
            started_at=session.started_at,
            ended_at=ended_at,
            saw_first=session.saw_first,
            saw_last=session.saw_last,
        )
        if status is FrameStatus.COMPLETE:
            self.stats.frames_completed += 1
        elif status is FrameStatus.TIMEOUT:
            self.stats.frames_timed_out += 1
        elif status is FrameStatus.CORRUPT:
            self.stats.frames_corrupt += 1
        else:
            self.stats.frames_partial += 1
        return completed

    def _evict_if_needed(self, cam_id: int, now: float) -> None:
        order = self._order.setdefault(cam_id, [])
        while len(order) >= self._max_open:
            key = (cam_id, order[0])
            frame = self._close(key, close_reason="evicted", ended_at=now)
            if frame is not None:
                self._pending.append(frame)
