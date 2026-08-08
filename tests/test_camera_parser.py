from pathlib import Path

from taxi_receiver.camera_parser import parse_camera_mode, parse_fixed_mode
from taxi_receiver.packet_format import (
    ROW_BYTES,
    SYNC0_DEFAULT,
    SYNC1_DEFAULT,
    build_camera_row,
)


VECTOR_DIR = Path(__file__).with_name("vectors")


def test_fixed_mode_ok():
    payload = bytes(range(128))
    result = parse_fixed_mode(payload)
    assert result.ok


def test_fixed_mode_bad_length():
    result = parse_fixed_mode(bytes(100))
    assert not result.ok
    assert result.reason == "bad_length"


def test_fixed_mode_bad_data():
    payload = bytearray(range(128))
    payload[10] ^= 0xFF
    result = parse_fixed_mode(bytes(payload))
    assert not result.ok
    assert result.reason == "bad_data"
    assert result.mismatch_offset == 10


def test_camera_mode_crc_error_surfaced():
    raw = build_camera_row(
        cam_id=3, frame_id=1, row_idx=0, row_flags=0,
        row_seq=0, payload=bytes(80), corrupt_crc=True,
    )
    result = parse_camera_mode(raw)
    assert not result.ok
    assert result.reason == "crc_error"
    assert result.packet is not None  # still parsed, just flagged


def test_camera_mode_rejects_wrong_sync_even_with_valid_crc():
    raw = build_camera_row(
        cam_id=0,
        frame_id=1,
        row_idx=0,
        row_flags=0,
        row_seq=0,
        payload=bytes(ROW_BYTES),
        sync0=0x1234,
        sync1=SYNC1_DEFAULT,
    )
    result = parse_camera_mode(raw)
    assert not result.ok
    assert result.reason == "bad_sync"
    assert result.errors == ("bad_sync",)


def test_camera_mode_rejects_payload_len_above_physical_payload():
    raw = build_camera_row(
        cam_id=0,
        frame_id=1,
        row_idx=0,
        row_flags=0,
        row_seq=0,
        payload=bytes(ROW_BYTES),
        payload_len=ROW_BYTES + 1,
    )
    result = parse_camera_mode(raw)
    assert not result.ok
    assert result.reason == "payload_len_out_of_range"


def test_live_first_row_bit_is_not_misclassified_as_overflow():
    raw = build_camera_row(
        cam_id=0,
        frame_id=10,
        row_idx=0,
        row_flags=0x04,
        row_seq=100,
        payload=bytes(ROW_BYTES),
    )
    result = parse_camera_mode(raw)
    assert result.ok
    assert result.packet is not None
    assert result.packet.first_row
    assert not result.packet.frame_overflow


def test_live_overflow_bit_is_rejected():
    raw = build_camera_row(
        cam_id=0,
        frame_id=10,
        row_idx=1,
        row_flags=0x01,
        row_seq=101,
        payload=bytes(ROW_BYTES),
    )
    result = parse_camera_mode(raw)
    assert not result.ok
    assert result.errors == ("frame_overflow",)


def test_camera_mode_surfaces_zero_payload_len_without_guessing_firmware_rule():
    raw = build_camera_row(
        cam_id=0,
        frame_id=1,
        row_idx=0,
        row_flags=0,
        row_seq=0,
        payload=bytes(ROW_BYTES),
        payload_len=0,
    )
    result = parse_camera_mode(raw)
    assert result.ok
    assert result.warnings == ("zero_payload_len",)


def test_camera_mode_rejects_fpga_capture_error_flags():
    raw = build_camera_row(
        cam_id=0,
        frame_id=1,
        row_idx=0,
        row_flags=0,
        row_seq=0,
        payload=bytes(ROW_BYTES),
        fpga_status=0x09,
    )
    result = parse_camera_mode(raw)
    assert not result.ok
    assert result.errors == ("frame_overflow", "length_error")


def test_camera_mode_rejects_undefined_source_flag_bits():
    raw = build_camera_row(
        cam_id=0,
        frame_id=1,
        row_idx=0,
        row_flags=0xF8,
        row_seq=0,
        payload=bytes(ROW_BYTES),
    )
    result = parse_camera_mode(raw)
    assert not result.ok
    assert result.errors == ("undefined_flag_bits",)


def test_camera_mode_rejects_out_of_range_header_identity():
    raw = build_camera_row(
        cam_id=4,
        frame_id=1,
        row_idx=480,
        row_flags=0,
        row_seq=0,
        payload=bytes(ROW_BYTES),
    )
    result = parse_camera_mode(raw)
    assert not result.ok
    assert result.errors == ("cam_id_out_of_range", "row_idx_out_of_range")


def test_camera_mode_warns_when_unassigned_reserved_bytes_are_nonzero():
    raw = build_camera_row(
        cam_id=0,
        frame_id=1,
        row_idx=0,
        row_flags=0,
        row_seq=0,
        payload=bytes(ROW_BYTES),
        reserved=b"\x00\x00\x01" + bytes(8),
    )
    result = parse_camera_mode(raw)
    assert result.ok
    assert result.warnings == ("reserved_nonzero",)


def test_ila_legacy_v0_regression_vector():
    raw = bytes.fromhex(
        (VECTOR_DIR / "ila_camera_payload_legacy_v0.hex").read_text(
            encoding="ascii"
        )
    )
    result = parse_camera_mode(raw)

    assert len(raw) == 128
    assert not result.ok
    # This archived ILA vector predates the current RP2350A A5A0/5A50
    # protocol words. It remains useful as a byte/CRC regression, but must
    # now be identified honestly as legacy sync instead of current-valid.
    assert result.reason == "bad_sync"
    assert result.errors == ("bad_sync", "undefined_flag_bits")
    assert result.packet is not None
    assert result.packet.crc_ok
    assert result.packet.header.sync0 == 0xA5A5
    assert result.packet.header.sync1 == 0x5A5A
    assert result.packet.header.cam_id == 0
    assert result.packet.header.frame_id == 2073
    assert result.packet.header.row_idx == 330
    assert result.packet.header.row_flags == 0x08
    assert result.packet.header.payload_len == 80
    assert result.packet.header.row_seq == 12330
    assert result.packet.received_crc == 0xB753


def test_current_sync_words_are_msb_byte_first_on_the_wire():
    raw = build_camera_row(
        cam_id=0,
        frame_id=1,
        row_idx=0,
        row_flags=0,
        row_seq=0,
        payload=bytes(ROW_BYTES),
    )

    assert (SYNC0_DEFAULT, SYNC1_DEFAULT) == (0xA5A0, 0x5A50)
    assert raw[:4] == bytes.fromhex("a5a05a50")
    result = parse_camera_mode(raw)
    assert result.ok
    assert result.packet is not None
    assert result.packet.header.frame_id == 1
