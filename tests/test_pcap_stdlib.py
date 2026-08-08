from __future__ import annotations

import struct
from pathlib import Path

import pytest

from taxi_receiver.eth_validate import validate_ethernet_frame
from taxi_receiver.pcap_stdlib import (
    PcapFormatError,
    StdlibPcapReplayFrameSource,
    decode_ethernet_frame,
    iter_ethernet_frames,
    read_ethernet_frames,
)
from taxi_receiver.pipeline import TaxiReceiverPipeline
from taxi_receiver.reassembler import NullReassembler


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "taxi_receiver").is_dir() and (candidate / "tests").is_dir():
            return candidate
    raise RuntimeError(f"Could not locate repository root from {start}")


PROJECT_ROOT = _find_repo_root(Path(__file__).resolve())
FIXED_1000 = PROJECT_ROOT / "build/ethernet_ila/wireshark_fixed_1000.pcapng"
INTERNAL_FIFO = PROJECT_ROOT / "build/ethernet_ila/internal_byte_fifo_0x88b5.pcapng"

EXPECTED_PAYLOAD = bytes(range(128))
DST = bytes.fromhex("ffffffffffff")
SRC = bytes.fromhex("020000000002")


def _ethernet(payload: bytes = EXPECTED_PAYLOAD, ether_type: int = 0x88B5) -> bytes:
    return DST + SRC + ether_type.to_bytes(2, "big") + payload


def _classic_pcap(
    frames: list[bytes],
    *,
    endian: str = "<",
    nanoseconds: bool = False,
) -> bytes:
    if endian == "<":
        magic = b"\x4d\x3c\xb2\xa1" if nanoseconds else b"\xd4\xc3\xb2\xa1"
    else:
        magic = b"\xa1\xb2\x3c\x4d" if nanoseconds else b"\xa1\xb2\xc3\xd4"
    result = bytearray(magic)
    result += struct.pack(f"{endian}HHiiii", 2, 4, 0, 0, 65535, 1)
    fraction = 500_000_000 if nanoseconds else 500_000
    for index, frame in enumerate(frames):
        result += struct.pack(
            f"{endian}IIII", index + 1, fraction, len(frame), len(frame)
        )
        result += frame
    return bytes(result)


def _block(block_type: int, body: bytes, endian: str) -> bytes:
    body += b"\x00" * ((-len(body)) & 3)
    block_len = 12 + len(body)
    return (
        struct.pack(f"{endian}II", block_type, block_len)
        + body
        + struct.pack(f"{endian}I", block_len)
    )


def _pcapng(frames: list[bytes], *, endian: str = "<") -> bytes:
    bom = 0x1A2B3C4D
    shb = _block(
        0x0A0D0D0A,
        struct.pack(f"{endian}IHHq", bom, 1, 0, -1),
        endian,
    )
    idb = _block(
        1,
        struct.pack(f"{endian}HHI", 1, 0, 65535),
        endian,
    )
    packets = bytearray()
    for index, frame in enumerate(frames):
        packets += _block(
            6,
            struct.pack(
                f"{endian}IIIII", 0, 0, index + 1, len(frame), len(frame)
            )
            + frame,
            endian,
        )
    return shb + idb + bytes(packets)


def test_decode_ethernet_header_and_payload():
    frame = decode_ethernet_frame(_ethernet())
    assert frame.dst_mac == "ff:ff:ff:ff:ff:ff"
    assert frame.src_mac == "02:00:00:00:00:02"
    assert frame.ethertype == 0x88B5
    assert frame.payload == EXPECTED_PAYLOAD


def test_decode_rejects_short_ethernet_frame():
    with pytest.raises(PcapFormatError, match="too short"):
        decode_ethernet_frame(bytes(13))


def test_classic_pcap_little_endian(tmp_path):
    path = tmp_path / "little.pcap"
    path.write_bytes(_classic_pcap([_ethernet()]))
    frames = read_ethernet_frames(path)
    assert len(frames) == 1
    assert frames[0].timestamp == pytest.approx(1.5)
    assert frames[0].payload == EXPECTED_PAYLOAD


def test_classic_pcap_big_endian_nanoseconds(tmp_path):
    path = tmp_path / "big-ns.pcap"
    path.write_bytes(
        _classic_pcap([_ethernet()], endian=">", nanoseconds=True)
    )
    frames = read_ethernet_frames(path)
    assert len(frames) == 1
    assert frames[0].timestamp == pytest.approx(1.5)


def test_pcapng_little_endian_enhanced_packet(tmp_path):
    path = tmp_path / "little.pcapng"
    path.write_bytes(_pcapng([_ethernet()]))
    frame = read_ethernet_frames(path)[0]
    assert frame.ethertype == 0x88B5
    assert frame.payload == EXPECTED_PAYLOAD


def test_pcapng_big_endian_enhanced_packet(tmp_path):
    path = tmp_path / "big.pcapng"
    path.write_bytes(_pcapng([_ethernet()], endian=">"))
    frame = read_ethernet_frames(path)[0]
    assert frame.src_mac == "02:00:00:00:00:02"
    assert frame.payload == EXPECTED_PAYLOAD


def test_filters_and_frame_source_callback(tmp_path):
    path = tmp_path / "filter.pcap"
    path.write_bytes(
        _classic_pcap(
            [
                _ethernet(ether_type=0x0806),
                _ethernet(),
            ]
        )
    )
    received = []
    source = StdlibPcapReplayFrameSource(
        path,
        ether_type=0x88B5,
        source_mac="02-00-00-00-00-02",
    )
    source.start(received.append)
    assert len(received) == 1
    assert validate_ethernet_frame(received[0]).ok


def test_truncated_capture_reports_format_error(tmp_path):
    path = tmp_path / "truncated.pcap"
    path.write_bytes(_classic_pcap([_ethernet()])[:-1])
    with pytest.raises(PcapFormatError, match="truncated"):
        list(iter_ethernet_frames(path))


@pytest.mark.skipif(
    not FIXED_1000.exists(),
    reason=(
        "requires the repository's external pcapng regression artifact: "
        "build/ethernet_ila/wireshark_fixed_1000.pcapng"
    ),
)
def test_repository_fixed_1000_pcap_layer1_layer2_regression():
    count = 0
    for frame in iter_ethernet_frames(
        FIXED_1000,
        ether_type=0x88B5,
        source_mac="02:00:00:00:00:02",
    ):
        assert len(frame.raw_bytes) == 142
        assert frame.payload == EXPECTED_PAYLOAD
        assert validate_ethernet_frame(frame).ok
        count += 1
    assert count == 1000


@pytest.mark.skipif(
    not INTERNAL_FIFO.exists(),
    reason=(
        "requires the repository's external pcapng regression artifact: "
        "build/ethernet_ila/internal_byte_fifo_0x88b5.pcapng"
    ),
)
def test_repository_internal_fifo_pcap_layer1_layer2_regression():
    count = 0
    for frame in iter_ethernet_frames(
        INTERNAL_FIFO,
        ether_type=0x88B5,
        source_mac="02:00:00:00:00:02",
    ):
        assert len(frame.raw_bytes) == 142
        assert frame.payload == EXPECTED_PAYLOAD
        assert validate_ethernet_frame(frame).ok
        count += 1
    assert count == 229629


def test_lossless_replay_backpressures_a_small_pipeline_queue(tmp_path):
    frame_count = 200
    path = tmp_path / "lossless.pcap"
    path.write_bytes(
        _classic_pcap(
            [
                _ethernet(payload=bytes([index & 0xFF]) * 128)
                for index in range(frame_count)
            ]
        )
    )
    source = StdlibPcapReplayFrameSource(
        path,
        ether_type=0x88B5,
        source_mac="02:00:00:00:00:02",
    )
    pipeline = TaxiReceiverPipeline(
        frame_source=source,
        mode="camera",
        max_stage="validate",
        reassembler=NullReassembler(),
        queue_depth=1,
        lossless_input=True,
        report_interval=999,
        sink=lambda *_: None,
    )

    pipeline.start()
    pipeline.stop()

    assert pipeline.monitor.stats.total_ethernet_frames == frame_count
    assert pipeline.monitor.stats.dropped_capture_queue == 0
