"""
capture.py  --  Layer 1 (Capture).

This is the *only* layer that is allowed to know Scapy/Npcap exist,
and even here the import is deferred into the methods that need it,
not done at module scope. That means:

  * Importing taxi_receiver.capture (or anything downstream of it)
    never requires scapy or Npcap to be installed.
  * Unit tests for Layers 2-5 use SyntheticFrameSource below instead
    of a real NIC, with zero scapy dependency.
  * The RMII/PHY/MAC bit-to-byte assembly happens entirely below this
    module (in hardware, or in the OS driver) -- by the time a
    RawEthernetFrame exists, framing is already done. See
    packet_format.ByteStreamFramer for the one scenario where Python
    *would* need to do its own (byte-level) framing: a future capture
    source that hands over a raw, un-delimited byte stream instead of
    already-segmented Ethernet frames.
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from ctypes import POINTER, byref, c_ubyte, create_string_buffer, string_at
from typing import Callable, Iterable, Optional, Protocol

from .packet_format import peek_camera_id


DEFAULT_PCAP_BUFFER_SIZE = 8 * 1024 * 1024


@dataclass(slots=True)
class PcapStatistics:
    ps_recv: int
    ps_drop: int
    ps_ifdrop: int
    ps_capt: Optional[int] = None
    ps_sent: Optional[int] = None
    ps_netdrop: Optional[int] = None


@dataclass(slots=True)
class RawEthernetFrame:
    """Output of Layer 1 / input to Layer 2. Deliberately plain data --
    no scapy Packet object survives past this point."""
    src_mac: str
    dst_mac: str
    ethertype: int
    payload: bytes
    raw_bytes: bytes  # full L2 frame, kept only for optional pcap recording
    timestamp: float
    camera_id: Optional[int] = None


class FrameSource(Protocol):
    def start(self, on_frame: Callable[[RawEthernetFrame], None]) -> None: ...
    def stop(self) -> None: ...


def _packet_timestamp(packet) -> float:
    packet_time = getattr(packet, "time", None)
    if packet_time is None:
        return time.time()
    return float(packet_time)


def _packet_timestamp_from_header(header) -> float:
    packet_header = getattr(header, "contents", header)
    timestamp = getattr(packet_header, "ts", None)
    if timestamp is not None:
        seconds = getattr(timestamp, "tv_sec", None)
        micros = getattr(timestamp, "tv_usec", None)
        if seconds is not None and micros is not None:
            return float(seconds) + float(micros) / 1_000_000.0

    seconds = getattr(packet_header, "tv_sec", None)
    micros = getattr(packet_header, "tv_usec", None)
    if seconds is not None and micros is not None:
        return float(seconds) + float(micros) / 1_000_000.0

    packet_time = getattr(packet_header, "time", None)
    if packet_time is not None:
        return float(packet_time)
    return time.time()


def list_interfaces() -> list[str]:
    from scapy.all import get_if_list
    return list(get_if_list())


class ScapyLiveCapture:
    """Real Layer 1: opens an explicit pcap handle so buffer sizing can
    be applied before activation.

    The capture loop extracts only src/dst/ethertype/payload/raw bytes
    -- no CRC math, no struct unpack of the 128-byte body, no printing,
    no file I/O -- keeping it as light as the original prototype's
    comment insisted on, while still handing every other layer plain
    data instead of a scapy Packet.
    """

    def __init__(
        self,
        interface: str,
        ether_type: int = 0x88B5,
        *,
        include_raw: bool = True,
        pcap_buffer_size: int = DEFAULT_PCAP_BUFFER_SIZE,
        read_timeout_ms: int = 100,
    ):
        self.interface = interface
        self.ether_type = ether_type
        self.include_raw = include_raw
        self.pcap_buffer_size = pcap_buffer_size
        self.read_timeout_ms = read_timeout_ms
        self._pcap_handle = None
        self._capture_thread: Optional[threading.Thread] = None
        self._stop_requested = threading.Event()
        self._sniffer = None
        self._socket = None
        self._pcap_stats_snapshot: Optional[PcapStatistics] = None

    def start(self, on_frame: Callable[[RawEthernetFrame], None]) -> None:
        from ctypes import byref
        from scapy.libs import winpcapy

        if self._pcap_handle is not None or (
            self._capture_thread is not None and self._capture_thread.is_alive()
        ):
            raise RuntimeError("capture is already running")

        self._stop_requested.clear()
        errbuf = create_string_buffer(getattr(winpcapy, "PCAP_ERRBUF_SIZE", 256))
        iface = create_string_buffer(self.interface.encode("utf8"))

        handle = winpcapy.pcap_create(iface, errbuf)
        if not handle:
            error = self._decode_error_buffer(errbuf)
            raise OSError(error or "pcap_create failed")

        self._pcap_handle = handle
        self._socket = None

        try:
            self._configure_pcap_handle(winpcapy, handle, errbuf)

            activate_status = winpcapy.pcap_activate(handle)
            if activate_status < 0:
                raise OSError(self._pcap_error(winpcapy, handle))

            filter_program = winpcapy.bpf_program()
            filter_expression = f"ether proto 0x{self.ether_type:04x}".encode("utf8")
            if winpcapy.pcap_compile(handle, byref(filter_program), filter_expression, 1, 0) != 0:
                raise OSError(self._pcap_error(winpcapy, handle))
            try:
                if winpcapy.pcap_setfilter(handle, byref(filter_program)) != 0:
                    raise OSError(self._pcap_error(winpcapy, handle))
            finally:
                winpcapy.pcap_freecode(byref(filter_program))

            if hasattr(winpcapy, "pcap_setmintocopy"):
                mintocopy = max(64 * 1024, min(self.pcap_buffer_size // 128, 256 * 1024))
                if winpcapy.pcap_setmintocopy(handle, mintocopy) != 0:
                    raise OSError(self._pcap_error(winpcapy, handle))
        except Exception:
            try:
                winpcapy.pcap_close(handle)
            finally:
                self._pcap_handle = None
            raise

        def _run_capture() -> None:
            try:
                while not self._stop_requested.is_set():
                    packet = self._next_packet(winpcapy, handle)
                    if packet is None:
                        continue
                    packet_bytes, timestamp = packet
                    if len(packet_bytes) < 14:
                        continue
                    src_mac = ":".join(f"{octet:02x}" for octet in packet_bytes[6:12])
                    dst_mac = ":".join(f"{octet:02x}" for octet in packet_bytes[0:6])
                    on_frame(RawEthernetFrame(
                        src_mac=src_mac,
                        dst_mac=dst_mac,
                        ethertype=int.from_bytes(packet_bytes[12:14], "big"),
                        camera_id=peek_camera_id(packet_bytes),
                        payload=packet_bytes[14:],
                        raw_bytes=packet_bytes if self.include_raw else b"",
                        timestamp=timestamp,
                    ))
            except Exception:
                self._stop_requested.set()

        self._capture_thread = threading.Thread(
            target=_run_capture,
            name=f"pcap-capture-{self.interface}",
            daemon=True,
        )
        self._capture_thread.start()

    def stop(self) -> None:
        from scapy.libs import winpcapy

        handle = self._get_pcap_handle()
        if handle is not None:
            snapshot = self._read_pcap_stats()
            if snapshot is not None:
                self._pcap_stats_snapshot = snapshot

        stop_requested = getattr(self, "_stop_requested", None)
        if stop_requested is not None:
            stop_requested.set()
        if handle is not None and hasattr(winpcapy, "pcap_breakloop"):
            try:
                winpcapy.pcap_breakloop(handle)
            except Exception:
                pass

        capture_thread = getattr(self, "_capture_thread", None)
        if capture_thread is not None and capture_thread.is_alive():
            capture_thread.join(timeout=2.0)

        if handle is not None and hasattr(winpcapy, "pcap_close"):
            try:
                winpcapy.pcap_close(handle)
            finally:
                self._pcap_handle = None
        elif handle is not None:
            self._pcap_handle = None

        if self._sniffer is not None and getattr(self._sniffer, "running", False):
            self._sniffer.stop()

        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def pcap_stats(self) -> Optional[PcapStatistics]:
        if self._get_pcap_handle() is None:
            return self._pcap_stats_snapshot

        snapshot = self._read_pcap_stats()
        if snapshot is not None:
            self._pcap_stats_snapshot = snapshot
        return snapshot

    def _get_pcap_handle(self):
        if getattr(self, "_pcap_handle", None) is not None:
            return self._pcap_handle
        socket = getattr(self, "_socket", None)
        if socket is None:
            return None

        pcap_fd = getattr(socket, "pcap_fd", None)
        if pcap_fd is None:
            return None
        return getattr(pcap_fd, "pcap", None)

    def _configure_pcap_handle(self, winpcapy, handle, errbuf) -> None:
        if winpcapy.pcap_set_snaplen(handle, 65535) != 0:
            raise OSError(self._pcap_error(winpcapy, handle, errbuf))
        if winpcapy.pcap_set_promisc(handle, 1) != 0:
            raise OSError(self._pcap_error(winpcapy, handle, errbuf))
        if winpcapy.pcap_set_timeout(handle, self.read_timeout_ms) != 0:
            raise OSError(self._pcap_error(winpcapy, handle, errbuf))
        if winpcapy.pcap_set_buffer_size(handle, self.pcap_buffer_size) != 0:
            raise OSError(self._pcap_error(winpcapy, handle, errbuf))

    def _next_packet(self, winpcapy, handle) -> Optional[tuple[bytes, float]]:
        header = POINTER(winpcapy.pcap_pkthdr)()
        packet_data = POINTER(c_ubyte)()
        result = winpcapy.pcap_next_ex(handle, byref(header), byref(packet_data))
        if result == 1:
            caplen = int(header.contents.caplen)
            return bytes(string_at(packet_data, caplen)), _packet_timestamp_from_header(header)
        if result in (0, -2):
            return None
        raise OSError(self._pcap_error(winpcapy, handle))

    def _read_pcap_stats(self) -> Optional[PcapStatistics]:
        handle = self._get_pcap_handle()
        if handle is None:
            return None

        from ctypes import byref

        try:
            from scapy.libs import winpcapy
        except Exception:
            return None

        stat = winpcapy.pcap_stat()
        result = winpcapy.pcap_stats(handle, byref(stat))
        if result != 0:
            return None

        return PcapStatistics(
            ps_recv=int(stat.ps_recv),
            ps_drop=int(stat.ps_drop),
            ps_ifdrop=int(stat.ps_ifdrop),
            ps_capt=int(stat.ps_capt) if hasattr(stat, "ps_capt") else None,
            ps_sent=int(stat.ps_sent) if hasattr(stat, "ps_sent") else None,
            ps_netdrop=int(stat.ps_netdrop) if hasattr(stat, "ps_netdrop") else None,
        )

    def _decode_error_buffer(self, errbuf) -> str:
        return bytes(errbuf).split(b"\x00", 1)[0].decode("utf-8", errors="replace")

    def _pcap_error(self, winpcapy, handle, errbuf=None) -> str:
        try:
            error = winpcapy.pcap_geterr(handle)
            if error:
                return bytes(error).split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        except Exception:
            pass
        if errbuf is not None:
            message = self._decode_error_buffer(errbuf)
            if message:
                return message
        return "pcap operation failed"


class SyntheticFrameSource:
    """Test/offline Layer 1: replays a pre-built list (or generator)
    of RawEthernetFrame objects through the same FrameSource interface
    a real capture would use. No scapy, no NIC, no admin/root rights.

    This is the seam referred to throughout: because Layer 1's output
    type (RawEthernetFrame) is the boundary where "hardware-assembled
    bytes" become "Python objects", everything from here down can be
    validated with synthetic data regardless of whether the real
    source is a NIC, a pcap replay, or eventually a raw RMII/FPGA
    byte stream via ByteStreamFramer.
    """

    def __init__(self, frames: Iterable[RawEthernetFrame]):
        self._frames = list(frames)

    def start(self, on_frame: Callable[[RawEthernetFrame], None]) -> None:
        for frame in self._frames:
            on_frame(frame)

    def stop(self) -> None:
        pass


class PcapReplayFrameSource:
    """Offline Layer 1: replays frames from an existing .pcap file.
    Useful for regression testing against a real bench capture without
    needing the hardware present. Still touches scapy, but only here,
    and only for reading -- not for anything downstream."""

    def __init__(self, path: str, ether_type: Optional[int] = None):
        self.path = path
        self.ether_type = ether_type

    def start(self, on_frame: Callable[[RawEthernetFrame], None]) -> None:
        from scapy.all import rdpcap
        from scapy.layers.l2 import Ether

        for packet in rdpcap(self.path):
            if Ether not in packet:
                continue
            eth = packet[Ether]
            if self.ether_type is not None and int(eth.type) != self.ether_type:
                continue
            on_frame(RawEthernetFrame(
                src_mac=eth.src,
                dst_mac=eth.dst,
                ethertype=int(eth.type),
                camera_id=peek_camera_id(bytes(packet)),
                payload=bytes(eth.payload),
                raw_bytes=bytes(packet),
                timestamp=_packet_timestamp(packet),
            ))

    def stop(self) -> None:
        pass
