"""
stages.py  --  turns Layer 2-5 into an explicit, appendable chain.

Each Stage does exactly one layer's job against a shared FrameContext
and returns True ("keep going") or False ("stop here for this frame").
`build_stage_chain()` is the declarative way to ask for "Layer 1-2",
"1-3", "1-4", or "1-5" by name; `pipeline.py` also happily accepts a
hand-built `list[Stage]` if you'd rather wire it yourself at the top
of your own script.

Only ValidationStage can stop the chain outright (not-our-traffic,
MAC-filtered, bad length -- there's nothing further to do). Parsing
failures (bad CRC, bad fixed payload) do NOT stop the chain: Layer 4
still wants to count them, so ParsingStage always returns True and
lets MonitoringStage decide what "not ok" means statistically.
Reassembly then simply skips anything that wasn't a good camera packet.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Optional, Protocol

from .camera_parser import CameraModeResult, FixedModeResult, parse_camera_mode, parse_fixed_mode
from .capture import RawEthernetFrame
from .eth_validate import ETHER_TYPE, ValidationResult, validate_ethernet_frame
from .reassembler import CompletedFrame, NullReassembler, RowReassembler
from .recorder import ErrorFrameRecorder, PcapRecorder
from .stream_monitor import StreamMonitor

# Layer 1 (capture) isn't in this list -- it's the frame *source*, not
# a per-frame stage. This is the ordering "depth" is measured against.
STAGE_ORDER = ("validate", "parse", "monitor", "reassemble")


@dataclass
class FrameContext:
    """Threaded through the stage chain; every stage reads what it
    needs and writes its own result field. Left fully populated (or
    partially populated, if the chain stopped early) for
    `on_frame_processed` to inspect -- handy for bring-up debugging
    when you deliberately haven't enabled the later layers yet."""
    frame: RawEthernetFrame
    mode: str
    validation: Optional[ValidationResult] = None
    fixed_result: Optional[FixedModeResult] = None
    camera_result: Optional[CameraModeResult] = None
    completed_frame: Optional[CompletedFrame] = None
    stopped_at: Optional[str] = None
    stop_reason: Optional[str] = None


class Stage(Protocol):
    name: str
    def process(self, ctx: FrameContext) -> bool: ...


class ValidationStage:
    """Layer 2."""
    name = "validate"

    def __init__(self, monitor: StreamMonitor, ether_type: int = ETHER_TYPE, allowed_src_macs=None):
        self.monitor = monitor
        self.ether_type = ether_type
        self.allowed_src_macs = allowed_src_macs

    def process(self, ctx: FrameContext) -> bool:
        result = validate_ethernet_frame(
            ctx.frame, ether_type=self.ether_type, allowed_src_macs=self.allowed_src_macs,
        )
        ctx.validation = result

        if not result.ok:
            if result.reason != "not_taxi_ethertype":
                self.monitor.record_validation_failure(result.reason)
            ctx.stopped_at, ctx.stop_reason = self.name, result.reason
            return False

        self.monitor.record_matching_frame(len(ctx.frame.payload))
        return True


class PcapRecordingStage:
    """Side effect, not a numbered layer -- slot it in wherever you like
    (build_stage_chain places it right after validation, matching the
    original prototype's order)."""
    name = "record_pcap"

    def __init__(self, pcap_recorder: PcapRecorder):
        self.pcap_recorder = pcap_recorder

    def process(self, ctx: FrameContext) -> bool:
        self.pcap_recorder.write_raw(ctx.frame.raw_bytes)
        return True


class ParsingStage:
    """Layer 3. Always returns True: a bad CRC or bad fixed payload is
    still meaningful telemetry for Layer 4, not a reason to stop."""
    name = "parse"

    def __init__(self, mode: str, error_recorder: Optional[ErrorFrameRecorder] = None):
        self.mode = mode
        self.error_recorder = error_recorder

    def process(self, ctx: FrameContext) -> bool:
        if self.mode == "fixed":
            result = parse_fixed_mode(ctx.frame.payload)
            ctx.fixed_result = result
            if not result.ok and self.error_recorder is not None:
                self.error_recorder.save(f"fixed_{result.reason}", ctx.frame.payload)
        else:
            result = parse_camera_mode(ctx.frame.payload)
            ctx.camera_result = result
            if not result.ok and self.error_recorder is not None:
                self.error_recorder.save(f"camera_{result.reason}", ctx.frame.payload)
        return True


class MonitoringStage:
    """Layer 4: this is the only stage that touches GlobalStatistics
    beyond the basic frame/byte counters Layer 2 already tracks."""
    name = "monitor"

    def __init__(self, monitor: StreamMonitor):
        self.monitor = monitor

    def process(self, ctx: FrameContext) -> bool:
        if ctx.fixed_result is not None:
            self.monitor.record_fixed_result(ctx.fixed_result)
        elif ctx.camera_result is not None:
            self.monitor.record_camera_result(ctx.camera_result)
        return True


class ReassemblyStage:
    """Layer 5 image-session reassembly.

    Parsed packets with validation errors are still presented to the
    reassembler as structured error records, but their payload bytes
    are not accepted into an image.
    """
    name = "reassemble"

    def __init__(self, reassembler: RowReassembler, on_completed_frame: Optional[Callable[[CompletedFrame], None]] = None):
        self.reassembler = reassembler
        self.on_completed_frame = on_completed_frame

    def process(self, ctx: FrameContext) -> bool:
        if ctx.camera_result is None or ctx.camera_result.packet is None:
            return True
        completed = self.reassembler.on_row(
            ctx.camera_result.packet,
            errors=ctx.camera_result.errors,
            warnings=ctx.camera_result.warnings,
            capture_timestamp=ctx.frame.timestamp,
            now=time.monotonic(),
        )
        self._emit(completed, ctx)
        drain = getattr(self.reassembler, "drain_completed", None)
        if drain is not None:
            for pending in drain():
                self._emit(pending, ctx)
        return True

    def poll_timeouts(self) -> None:
        expire = getattr(self.reassembler, "expire", None)
        if expire is None:
            return
        for completed in expire(time.monotonic()):
            self._emit(completed, None)

    def _emit(
        self,
        completed: Optional[CompletedFrame],
        ctx: Optional[FrameContext],
    ) -> None:
        if completed is None:
            return
        if ctx is not None:
            ctx.completed_frame = completed
        if self.on_completed_frame is not None:
            self.on_completed_frame(completed)


def build_stage_chain(
    *,
    mode: str,
    monitor: StreamMonitor,
    max_stage: str = "monitor",
    ether_type: int = ETHER_TYPE,
    allowed_src_macs=None,
    pcap_recorder: Optional[PcapRecorder] = None,
    error_recorder: Optional[ErrorFrameRecorder] = None,
    reassembler: Optional[RowReassembler] = None,
    on_completed_frame: Optional[Callable[[CompletedFrame], None]] = None,
) -> list[Stage]:
    """Declarative "Layer 1-N" builder.

        max_stage="validate"    -> Layer 1-2   (does traffic even show up & pass the filter?)
        max_stage="parse"       -> Layer 1-3   (does CRC/parsing look right, packet by packet?)
        max_stage="monitor"     -> Layer 1-4   (full stats: gaps/dup/ooo, rate, throughput)
        max_stage="reassemble"  -> Layer 1-5   (+ row reassembly)

    This is the "add layers one at a time" workflow: start a bring-up
    session with max_stage="validate" and an on_frame_processed hook
    that prints ctx.validation, confirm that's right, bump to "parse"
    and print ctx.camera_result instead, and so on up to "reassemble".
    """
    if max_stage not in STAGE_ORDER:
        raise ValueError(f"max_stage must be one of {STAGE_ORDER}, got {max_stage!r}")

    depth = STAGE_ORDER.index(max_stage)  # validate=0, parse=1, monitor=2, reassemble=3

    stages: list[Stage] = [
        ValidationStage(monitor, ether_type=ether_type, allowed_src_macs=allowed_src_macs)
    ]

    if pcap_recorder is not None:
        stages.append(PcapRecordingStage(pcap_recorder))

    if depth >= 1:
        stages.append(ParsingStage(mode, error_recorder=error_recorder))
    if depth >= 2:
        stages.append(MonitoringStage(monitor))
    if depth >= 3:
        stages.append(ReassemblyStage(reassembler or NullReassembler(), on_completed_frame=on_completed_frame))

    return stages
