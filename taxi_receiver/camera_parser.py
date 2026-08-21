"""
camera_parser.py  --  Layer 3 (Camera Packet Parser).

Wraps packet_format's pure struct/CRC logic with the two operating
modes from the original tool (`fixed` self-test, `camera` real
packets), and returns plain result dataclasses instead of mutating
statistics or printing -- that split is what makes this layer testable
on its own, and lets Layer 4 (stream_monitor) stay a dumb consumer of
results rather than reimplementing parsing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .packet_format import (
    CameraRowPacket,
    PACKET_LEN,
    ROW_BYTES,
    SOURCE_ROW_FLAG_MASK,
    SYNC0_DEFAULT,
    SYNC1_DEFAULT,
    parse_camera_row,
)

FIXED_TEST_PAYLOAD = bytes(range(128))
LEGACY_PROTOCOL = "legacy-v0-observed"
EXPECTED_IMAGE_ROWS = 480


@dataclass(slots=True)
class FixedModeResult:
    ok: bool
    reason: str = ""  # "", "bad_length", "bad_data"
    mismatch_offset: Optional[int] = None
    received_len: int = 0


@dataclass(slots=True)
class CameraModeResult:
    ok: bool
    reason: str = ""
    packet: Optional[CameraRowPacket] = None
    received_len: int = 0
    protocol: str = LEGACY_PROTOCOL
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def parse_fixed_mode(payload: bytes) -> FixedModeResult:
    if len(payload) != PACKET_LEN:
        return FixedModeResult(ok=False, reason="bad_length", received_len=len(payload))

    if payload != FIXED_TEST_PAYLOAD:
        offset = _first_mismatch(payload, FIXED_TEST_PAYLOAD)
        return FixedModeResult(
            ok=False, reason="bad_data",
            mismatch_offset=offset, received_len=len(payload),
        )

    return FixedModeResult(ok=True, received_len=len(payload))


def parse_camera_mode(payload: bytes) -> CameraModeResult:
    if len(payload) != PACKET_LEN:
        return CameraModeResult(
            ok=False,
            reason="bad_length",
            received_len=len(payload),
            errors=("bad_length",),
        )

    packet = parse_camera_row(payload)
    errors: list[str] = []
    warnings: list[str] = []

    # Current protocol words are 0xA5A0/0x5A50 and the observed/expected raw
    # Ethernet payload bytes are A5 A0 5A 50 (MSB byte first). There is no
    # protocol-version field; reject another pair instead of silently parsing
    # an incompatible layout.
    if (
        packet.header.sync0 != SYNC0_DEFAULT
        or packet.header.sync1 != SYNC1_DEFAULT
    ):
        errors.append("bad_sync")

    # payload_len describes valid bytes inside the fixed 80-byte row payload.
    # Zero is not rejected because the RP2350A source definition is absent;
    # surface it as a warning until firmware defines whether an empty row is
    # legal.  A value larger than the physical field is unambiguously invalid.
    if packet.header.payload_len > ROW_BYTES:
        errors.append("payload_len_out_of_range")
    elif packet.header.payload_len == 0:
        warnings.append("zero_payload_len")

    if not packet.crc_ok:
        errors.append("crc_error")

    if packet.header.cam_id > 3:
        errors.append("cam_id_out_of_range")
    if packet.header.row_idx >= EXPECTED_IMAGE_ROWS:
        errors.append("row_idx_out_of_range")

    # offset 9 now belongs only to the MCU.  Any bit outside overflow/last/first
    # is source-data corruption or a protocol-version mismatch, not FPGA status.
    if packet.header.row_flags & ~SOURCE_ROW_FLAG_MASK:
        errors.append("undefined_flag_bits")

    # reserved[0] is FPGA status and reserved[1] is reserved for a future MCU
    # header check.  The remaining bytes are expected to stay zero.
    if any(packet.header.reserved[2:]):
        warnings.append("reserved_nonzero")

    # Source overflow (offset 9 bit 0) and FPGA status (offset 13) both make a
    # row unsuitable, but packet_format keeps their wire origins observable.
    if packet.frame_overflow:
        errors.append("frame_overflow")
    if packet.length_error:
        errors.append("length_error")
    if packet.fpga_crc_error:
        errors.append("fpga_crc_error")

    return CameraModeResult(
        ok=not errors,
        reason=errors[0] if errors else "",
        packet=packet,
        received_len=len(payload),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def _first_mismatch(received: bytes, expected: bytes) -> int:
    for index, (actual, wanted) in enumerate(zip(received, expected)):
        if actual != wanted:
            return index
    return min(len(received), len(expected))
