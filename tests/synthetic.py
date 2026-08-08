"""Synthetic packet/frame builders shared by the tests. This is the
concrete answer to "how do I validate Layers 2-4 without any RMII/FPGA
hardware": build 128-byte packets directly with packet_format's
struct.pack-based builder, wrap them in a plain RawEthernetFrame, and
feed those into whichever layer you're testing."""
from __future__ import annotations

import time

from taxi_receiver.capture import RawEthernetFrame
from taxi_receiver.packet_format import build_camera_row

ETHER_TYPE = 0x88B5
FIXED_TEST_PAYLOAD = bytes(range(128))


def make_raw_frame(
    payload: bytes,
    *,
    src_mac: str = "02:00:00:00:00:01",
    dst_mac: str = "ff:ff:ff:ff:ff:ff",
    ethertype: int = ETHER_TYPE,
    timestamp: float | None = None,
    camera_id: int | None = None,
) -> RawEthernetFrame:
    header = (
        bytes.fromhex(dst_mac.replace(":", ""))
        + bytes.fromhex(src_mac.replace(":", ""))
        + ethertype.to_bytes(2, "big")
    )
    return RawEthernetFrame(
        src_mac=src_mac,
        dst_mac=dst_mac,
        ethertype=ethertype,
        camera_id=camera_id,
        payload=payload,
        raw_bytes=header + payload,
        timestamp=time.time() if timestamp is None else timestamp,
    )


def make_camera_frame(
    *,
    cam_id: int,
    frame_id: int,
    row_idx: int,
    row_seq: int,
    row_flags: int = 0,
    payload: bytes | None = None,
    corrupt_crc: bool = False,
) -> RawEthernetFrame:
    if payload is None:
        payload = bytes([row_idx & 0xFF]) * 80
    raw = build_camera_row(
        cam_id=cam_id, frame_id=frame_id, row_idx=row_idx,
        row_flags=row_flags, row_seq=row_seq, payload=payload,
        corrupt_crc=corrupt_crc,
    )
    return make_raw_frame(raw, camera_id=cam_id)


def make_fixed_frame(corrupt: bool = False) -> RawEthernetFrame:
    payload = bytearray(FIXED_TEST_PAYLOAD)
    if corrupt:
        payload[5] ^= 0xFF
    return make_raw_frame(bytes(payload))
