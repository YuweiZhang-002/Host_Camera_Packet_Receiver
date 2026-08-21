"""
packet_format.py  --  binary layout only, no I/O, no threading.

Mirrors the FPGA-side C structures exactly:

    pkt_row_header_t   (24 bytes)
    pkt_row_payload_t  (ROW_BYTES bytes)
    plt_row_trailer_t  (24 bytes)
                        ----------------
                        128 bytes total  =>  ROW_BYTES = 128-24-24 = 80

Kept deliberately dependency-free (just `struct` + dataclasses) so it
can be imported and unit tested without scapy, Npcap, or any hardware
in the loop -- this is the module every other layer, and every test,
builds on.

Endianness note: current RP2350A metadata words are emitted MSB byte
first (wire sync ``A5 A0 5A 50``), while FPGA Byte_Replacer regenerates
the final CRC low byte first.  The current packet is therefore mixed:
big-endian header/trailer metadata and little-endian CRC16.
"""
from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass
from typing import Optional

HEADER_LEN = 24
TRAILER_LEN = 24
PACKET_LEN = 128
ROW_BYTES = PACKET_LEN - HEADER_LEN - TRAILER_LEN  # 80

# Current RP2350A wire semantics, confirmed against the live attempt2 CSV:
# bit 0 reports a frame/line-buffer overflow, bit 1 terminates the frame, and
# bit 2 marks the first valid row.  Older project-side code had bit 0 and bit 2
# swapped; that made every real 0x04 first-row packet look corrupt.
FLAG_FRAME_OVERFLOW = 1 << 0
FLAG_LAST_ROW = 1 << 1
FLAG_FIRST_ROW = 1 << 2
SOURCE_ROW_FLAG_MASK = FLAG_FRAME_OVERFLOW | FLAG_LAST_ROW | FLAG_FIRST_ROW

# FPGA-owned status is no longer ORed into the MCU row_flags byte.  It occupies
# pkt_row_header_t.reserved[0] (wire offset 13), keeping the two fault domains
# independently observable.  FLAG_LENGTH_ERROR remains as a compatibility name
# for project-side code, but it is a bit in fpga_status, not in row_flags.
FPGA_STATUS_FRAME_OVERFLOW = 1 << 0
FPGA_STATUS_LENGTH_ERROR = 1 << 3
FPGA_STATUS_CRC_ERROR = 1 << 4
FLAG_LENGTH_ERROR = FPGA_STATUS_LENGTH_ERROR

# Protocol word values and their expected MSB-byte-first wire representation.
# This is byte order inside each multi-byte metadata field; it is not an
# MSB/LSB bit reversal inside an individual byte.
SYNC0_DEFAULT = 0xA5A0
SYNC1_DEFAULT = 0x5A50
SYNC_BYTES_DEFAULT = struct.pack(">HH", SYNC0_DEFAULT, SYNC1_DEFAULT)
assert SYNC_BYTES_DEFAULT == bytes.fromhex("a5a05a50")

_LEGACY_SYNC_BYTES = bytes.fromhex("a5a55a5a")

# ---- pkt_row_header_t ---------------------------------------------------
# uint16 sync0, uint16 sync1, uint8 cam_id, uint16 frame_id, uint16 row_idx,
# uint8 row_flags, uint8 payload_len, uint16 row_seq, uint8 reserved[11]
_HEADER_STRUCT_BE = struct.Struct(">HHBHHBBH11s")
_HEADER_STRUCT_LE = struct.Struct("<HHBHHBBH11s")
assert _HEADER_STRUCT_BE.size == HEADER_LEN
assert _HEADER_STRUCT_LE.size == HEADER_LEN

# ---- plt_row_trailer_t ---------------------------------------------------
# uint8 pad[10], uint32 m00, uint16 xc_q4, uint16 yc_q4,
# int16 vx_q8, int16 vy_q8, uint16 crc16
_TRAILER_BODY_STRUCT_BE = struct.Struct(">10sIHHhh")
_TRAILER_BODY_STRUCT_LE = struct.Struct("<10sIHHhh")
assert _TRAILER_BODY_STRUCT_BE.size == TRAILER_LEN - 2
assert _TRAILER_BODY_STRUCT_LE.size == TRAILER_LEN - 2

# Everything except the trailing crc16 -- this is exactly what the CRC
# is calculated over (mirrors the original code's payload[:126]).
_CRC_COVERED_LEN = PACKET_LEN - 2
_BODY_STRUCT_BE = struct.Struct(f">HHBHHBBH11s{ROW_BYTES}s10sIHHhh")
_BODY_STRUCT_LE = struct.Struct(f"<HHBHHBBH11s{ROW_BYTES}s10sIHHhh")
assert _BODY_STRUCT_BE.size == _CRC_COVERED_LEN
assert _BODY_STRUCT_LE.size == _CRC_COVERED_LEN


@dataclass(slots=True)
class RowHeader:
    sync0: int
    sync1: int
    cam_id: int
    frame_id: int
    row_idx: int
    row_flags: int
    payload_len: int
    row_seq: int
    reserved: bytes

    @property
    def fpga_status(self) -> int:
        """FPGA capture/buffer status from reserved[0], wire offset 13."""
        return self.reserved[0]

    @property
    def header_check(self) -> int:
        """Reserved[1] placeholder for the proposed MCU header check byte."""
        return self.reserved[1]


@dataclass(slots=True)
class RowTrailer:
    pad: bytes
    m00: int
    xc_q4: int
    yc_q4: int
    vx_q8: int
    vy_q8: int
    crc16: int


@dataclass(slots=True)
class CameraRowPacket:
    raw: bytes
    header: RowHeader
    payload: bytes
    trailer: RowTrailer

    received_crc: int
    calculated_crc: int
    crc_ok: bool

    first_row: bool
    last_row: bool
    frame_overflow: bool
    length_error: bool
    fpga_crc_error: bool


def peek_camera_id(raw_ethernet_frame: bytes) -> Optional[int]:
    """Return the on-wire cam_id byte if the Ethernet frame is long enough.

    The current protocol places cam_id at Ethernet payload offset 4, which is
    absolute frame offset 18 once the 14-byte Ethernet header is included.
    This helper intentionally does not validate the full camera header; it is
    just the cheap routing hint needed by the capture thread.
    """
    if len(raw_ethernet_frame) <= 18:
        return None
    return raw_ethernet_frame[18]


def crc16_ccitt_false(data: bytes, initial: int = 0xFFFF) -> int:
    """CRC-16-CCITT (False): poly=0x1021, init=0xFFFF, refin=false,
    refout=false, xorout=0x0000 -- matches the FPGA crc16_byte core."""
    return binascii.crc_hqx(data, initial & 0xFFFF)


def parse_camera_row(payload: bytes) -> CameraRowPacket:
    """Unpack a 128-byte camera-row packet. Raises ValueError if the
    length is wrong; callers that need a non-raising result should use
    camera_parser.parse_camera_mode() instead."""
    if len(payload) != PACKET_LEN:
        raise ValueError(f"camera row packet must be {PACKET_LEN} bytes, got {len(payload)}")

    # The archived A5 A5 5A 5A vector predates the current wire format and
    # used little-endian metadata. Keep that one regression readable; all
    # current and malformed packets are interpreted with the current
    # MSB-byte-first metadata layout.
    body_struct = (
        _BODY_STRUCT_LE
        if payload[:4] == _LEGACY_SYNC_BYTES
        else _BODY_STRUCT_BE
    )
    (sync0, sync1, cam_id, frame_id, row_idx, row_flags, payload_len,
     row_seq, reserved, row_payload, pad, m00, xc_q4, yc_q4, vx_q8,
     vy_q8) = body_struct.unpack(payload[:_CRC_COVERED_LEN])
    crc16 = int.from_bytes(payload[_CRC_COVERED_LEN:], "little")

    header = RowHeader(sync0, sync1, cam_id, frame_id, row_idx,
                        row_flags, payload_len, row_seq, reserved)
    trailer = RowTrailer(pad, m00, xc_q4, yc_q4, vx_q8, vy_q8, crc16)

    calculated_crc = crc16_ccitt_false(payload[:_CRC_COVERED_LEN])

    return CameraRowPacket(
        raw=payload,
        header=header,
        payload=row_payload,
        trailer=trailer,
        received_crc=crc16,
        calculated_crc=calculated_crc,
        crc_ok=(crc16 == calculated_crc),
        first_row=bool(row_flags & FLAG_FIRST_ROW),
        last_row=bool(row_flags & FLAG_LAST_ROW),
        frame_overflow=bool(
            (row_flags & FLAG_FRAME_OVERFLOW)
            or (header.fpga_status & FPGA_STATUS_FRAME_OVERFLOW)
        ),
        length_error=bool(header.fpga_status & FPGA_STATUS_LENGTH_ERROR),
        fpga_crc_error=bool(header.fpga_status & FPGA_STATUS_CRC_ERROR),
    )


def build_camera_row(
    *,
    cam_id: int,
    frame_id: int,
    row_idx: int,
    row_flags: int,
    row_seq: int,
    payload: bytes,
    sync0: int = SYNC0_DEFAULT,
    sync1: int = SYNC1_DEFAULT,
    payload_len: Optional[int] = None,
    reserved: bytes = b"\x00" * 11,
    fpga_status: Optional[int] = None,
    header_check: Optional[int] = None,
    pad: bytes = b"\x00" * 10,
    m00: int = 0,
    xc_q4: int = 0,
    yc_q4: int = 0,
    vx_q8: int = 0,
    vy_q8: int = 0,
    corrupt_crc: bool = False,
) -> bytes:
    """Build a well-formed (or, with corrupt_crc=True, deliberately
    broken) 128-byte camera row packet.

    This is the synthetic-packet generator: it's what lets Layers 2-4
    be exercised in unit tests without any RMII/FPGA hardware, real
    NIC, or Npcap/root privileges in the loop -- see README.md.
    """
    if len(payload) != ROW_BYTES:
        raise ValueError(f"payload must be {ROW_BYTES} bytes, got {len(payload)}")
    if len(reserved) != 11:
        raise ValueError("reserved must be 11 bytes")
    if len(pad) != 10:
        raise ValueError("pad must be 10 bytes")
    if payload_len is None:
        payload_len = ROW_BYTES

    reserved_bytes = bytearray(reserved)
    if fpga_status is not None:
        if not 0 <= fpga_status <= 0xFF:
            raise ValueError("fpga_status must fit in one byte")
        reserved_bytes[0] = fpga_status
    if header_check is not None:
        if not 0 <= header_check <= 0xFF:
            raise ValueError("header_check must fit in one byte")
        reserved_bytes[1] = header_check

    body = _BODY_STRUCT_BE.pack(
        sync0, sync1, cam_id, frame_id, row_idx, row_flags,
        payload_len, row_seq, bytes(reserved_bytes), payload, pad, m00,
        xc_q4, yc_q4, vx_q8, vy_q8,
    )
    crc = crc16_ccitt_false(body)
    if corrupt_crc:
        crc ^= 0xFFFF
    return body + struct.pack("<H", crc)


class ByteStreamFramer:
    """Depacketizer for a *continuous, unframed* byte stream -- e.g. a
    raw UART/serial bridge or an FPGA DMA/debug channel that hasn't
    already been split into discrete Ethernet frames by an OS NIC
    driver.

    This is NOT needed for the current Scapy/Npcap capture path --
    libpcap already delivers whole frames there. It's the answer to
    "what if I extend capture to a raw byte-stream source later":
    the FPGA's RMII-side "shift 2 bits in, count to 8" logic has
    already been done for you by the time bytes reach Python (by the
    PHY/MAC + driver, or by the FPGA's own RMII receiver block if
    *you* control that RTL). Python only ever needs the byte-level
    analogue: hunt for the sync word to (re)gain alignment, then count
    up to PACKET_LEN bytes.
    """

    def __init__(self, on_packet):
        self._buf = bytearray()
        self._on_packet = on_packet
        self._sync = SYNC_BYTES_DEFAULT

    def feed(self, chunk: bytes) -> None:
        self._buf.extend(chunk)
        while True:
            idx = self._buf.find(self._sync)
            if idx == -1:
                # No sync word yet -- keep only enough tail bytes that
                # a sync word could still be found once split across
                # chunk boundaries.
                keep = len(self._sync) - 1
                if len(self._buf) > keep:
                    del self._buf[: len(self._buf) - keep]
                return

            if idx > 0:
                del self._buf[:idx]

            if len(self._buf) < PACKET_LEN:
                return

            packet = bytes(self._buf[:PACKET_LEN])
            del self._buf[:PACKET_LEN]
            self._on_packet(packet)

    def reset(self) -> None:
        self._buf.clear()
