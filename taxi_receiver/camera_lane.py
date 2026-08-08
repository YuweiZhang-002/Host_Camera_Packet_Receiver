"""Per-camera receive lanes (S1).

Layer 1-3 stays on the single shared worker: EtherType validation, the 128-byte
fixed-length check and CRC/parse.  Everything from Layer 5 upwards -- session
reassembly, rows.csv, recovery assessment and image/archive publication -- runs
on one dedicated thread per camera, each owning its own reassembler, CSV sink,
output dispatcher and image pipeline.

The point is head-of-line isolation: before this split, one 50 ms image
publication for cam1 stalled cam0 row ingestion, and both cameras were dropped
indiscriminately at a single shared queue.  Routing uses the cheap offset-18
cam_id peek taken on the capture thread (``peek_camera_id``), which is an
*untrusted* byte -- hence the explicit lane allowlist below.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from .async_sink import AsyncCallbackDispatcher
from .image_pipeline import CameraImagePipeline
from .reassembler import CompletedFrame, FrameReassembler, FrameStatus
from .session_audit import SessionAuditLogger
from .stages import FrameContext
from .storage import StorageAndPipeline
from .stream_monitor import StreamMonitor


DEFAULT_CAMERA_IDS: tuple[int, ...] = (0, 1, 2, 3)


class PublishPolicy(str, Enum):
    """Which completed frames are worth handing to the output sinks.

    ``COMPLETE`` is the live default.  A partial frame still costs a full
    ``to_bytes()`` serialisation, a sha256 and a recovery assessment in every
    sink, which is pure waste when the recovery gate is going to reject it
    anyway -- 2415 of 2767 frames in the 150 s attempt11 run.
    """

    COMPLETE = "complete"
    ELIGIBLE = "eligible"
    ALL = "all"

    def accepts(self, frame: CompletedFrame) -> bool:
        if self is PublishPolicy.ALL:
            return True
        if frame.status is FrameStatus.COMPLETE:
            return True
        if self is PublishPolicy.COMPLETE:
            return False
        # ELIGIBLE: let anything with at least one row through and leave the
        # real decision to CameraImagePipeline's recovery gate, which owns the
        # missing-row thresholds.
        return frame.row_count > 0


class _NamedCallbackError(RuntimeError):
    def __init__(self, failures):
        self.failures = tuple(failures)

    def __str__(self) -> str:
        return "; ".join(f"{name} failed: {exc}" for name, exc in self.failures)


def _fanout_callbacks(*callbacks):
    active = tuple(
        normalized
        for callback in callbacks
        if (normalized := _normalize_callback(callback)) is not None
    )
    if not active:
        return None

    def invoke(value) -> None:
        failures: list[tuple[str, Exception]] = []
        for name, callback in active:
            try:
                callback(value)
            except Exception as exc:  # noqa: BLE001 - isolate sink failure
                failures.append((name, exc))
        if failures:
            raise _NamedCallbackError(failures)

    return invoke


def _normalize_callback(callback):
    if callback is None:
        return None
    if isinstance(callback, tuple) and len(callback) == 2:
        name, value = callback
        if value is None:
            return None
        if not callable(value):
            raise TypeError(f"callback for {name!r} must be callable")
        return str(name), value
    if callable(callback):
        return getattr(callback, "__name__", callback.__class__.__name__), callback
    raise TypeError(f"unsupported callback specification: {callback!r}")


@dataclass
class CameraLaneConfig:
    output_root: Path | None
    images_root: Path | None
    enable_row_csv: bool
    expected_rows: int
    bit_order: str
    image_policy: str
    max_missing_rows: int
    max_consecutive_missing: int
    report_interval: float
    csv_queue_depth: int
    csv_backpressure: str
    frame_output_queue_depth: int
    lane_queue_depth: int
    # Shared, thread-safe sinks.  Both are deliberately NOT per-lane: storage
    # keeps one summary.csv and session audit keeps one session_audit.csv for
    # the whole run.  They serialise internally, so enabling either one
    # partially re-couples the lanes; session audit in particular writes
    # synchronously on the lane thread.
    storage: StorageAndPipeline | None
    monitor: StreamMonitor
    error_sink: Callable[[str], None]
    report_sink: Callable[[str], None]
    # True for offline replay: a lane queue must then block instead of drop,
    # otherwise the "replay is a complete audit" guarantee is void.
    lossless: bool = False
    publish_policy: PublishPolicy = PublishPolicy.COMPLETE
    # S2: move image publication into a dedicated process per lane.  Threads
    # cannot help here -- recovery unpacking, PGM encoding and RAW writing are
    # CPU work under the GIL, and the run001 report showed each lane thread
    # blocked 34 s of 65 s handing frames to its in-thread publisher.
    publish_async: bool = False
    publisher_queue_depth: int = 256
    on_frame_processed: Callable[[FrameContext], None] | None = None


class CameraLane:
    def __init__(self, camera_id: int, config: CameraLaneConfig) -> None:
        self.camera_id = camera_id
        self.config = config
        self.monitor = config.monitor
        self._queue: "queue.Queue[FrameContext | object]" = queue.Queue(
            maxsize=config.lane_queue_depth
        )
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._run,
            name=f"taxi-camera-lane-{camera_id}",
            daemon=True,
        )
        self.frames_not_published = 0
        self._reassembler = FrameReassembler(expected_rows=config.expected_rows)
        self._image_pipeline = self._build_image_pipeline()
        self._frame_outputs = self._build_frame_outputs()
        self._worker.start()

    def submit(self, ctx: FrameContext) -> bool:
        if self._stop_event.is_set():
            return False
        if self.config.lossless:
            # Mirror TaxiReceiverPipeline._on_frame: offline replay applies
            # backpressure rather than dropping evidence.  Without this the
            # lane queue silently voided 63% of a lossless replay while the
            # capture queue reported zero drops.
            while not self._stop_event.is_set():
                try:
                    self._queue.put(ctx, timeout=0.2)
                    break
                except queue.Full:
                    continue
            else:
                return False
        else:
            try:
                self._queue.put_nowait(ctx)
            except queue.Full:
                self.monitor.record_lane_drop(self.camera_id)
                return False
        self.monitor.record_lane_queue_depth(
            self._queue.qsize(),
            self.camera_id,
        )
        return True

    def stop(self) -> None:
        # Same ordering rule as TaxiReceiverPipeline.stop(): drain before
        # signalling.  Setting the event first races the shared worker -- the
        # lane worker can observe "stop set and queue momentarily empty" and
        # leave while a ctx is still being enqueued, after which queue.join()
        # below never returns.  Callers must have silenced the producer first.
        self._queue.join()
        self._stop_event.set()
        self._worker.join(timeout=5.0)
        if self._worker.is_alive():
            raise RuntimeError(
                f"camera lane worker {self.camera_id} did not stop after queue drain"
            )
        self._flush_completed_frames()
        for dispatcher in self._frame_outputs.values():
            dispatcher.close()
        if self._image_pipeline is not None:
            self._image_pipeline.close()

    @property
    def reassembler(self) -> FrameReassembler:
        return self._reassembler

    @property
    def image_pipeline(self) -> CameraImagePipeline | None:
        return self._image_pipeline

    @property
    def frame_outputs(
        self,
    ) -> dict[str, AsyncCallbackDispatcher[CompletedFrame]]:
        return dict(self._frame_outputs)

    def report_lines(self) -> tuple[str, ...]:
        camera = self.monitor.stats.camera(self.camera_id)
        lines = [f"CAMERA LANE {self.camera_id}"]
        lines.extend(
            [
                f"  lane queue capacity : {self.config.lane_queue_depth}",
                f"  lane queue peak     : {camera.lane_queue_peak}",
                f"  lane queue drops    : {camera.lane_queue_drops}",
                f"  publish policy      : {self.config.publish_policy.value}",
                f"  frames not published: {self.frames_not_published}",
            ]
        )
        lane_stats = self._reassembler.stats
        lines.extend(
            [
                f"  sessions created    : {lane_stats.sessions_created}",
                f"  rows accepted       : {lane_stats.rows_accepted}",
                f"  rows rejected       : {lane_stats.rows_rejected}",
                f"  frames complete     : {lane_stats.frames_completed}",
                f"  frames timeout      : {lane_stats.frames_timed_out}",
                f"  frames partial      : {lane_stats.frames_partial}",
                f"  frames corrupt      : {lane_stats.frames_corrupt}",
            ]
        )
        for name, dispatcher in self._frame_outputs.items():
            lines.append(f"  -- sink: {name}")
            lines.extend(dispatcher.report_lines())
        if self._image_pipeline is not None:
            lines.extend(self._image_pipeline.report_lines())
        return tuple(lines)

    def _build_image_pipeline(self) -> CameraImagePipeline | None:
        if self.config.images_root is None:
            return None
        return CameraImagePipeline(
            self.config.images_root,
            expected_rows=self.config.expected_rows,
            bit_order=self.config.bit_order,
            image_policy=self.config.image_policy,
            max_missing_rows=self.config.max_missing_rows,
            max_consecutive_missing=self.config.max_consecutive_missing,
            report_interval=self.config.report_interval,
            enable_row_csv=self.config.enable_row_csv,
            csv_queue_depth=self.config.csv_queue_depth,
            csv_backpressure=self.config.csv_backpressure,
            publish_async=self.config.publish_async,
            publisher_queue_depth=self.config.publisher_queue_depth,
            precreate_cameras=(self.camera_id,),
            report_sink=self.config.report_sink,
            error_sink=self.config.error_sink,
        )

    def _build_frame_outputs(
        self,
    ) -> dict[str, AsyncCallbackDispatcher[CompletedFrame]]:
        """One dispatcher per sink, not one fanout behind a shared queue.

        With a fanout, a permanently broken storage sink took the whole
        dispatcher down -- including the healthy image publication sharing it --
        and its blocked-time accounting could not say which sink was slow.
        """
        sinks: list[tuple[str, Callable[[CompletedFrame], None]]] = []
        if self.config.storage is not None:
            sinks.append(("storage", self.config.storage))
        if self._image_pipeline is not None:
            sinks.append(("images", self._image_pipeline.archive_frame))
        return {
            name: AsyncCallbackDispatcher(
                callback,
                queue_depth=self.config.frame_output_queue_depth,
                name=f"taxi-{name}-cam{self.camera_id}",
                error_sink=self.config.error_sink,
            )
            for name, callback in sinks
        }

    def _run(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                ctx = self._queue.get(timeout=0.2)
            except queue.Empty:
                self._poll_timeouts()
                self.monitor.maybe_report()
                continue

            try:
                self._process(ctx)
            except Exception as exc:  # noqa: BLE001 - keep the lane alive
                self.monitor.record_processing_error()
                self.config.error_sink(
                    f"[PROCESSING ERROR] CAM{self.camera_id}: {exc}"
                )
            finally:
                self._queue.task_done()

            self.monitor.maybe_report()

    def _process(self, ctx: FrameContext) -> None:
        result = ctx.camera_result
        if result is not None:
            self.monitor.record_camera_result(result)

        try:
            if result is None or result.packet is None:
                return

            completed = self._reassembler.on_row(
                result.packet,
                errors=result.errors,
                warnings=result.warnings,
                capture_timestamp=ctx.frame.timestamp,
                now=time.monotonic(),
            )
            self._emit(completed)
            drain = getattr(self._reassembler, "drain_completed", None)
            if drain is not None:
                for pending in drain():
                    self._emit(pending)
            if self._image_pipeline is not None:
                self._image_pipeline.record_packet(ctx)
        finally:
            if self.config.on_frame_processed is not None:
                self.config.on_frame_processed(ctx)

    def _emit(self, completed: Optional[CompletedFrame]) -> None:
        if completed is None:
            return
        if not self.config.publish_policy.accepts(completed):
            self.frames_not_published += 1
            return
        for dispatcher in self._frame_outputs.values():
            dispatcher.submit(completed)

    def _poll_timeouts(self) -> None:
        expire = getattr(self._reassembler, "expire", None)
        if expire is None:
            return
        for completed in expire(time.monotonic()):
            self._emit(completed)

    def _flush_completed_frames(self) -> None:
        for completed in self._reassembler.flush():
            self._emit(completed)


class CameraLanePool:
    def __init__(
        self,
        *,
        monitor: StreamMonitor,
        output_root: Path | None,
        images_root: Path | None,
        expected_rows: int,
        bit_order: str,
        image_policy: str,
        max_missing_rows: int,
        max_consecutive_missing: int,
        report_interval: float,
        enable_row_csv: bool,
        csv_queue_depth: int,
        csv_backpressure: str,
        frame_output_queue_depth: int,
        lane_queue_depth: int,
        session_audit: SessionAuditLogger | None,
        storage: StorageAndPipeline | None,
        report_sink: Callable[[str], None],
        error_sink: Callable[[str], None],
        camera_ids: Iterable[int] = DEFAULT_CAMERA_IDS,
        lossless: bool = False,
        publish_policy: PublishPolicy | str = PublishPolicy.COMPLETE,
        publish_async: bool = False,
        publisher_queue_depth: int = 256,
        extra_on_frame_processed: Callable[[FrameContext], None] | None = None,
    ) -> None:
        self.publish_async = publish_async
        self.publisher_queue_depth = publisher_queue_depth
        self.camera_ids = frozenset(int(cam_id) for cam_id in camera_ids)
        if not self.camera_ids:
            raise ValueError("camera_ids must not be empty")
        self.lossless = lossless
        self.publish_policy = PublishPolicy(publish_policy)
        self.monitor = monitor
        self.output_root = output_root
        self.images_root = images_root
        self.expected_rows = expected_rows
        self.bit_order = bit_order
        self.image_policy = image_policy
        self.max_missing_rows = max_missing_rows
        self.max_consecutive_missing = max_consecutive_missing
        self.report_interval = report_interval
        self.enable_row_csv = enable_row_csv
        self.csv_queue_depth = csv_queue_depth
        self.csv_backpressure = csv_backpressure
        self.frame_output_queue_depth = frame_output_queue_depth
        self.lane_queue_depth = lane_queue_depth
        self.session_audit = session_audit
        self.storage = storage
        self.report_sink = report_sink
        self.error_sink = error_sink
        self.extra_on_frame_processed = extra_on_frame_processed
        self._lanes: dict[int, CameraLane] = {}
        self._lock = threading.Lock()

    def submit(self, ctx: FrameContext) -> None:
        camera_id = self._camera_id(ctx)
        if camera_id is None:
            # The offset-18 peek is raw wire data.  A stuck data bit or a
            # bad-sync storm (the attempt8 0x8C case) would otherwise mint a
            # lane -- two threads, a CSV writer and a camN directory -- per
            # bogus byte value.  Count it and move on.
            self.monitor.record_unroutable_camera_packet()
            return
        self._lane_for(camera_id).submit(ctx)

    @property
    def lanes(self) -> tuple[CameraLane, ...]:
        return tuple(self._lanes[cam_id] for cam_id in sorted(self._lanes))

    def close(self) -> None:
        for camera_id in sorted(self._lanes):
            self._lanes[camera_id].stop()

    def report_lines(self) -> tuple[str, ...]:
        lines: list[str] = []
        for camera_id in sorted(self._lanes):
            if lines:
                lines.append("")
            lines.extend(self._lanes[camera_id].report_lines())
        return tuple(lines)

    def _camera_id(self, ctx: FrameContext) -> Optional[int]:
        """Resolve a routable camera id, or None if it is not trustworthy.

        A parsed header is preferred over the raw peek because Layer 3 has
        already checked sync and length by then.  Either way the value must be
        inside the configured lane set.
        """
        packet = ctx.camera_result.packet if ctx.camera_result is not None else None
        candidate = (
            packet.header.cam_id if packet is not None else ctx.frame.camera_id
        )
        if candidate is None or candidate not in self.camera_ids:
            return None
        return candidate

    def _lane_for(self, camera_id: int) -> CameraLane:
        lane = self._lanes.get(camera_id)
        if lane is not None:
            return lane

        with self._lock:
            lane = self._lanes.get(camera_id)
            if lane is not None:
                return lane

            lane = CameraLane(
                camera_id,
                CameraLaneConfig(
                    output_root=self.output_root,
                    images_root=self.images_root,
                    enable_row_csv=self.enable_row_csv,
                    expected_rows=self.expected_rows,
                    bit_order=self.bit_order,
                    image_policy=self.image_policy,
                    max_missing_rows=self.max_missing_rows,
                    max_consecutive_missing=self.max_consecutive_missing,
                    report_interval=self.report_interval,
                    csv_queue_depth=self.csv_queue_depth,
                    csv_backpressure=self.csv_backpressure,
                    frame_output_queue_depth=self.frame_output_queue_depth,
                    lane_queue_depth=self.lane_queue_depth,
                    storage=self.storage,
                    monitor=self.monitor,
                    error_sink=self.error_sink,
                    report_sink=self.report_sink,
                    lossless=self.lossless,
                    publish_policy=self.publish_policy,
                    publish_async=self.publish_async,
                    publisher_queue_depth=self.publisher_queue_depth,
                    on_frame_processed=_fanout_callbacks(
                        ("session audit", self.session_audit),
                        self.extra_on_frame_processed,
                    ),
                ),
            )
            self._lanes[camera_id] = lane
            return lane
