"""Numbered camera-image publication and per-row CSV telemetry.

This is a project-side Layer-5 sink.  It does not alter the 128-byte wire
format or TAXI.  Parsed rows are handed to a dedicated CSV writer thread and
appended to ``images/camN/rows.csv``; only a *reliable* last row terminates the
human-readable CSV group with one blank line.

Only a fully reassembled frame is published as an image.  The current payload
is an 80-byte packed threshold row (640 one-bit pixels), so the dependency-free
image format is binary PGM (P5), with the numeric ``frame_id`` as its stem.

Two properties of this module are diagnostic contracts, not implementation
details, because attempt2 could not distinguish "the packet never arrived" from
"the recorder dropped it":

``capture_index``
    Assigned on the packet-consumer thread, before any queueing.  A gap means
    the row was dropped by *this* recorder, not by the FPGA or the NIC.

``csv_sequence``
    Assigned on the writer thread, per camera, immediately before the row is
    formatted.  It is always contiguous.  A contiguous ``csv_sequence`` next to
    a gapped ``capture_index`` localises the loss to the CSV queue; a gapped
    ``row_seq`` next to a contiguous ``capture_index`` exonerates the recorder.
"""
from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import itertools
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any, Callable, TextIO
import uuid

from .packet_format import (
    FLAG_LAST_ROW,
    ROW_BYTES,
    SYNC0_DEFAULT,
    SYNC1_DEFAULT,
)
from .reassembler import CompletedFrame, FrameStatus
from .stages import FrameContext
from .threshold_recover import (
    BitOrder,
    MissingRowPolicy,
    ROW_PIXELS,
    recover_completed_frame,
)


ROW_CSV_FIELDS = (
    "timestamp",
    # Recorder-side ordering columns.  See the module docstring: these exist to
    # separate "lost upstream" from "lost inside this recorder".
    "capture_index",
    "csv_sequence",
    "cam_id",
    "frame_id",
    "row_idx",
    "row_seq",
    "row_flags",
    "fpga_status",
    "header_check",
    "first_row",
    "first_processed_row",
    "last_row",
    "frame_overflow",
    "length_error",
    "frame_end",
    "sync0",
    "sync1",
    "sync_ok",
    "payload_len",
    "payload_len_ok",
    "crc_ok",
    "received_crc",
    "calculated_crc",
    "trailer_pad_zero",
    "m00",
    "xc_q4",
    "yc_q4",
    "x",
    "y",
    "vx_q8",
    "vy_q8",
    "vx",
    "vy",
    "parse_ok",
    # Trust columns.  ``first_processed_row`` and ``last_row`` preserve sender
    # flag evidence; ``first_row`` is derived from row_idx == 0.  These four
    # say whether the row evidence may be counted.
    "layer3_valid",
    "row_accepted",
    "reliable_first",
    "reliable_last",
    "errors",
    "warnings",
)


_CSV_STOP = object()


@dataclass
class _CsvSink:
    handle: TextIO
    writer: csv.DictWriter
    pending_rows: int = 0
    last_flush: float = 0.0
    # Rows formatted but not yet handed to csv.writerows().
    batch: list[dict[str, Any]] = field(default_factory=list)
    # Indices into ``batch`` after which a human-readable blank line belongs.
    blank_after: list[int] = field(default_factory=list)
    csv_sequence: int = 0
    # Duplicate mirror, reset on frame switch.  This deliberately re-derives
    # acceptance instead of reading FrameReassembler's PacketRecord: an
    # independent second opinion is what makes a disagreement informative.
    mirror_frame_id: int | None = None
    mirror_rows: set[int] = field(default_factory=set)


@dataclass(slots=True)
class _RowEvent:
    """Everything the writer thread needs, captured in one cheap tuple.

    The parsed result object is referenced, not copied: keeping the consumer
    thread's work to one allocation plus one ``Queue.put`` is the entire point
    of moving CSV off that thread.
    """

    capture_index: int
    timestamp: float
    result: Any


@dataclass(slots=True)
class _PublishedFrameEnvelope:
    """A completed frame flattened for the publication process (S2).

    ``rows_blob`` is one contiguous ``expected_rows * ROW_BYTES`` buffer instead
    of a dict of 480 small ``bytes`` objects, so the transfer is a single large
    pickle rather than hundreds of tiny ones.

    ``present_rows`` is mandatory, not a convenience.  ``to_bytes()`` zero-fills
    absent rows, so a blob alone cannot distinguish "row missing" from "row of
    zero pixels".  Dropping it would make the recovery gate in the child see
    every frame as complete and publish zero-filled rows as real data.
    """

    camera_id: int
    frame_id: int
    row_count: int
    expected_rows: int
    had_overflow: bool
    status: str
    close_reason: str
    started_at: float
    ended_at: float
    saw_first: bool
    saw_last: bool
    rows_blob: bytes
    present_rows: bytes
    missing_rows: tuple[int, ...]


@dataclass(slots=True)
class _PublisherStop:
    """Picklable shutdown sentinel.

    A bare ``object()`` sentinel does not survive the queue: ``spawn`` gives the
    child its own module instance, so the unpickled sentinel fails an ``is``
    comparison against the child's own, the worker never returns, and shutdown
    degrades into two 30 s timeouts with the final image counters lost.
    """


def _frame_to_envelope(frame: CompletedFrame) -> _PublishedFrameEnvelope:
    expected_rows = frame.expected_rows or frame.row_count
    rows_blob = frame.to_bytes(expected_rows)
    present = bytearray(expected_rows)
    for row_index in frame.rows:
        if 0 <= row_index < expected_rows:
            present[row_index] = 1
    return _PublishedFrameEnvelope(
        camera_id=frame.camera_id,
        frame_id=frame.frame_id,
        row_count=frame.row_count,
        expected_rows=expected_rows,
        had_overflow=frame.had_overflow,
        status=frame.status.value,
        close_reason=frame.close_reason,
        started_at=frame.started_at,
        ended_at=frame.ended_at,
        saw_first=frame.saw_first,
        saw_last=frame.saw_last,
        rows_blob=rows_blob,
        present_rows=bytes(present),
        missing_rows=tuple(frame.missing_rows),
    )


def _envelope_to_frame(envelope: _PublishedFrameEnvelope) -> CompletedFrame:
    rows: dict[int, bytes] = {}
    for row_index in range(envelope.expected_rows):
        if not envelope.present_rows[row_index]:
            continue
        start = row_index * ROW_BYTES
        rows[row_index] = envelope.rows_blob[start:start + ROW_BYTES]
    return CompletedFrame(
        camera_id=envelope.camera_id,
        frame_id=envelope.frame_id,
        row_count=envelope.row_count,
        rows=rows,
        missing_rows=list(envelope.missing_rows),
        had_overflow=envelope.had_overflow,
        status=FrameStatus(envelope.status),
        close_reason=envelope.close_reason,
        expected_rows=envelope.expected_rows,
        packet_records=[],
        errors=[],
        duplicate_packets=0,
        conflicting_duplicates=0,
        started_at=envelope.started_at,
        ended_at=envelope.ended_at,
        saw_first=envelope.saw_first,
        saw_last=envelope.saw_last,
    )


def _run_image_publication_worker(
    images_root: str,
    expected_rows: int,
    bit_order: BitOrder,
    image_policy: ImagePolicy,
    max_missing_rows: int,
    max_consecutive_missing: int,
    report_interval: float,
    work_queue,
    result_queue,
) -> None:
    pipeline = CameraImagePipeline(
        images_root,
        expected_rows=expected_rows,
        bit_order=bit_order,
        image_policy=image_policy,
        max_missing_rows=max_missing_rows,
        max_consecutive_missing=max_consecutive_missing,
        report_interval=report_interval,
        enable_row_csv=False,
        publish_async=False,
        report_sink=print,
        error_sink=lambda message: print(message, file=sys.stderr),
    )
    published = 0
    failures = 0
    while True:
        item = work_queue.get()
        if isinstance(item, _PublisherStop):
            pipeline.close()
            result_queue.put((pipeline.stats, published, failures))
            return
        try:
            pipeline.archive_frame(_envelope_to_frame(item))
            published += 1
        except Exception as exc:  # noqa: BLE001 - keep the child alive
            failures += 1
            print(
                f"[IMAGE PUBLISH ERROR] CAM{getattr(item, 'camera_id', '?')}: {exc}",
                file=sys.stderr,
            )


@dataclass
class CsvWriterStatistics:
    queue_capacity: int = 0
    queue_peak: int = 0
    rows_submitted: int = 0
    rows_written: int = 0
    rows_dropped: int = 0
    writer_failures: int = 0
    flush_count: int = 0
    flush_latency_ms_total: float = 0.0
    flush_latency_ms_max: float = 0.0

    @property
    def flush_latency_ms_mean(self) -> float:
        if self.flush_count == 0:
            return 0.0
        return self.flush_latency_ms_total / self.flush_count


class CsvBackpressure(str, Enum):
    """What to do when the bounded CSV queue is full.

    ``DROP`` is the live-capture default: telemetry must never become the
    reason the image path falls behind.  ``BLOCK`` is the evidence default for
    offline replay, where a slow disk is not a reason to lose an audit row.
    """

    DROP = "drop"
    BLOCK = "block"


@dataclass
class PublisherProcessStatistics:
    """Parent-side view of the S2 publication process."""

    enabled: bool = False
    queue_capacity: int = 0
    submitted: int = 0
    published: int = 0
    failures: int = 0
    submit_blocked_seconds: float = 0.0
    submit_blocked_count: int = 0
    stats_returned: bool = False


@dataclass
class ImagePublicationStatistics:
    completed_frames_seen: int = 0
    noncomplete_frames_skipped: int = 0
    recovery_failures: int = 0
    raw_write_attempts: int = 0
    raw_write_success: int = 0
    raw_write_failures: int = 0
    pgm_write_attempts: int = 0
    pgm_write_success: int = 0
    pgm_write_failures: int = 0
    images_complete: int = 0
    images_recovered: int = 0
    images_rejected: int = 0
    rows_zero_filled: int = 0
    reject_reasons: Counter[str] | None = None

    def __post_init__(self) -> None:
        if self.reject_reasons is None:
            self.reject_reasons = Counter()


class ImagePolicy(str, Enum):
    """Layer-5 image publication policy; Layer1-3 remain unchanged."""

    STRICT = "strict"
    RECOVER_ZERO_FILL = "recover-zero-fill"


@dataclass(frozen=True)
class RecoveryDecision:
    eligible: bool
    reject_reasons: tuple[str, ...]
    missing_rows: tuple[int, ...]
    max_consecutive_missing: int
    wire_row_bytes: int
    row_bytes: int


class CameraImagePipeline:
    """Publish ``camN/<frame_id>.pgm`` and append ``camN/rows.csv``."""

    def __init__(
        self,
        images_root: str | Path,
        *,
        expected_rows: int = 480,
        bit_order: BitOrder | str = BitOrder.MSB_FIRST,
        precreate_cameras: tuple[int, ...] = (0, 1),
        csv_flush_rows: int = 256,
        csv_flush_seconds: float = 0.5,
        enable_row_csv: bool = True,
        csv_queue_depth: int = 65536,
        csv_backpressure: CsvBackpressure | str = CsvBackpressure.DROP,
        image_policy: ImagePolicy | str = ImagePolicy.STRICT,
        max_missing_rows: int = 4,
        max_consecutive_missing: int = 2,
        report_interval: float = 1.0,
        report_sink: Callable[[str], None] = print,
        error_sink: Callable[[str], None] | None = None,
        publish_async: bool = False,
        publisher_queue_depth: int = 256,
    ) -> None:
        if expected_rows <= 0:
            raise ValueError("expected_rows must be positive")
        if csv_flush_rows <= 0:
            raise ValueError("csv_flush_rows must be positive")
        if csv_flush_seconds <= 0:
            raise ValueError("csv_flush_seconds must be positive")
        if csv_queue_depth <= 0:
            raise ValueError("csv_queue_depth must be positive")
        if max_missing_rows < 0:
            raise ValueError("max_missing_rows must be non-negative")
        if max_consecutive_missing < 1:
            raise ValueError("max_consecutive_missing must be positive")
        if report_interval <= 0:
            raise ValueError("report_interval must be positive")
        if publish_async and publisher_queue_depth <= 0:
            raise ValueError("publisher_queue_depth must be positive")
        self.images_root = Path(images_root)
        self.expected_rows = expected_rows
        self.bit_order = BitOrder(bit_order)
        self.image_policy = ImagePolicy(image_policy)
        self.max_missing_rows = max_missing_rows
        # This is deliberately an exclusive reject threshold: the requested
        # CLI value 2 rejects a run of two missing rows and therefore permits
        # at most one consecutive missing row.
        self.max_consecutive_missing = max_consecutive_missing
        self.report_interval = report_interval
        self.report_sink = report_sink
        self.csv_flush_rows = csv_flush_rows
        self.csv_flush_seconds = csv_flush_seconds
        self.enable_row_csv = enable_row_csv
        self.csv_queue_depth = csv_queue_depth
        self.csv_backpressure = CsvBackpressure(csv_backpressure)
        self.publish_async = publish_async
        self.publisher_queue_depth = publisher_queue_depth
        self.error_sink = error_sink or (
            lambda message: print(message, file=sys.stderr)
        )
        self.images_root.mkdir(parents=True, exist_ok=True)
        for cam_id in precreate_cameras:
            self._camera_dir(cam_id).mkdir(parents=True, exist_ok=True)
        self._csv_lock = threading.Lock()
        self._csv_sinks: dict[int, _CsvSink] = {}
        self.stats = ImagePublicationStatistics()
        self.csv_stats = CsvWriterStatistics(queue_capacity=csv_queue_depth)
        self._rate_started = time.monotonic()
        self._rate_last = self._rate_started
        self._rate_last_counts = (0, 0, 0)
        self._publisher_context: mp.context.BaseContext | None = None
        self._publisher_queue = None
        self._publisher_result_queue = None
        self._publisher_process: mp.Process | None = None
        self._publisher_stats: ImagePublicationStatistics | None = None
        self.publisher_stats = PublisherProcessStatistics(
            enabled=self.publish_async,
            queue_capacity=publisher_queue_depth if self.publish_async else 0,
        )
        if self.publish_async:
            self._publisher_context = mp.get_context("spawn")
            self._publisher_queue = self._publisher_context.Queue(
                maxsize=self.publisher_queue_depth
            )
            self._publisher_result_queue = self._publisher_context.Queue(
                maxsize=1
            )
            self._publisher_process = self._publisher_context.Process(
                target=_run_image_publication_worker,
                name="taxi-image-publisher",
                args=(
                    str(self.images_root),
                    self.expected_rows,
                    self.bit_order,
                    self.image_policy,
                    self.max_missing_rows,
                    self.max_consecutive_missing,
                    self.report_interval,
                    self._publisher_queue,
                    self._publisher_result_queue,
                ),
                daemon=True,
            )
            self._publisher_process.start()

        # P2: rows.csv leaves the packet-consumer hot path.  ``record_packet``
        # now only stamps a capture index and enqueues; all formatting, all
        # duplicate bookkeeping and all disk I/O happen on this thread.
        self._capture_index = itertools.count()
        self._csv_queue: "queue.Queue[_RowEvent | object]" = queue.Queue(
            maxsize=csv_queue_depth
        )
        self._csv_closed = False
        self._csv_thread: threading.Thread | None = None
        if self.enable_row_csv:
            self._csv_thread = threading.Thread(
                target=self._run_csv_writer,
                name="taxi-rows-csv",
                daemon=True,
            )
            self._csv_thread.start()

    def record_packet(self, ctx: FrameContext) -> None:
        """Hand one parsed Camera packet to the CSV writer thread.

        This runs on the packet-consumer thread and must stay cheap: one
        counter increment, one small object and one bounded ``put``.  Packets
        with CRC/flag validation errors are intentionally retained as evidence.
        Ethernet frames that could not be parsed into a complete 128-byte
        Camera packet have no trustworthy cam_id and cannot be routed into a
        camN file.
        """

        if not self.enable_row_csv or self._csv_closed:
            return
        result = ctx.camera_result
        if result is None or result.packet is None:
            return

        event = _RowEvent(
            capture_index=next(self._capture_index),
            timestamp=ctx.frame.timestamp,
            result=result,
        )
        self.csv_stats.rows_submitted += 1
        if self.csv_backpressure is CsvBackpressure.BLOCK:
            self._csv_queue.put(event)
        else:
            try:
                self._csv_queue.put_nowait(event)
            except queue.Full:
                # Counted, never silent.  A gap in capture_index plus a nonzero
                # csv_rows_dropped is the signature this column exists for.
                self.csv_stats.rows_dropped += 1
                return
        depth = self._csv_queue.qsize()
        if depth > self.csv_stats.queue_peak:
            self.csv_stats.queue_peak = depth

    def flush_rows(self, timeout: float = 30.0) -> bool:
        """Block until every submitted row has been written and flushed.

        Returns False if the writer thread did not drain within ``timeout``.
        Tests and shutdown use this instead of sleeping.
        """

        if self._csv_thread is None:
            return True
        deadline = time.monotonic() + timeout
        while not self._csv_queue.empty():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        with self._csv_lock:
            now = time.monotonic()
            for sink in self._csv_sinks.values():
                self._emit_batch(sink, now)
        return True

    def close(self) -> None:
        """Drain the writer thread, then flush and close every camera CSV.

        Idempotent: the CLI calls this once to make the final report truthful
        and again from its safety-net ``finally``.
        """

        if self._csv_closed:
            return
        self._csv_closed = True
        if self._publisher_process is not None:
            assert self._publisher_queue is not None
            assert self._publisher_result_queue is not None
            self._publisher_queue.put(_PublisherStop())
            try:
                (
                    self._publisher_stats,
                    published,
                    failures,
                ) = self._publisher_result_queue.get(timeout=120.0)
                self.publisher_stats.published = published
                self.publisher_stats.failures = failures
                self.publisher_stats.stats_returned = True
            except Exception:
                self.error_sink(
                    "[IMAGE PUBLISH] child process did not report final stats "
                    "within 120s; final image counters may be stale"
                )
            self._publisher_process.join(timeout=30.0)
            if self._publisher_process.is_alive():
                self.error_sink(
                    "[IMAGE PUBLISH] child process did not stop within 30s; "
                    "image publication may be truncated"
                )
            if self._publisher_stats is not None:
                # The parent never touched these counters in async mode, so the
                # child's copy is the only truthful version.
                self.stats = self._publisher_stats
            self._publisher_process = None
        thread = self._csv_thread
        if thread is not None:
            self._csv_queue.put(_CSV_STOP)
            thread.join(timeout=30.0)
            if thread.is_alive():
                self.error_sink(
                    "[ROW CSV] writer thread did not stop within 30s; "
                    "rows.csv may be truncated"
                )
            self._csv_thread = None
        with self._csv_lock:
            now = time.monotonic()
            for sink in self._csv_sinks.values():
                self._emit_batch(sink, now)
                sink.handle.close()
            self._csv_sinks.clear()

    # ---- CSV writer thread ------------------------------------------------

    def _run_csv_writer(self) -> None:
        while True:
            try:
                event = self._csv_queue.get(timeout=self.csv_flush_seconds)
            except queue.Empty:
                # Idle: make whatever is buffered visible on disk promptly.
                self._flush_idle_sinks()
                continue
            try:
                if event is _CSV_STOP:
                    return
                try:
                    self._write_row_event(event)  # type: ignore[arg-type]
                except Exception as exc:  # noqa: BLE001 - keep writer alive
                    self.csv_stats.writer_failures += 1
                    self.error_sink(f"[ROW CSV ERROR] {exc}")
            finally:
                self._csv_queue.task_done()

    def _flush_idle_sinks(self) -> None:
        with self._csv_lock:
            now = time.monotonic()
            for sink in self._csv_sinks.values():
                if sink.batch:
                    self._emit_batch(sink, now)

    def _write_row_event(self, event: _RowEvent) -> None:
        result = event.result
        packet = result.packet
        header = packet.header
        trailer = packet.trailer
        flags = header.row_flags
        frame_end = (flags & FLAG_LAST_ROW) == FLAG_LAST_ROW
        layer3_valid = bool(result.ok)

        csv_path = self._camera_dir(header.cam_id) / "rows.csv"
        with self._csv_lock:
            sink = self._csv_sink(header.cam_id, csv_path)

            # Mirror of FrameReassembler's ``accepted = not errors and not
            # duplicate``.  The session resets on frame switch, matching the
            # reassembler's frame_switch close.
            if sink.mirror_frame_id != header.frame_id:
                sink.mirror_frame_id = header.frame_id
                sink.mirror_rows = set()
            row_accepted = layer3_valid and header.row_idx not in sink.mirror_rows
            if row_accepted:
                sink.mirror_rows.add(header.row_idx)

            # Frame start is a row-index property.  Offset-9 bit 2 means the
            # first MCU-processed Sobel row (row 2), not row 0.  LAST remains
            # a sender flag and is accepted only at the expected final row.
            reliable_first = (
                layer3_valid
                and header.row_idx == 0
            )
            reliable_last = (
                layer3_valid
                and header.row_idx == self.expected_rows - 1
                and bool(flags & FLAG_LAST_ROW)
            )

            sink.csv_sequence += 1
            row = {
                "timestamp": f"{event.timestamp:.9f}",
                "capture_index": event.capture_index,
                "csv_sequence": sink.csv_sequence,
                "cam_id": header.cam_id,
                "frame_id": header.frame_id,
                "row_idx": header.row_idx,
                "row_seq": header.row_seq,
                "row_flags": f"0x{flags:02X}",
                "first_row": int(packet.first_row),
                "first_processed_row": int(packet.first_processed_row),
                "last_row": int(bool(flags & FLAG_LAST_ROW)),
                "fpga_status": f"0x{header.fpga_status:02X}",
                "header_check": f"0x{header.header_check:02X}",
                "frame_overflow": int(packet.frame_overflow),
                "length_error": int(packet.length_error),
                "frame_end": int(frame_end),
                "sync0": f"0x{header.sync0:04X}",
                "sync1": f"0x{header.sync1:04X}",
                "sync_ok": int(
                    header.sync0 == SYNC0_DEFAULT
                    and header.sync1 == SYNC1_DEFAULT
                ),
                "payload_len": header.payload_len,
                "payload_len_ok": int(0 < header.payload_len <= ROW_BYTES),
                "crc_ok": int(packet.crc_ok),
                "received_crc": f"0x{packet.received_crc:04X}",
                "calculated_crc": f"0x{packet.calculated_crc:04X}",
                "trailer_pad_zero": int(not any(trailer.pad)),
                "m00": trailer.m00,
                "xc_q4": trailer.xc_q4,
                "yc_q4": trailer.yc_q4,
                "x": f"{trailer.xc_q4 / 16.0:.4f}",
                "y": f"{trailer.yc_q4 / 16.0:.4f}",
                "vx_q8": trailer.vx_q8,
                "vy_q8": trailer.vy_q8,
                "vx": f"{trailer.vx_q8 / 256.0:.6f}",
                "vy": f"{trailer.vy_q8 / 256.0:.6f}",
                "parse_ok": int(result.ok),
                "layer3_valid": int(layer3_valid),
                "row_accepted": int(row_accepted),
                "reliable_first": int(reliable_first),
                "reliable_last": int(reliable_last),
                "errors": ";".join(result.errors),
                "warnings": ";".join(result.warnings),
            }

            sink.batch.append(row)
            if reliable_last:
                sink.blank_after.append(len(sink.batch) - 1)
            sink.pending_rows += 1
            now = time.monotonic()
            if (
                sink.pending_rows >= self.csv_flush_rows
                or now - sink.last_flush >= self.csv_flush_seconds
            ):
                self._emit_batch(sink, now)

    def _emit_batch(self, sink: _CsvSink, now: float) -> None:
        """Write the buffered rows with ``writerows`` and flush once.

        Blank lines are real newlines, not comma-filled empty CSV records, so
        the batch is split around them to keep their position exact.
        """

        if sink.batch:
            start = 0
            for index in sink.blank_after:
                sink.writer.writerows(sink.batch[start:index + 1])
                sink.handle.write("\n")
                start = index + 1
            if start < len(sink.batch):
                sink.writer.writerows(sink.batch[start:])
            self.csv_stats.rows_written += len(sink.batch)
            sink.batch.clear()
            sink.blank_after.clear()
        started = time.perf_counter()
        sink.handle.flush()
        latency_ms = (time.perf_counter() - started) * 1000.0
        self.csv_stats.flush_count += 1
        self.csv_stats.flush_latency_ms_total += latency_ms
        if latency_ms > self.csv_stats.flush_latency_ms_max:
            self.csv_stats.flush_latency_ms_max = latency_ms
        sink.pending_rows = 0
        sink.last_flush = now

    def archive_frame(self, frame: CompletedFrame) -> Path | None:
        """Publish a COMPLETE image or an explicitly eligible RECOVERED image."""

        if self.publish_async:
            # Every frame goes across, not only COMPLETE ones.  Recovery
            # assessment plus a ZERO_FILL unpack is the most expensive path in
            # this module, so leaving non-complete frames in the caller's
            # thread would keep exactly the work S2 exists to move.
            if self._publisher_queue is None:
                raise RuntimeError("image publication process is not available")
            envelope = _frame_to_envelope(frame)
            self.publisher_stats.submitted += 1
            started = time.monotonic()
            self._publisher_queue.put(envelope)
            blocked = time.monotonic() - started
            # An mp.Queue feeder makes put() nearly free until the bound is hit,
            # so a growing blocked total is the signal that the child, not the
            # caller, has become the ceiling.
            if blocked > 0.001:
                self.publisher_stats.submit_blocked_seconds += blocked
                self.publisher_stats.submit_blocked_count += 1
            return None

        decision: RecoveryDecision | None = None
        if frame.status is FrameStatus.COMPLETE:
            self.stats.completed_frames_seen += 1
            output_status = "COMPLETE"
            missing_policy = MissingRowPolicy.REJECT
        elif self.image_policy is ImagePolicy.RECOVER_ZERO_FILL:
            self.stats.noncomplete_frames_skipped += 1
            decision = self._assess_recovery(frame)
            if not decision.eligible:
                self._record_rejection(frame, decision)
                return None
            output_status = "RECOVERED"
            missing_policy = MissingRowPolicy.ZERO_FILL
        else:
            self.stats.noncomplete_frames_skipped += 1
            # Preserve strict mode's pre-change disk behavior: incomplete
            # frames produce no image-side artifact.  They are counted for
            # truthful rate reporting but rejected.csv is recovery-mode
            # evidence only.
            self.stats.images_rejected += 1
            assert self.stats.reject_reasons is not None
            self.stats.reject_reasons["strict_requires_complete"] += 1
            self._maybe_report_rates()
            return None

        try:
            recovered = recover_completed_frame(
                frame,
                self.expected_rows,
                bit_order=self.bit_order,
                missing_policy=missing_policy,
            )
        except Exception:
            self.stats.recovery_failures += 1
            raise

        required_height = (
            480 if output_status == "RECOVERED" else self.expected_rows
        )
        if (
            recovered.height != required_height
            or recovered.width != ROW_PIXELS
            or len(recovered.pixels) != required_height * ROW_PIXELS
        ):
            raise ValueError(
                "image geometry mismatch after publication recovery: "
                f"{recovered.width}x{recovered.height}, "
                f"{len(recovered.pixels)} bytes"
            )

        camera_dir = self._camera_dir(frame.camera_id)
        if output_status == "RECOVERED":
            artifact_dir = (
                camera_dir / "recovered" / f"frame_{frame.frame_id}"
            )
            artifact_dir.mkdir(parents=True, exist_ok=True)
            pgm_path = artifact_dir / "image.pgm"
            raw_path = artifact_dir / "image.raw"
            metadata_path = artifact_dir / "metadata.json"
            row_csv_ref = "../../rows.csv"
        else:
            camera_dir.mkdir(parents=True, exist_ok=True)
            stem = str(frame.frame_id)
            pgm_path = camera_dir / f"{stem}.pgm"
            raw_path = camera_dir / f"{stem}.raw"
            metadata_path = camera_dir / f"{stem}.json"
            row_csv_ref = "rows.csv"

        targets = (pgm_path, raw_path, metadata_path)
        pgm = (
            f"P5\n{recovered.width} {recovered.height}\n255\n".encode("ascii")
            + recovered.pixels
        )
        metadata = {
            "cam_id": frame.camera_id,
            "frame_id": frame.frame_id,
            "status": output_status,
            "close_reason": frame.close_reason,
            "width": recovered.width,
            "height": recovered.height,
            "pixel_format": "threshold_u8_0_255",
            "wire_row_format": f"{ROW_BYTES}_byte_packed_1bpp",
            "bit_order": recovered.bit_order.value,
            "row_count": frame.row_count,
            "expected_rows": required_height,
            "missing_rows": (
                list(decision.missing_rows) if decision is not None else []
            ),
            "missing_count": (
                len(decision.missing_rows) if decision is not None else 0
            ),
            "max_consecutive_missing": (
                decision.max_consecutive_missing if decision is not None else 0
            ),
            "fill_policy": "zero" if decision is not None else "none",
            "row_bytes": recovered.width,
            "wire_row_bytes": ROW_BYTES,
            "had_overflow": frame.had_overflow,
            "had_crc_error": False,
            "had_sync_error": False,
            "had_conflicting_duplicate": False,
            "pixels_sha256": hashlib.sha256(recovered.pixels).hexdigest(),
            "pgm_file": pgm_path.name,
            "raw_file": raw_path.name,
            "row_csv": row_csv_ref,
        }

        existing = [str(path) for path in targets if path.exists()]
        if existing:
            if self._already_published(
                pgm_path,
                raw_path,
                metadata_path,
                pgm,
                recovered.pixels,
                metadata,
            ):
                return pgm_path
            raise FileExistsError(
                "refusing to overwrite numbered image artifact(s): "
                + ", ".join(existing)
            )

        # Each visible file appears only after its complete temporary file has
        # been closed.  Existing numbered frames are never silently replaced.
        self.stats.pgm_write_attempts += 1
        try:
            self._atomic_create(pgm_path, pgm)
        except Exception:
            self.stats.pgm_write_failures += 1
            raise
        else:
            self.stats.pgm_write_success += 1

        self.stats.raw_write_attempts += 1
        try:
            self._atomic_create(raw_path, recovered.pixels)
        except Exception:
            self.stats.raw_write_failures += 1
            raise
        else:
            self.stats.raw_write_success += 1
        self._atomic_create(
            metadata_path,
            (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
        if output_status == "RECOVERED":
            assert decision is not None
            self.stats.images_recovered += 1
            self.stats.rows_zero_filled += len(decision.missing_rows)
        else:
            self.stats.images_complete += 1
        self._maybe_report_rates()
        return pgm_path

    def _assess_recovery(self, frame: CompletedFrame) -> RecoveryDecision:
        """Apply the opt-in recovery gate without relaxing packet validation."""

        reasons: list[str] = []
        if self.expected_rows != 480 or frame.expected_rows != 480:
            reasons.append("expected_rows_not_480")
        if not 0 <= frame.frame_id <= 0xFFFF:
            reasons.append("frame_id_out_of_range")

        accepted_records = [
            record for record in frame.packet_records if record.accepted
        ]
        reliable_last = any(
            record.row_idx == 479
            and bool(record.row_flags & FLAG_LAST_ROW)
            for record in accepted_records
        )
        if not reliable_last:
            reasons.append("reliable_last_row_not_seen")

        all_errors = {
            error
            for record in frame.packet_records
            for error in record.errors
        }
        if frame.had_overflow or "frame_overflow" in all_errors:
            reasons.append("overflow")
        if "bad_sync" in all_errors:
            reasons.append("bad_sync")
        if "crc_error" in all_errors:
            reasons.append("crc_error")
        if frame.conflicting_duplicates or any(
            record.conflicting_duplicate for record in frame.packet_records
        ):
            reasons.append("conflicting_duplicate")

        # Length-invalid rows may account for a missing row, but their payload
        # is never used.  Any other Layer-3 error is a hard recovery rejection.
        permitted_missing_row_errors = {
            "length_error",
            "payload_len_out_of_range",
        }
        unhandled_errors = sorted(all_errors - permitted_missing_row_errors - {
            "frame_overflow",
            "bad_sync",
            "crc_error",
        })
        reasons.extend(f"layer3_error:{error}" for error in unhandled_errors)

        valid_row_indices = set(frame.rows)
        accepted_row_indices = {record.row_idx for record in accepted_records}
        if any(index < 0 or index >= 480 for index in valid_row_indices):
            reasons.append("row_idx_out_of_range")
        if any(index < 0 or index >= 480 for index in accepted_row_indices):
            reasons.append("row_idx_out_of_range")

        valid_in_range = {
            index for index in valid_row_indices if 0 <= index < 480
        }
        expected = set(range(480))
        missing_rows = tuple(sorted(expected - valid_in_range))
        max_consecutive = self._max_consecutive(missing_rows)
        if not missing_rows:
            reasons.append("no_missing_rows_to_recover")
        if len(missing_rows) > self.max_missing_rows:
            reasons.append("too_many_missing_rows")
        if max_consecutive >= self.max_consecutive_missing:
            reasons.append("consecutive_missing_rows")

        row_lengths = {len(frame.rows[index]) for index in valid_in_range}
        if not row_lengths:
            reasons.append("no_valid_rows")
        elif row_lengths != {ROW_BYTES}:
            reasons.append("row_byte_length_mismatch")

        if self.bit_order not in (BitOrder.MSB_FIRST, BitOrder.LSB_FIRST):
            reasons.append("unsupported_pixel_format")

        # The accepted row sequence must advance by exactly the row-index gap.
        # This admits known missing rows and natural 16-bit wrap, but rejects a
        # jump that cannot be explained by either condition.
        records_by_row = {
            record.row_idx: record
            for record in accepted_records
            if 0 <= record.row_idx < 480
        }
        ordered = sorted(records_by_row.items())
        for (previous_idx, previous), (current_idx, current) in zip(
            ordered, ordered[1:]
        ):
            row_gap = current_idx - previous_idx
            sequence_gap = (current.row_seq - previous.row_seq) & 0xFFFF
            if sequence_gap != row_gap:
                reasons.append("row_seq_discontinuity")
                break

        return RecoveryDecision(
            eligible=not reasons,
            reject_reasons=tuple(dict.fromkeys(reasons)),
            missing_rows=missing_rows,
            max_consecutive_missing=max_consecutive,
            wire_row_bytes=ROW_BYTES,
            row_bytes=ROW_PIXELS,
        )

    @staticmethod
    def _max_consecutive(rows: tuple[int, ...]) -> int:
        longest = 0
        current = 0
        previous: int | None = None
        for row in rows:
            current = current + 1 if previous is not None and row == previous + 1 else 1
            longest = max(longest, current)
            previous = row
        return longest

    def _record_rejection(
        self,
        frame: CompletedFrame,
        decision: RecoveryDecision,
    ) -> None:
        self.stats.images_rejected += 1
        assert self.stats.reject_reasons is not None
        for reason in decision.reject_reasons:
            self.stats.reject_reasons[reason] += 1

        camera_dir = self._camera_dir(frame.camera_id)
        camera_dir.mkdir(parents=True, exist_ok=True)
        path = camera_dir / "rejected.csv"
        exists = path.exists() and path.stat().st_size > 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if not exists:
                writer.writerow(
                    (
                        "timestamp",
                        "cam_id",
                        "frame_id",
                        "publication_status",
                        "frame_status",
                        "close_reason",
                        "reject_reason",
                        "missing_count",
                        "missing_rows",
                        "max_consecutive_missing",
                    )
                )
            writer.writerow(
                (
                    f"{time.time():.9f}",
                    frame.camera_id,
                    frame.frame_id,
                    "REJECTED",
                    frame.status.value,
                    frame.close_reason,
                    ";".join(decision.reject_reasons),
                    len(decision.missing_rows),
                    " ".join(str(row) for row in decision.missing_rows),
                    decision.max_consecutive_missing,
                )
            )
        self._maybe_report_rates()

    def _maybe_report_rates(self, *, force: bool = False) -> None:
        now = time.monotonic()
        elapsed = now - self._rate_last
        if not force and elapsed < self.report_interval:
            return
        counts = (
            self.stats.images_complete,
            self.stats.images_recovered,
            self.stats.images_rejected,
        )
        if elapsed <= 0:
            return
        deltas = tuple(
            current - previous
            for current, previous in zip(counts, self._rate_last_counts)
        )
        self.report_sink(
            "[IMAGE RATE] "
            f"complete_fps={deltas[0] / elapsed:.3f} "
            f"recovered_fps={deltas[1] / elapsed:.3f} "
            f"rejected_fps={deltas[2] / elapsed:.3f} "
            f"total_usable_fps={(deltas[0] + deltas[1]) / elapsed:.3f}"
        )
        self._rate_last = now
        self._rate_last_counts = counts

    def report_lines(self) -> tuple[str, ...]:
        """Return stable final-report lines without coupling this sink to CLI."""

        elapsed = max(time.monotonic() - self._rate_started, 1e-9)
        reject_summary = (
            ", ".join(
                f"{reason}={count}"
                for reason, count in sorted(
                    (self.stats.reject_reasons or {}).items()
                )
            )
            or "none"
        )
        publisher_lines: tuple[str, ...] = ()
        if self.publisher_stats.enabled:
            publisher = self.publisher_stats
            publisher_lines = (
                f"  publish mode        : process "
                f"(queue={publisher.queue_capacity})",
                f"  publisher submitted : {publisher.submitted}",
                f"  publisher published : {publisher.published}",
                f"  publisher failures  : {publisher.failures}",
                f"  publisher stats ok  : {int(publisher.stats_returned)}",
                f"  publisher blocked   : "
                f"{publisher.submit_blocked_seconds:.3f} s "
                f"over {publisher.submit_blocked_count} waits",
            )
        else:
            publisher_lines = ("  publish mode        : in-thread",)
        return (
            "IMAGE PUBLICATION",
            f"  image policy        : {self.image_policy.value}",
            *publisher_lines,
            f"  complete frames seen: {self.stats.completed_frames_seen}",
            f"  noncomplete skipped : {self.stats.noncomplete_frames_skipped}",
            f"  recovery failures   : {self.stats.recovery_failures}",
            f"  images complete     : {self.stats.images_complete}",
            f"  images recovered    : {self.stats.images_recovered}",
            f"  images rejected     : {self.stats.images_rejected}",
            f"  rows zero-filled    : {self.stats.rows_zero_filled}",
            f"  complete_fps        : "
            f"{self.stats.images_complete / elapsed:.3f}",
            f"  recovered_fps       : "
            f"{self.stats.images_recovered / elapsed:.3f}",
            f"  total_usable_fps    : "
            f"{(self.stats.images_complete + self.stats.images_recovered) / elapsed:.3f}",
            f"  reject reasons      : {reject_summary}",
            f"  RAW attempts/success/fail: "
            f"{self.stats.raw_write_attempts}/"
            f"{self.stats.raw_write_success}/"
            f"{self.stats.raw_write_failures}",
            f"  PGM attempts/success/fail: "
            f"{self.stats.pgm_write_attempts}/"
            f"{self.stats.pgm_write_success}/"
            f"{self.stats.pgm_write_failures}",
            f"  resolved images root: {self.images_root.resolve()}",
            "",
            "ROW CSV WRITER",
            f"  rows.csv enabled     : {int(self.enable_row_csv)}",
            f"  backpressure policy  : {self.csv_backpressure.value}",
            f"  csv_queue_capacity   : {self.csv_stats.queue_capacity}",
            f"  csv_queue_peak       : {self.csv_stats.queue_peak}",
            f"  csv_rows_submitted   : {self.csv_stats.rows_submitted}",
            f"  csv_rows_written     : {self.csv_stats.rows_written}",
            f"  csv_rows_dropped     : {self.csv_stats.rows_dropped}",
            f"  csv_writer_failures  : {self.csv_stats.writer_failures}",
            f"  csv_flush_latency_ms : "
            f"mean={self.csv_stats.flush_latency_ms_mean:.3f} "
            f"max={self.csv_stats.flush_latency_ms_max:.3f} "
            f"count={self.csv_stats.flush_count}",
        )

    def _camera_dir(self, cam_id: int) -> Path:
        if cam_id < 0:
            raise ValueError(f"cam_id must be non-negative, got {cam_id}")
        return self.images_root / f"cam{cam_id}"

    def _csv_sink(self, cam_id: int, path: Path) -> _CsvSink:
        existing_sink = self._csv_sinks.get(cam_id)
        if existing_sink is not None:
            return existing_sink

        path.parent.mkdir(parents=True, exist_ok=True)
        has_content = path.exists() and path.stat().st_size > 0
        if has_content:
            with path.open(newline="", encoding="utf-8") as check:
                header = next(csv.reader(check), [])
            if tuple(header) != ROW_CSV_FIELDS:
                raise ValueError(
                    f"existing CSV schema does not match current receiver: {path}"
                )

        handle = path.open("a", newline="", encoding="utf-8")
        writer = csv.DictWriter(handle, fieldnames=ROW_CSV_FIELDS)
        if not has_content:
            writer.writeheader()
            handle.flush()
        sink = _CsvSink(
            handle=handle,
            writer=writer,
            last_flush=time.monotonic(),
        )
        self._csv_sinks[cam_id] = sink
        return sink

    @staticmethod
    def _atomic_create(path: Path, data: bytes) -> None:
        temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temp.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists():
                raise FileExistsError(f"refusing to overwrite: {path}")
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    @staticmethod
    def _already_published(
        pgm_path: Path,
        raw_path: Path,
        metadata_path: Path,
        pgm: bytes,
        raw: bytes,
        metadata: dict[str, object],
    ) -> bool:
        if not (pgm_path.is_file() and raw_path.is_file() and metadata_path.is_file()):
            return False
        try:
            if pgm_path.read_bytes() != pgm:
                return False
            if raw_path.read_bytes() != raw:
                return False
            existing_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        return _stable_publication_metadata(existing_metadata) == _stable_publication_metadata(metadata)


def _stable_publication_metadata(metadata: dict[str, object]) -> dict[str, object]:
    volatile_keys = {
        "timestamp",
    }
    return {
        key: value
        for key, value in metadata.items()
        if key not in volatile_keys
    }
