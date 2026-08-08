"""
Streaming Ethernet reader for classic PCAP and PCAPNG files.

This module intentionally uses only the Python standard library.  It provides
an offline Layer-1 source when Scapy/Npcap is unavailable and returns the same
RawEthernetFrame type as the live capture path.

Supported capture records:

* classic PCAP, little- or big-endian, microsecond or nanosecond timestamps;
* PCAPNG Enhanced Packet Blocks and Simple Packet Blocks;
* multiple PCAPNG sections and interfaces;
* Ethernet link type (LINKTYPE_ETHERNET / DLT_EN10MB) only.

Unknown PCAPNG block types are skipped.  Corrupt/truncated block structure is
reported as PcapFormatError instead of being silently ignored.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Optional

from .capture import RawEthernetFrame
from .packet_format import peek_camera_id


LINKTYPE_ETHERNET = 1

_PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": ("<", 1_000_000),
    b"\xa1\xb2\xc3\xd4": (">", 1_000_000),
    b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000),
    b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000),
}

_PCAPNG_SECTION_HEADER = b"\x0a\x0d\x0d\x0a"
_PCAPNG_BYTE_ORDER = {
    b"\x4d\x3c\x2b\x1a": "<",
    b"\x1a\x2b\x3c\x4d": ">",
}

_PCAPNG_IDB = 0x00000001
_PCAPNG_PACKET = 0x00000002
_PCAPNG_SIMPLE_PACKET = 0x00000003
_PCAPNG_ENHANCED_PACKET = 0x00000006


class PcapFormatError(ValueError):
    """The capture file is truncated, structurally invalid, or unsupported."""


@dataclass(frozen=True, slots=True)
class _Interface:
    link_type: int
    timestamp_unit: float = 1e-6


def _read_exact(handle: BinaryIO, size: int, context: str) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise PcapFormatError(
            f"truncated {context}: expected {size} bytes, got {len(data)}"
        )
    return data


def _normalise_mac(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    octets = value.replace("-", ":").lower().split(":")
    if len(octets) != 6 or any(
        len(octet) != 2 or any(ch not in "0123456789abcdef" for ch in octet)
        for octet in octets
    ):
        raise ValueError(f"invalid MAC address: {value!r}")
    return ":".join(octets)


def _mac_string(raw: bytes) -> str:
    return ":".join(f"{octet:02x}" for octet in raw)


def decode_ethernet_frame(raw: bytes, timestamp: float = 0.0) -> RawEthernetFrame:
    """Decode a captured Ethernet-II frame without interpreting its payload."""
    if len(raw) < 14:
        raise PcapFormatError(
            f"captured Ethernet frame is too short: {len(raw)} bytes"
        )

    return RawEthernetFrame(
        src_mac=_mac_string(raw[6:12]),
        dst_mac=_mac_string(raw[0:6]),
        ethertype=int.from_bytes(raw[12:14], "big"),
        camera_id=peek_camera_id(raw),
        payload=raw[14:],
        raw_bytes=raw,
        timestamp=timestamp,
    )


def iter_ethernet_frames(
    path: str | Path,
    *,
    ether_type: Optional[int] = None,
    source_mac: Optional[str] = None,
) -> Iterator[RawEthernetFrame]:
    """
    Yield Ethernet frames from a PCAP/PCAPNG file in capture order.

    Filtering happens after Ethernet decoding.  ``ether_type=None`` and
    ``source_mac=None`` disable the corresponding filter.
    """
    capture_path = Path(path)
    wanted_source = _normalise_mac(source_mac)
    if ether_type is not None and not 0 <= ether_type <= 0xFFFF:
        raise ValueError("ether_type must fit in 16 bits")

    with capture_path.open("rb") as handle:
        magic = _read_exact(handle, 4, "capture file header")
        handle.seek(0)

        if magic in _PCAP_MAGIC:
            records = _iter_classic_pcap(handle)
        elif magic == _PCAPNG_SECTION_HEADER:
            records = _iter_pcapng(handle)
        else:
            raise PcapFormatError(
                f"unsupported capture magic {magic.hex()} in {capture_path}"
            )

        for raw, timestamp, link_type in records:
            if link_type != LINKTYPE_ETHERNET:
                raise PcapFormatError(
                    f"unsupported link type {link_type}; expected Ethernet (1)"
                )
            frame = decode_ethernet_frame(raw, timestamp)
            if ether_type is not None and frame.ethertype != ether_type:
                continue
            if wanted_source is not None and frame.src_mac != wanted_source:
                continue
            yield frame


def read_ethernet_frames(
    path: str | Path,
    *,
    ether_type: Optional[int] = None,
    source_mac: Optional[str] = None,
) -> list[RawEthernetFrame]:
    """Materialise :func:`iter_ethernet_frames`; intended for small captures."""
    return list(
        iter_ethernet_frames(
            path, ether_type=ether_type, source_mac=source_mac
        )
    )


class StdlibPcapReplayFrameSource:
    """Synchronous FrameSource-compatible replay without Scapy."""

    def __init__(
        self,
        path: str | Path,
        *,
        ether_type: Optional[int] = None,
        source_mac: Optional[str] = None,
    ):
        self.path = Path(path)
        self.ether_type = ether_type
        self.source_mac = source_mac
        self._stopped = False

    def start(self, on_frame: Callable[[RawEthernetFrame], None]) -> None:
        self._stopped = False
        for frame in iter_ethernet_frames(
            self.path,
            ether_type=self.ether_type,
            source_mac=self.source_mac,
        ):
            if self._stopped:
                break
            on_frame(frame)

    def stop(self) -> None:
        self._stopped = True


def _iter_classic_pcap(
    handle: BinaryIO,
) -> Iterator[tuple[bytes, float, int]]:
    header = _read_exact(handle, 24, "classic PCAP global header")
    try:
        endian, timestamp_divisor = _PCAP_MAGIC[header[:4]]
    except KeyError as exc:
        raise PcapFormatError("invalid classic PCAP magic") from exc

    _major, _minor, _thiszone, _sigfigs, _snaplen, network = struct.unpack(
        f"{endian}HHiiii", header[4:]
    )
    link_type = network & 0xFFFF
    packet_header = struct.Struct(f"{endian}IIII")

    while True:
        raw_header = handle.read(packet_header.size)
        if raw_header == b"":
            return
        if len(raw_header) != packet_header.size:
            raise PcapFormatError("truncated classic PCAP packet header")

        seconds, fraction, captured_len, _original_len = packet_header.unpack(
            raw_header
        )
        packet = _read_exact(
            handle, captured_len, "classic PCAP packet data"
        )
        timestamp = seconds + fraction / timestamp_divisor
        yield packet, timestamp, link_type


def _iter_pcapng(
    handle: BinaryIO,
) -> Iterator[tuple[bytes, float, int]]:
    endian: Optional[str] = None
    interfaces: list[_Interface] = []

    while True:
        block_type_raw = handle.read(4)
        if block_type_raw == b"":
            return
        if len(block_type_raw) != 4:
            raise PcapFormatError("truncated PCAPNG block type")

        block_length_raw = _read_exact(
            handle, 4, "PCAPNG block total length"
        )

        if block_type_raw == _PCAPNG_SECTION_HEADER:
            byte_order_raw = _read_exact(
                handle, 4, "PCAPNG byte-order magic"
            )
            try:
                endian = _PCAPNG_BYTE_ORDER[byte_order_raw]
            except KeyError as exc:
                raise PcapFormatError(
                    f"invalid PCAPNG byte-order magic {byte_order_raw.hex()}"
                ) from exc

            block_length = struct.unpack(
                f"{endian}I", block_length_raw
            )[0]
            _validate_block_length(block_length, minimum=28)
            remainder = _read_exact(
                handle,
                block_length - 12,
                "PCAPNG section header block",
            )
            _validate_trailer(remainder[-4:], block_length, endian)
            interfaces = []
            continue

        if endian is None:
            raise PcapFormatError(
                "PCAPNG data block encountered before a section header"
            )

        block_type = struct.unpack(f"{endian}I", block_type_raw)[0]
        block_length = struct.unpack(f"{endian}I", block_length_raw)[0]
        _validate_block_length(block_length, minimum=12)
        remainder = _read_exact(
            handle, block_length - 8, "PCAPNG block"
        )
        _validate_trailer(remainder[-4:], block_length, endian)
        body = remainder[:-4]

        if block_type == _PCAPNG_IDB:
            interfaces.append(_parse_interface(body, endian))
        elif block_type == _PCAPNG_ENHANCED_PACKET:
            yield _parse_enhanced_packet(body, endian, interfaces)
        elif block_type == _PCAPNG_SIMPLE_PACKET:
            yield _parse_simple_packet(body, endian, interfaces)
        elif block_type == _PCAPNG_PACKET:
            yield _parse_obsolete_packet(body, endian, interfaces)


def _validate_block_length(block_length: int, *, minimum: int) -> None:
    if block_length < minimum or block_length % 4:
        raise PcapFormatError(
            f"invalid PCAPNG block length {block_length}"
        )


def _validate_trailer(
    trailer: bytes, block_length: int, endian: str
) -> None:
    trailing_length = struct.unpack(f"{endian}I", trailer)[0]
    if trailing_length != block_length:
        raise PcapFormatError(
            "PCAPNG leading and trailing block lengths do not match"
        )


def _parse_interface(body: bytes, endian: str) -> _Interface:
    if len(body) < 8:
        raise PcapFormatError("short PCAPNG Interface Description Block")
    link_type = struct.unpack(f"{endian}H", body[0:2])[0]
    timestamp_unit = 1e-6

    offset = 8
    while offset + 4 <= len(body):
        option_code, option_length = struct.unpack(
            f"{endian}HH", body[offset : offset + 4]
        )
        offset += 4
        if offset + option_length > len(body):
            raise PcapFormatError("truncated PCAPNG interface option")
        option_value = body[offset : offset + option_length]
        offset += (option_length + 3) & ~3

        if option_code == 0:
            break
        if option_code == 9 and option_length == 1:
            resolution = option_value[0]
            if resolution & 0x80:
                timestamp_unit = 2.0 ** -(resolution & 0x7F)
            else:
                timestamp_unit = 10.0 ** -resolution

    return _Interface(link_type=link_type, timestamp_unit=timestamp_unit)


def _get_interface(
    interfaces: list[_Interface], interface_id: int
) -> _Interface:
    if interface_id >= len(interfaces):
        raise PcapFormatError(
            f"PCAPNG packet references missing interface {interface_id}"
        )
    return interfaces[interface_id]


def _packet_bytes(body: bytes, offset: int, captured_len: int) -> bytes:
    padded_len = (captured_len + 3) & ~3
    if offset + padded_len > len(body):
        raise PcapFormatError("truncated PCAPNG packet data")
    return body[offset : offset + captured_len]


def _parse_enhanced_packet(
    body: bytes, endian: str, interfaces: list[_Interface]
) -> tuple[bytes, float, int]:
    if len(body) < 20:
        raise PcapFormatError("short PCAPNG Enhanced Packet Block")
    interface_id, timestamp_high, timestamp_low, captured_len, _packet_len = (
        struct.unpack(f"{endian}IIIII", body[:20])
    )
    interface = _get_interface(interfaces, interface_id)
    timestamp_ticks = (timestamp_high << 32) | timestamp_low
    packet = _packet_bytes(body, 20, captured_len)
    return (
        packet,
        timestamp_ticks * interface.timestamp_unit,
        interface.link_type,
    )


def _parse_simple_packet(
    body: bytes, endian: str, interfaces: list[_Interface]
) -> tuple[bytes, float, int]:
    if not interfaces:
        raise PcapFormatError(
            "PCAPNG Simple Packet Block has no interface"
        )
    if len(body) < 4:
        raise PcapFormatError("short PCAPNG Simple Packet Block")
    original_len = struct.unpack(f"{endian}I", body[:4])[0]
    captured_len = min(original_len, len(body) - 4)
    return body[4 : 4 + captured_len], 0.0, interfaces[0].link_type


def _parse_obsolete_packet(
    body: bytes, endian: str, interfaces: list[_Interface]
) -> tuple[bytes, float, int]:
    if len(body) < 20:
        raise PcapFormatError("short PCAPNG obsolete Packet Block")
    interface_id, _drops, timestamp_high, timestamp_low, captured_len, _packet_len = (
        struct.unpack(f"{endian}HHIIII", body[:20])
    )
    interface = _get_interface(interfaces, interface_id)
    timestamp_ticks = (timestamp_high << 32) | timestamp_low
    packet = _packet_bytes(body, 20, captured_len)
    return (
        packet,
        timestamp_ticks * interface.timestamp_unit,
        interface.link_type,
    )
