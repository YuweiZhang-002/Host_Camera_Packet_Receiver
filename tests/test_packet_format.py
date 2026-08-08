import binascii

import pytest

from taxi_receiver.packet_format import (
    ByteStreamFramer,
    FLAG_FRAME_OVERFLOW,
    FLAG_FIRST_ROW,
    FLAG_LAST_ROW,
    FLAG_LENGTH_ERROR,
    FPGA_STATUS_LENGTH_ERROR,
    PACKET_LEN,
    ROW_BYTES,
    crc16_ccitt_false,
    build_camera_row,
    parse_camera_row,
)


def test_sizes():
    assert PACKET_LEN == 128
    assert ROW_BYTES == 80


def test_live_wire_flag_assignments():
    assert FLAG_FRAME_OVERFLOW == 0x01
    assert FLAG_LAST_ROW == 0x02
    assert FLAG_FIRST_ROW == 0x04
    assert FLAG_LENGTH_ERROR == 0x08


def test_round_trip_ok():
    payload = bytes(range(80))
    raw = build_camera_row(
        cam_id=2, frame_id=100, row_idx=5, row_flags=FLAG_FIRST_ROW,
        row_seq=42, payload=payload,
    )
    assert len(raw) == PACKET_LEN

    pkt = parse_camera_row(raw)
    assert pkt.crc_ok
    assert pkt.header.cam_id == 2
    assert pkt.header.frame_id == 100
    assert pkt.header.row_idx == 5
    assert pkt.header.row_seq == 42
    assert pkt.first_row is True
    assert pkt.last_row is False
    assert pkt.payload == payload


def test_fpga_status_is_separate_from_raw_mcu_row_flags():
    raw = build_camera_row(
        cam_id=0,
        frame_id=7,
        row_idx=3,
        row_flags=FLAG_FIRST_ROW,
        row_seq=9,
        payload=bytes(ROW_BYTES),
        fpga_status=FPGA_STATUS_LENGTH_ERROR,
        header_check=0x5A,
    )
    packet = parse_camera_row(raw)

    assert raw[9] == FLAG_FIRST_ROW
    assert raw[13] == FPGA_STATUS_LENGTH_ERROR
    assert raw[14] == 0x5A
    assert packet.header.row_flags == FLAG_FIRST_ROW
    assert packet.header.fpga_status == FPGA_STATUS_LENGTH_ERROR
    assert packet.header.header_check == 0x5A
    assert packet.first_row
    assert packet.length_error


def test_current_metadata_is_big_endian_but_crc_tail_is_little_endian():
    raw = build_camera_row(
        cam_id=0,
        frame_id=0x1234,
        row_idx=0x01DF,
        row_flags=0,
        row_seq=0xABCD,
        payload=bytes(80),
        m00=0x10203040,
        xc_q4=0x1122,
        yc_q4=0x3344,
        vx_q8=0x1234,
        vy_q8=-2,
    )
    packet = parse_camera_row(raw)

    assert raw[:4] == bytes.fromhex("a5a05a50")
    assert raw[5:7] == bytes.fromhex("1234")
    assert raw[7:9] == bytes.fromhex("01df")
    assert raw[11:13] == bytes.fromhex("abcd")
    assert raw[114:118] == bytes.fromhex("10203040")
    assert raw[118:122] == bytes.fromhex("11223344")
    assert raw[126:128] == packet.calculated_crc.to_bytes(2, "little")
    assert packet.header.frame_id == 0x1234
    assert packet.header.row_idx == 0x01DF
    assert packet.header.row_seq == 0xABCD
    assert packet.trailer.m00 == 0x10203040
    assert packet.trailer.vy_q8 == -2
    assert packet.crc_ok


def test_corrupt_crc_detected():
    payload = bytes(range(80))
    raw = build_camera_row(
        cam_id=1, frame_id=1, row_idx=0, row_flags=0,
        row_seq=0, payload=payload, corrupt_crc=True,
    )
    pkt = parse_camera_row(raw)
    assert not pkt.crc_ok


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00",
        b"\x00\x01",
        bytes(range(80)),
        bytes(range(126)),
    ],
)
def test_crc16_ccitt_false_matches_binascii_crc_hqx(data):
    assert crc16_ccitt_false(data) == binascii.crc_hqx(data, 0xFFFF)


def test_byte_stream_framer_resyncs_and_extracts_packets():
    good = build_camera_row(
        cam_id=0, frame_id=1, row_idx=0, row_flags=0,
        row_seq=0, payload=bytes(80),
    )
    garbage = b"\x00\x11\x22\x33\x44"  # noise before the sync word

    seen = []
    framer = ByteStreamFramer(on_packet=seen.append)

    # feed it in small, arbitrary chunks to mimic a live byte stream
    stream = garbage + good + good
    for i in range(0, len(stream), 7):
        framer.feed(stream[i:i + 7])

    assert len(seen) == 2
    assert seen[0] == good
    assert seen[1] == good
