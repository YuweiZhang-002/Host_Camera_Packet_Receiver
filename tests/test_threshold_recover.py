import time

import pytest

from taxi_receiver.capture import SyntheticFrameSource
from taxi_receiver.packet_format import FLAG_FIRST_ROW
from taxi_receiver.pipeline import TaxiReceiverPipeline
from taxi_receiver.reassembler import CompletedFrame, FrameReassembler
from taxi_receiver.threshold_recover import (
    BitOrder,
    IncompleteThresholdFrameError,
    MissingRowPolicy,
    ROW_PIXELS,
    ThresholdFrameRecoverer,
    ThresholdRowDecoder,
    recover_completed_frame,
)

from .synthetic import make_camera_frame


def test_known_bit_order_vectors():
    msb = ThresholdRowDecoder(BitOrder.MSB_FIRST)
    lsb = ThresholdRowDecoder("lsb_first")
    packed = bytes([0x80, 0x01, 0x96]) + bytes(77)

    assert msb.expand_row(packed)[:24] == bytes([
        0xFF, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0xFF,
        0xFF, 0, 0, 0xFF, 0, 0xFF, 0xFF, 0,
    ])
    assert lsb.expand_row(packed)[:24] == bytes([
        0, 0, 0, 0, 0, 0, 0, 0xFF,
        0xFF, 0, 0, 0, 0, 0, 0, 0,
        0, 0xFF, 0xFF, 0, 0xFF, 0, 0, 0xFF,
    ])


def test_completed_frame_zero_fills_missing_rows():
    completed = CompletedFrame(
        camera_id=2,
        frame_id=10,
        row_count=1,
        rows={0: bytes([0xFF]) * 80},
        missing_rows=[],
    )
    recovered = recover_completed_frame(
        completed,
        expected_rows=2,
        missing_policy=MissingRowPolicy.ZERO_FILL,
    )

    assert recovered.width == ROW_PIXELS
    assert recovered.height == 2
    assert recovered.missing_rows == (1,)
    assert recovered.row(0) == bytes([0xFF]) * ROW_PIXELS
    assert recovered.row(1) == bytes(ROW_PIXELS)


def test_completed_frame_rejects_missing_rows_by_default():
    completed = CompletedFrame(
        camera_id=0, frame_id=1, row_count=1, rows={0: bytes(80)}
    )
    with pytest.raises(IncompleteThresholdFrameError):
        recover_completed_frame(completed, expected_rows=2)


def test_callback_adapter_records_rejection_without_raising():
    completed = CompletedFrame(
        camera_id=0, frame_id=1, row_count=1, rows={0: bytes(80)}
    )
    rejected = []
    recoverer = ThresholdFrameRecoverer(
        expected_rows=2,
        on_rejected_frame=rejected.append,
    )

    assert recoverer(completed) is None
    assert recoverer.rejected_frames == 1
    assert recoverer.last_rejection is rejected[0]


def test_pipeline_recovers_at_on_completed_frame_boundary():
    frames = [
        make_camera_frame(
            cam_id=0,
            frame_id=7,
            row_idx=0,
            row_seq=0,
            row_flags=FLAG_FIRST_ROW,
            payload=bytes([0x80]) + bytes(79),
        ),
        make_camera_frame(
            cam_id=0,
            frame_id=7,
            row_idx=1,
            row_seq=1,
            row_flags=0b10,
            payload=bytes([0x01]) + bytes(79),
        ),
    ]
    recovered_frames = []
    recoverer = ThresholdFrameRecoverer(
        expected_rows=2,
        bit_order=BitOrder.MSB_FIRST,
        on_recovered_frame=recovered_frames.append,
    )
    pipeline = TaxiReceiverPipeline(
        frame_source=SyntheticFrameSource(frames),
        mode="camera",
        max_stage="reassemble",
        reassembler=FrameReassembler(),
        report_interval=999,
        sink=lambda *_: None,
        on_completed_frame=recoverer,
    )

    pipeline.start()
    time.sleep(0.3)
    pipeline.stop()

    assert len(recovered_frames) == 1
    recovered = recovered_frames[0]
    assert len(recovered.pixels) == 2 * ROW_PIXELS
    assert recovered.row(0)[:8] == bytes([0xFF, 0, 0, 0, 0, 0, 0, 0])
    assert recovered.row(1)[:8] == bytes([0, 0, 0, 0, 0, 0, 0, 0xFF])
