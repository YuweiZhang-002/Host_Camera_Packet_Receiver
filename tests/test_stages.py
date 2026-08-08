"""
Demonstrates the "add layers one at a time" workflow: run the same
synthetic frames through progressively deeper chains and check that
each layer's effect only shows up once it's actually included.
"""
from taxi_receiver.capture import SyntheticFrameSource
from taxi_receiver.packet_format import FLAG_FIRST_ROW, FLAG_LAST_ROW
from taxi_receiver.pipeline import TaxiReceiverPipeline
from taxi_receiver.reassembler import FrameReassembler
from taxi_receiver.stages import (
    MonitoringStage,
    ParsingStage,
    ReassemblyStage,
    ValidationStage,
    build_stage_chain,
)
from taxi_receiver.stream_monitor import StreamMonitor

from .synthetic import make_camera_frame


def _last_row_frame():
    return make_camera_frame(
        cam_id=0,
        frame_id=1,
        row_idx=0,
        row_seq=0,
        row_flags=FLAG_FIRST_ROW | FLAG_LAST_ROW,
    )


def test_max_stage_validate_only_counts_matching_frames():
    monitor = StreamMonitor(report_interval=999, sink=lambda *_: None)
    stages = build_stage_chain(mode="camera", monitor=monitor, max_stage="validate")
    assert [s.name for s in stages] == ["validate"]

    ctx_stage = stages[0]
    from taxi_receiver.stages import FrameContext
    ctx = FrameContext(frame=_last_row_frame(), mode="camera")
    ctx_stage.process(ctx)

    assert monitor.stats.matching_frames == 1
    assert monitor.stats.valid_packets == 0  # ParsingStage never ran
    assert ctx.camera_result is None


def test_max_stage_parse_populates_result_but_not_monitor_stats():
    monitor = StreamMonitor(report_interval=999, sink=lambda *_: None)
    stages = build_stage_chain(mode="camera", monitor=monitor, max_stage="parse")
    assert [s.name for s in stages] == ["validate", "parse"]

    from taxi_receiver.stages import FrameContext
    ctx = FrameContext(frame=_last_row_frame(), mode="camera")
    for stage in stages:
        stage.process(ctx)

    assert ctx.camera_result is not None
    assert ctx.camera_result.ok
    # MonitoringStage wasn't included, so Layer 4 never saw this packet.
    assert monitor.stats.valid_packets == 0


def test_max_stage_monitor_is_full_layer1_4():
    monitor = StreamMonitor(report_interval=999, sink=lambda *_: None)
    stages = build_stage_chain(mode="camera", monitor=monitor, max_stage="monitor")
    assert [s.name for s in stages] == ["validate", "parse", "monitor"]

    from taxi_receiver.stages import FrameContext
    ctx = FrameContext(frame=_last_row_frame(), mode="camera")
    for stage in stages:
        stage.process(ctx)

    assert monitor.stats.valid_packets == 1
    assert ctx.completed_frame is None  # no ReassemblyStage at this depth


def test_max_stage_reassemble_is_full_layer1_5():
    monitor = StreamMonitor(report_interval=999, sink=lambda *_: None)
    stages = build_stage_chain(
        mode="camera", monitor=monitor, max_stage="reassemble", reassembler=FrameReassembler(),
    )
    assert [s.name for s in stages] == ["validate", "parse", "monitor", "reassemble"]

    from taxi_receiver.stages import FrameContext
    ctx = FrameContext(frame=_last_row_frame(), mode="camera")
    for stage in stages:
        stage.process(ctx)

    assert ctx.completed_frame is not None
    assert ctx.completed_frame.row_count == 1


def test_manual_top_level_stage_list_bypasses_build_stage_chain():
    """The 'wire it yourself at the top' path: hand TaxiReceiverPipeline
    an explicit list instead of asking it to build one."""
    monitor = StreamMonitor(report_interval=999, sink=lambda *_: None)
    manual_stages = [ValidationStage(monitor), ParsingStage(mode="camera")]

    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource([_last_row_frame()]),
        mode="camera",
        stages=manual_stages,  # overrides max_stage entirely
        report_interval=999,
        sink=lambda *_: None,
    )
    # The pipeline's own monitor is separate from the one used to build
    # manual_stages above, so pull stats from the stage we constructed.
    assert pipeline.stages is manual_stages
    assert isinstance(pipeline.stages[-1], ParsingStage)
    assert not any(isinstance(s, MonitoringStage) for s in pipeline.stages)
