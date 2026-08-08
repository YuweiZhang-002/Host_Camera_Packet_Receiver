from taxi_receiver.camera_parser import parse_camera_mode
from taxi_receiver.reassembler import FrameReassembler, NullReassembler
from taxi_receiver.packet_format import (
    FLAG_FIRST_ROW,
    FLAG_LAST_ROW,
    ROW_BYTES,
    build_camera_row,
)

from .synthetic import make_camera_frame


def _parsed(cam_id, frame_id, row_idx, flags):
    frame = make_camera_frame(cam_id=cam_id, frame_id=frame_id, row_idx=row_idx, row_seq=row_idx, row_flags=flags)
    return parse_camera_mode(frame.payload).packet


def _parsed_fill(cam_id, frame_id, row_idx, row_seq, flags, fill):
    return parse_camera_mode(
        build_camera_row(
            cam_id=cam_id,
            frame_id=frame_id,
            row_idx=row_idx,
            row_seq=row_seq,
            row_flags=flags,
            payload=bytes([fill]) * ROW_BYTES,
        )
    ).packet


def test_null_reassembler_never_completes():
    reassembler = NullReassembler()
    pkt = _parsed(0, 1, 0, FLAG_FIRST_ROW | FLAG_LAST_ROW)
    assert reassembler.on_row(pkt) is None
    assert reassembler.flush() == []


def test_frame_reassembler_completes_on_last_row():
    reassembler = FrameReassembler()

    assert reassembler.on_row(_parsed(0, 1, 0, FLAG_FIRST_ROW)) is None
    assert reassembler.on_row(_parsed(0, 1, 1, 0)) is None
    completed = reassembler.on_row(_parsed(0, 1, 2, FLAG_LAST_ROW))

    assert completed is not None
    assert completed.camera_id == 0
    assert completed.frame_id == 1
    assert completed.row_count == 3
    assert completed.missing_rows == []
    assert reassembler.stats.sessions_created == 1
    assert reassembler.stats.rows_accepted == 3
    assert reassembler.stats.rows_rejected == 0
    assert reassembler.stats.frames_completed == 1


def test_frame_reassembler_reports_missing_rows():
    reassembler = FrameReassembler()
    reassembler.on_row(_parsed(0, 1, 0, FLAG_FIRST_ROW))
    # row 1 lost
    assert reassembler.on_row(_parsed(0, 1, 2, FLAG_LAST_ROW)) is None
    completed = reassembler.flush()[0]

    assert completed.missing_rows == [1]
    assert reassembler.stats.frames_partial == 1


def test_flush_closes_in_progress_frames():
    reassembler = FrameReassembler()
    reassembler.on_row(_parsed(0, 1, 0, FLAG_FIRST_ROW))
    completed = reassembler.flush()

    assert len(completed) == 1
    assert completed[0].frame_id == 1
    assert completed[0].row_count == 1


def test_bad_sync_does_not_create_or_rotate_sessions():
    """Untrusted header identity is audited but cannot churn frame sessions."""
    reassembler = FrameReassembler(expected_rows=2)

    reassembler.on_row(
        _parsed_fill(0, 10, 0, 100, FLAG_FIRST_ROW, 0x11)
    )
    bogus = _parsed_fill(0, 50000, 1, 101, FLAG_LAST_ROW, 0x22)
    assert reassembler.on_row(bogus, errors=("bad_sync",)) is None

    completed = reassembler.on_row(
        _parsed_fill(0, 10, 1, 101, FLAG_LAST_ROW, 0x33)
    )
    assert completed is not None
    assert completed.frame_id == 10
    assert completed.rows[0] == bytes([0x11]) * ROW_BYTES
    assert completed.rows[1] == bytes([0x33]) * ROW_BYTES
    assert reassembler.stats.sessions_created == 1
    assert reassembler.stats.rows_accepted == 2
    assert reassembler.stats.rows_rejected == 1


def test_crc_error_does_not_create_session():
    reassembler = FrameReassembler(expected_rows=1)
    packet = _parsed(1, 77, 0, FLAG_FIRST_ROW | FLAG_LAST_ROW)

    assert reassembler.on_row(packet, errors=("crc_error",)) is None
    assert reassembler.flush() == []
    assert reassembler.stats.sessions_created == 0
    assert reassembler.stats.rows_rejected == 1


def test_frame_switch_never_reuses_previous_frame_rows():
    reassembler = FrameReassembler(expected_rows=2)

    reassembler.on_row(
        _parsed_fill(0, 10, 0, 100, FLAG_FIRST_ROW, 0x11)
    )
    first = reassembler.on_row(
        _parsed_fill(0, 10, 1, 101, FLAG_LAST_ROW, 0x22)
    )
    assert first is not None
    assert first.rows[0] == bytes([0x11]) * ROW_BYTES

    assert reassembler.on_row(
        _parsed_fill(0, 11, 1, 103, FLAG_LAST_ROW, 0x44)
    ) is None
    switched = reassembler.on_row(
        _parsed_fill(0, 12, 0, 104, FLAG_FIRST_ROW, 0x55)
    )
    assert switched is not None
    assert switched.frame_id == 11
    assert set(switched.rows) == {1}
    assert switched.to_bytes(2)[:ROW_BYTES] == bytes(ROW_BYTES)
    assert switched.to_bytes(2)[ROW_BYTES:] == bytes([0x44]) * ROW_BYTES


def test_out_of_range_row_is_never_inserted_even_for_direct_caller():
    reassembler = FrameReassembler(expected_rows=480)
    packet = _parsed_fill(0, 20, 65535, 500, 0, 0x77)

    assert reassembler.on_row(packet) is None
    completed = reassembler.flush()

    assert len(completed) == 1
    assert completed[0].rows == {}
    assert completed[0].packet_records[0].accepted is False
    assert completed[0].packet_records[0].errors == (
        "row_idx_out_of_range",
    )
