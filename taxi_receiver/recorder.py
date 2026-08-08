"""
recorder.py  --  optional side-effects: saving matching frames to a
PCAP file, and saving malformed payloads as individual .bin files for
offline debugging. Kept out of every other layer so none of them do
file I/O directly; the pipeline wires these in only if requested.
"""
from __future__ import annotations

from pathlib import Path


class PcapRecorder:
    """Scapy is imported lazily here -- this is a capture-adjacent I/O
    concern, same category as capture.py, so it's fine for it to be
    the other module that knows scapy exists."""

    def __init__(self, path: Path):
        from scapy.utils import PcapWriter
        path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = PcapWriter(str(path), append=False, sync=True)

    def write_raw(self, raw_bytes: bytes) -> None:
        from scapy.layers.l2 import Ether
        self._writer.write(Ether(raw_bytes))

    def close(self) -> None:
        self._writer.close()


class ErrorFrameRecorder:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self._directory = directory
        self._index = 0

    def save(self, reason: str, payload: bytes) -> Path:
        self._index += 1
        path = self._directory / f"{self._index:06d}_{reason}.bin"
        path.write_bytes(payload)
        return path
