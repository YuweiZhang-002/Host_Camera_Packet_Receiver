"""
stream_monitor.py  --  Layer 4 (Stream Monitor).

Consumes the plain result objects produced by Layer 2/3 and turns them
into running statistics, per-camera sequence-integrity checks
(gap/duplicate/out-of-order), and periodic rate/throughput reports.

Nothing here imports scapy or does any capture-related work, so it's
fully testable with results built straight from synthetic packets
(see tests/).
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from .camera_parser import CameraModeResult, FixedModeResult


@dataclass
class CameraStatistics:
    packets: int = 0
    crc_errors: int = 0
    length_errors: int = 0
    frame_overflow_packets: int = 0

    # Layer-1 shared capture queue, attributed by the cheap offset-18 cam_id
    # peek.  Kept separate from the per-camera lane queue below: they are two
    # different queues in series and merging their peaks hides which one
    # actually overflowed.
    capture_queue_drops: int = 0
    capture_queue_peak: int = 0

    lane_queue_drops: int = 0
    lane_queue_peak: int = 0

    sequence_gaps: int = 0
    duplicate_packets: int = 0
    out_of_order_packets: int = 0

    first_row_packets: int = 0
    last_row_packets: int = 0

    last_sequence: Optional[int] = None


@dataclass
class GlobalStatistics:
    total_ethernet_frames: int = 0
    matching_frames: int = 0
    valid_packets: int = 0

    bad_ethernet_length: int = 0
    bad_fixed_payload: int = 0
    bad_crc: int = 0
    parser_errors: int = 0
    processing_errors: int = 0

    dropped_capture_queue: int = 0
    capture_queue_capacity: int = 0
    capture_queue_peak: int = 0
    ethernet_validation_failures: int = 0

    # Split-by-camera routing.  A packet whose peeked cam_id is outside the
    # configured lane set is counted here instead of creating a new lane, so a
    # stuck-bit/bad-sync storm cannot spawn threads and directories.
    lane_queue_drops: int = 0
    unroutable_camera_packets: int = 0

    total_payload_bytes: int = 0

    start_time: float = field(default_factory=time.monotonic)
    last_report_time: float = field(default_factory=time.monotonic)
    last_report_frames: int = 0
    last_report_bytes: int = 0

    cameras: dict[int, CameraStatistics] = field(default_factory=dict)

    def camera(self, camera_id: int) -> CameraStatistics:
        if camera_id not in self.cameras:
            self.cameras[camera_id] = CameraStatistics()
        return self.cameras[camera_id]


class StreamMonitor:
    def __init__(self, report_interval: float = 1.0, sink: Callable[[str], None] = print):
        self.stats = GlobalStatistics()
        self.report_interval = report_interval
        self.sink = sink
        self._lock = threading.Lock()

    # ---- ingestion from Layers 1-3 ----------------------------------

    def record_ethernet_frame(self) -> None:
        with self._lock:
            self.stats.total_ethernet_frames += 1

    def record_dropped_capture(self, camera_id: int | None = None) -> None:
        with self._lock:
            self.stats.dropped_capture_queue += 1
            if camera_id is not None:
                self.stats.camera(camera_id).capture_queue_drops += 1

    def configure_capture_queue(self, capacity: int) -> None:
        with self._lock:
            self.stats.capture_queue_capacity = capacity

    def record_capture_queue_depth(self, depth: int, camera_id: int | None = None) -> None:
        with self._lock:
            self.stats.capture_queue_peak = max(
                self.stats.capture_queue_peak,
                depth,
            )
            if camera_id is not None:
                camera = self.stats.camera(camera_id)
                camera.capture_queue_peak = max(camera.capture_queue_peak, depth)

    # ---- split-by-camera lane queues ---------------------------------
    # These mirror the capture-queue pair above but describe the second,
    # per-camera queue.  Two series queues need two independent peaks: a full
    # lane queue and a full capture queue call for completely different fixes.

    def record_lane_queue_depth(self, depth: int, camera_id: int) -> None:
        with self._lock:
            camera = self.stats.camera(camera_id)
            camera.lane_queue_peak = max(camera.lane_queue_peak, depth)

    def record_lane_drop(self, camera_id: int) -> None:
        with self._lock:
            self.stats.lane_queue_drops += 1
            self.stats.camera(camera_id).lane_queue_drops += 1

    def record_unroutable_camera_packet(self) -> None:
        with self._lock:
            self.stats.unroutable_camera_packets += 1

    def record_processing_error(self) -> None:
        with self._lock:
            self.stats.processing_errors += 1

    def record_validation_failure(self, reason: str) -> None:
        with self._lock:
            self.stats.ethernet_validation_failures += 1
            if reason in ("payload_too_short", "payload_too_long"):
                self.stats.bad_ethernet_length += 1

    def record_matching_frame(self, payload_len: int) -> None:
        with self._lock:
            self.stats.matching_frames += 1
            self.stats.total_payload_bytes += payload_len

    def record_fixed_result(self, result: FixedModeResult) -> None:
        with self._lock:
            if not result.ok:
                if result.reason == "bad_length":
                    self.stats.bad_ethernet_length += 1
                else:
                    self.stats.bad_fixed_payload += 1
                return
            self.stats.valid_packets += 1

    def record_camera_result(self, result: CameraModeResult) -> None:
        with self._lock:
            if result.reason == "bad_length":
                self.stats.bad_ethernet_length += 1
                return

            packet = result.packet
            assert packet is not None
            camera = self.stats.camera(packet.header.cam_id)
            camera.packets += 1

            if packet.first_row:
                camera.first_row_packets += 1
            if packet.last_row:
                camera.last_row_packets += 1
            if packet.frame_overflow:
                camera.frame_overflow_packets += 1
            if packet.length_error:
                camera.length_errors += 1

            if not result.ok:
                if "crc_error" in result.errors:
                    camera.crc_errors += 1
                    self.stats.bad_crc += 1
                return

            self.stats.valid_packets += 1
            self._update_sequence(camera, packet.header.row_seq)

    @staticmethod
    def _update_sequence(camera: CameraStatistics, sequence: int) -> None:
        if camera.last_sequence is None:
            camera.last_sequence = sequence
            return

        expected = (camera.last_sequence + 1) & 0xFFFF

        if sequence == camera.last_sequence:
            camera.duplicate_packets += 1
        elif sequence == expected:
            camera.last_sequence = sequence
        else:
            forward_distance = (sequence - expected) & 0xFFFF
            if forward_distance < 0x8000:
                camera.sequence_gaps += forward_distance
                camera.last_sequence = sequence
            else:
                camera.out_of_order_packets += 1

    # ---- reporting ----------------------------------------------------

    def maybe_report(self) -> None:
        # Snapshot under the lock, then emit outside it.  Every hot-path
        # counter takes the same lock, so holding it across a console write --
        # milliseconds on a Windows terminal -- would stall the capture thread
        # and both camera lanes once per report interval.
        with self._lock:
            now = time.monotonic()
            if now - self.stats.last_report_time < self.report_interval:
                return

            interval = now - self.stats.last_report_time
            frame_delta = self.stats.matching_frames - self.stats.last_report_frames
            byte_delta = self.stats.total_payload_bytes - self.stats.last_report_bytes

            packets_per_second = frame_delta / interval
            payload_mbps = byte_delta * 8 / interval / 1_000_000

            lines = [
                f"[PACKET RATE] packets={self.stats.matching_frames} "
                f"valid={self.stats.valid_packets} "
                f"packets_per_second={packets_per_second:.2f} "
                f"payload={payload_mbps:.3f} Mb/s "
                f"crc_errors={self.stats.bad_crc} "
                f"queue_drops={self.stats.dropped_capture_queue}"
            ]
            for camera_id in sorted(self.stats.cameras):
                camera = self.stats.cameras[camera_id]
                lines.append(
                    f"       CAM{camera_id}: packets={camera.packets} "
                    f"crc={camera.crc_errors} len={camera.length_errors} "
                    f"overflow={camera.frame_overflow_packets} "
                    f"gaps={camera.sequence_gaps} dup={camera.duplicate_packets} "
                    f"ooo={camera.out_of_order_packets} "
                    f"cap_drop={camera.capture_queue_drops} "
                    f"lane_drop={camera.lane_queue_drops}"
                )

            self.stats.last_report_time = now
            self.stats.last_report_frames = self.stats.matching_frames
            self.stats.last_report_bytes = self.stats.total_payload_bytes

        for line in lines:
            self.sink(line)

    def final_report(self) -> None:
        elapsed = time.monotonic() - self.stats.start_time
        average_packets_per_second = (
            self.stats.matching_frames / elapsed if elapsed > 0 else 0.0
        )
        producer_rate = (
            self.stats.total_ethernet_frames / elapsed
            if elapsed > 0
            else 0.0
        )
        queue_drop_percent = (
            self.stats.dropped_capture_queue
            * 100.0
            / self.stats.total_ethernet_frames
            if self.stats.total_ethernet_frames
            else 0.0
        )
        average_mbps = (
            self.stats.total_payload_bytes * 8 / elapsed / 1_000_000 if elapsed > 0 else 0.0
        )

        self.sink("\n============== FINAL REPORT ==============")
        self.sink(f"Elapsed               : {elapsed:.3f} s")
        self.sink(f"Capture ingress       : {self.stats.total_ethernet_frames}")
        self.sink(f"Matching Ethernet     : {self.stats.matching_frames}")
        self.sink(f"Valid packets         : {self.stats.valid_packets}")
        self.sink(f"Bad Ethernet length   : {self.stats.bad_ethernet_length}")
        self.sink(f"Bad fixed payload     : {self.stats.bad_fixed_payload}")
        self.sink(f"CRC errors            : {self.stats.bad_crc}")
        self.sink(f"Parser errors         : {self.stats.parser_errors}")
        self.sink(f"Processing errors     : {self.stats.processing_errors}")
        self.sink(f"Capture queue drops   : {self.stats.dropped_capture_queue}")
        self.sink(
            f"Capture queue peak    : "
            f"{self.stats.capture_queue_peak}/"
            f"{self.stats.capture_queue_capacity}"
        )
        self.sink(f"Capture drop percent  : {queue_drop_percent:.4f}%")
        if self.stats.lane_queue_drops or self.stats.unroutable_camera_packets:
            self.sink(f"Lane queue drops      : {self.stats.lane_queue_drops}")
            self.sink(
                f"Unroutable cam_id     : "
                f"{self.stats.unroutable_camera_packets}"
            )
        self.sink(f"Producer rate         : {producer_rate:.2f} packets/s")
        self.sink(
            f"Consumer rate         : "
            f"{average_packets_per_second:.2f} packets/s"
        )
        self.sink(
            f"Average packet rate   : "
            f"{average_packets_per_second:.2f} packets/s"
        )
        self.sink(f"Average payload rate  : {average_mbps:.3f} Mb/s")

        for camera_id in sorted(self.stats.cameras):
            camera = self.stats.cameras[camera_id]
            self.sink(f"\nCAMERA {camera_id}")
            self.sink(f"  packets             : {camera.packets}")
            self.sink(f"  CRC errors          : {camera.crc_errors}")
            self.sink(f"  length errors       : {camera.length_errors}")
            self.sink(f"  overflow-marked     : {camera.frame_overflow_packets}")
            self.sink(f"  capture drops       : {camera.capture_queue_drops}")
            self.sink(f"  capture peak        : {camera.capture_queue_peak}")
            self.sink(f"  lane drops          : {camera.lane_queue_drops}")
            self.sink(f"  lane peak           : {camera.lane_queue_peak}")
            self.sink(f"  first-row packets   : {camera.first_row_packets}")
            self.sink(f"  last-row packets    : {camera.last_row_packets}")
            self.sink(f"  sequence gaps       : {camera.sequence_gaps}")
            self.sink(f"  duplicates          : {camera.duplicate_packets}")
            self.sink(f"  out-of-order        : {camera.out_of_order_packets}")
