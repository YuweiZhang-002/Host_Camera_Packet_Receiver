"""
pipeline.py  --  orchestration only: capture-thread -> bounded queue ->
worker-thread. What actually happens to each frame is delegated
entirely to a `list[Stage]` (see stages.py) -- this module doesn't
know or care whether that's 2 stages or 4.

Two ways to get that stage list, both supported here:

  1. Declarative: pass `max_stage="validate"/"parse"/"monitor"/"reassemble"`
     and the pipeline builds the chain for you via
     `stages.build_stage_chain(...)` -- this is "Layer 1-2/1-3/1-4/1-5"
     by name.

  2. Manual / top-level: build your own `list[Stage]` (mixing in a
     custom Stage subclass if you want) and pass it as `stages=`.
     Useful when you want full control from a top-level script instead
     of going through the keyword-argument surface here.
"""
from __future__ import annotations

import queue
import sys
import threading
from typing import Callable, Optional

from .capture import FrameSource, RawEthernetFrame
from .reassembler import CompletedFrame, NullReassembler, RowReassembler
from .recorder import ErrorFrameRecorder, PcapRecorder
from .stages import FrameContext, ReassemblyStage, Stage, build_stage_chain
from .stream_monitor import StreamMonitor


class TaxiReceiverPipeline:
    def __init__(
        self,
        frame_source: FrameSource,
        mode: str = "camera",
        *,
        max_stage: str = "monitor",
        stages: Optional[list[Stage]] = None,
        reassembler: Optional[RowReassembler] = None,
        pcap_recorder: Optional[PcapRecorder] = None,
        error_recorder: Optional[ErrorFrameRecorder] = None,
        queue_depth: int = 8192,
        lossless_input: bool = False,
        split_by_camera: bool = False,
        report_interval: float = 1.0,
        sink: Callable[[str], None] = print,
        on_completed_frame: Optional[Callable[[CompletedFrame], None]] = None,
        on_frame_processed: Optional[Callable[[FrameContext], None]] = None,
    ) -> None:
        if queue_depth <= 0:
            raise ValueError("queue_depth must be positive")
        self.frame_source = frame_source
        self.mode = mode
        self.sink = sink
        self.pcap_recorder = pcap_recorder
        # Offline replay must apply backpressure instead of dropping evidence
        # merely because the parser is slower than disk iteration. Live
        # capture keeps the bounded, non-blocking behaviour so NIC overload is
        # visible as an explicit capture-drop statistic.
        self.lossless_input = lossless_input
        self.split_by_camera = split_by_camera
        # Layer-5 extension hook: fires whenever the (optional)
        # reassembler completes a frame. Inert unless a real
        # reassembler is in play.
        #
        # Assigned through the property below because ReassemblyStage captures
        # this callback at construction time.  Setting the plain attribute
        # after __init__ used to leave the stage holding None, so every frame
        # completed during the run was silently unpublished and only the
        # stop()-time flush reached the sink.
        self._on_completed_frame = on_completed_frame
        # Fires after every frame finishes the chain, whatever depth
        # that chain runs to -- the debugging hook for the "add layers
        # one at a time" bring-up workflow.
        self.on_frame_processed = on_frame_processed

        self.monitor = StreamMonitor(report_interval=report_interval, sink=sink)
        self.monitor.configure_capture_queue(queue_depth)
        # Either take the caller's hand-built chain as-is, or build one
        # declaratively from max_stage. Either way, from here on this
        # class only ever does `for stage in self.stages: ...`.
        if self.split_by_camera and mode != "camera":
            raise ValueError("split_by_camera requires camera mode")

        stage_max = "parse" if self.split_by_camera else max_stage
        self.stages: list[Stage] = stages if stages is not None else build_stage_chain(
            mode=mode,
            monitor=self.monitor,
            max_stage=stage_max,
            pcap_recorder=pcap_recorder,
            error_recorder=error_recorder,
            reassembler=reassembler or NullReassembler(),
            on_completed_frame=on_completed_frame,
        )
        # Kept explicitly (rather than duck-typed in stop()) so flush()
        # is only ever called on stages that are actually a
        # ReassemblyStage, regardless of how self.stages was built.
        self._reassembly_stages: list[ReassemblyStage] = [
            s for s in self.stages if isinstance(s, ReassemblyStage)
        ]

        self._queue: "queue.Queue[RawEthernetFrame]" = queue.Queue(maxsize=queue_depth)
        self._stop_event = threading.Event()
        # Set before frame_source.stop() so a callback still in flight cannot
        # enqueue work after the drain gate has already been satisfied.
        self._source_stopped = False
        self._worker = threading.Thread(target=self._run_worker, name="taxi-worker", daemon=True)

    @property
    def on_completed_frame(self) -> Optional[Callable[[CompletedFrame], None]]:
        return self._on_completed_frame

    @on_completed_frame.setter
    def on_completed_frame(
        self,
        callback: Optional[Callable[[CompletedFrame], None]],
    ) -> None:
        self._on_completed_frame = callback
        for stage in self._reassembly_stages:
            stage.on_completed_frame = callback

    def start(self) -> None:
        self._worker.start()
        self.frame_source.start(self._on_frame)

    def stop(self) -> None:
        # Order matters, and getting it wrong deadlocks at high packet rates.
        #
        # The worker loop exits as soon as it observes `_stop_event` set AND the
        # queue momentarily empty.  Setting the event first therefore races the
        # capture thread: at ~7.7 kpkt/s the sniffer keeps calling `_on_frame`
        # while `frame_source.stop()` is still unwinding, the worker wins the
        # "empty" check and exits, and the frames enqueued after that never get
        # `task_done()` -- so the `queue.join()` below blocks forever and the
        # Final Report / rows.csv flush never happen.  Observed as a hard hang
        # with CPU pinned at 0 and three threads in Wait.
        #
        # Correct sequence: silence the producer, drain what it left, and only
        # then tell the worker it may leave.
        self._source_stopped = True
        self.frame_source.stop()
        # Do not flush Layer 5 while accepted capture records remain queued.
        # queue.join() is the explicit "no unexplained backlog" gate.
        self._queue.join()
        self._stop_event.set()
        self._worker.join(timeout=5.0)
        if self._worker.is_alive():
            raise RuntimeError("receiver worker did not stop after queue drain")

        for stage in self._reassembly_stages:
            for completed in stage.reassembler.flush():
                if self.on_completed_frame is not None:
                    self.on_completed_frame(completed)

        if self.pcap_recorder is not None:
            self.pcap_recorder.close()

    def print_final_report(self) -> None:
        self.monitor.final_report()
        pcap_stats_fn = getattr(self.frame_source, "pcap_stats", None)
        if callable(pcap_stats_fn):
            stats = pcap_stats_fn()
            self.sink("\nLIVE PCAP STATS")
            if stats is None:
                self.sink("  unavailable         : source does not expose libpcap stats")
            else:
                self.sink(f"  ps_recv             : {stats.ps_recv}")
                self.sink(f"  ps_drop             : {stats.ps_drop}")
                self.sink(f"  ps_ifdrop           : {stats.ps_ifdrop}")
                ps_capt = getattr(stats, "ps_capt", None)
                ps_sent = getattr(stats, "ps_sent", None)
                ps_netdrop = getattr(stats, "ps_netdrop", None)
                if ps_capt is not None:
                    self.sink(f"  ps_capt             : {ps_capt}")
                if ps_sent is not None:
                    self.sink(f"  ps_sent             : {ps_sent}")
                if ps_netdrop is not None:
                    self.sink(f"  ps_netdrop          : {ps_netdrop}")
        for stage in self._reassembly_stages:
            stats = getattr(stage.reassembler, "stats", None)
            if stats is None:
                continue
            self.sink("\nLAYER-5 REASSEMBLY")
            self.sink(f"  sessions created    : {stats.sessions_created}")
            self.sink(f"  rows accepted       : {stats.rows_accepted}")
            self.sink(f"  rows rejected       : {stats.rows_rejected}")
            self.sink(f"  frames complete     : {stats.frames_completed}")
            self.sink(f"  frames timeout      : {stats.frames_timed_out}")
            self.sink(f"  frames partial      : {stats.frames_partial}")
            self.sink(f"  frames corrupt      : {stats.frames_corrupt}")

    # ---- Layer 1 -> queue hand-off (runs on the capture thread) -------

    def _on_frame(self, frame: RawEthernetFrame) -> None:
        if self._source_stopped:
            # A late Npcap/scapy callback during shutdown. Accepting it would
            return
        self.monitor.record_ethernet_frame()
        if self.lossless_input:
            while not self._stop_event.is_set():
                try:
                    self._queue.put(frame, timeout=0.2)
                    self.monitor.record_capture_queue_depth(
                        self._queue.qsize(),
                        camera_id=frame.camera_id,
                    )
                    return
                except queue.Full:
                    continue
            return
        try:
            self._queue.put_nowait(frame)
            self.monitor.record_capture_queue_depth(
                self._queue.qsize(),
                camera_id=frame.camera_id,
            )
        except queue.Full:
            self.monitor.record_dropped_capture(frame.camera_id)

    # ---- worker thread: run every frame through the stage chain -------

    def _run_worker(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                frame = self._queue.get(timeout=0.2)
            except queue.Empty:
                for stage in self._reassembly_stages:
                    stage.poll_timeouts()
                self.monitor.maybe_report()
                continue

            try:
                self._process_frame(frame)
            except Exception as exc:  # noqa: BLE001 -- keep the worker alive
                # This boundary includes observer/storage callbacks as well as
                # parsing stages. Calling every such exception a parser error
                # made WinError 5 archive failures look like bad packet data.
                self.monitor.stats.processing_errors += 1
                print(f"[PROCESSING ERROR] {exc}", file=sys.stderr)
            finally:
                self._queue.task_done()

            self.monitor.maybe_report()

    def _process_frame(self, frame: RawEthernetFrame) -> None:
        ctx = FrameContext(frame=frame, mode=self.mode)
        try:
            for stage in self.stages:
                if not stage.process(ctx):
                    break
        finally:
            # The audit/debug hook is an observation boundary, not a success
            # callback.  It must see packets even when a later stage (for
            # example atomic storage refusing an overwrite) raises.
            if self.on_frame_processed is not None:
                self.on_frame_processed(ctx)
