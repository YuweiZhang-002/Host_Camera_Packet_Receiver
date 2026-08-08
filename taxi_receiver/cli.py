from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path

from .async_sink import AsyncCallbackDispatcher
from .capture import ScapyLiveCapture, list_interfaces
from .camera_lane import DEFAULT_CAMERA_IDS, CameraLanePool, PublishPolicy
from .eth_validate import ETHER_TYPE
from .image_pipeline import CameraImagePipeline, ImagePolicy
from .pcap_stdlib import StdlibPcapReplayFrameSource
from .pipeline import TaxiReceiverPipeline
from .reassembler import FrameReassembler, NullReassembler
from .recorder import ErrorFrameRecorder, PcapRecorder
from .session_audit import SessionAuditLogger
from .stages import STAGE_ORDER
from .storage import (
    StaleArchiveRootError,
    StorageAndPipeline,
    resolve_archive_root,
)


def print_interfaces() -> None:
    interfaces = list_interfaces()
    print("Available capture interfaces:")
    for index, interface in enumerate(interfaces):
        print(f"  [{index:02d}] {interface}")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture and validate FPGA TAXI Ethernet frames."
    )
    parser.add_argument("--interface", "-i", help="Npcap/libpcap capture interface.")
    parser.add_argument(
        "--replay-pcap",
        type=Path,
        help="Replay classic PCAP/PCAPNG with the standard-library reader.",
    )
    parser.add_argument(
        "--source-mac",
        default="02:00:00:00:00:02",
        help="Optional source-MAC filter for offline replay.",
    )
    parser.add_argument(
        "--mode", choices=("fixed", "camera"), default="camera",
        help="fixed: expect payload 00..7F; camera: parse camera row packets.",
    )
    parser.add_argument("--list", action="store_true", help="List capture interfaces.")
    parser.add_argument("--pcap", type=Path, help="Save matching Ethernet frames to a PCAP file.")
    parser.add_argument("--error-directory", type=Path, help="Save malformed payloads as binary files.")
    parser.add_argument("--queue-depth", type=int, default=8192)
    parser.add_argument(
        "--pcap-buffer-size",
        type=int,
        default=8 * 1024 * 1024,
        help=(
            "Npcap/libpcap kernel buffer size in bytes for live capture. "
            "This is applied via pcap_set_buffer_size() before activation."
        ),
    )
    parser.add_argument(
        "--frame-output-queue-depth",
        type=int,
        default=256,
        help=(
            "Bounded queue between reassembly and slow RAW/PGM/JSON/archive "
            "publication."
        ),
    )
    parser.add_argument("--report-interval", type=float, default=1.0)
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Atomically archive Layer-5 frame directories and summary.csv.",
    )
    parser.add_argument(
        "--images-root",
        type=Path,
        help=(
            "Publish complete threshold images as "
            "camN/<frame_id>.pgm and append camN/rows.csv."
        ),
    )
    parser.add_argument(
        "--no-rows-csv",
        action="store_true",
        help=(
            "Publish images without recording camN/rows.csv. Use this as the "
            "'A' half of an A/B replay when deciding whether the per-packet "
            "recorder is costing capture throughput."
        ),
    )
    parser.add_argument(
        "--csv-queue-depth",
        type=int,
        default=65536,
        help=(
            "Bounded queue between the packet consumer and the rows.csv "
            "writer thread."
        ),
    )
    parser.add_argument(
        "--csv-backpressure",
        choices=("auto", "drop", "block"),
        default="auto",
        help=(
            "auto (default): block on offline replay so no audit row is lost, "
            "drop on live capture so telemetry never stalls the image path. "
            "Dropped rows are always counted as csv_rows_dropped."
        ),
    )
    parser.add_argument(
        "--bit-order",
        choices=("msb_first", "lsb_first"),
        default="msb_first",
        help="Pixel-bit order inside each 80-byte packed Camera row.",
    )
    parser.add_argument(
        "--expected-rows",
        type=int,
        default=480,
        help="Expected packet rows per frame (current FPGA default: 480).",
    )
    parser.add_argument(
        "--image-policy",
        choices=tuple(policy.value for policy in ImagePolicy),
        default=ImagePolicy.STRICT.value,
        help=(
            "strict publishes only COMPLETE images; recover-zero-fill may "
            "publish narrowly eligible frames with missing rows filled dark."
        ),
    )
    parser.add_argument(
        "--max-missing-rows",
        type=int,
        default=4,
        help="Maximum total missing rows accepted by recover-zero-fill.",
    )
    parser.add_argument(
        "--max-consecutive-missing",
        type=int,
        default=2,
        help=(
            "Exclusive missing-run rejection threshold. The default 2 "
            "rejects two or more consecutive missing rows."
        ),
    )
    parser.add_argument(
        "--split-by-camera",
        choices=("auto", "on", "off"),
        default="auto",
        help=(
            "Run one reassembly/publication lane per camera (S1). auto "
            "(default): on for camera mode with an archive or images root. "
            "off restores the single shared worker, which is what an A/B "
            "comparison needs."
        ),
    )
    parser.add_argument(
        "--camera-ids",
        default=",".join(str(cam) for cam in DEFAULT_CAMERA_IDS),
        help=(
            "Comma-separated cam_id allowlist for lane routing. cam_id is "
            "read from an untrusted wire byte, so any value outside this set "
            "is counted as unroutable instead of creating a lane."
        ),
    )
    parser.add_argument(
        "--publish-frames",
        choices=tuple(policy.value for policy in PublishPolicy),
        default=PublishPolicy.COMPLETE.value,
        help=(
            "Which completed frames reach storage/image publication. "
            "complete (default) skips partial frames, which otherwise cost a "
            "full serialisation and recovery assessment per frame. Use "
            "eligible with --image-policy recover-zero-fill."
        ),
    )
    parser.add_argument(
        "--publish-images",
        choices=("process", "thread"),
        default="process",
        help=(
            "S2. process (default) runs image publication in a dedicated "
            "process per camera, so recovery unpacking, PGM encoding and RAW "
            "writing leave the GIL. thread keeps it on the lane's output "
            "thread, which is the configuration to A/B against."
        ),
    )
    parser.add_argument(
        "--publisher-queue-depth",
        type=int,
        default=256,
        help="Bounded frame queue between each lane and its publisher process.",
    )
    parser.add_argument(
        "--session-audit",
        choices=("auto", "on", "off"),
        default="auto",
        help=(
            "Per-packet session_audit.csv. It is written synchronously on the "
            "consumer thread and is nearly a subset of rows.csv, so auto "
            "means off for live capture and on for offline replay."
        ),
    )
    parser.add_argument(
        "--archive-root-policy",
        choices=("run-subdir", "require-empty", "reuse"),
        default="run-subdir",
        help=(
            "How --output-root is resolved. run-subdir (default) archives "
            "into <root>/run_<timestamp> so a rerun cannot collide with an "
            "earlier run; frame_id restarts at every board power-on. "
            "require-empty fails fast instead. reuse is the old behaviour."
        ),
    )
    parser.add_argument(
        "--archive-collision-policy",
        choices=("suffix", "error"),
        default="suffix",
        help=(
            "What an existing, differing frame directory does: suffix "
            "(default) publishes frame_<id>.dup<k>; error raises inside the "
            "output worker, which can disable the whole sink."
        ),
    )
    parser.add_argument("--frame-timeout", type=float, default=2.0)
    parser.add_argument(
        "--reassemble", action="store_true",
        help="Shorthand for --max-stage reassemble (enables the Layer-5 FrameReassembler).",
    )
    parser.add_argument(
        "--max-stage", choices=STAGE_ORDER, default="monitor",
        help=(
            "How far up the chain to run: validate=Layer1-2, parse=Layer1-3, "
            "monitor=Layer1-4 (default, full stats/rate reporting), "
            "reassemble=Layer1-5. Useful for bringing a new link up one "
            "layer at a time."
        ),
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()

    if args.list:
        try:
            print_interfaces()
            return 0
        except ModuleNotFoundError:
            print(
                "Scapy is not installed in this Python environment. "
                "Install the dependencies in requirements-live.txt, then "
                "rerun --list. Offline PCAP replay and pytest do not require "
                "Scapy.",
                file=sys.stderr,
            )
            return 5

    if args.interface and args.replay_pcap:
        print("Error: choose --interface or --replay-pcap, not both.")
        return 2
    if not args.interface and not args.replay_pcap:
        print("Error: --interface or --replay-pcap is required.\n")
        try:
            print_interfaces()
        except ModuleNotFoundError:
            print("Scapy is not installed; offline --replay-pcap still works.")
        return 2
    if (
        args.image_policy == ImagePolicy.RECOVER_ZERO_FILL.value
        and args.expected_rows != 480
    ):
        print(
            "Error: recover-zero-fill requires --expected-rows 480.",
            file=sys.stderr,
        )
        return 2
    if (
        args.image_policy == ImagePolicy.RECOVER_ZERO_FILL.value
        and args.images_root is None
    ):
        print(
            "Error: recover-zero-fill requires --images-root.",
            file=sys.stderr,
        )
        return 2

    pcap_recorder = PcapRecorder(args.pcap) if args.pcap else None
    error_recorder = ErrorFrameRecorder(args.error_directory) if args.error_directory else None

    # --reassemble is just sugar for --max-stage reassemble.
    max_stage = "reassemble" if args.reassemble else args.max_stage
    if args.output_root is not None or args.images_root is not None:
        max_stage = "reassemble"
    reassembler = (
        FrameReassembler(
            expected_rows=args.expected_rows,
            timeout_seconds=args.frame_timeout,
        )
        if max_stage == "reassemble"
        else NullReassembler()
    )
    # frame_id restarts near zero at every board power-on, so an archive root
    # is resolved once, before capture, instead of discovering the collision
    # per frame inside the bounded output worker.
    archive_root = None
    if args.output_root is not None:
        try:
            archive_root = resolve_archive_root(
                args.output_root,
                policy=args.archive_root_policy,
                run_id=time.strftime("%Y%m%d_%H%M%S"),
            )
        except StaleArchiveRootError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 6
    storage = (
        StorageAndPipeline(
            archive_root,
            collision_policy=args.archive_collision_policy,
        )
        if archive_root is not None
        else None
    )
    # session_audit.csv is a per-packet CSV written synchronously on the
    # consumer thread, and rows.csv already carries nearly the same columns
    # from a dedicated writer thread.  Live capture therefore defaults to off.
    session_audit_enabled = (
        args.session_audit == "on"
        or (args.session_audit == "auto" and args.replay_pcap is not None)
    )
    session_audit = (
        SessionAuditLogger(archive_root)
        if archive_root is not None and session_audit_enabled
        else None
    )
    # Offline replay already applies lossless backpressure to the capture
    # queue; making the CSV queue match keeps a replay a complete audit.  Live
    # capture keeps the drop policy so a slow disk can never become the reason
    # packets stop being consumed.
    csv_backpressure = (
        args.csv_backpressure
        if args.csv_backpressure != "auto"
        else ("block" if args.replay_pcap is not None else "drop")
    )
    image_pipeline = None
    frame_output = None
    try:
        camera_ids = tuple(
            int(token) for token in args.camera_ids.split(",") if token.strip()
        )
    except ValueError:
        print(
            f"Error: --camera-ids must be integers: {args.camera_ids!r}",
            file=sys.stderr,
        )
        return 2
    if not camera_ids:
        print("Error: --camera-ids must not be empty.", file=sys.stderr)
        return 2
    has_output_sink = args.output_root is not None or args.images_root is not None
    if args.split_by_camera == "on":
        if args.mode != "camera":
            print(
                "Error: --split-by-camera on requires --mode camera.",
                file=sys.stderr,
            )
            return 2
        split_by_camera = True
    elif args.split_by_camera == "off":
        split_by_camera = False
    else:
        split_by_camera = args.mode == "camera" and has_output_sink
    frame_source = (
        StdlibPcapReplayFrameSource(
            args.replay_pcap,
            ether_type=ETHER_TYPE,
            source_mac=args.source_mac,
        )
        if args.replay_pcap is not None
        else ScapyLiveCapture(
            args.interface,
            ether_type=ETHER_TYPE,
            include_raw=pcap_recorder is not None,
            pcap_buffer_size=args.pcap_buffer_size,
        )
    )

    pipeline = TaxiReceiverPipeline(
        frame_source=frame_source,
        mode=args.mode,
        max_stage=max_stage,
        reassembler=reassembler,
        pcap_recorder=pcap_recorder,
        error_recorder=error_recorder,
        queue_depth=args.queue_depth,
        lossless_input=args.replay_pcap is not None,
        split_by_camera=split_by_camera,
        report_interval=args.report_interval,
        on_completed_frame=None,
        on_frame_processed=None,
    )

    lane_queue_depth = max(1, args.queue_depth // 2)
    camera_lane_pool = (
        CameraLanePool(
            monitor=pipeline.monitor,
            output_root=archive_root,
            images_root=args.images_root,
            enable_row_csv=not args.no_rows_csv,
            expected_rows=args.expected_rows,
            bit_order=args.bit_order,
            image_policy=args.image_policy,
            max_missing_rows=args.max_missing_rows,
            max_consecutive_missing=args.max_consecutive_missing,
            report_interval=args.report_interval,
            csv_queue_depth=args.csv_queue_depth,
            csv_backpressure=csv_backpressure,
            frame_output_queue_depth=args.frame_output_queue_depth,
            lane_queue_depth=lane_queue_depth,
            session_audit=session_audit,
            storage=storage,
            camera_ids=camera_ids,
            # A lane queue that drops would void the "offline replay is a
            # complete audit" guarantee the capture queue already honours.
            lossless=args.replay_pcap is not None,
            publish_policy=args.publish_frames,
            publish_async=args.publish_images == "process",
            publisher_queue_depth=args.publisher_queue_depth,
            report_sink=print,
            error_sink=lambda message: print(message, file=sys.stderr),
        )
        if split_by_camera
        else None
    )
    if camera_lane_pool is not None:
        pipeline.on_frame_processed = camera_lane_pool.submit
    else:
        image_pipeline = (
            CameraImagePipeline(
                args.images_root,
                expected_rows=args.expected_rows,
                bit_order=args.bit_order,
                image_policy=args.image_policy,
                max_missing_rows=args.max_missing_rows,
                max_consecutive_missing=args.max_consecutive_missing,
                report_interval=args.report_interval,
                enable_row_csv=not args.no_rows_csv,
                csv_queue_depth=args.csv_queue_depth,
                csv_backpressure=csv_backpressure,
                publish_async=args.publish_images == "process",
                publisher_queue_depth=args.publisher_queue_depth,
                error_sink=lambda message: print(message, file=sys.stderr),
            )
            if args.images_root is not None
            else None
        )
        completed_frame_callback = _fanout_callbacks(
            ("storage", storage),
            (
                "image publication",
                image_pipeline.archive_frame if image_pipeline is not None else None,
            ),
        )
        frame_output = (
            AsyncCallbackDispatcher(
                completed_frame_callback,
                queue_depth=args.frame_output_queue_depth,
                error_sink=lambda message: print(message, file=sys.stderr),
            )
            if completed_frame_callback is not None
            else None
        )
        # The publish filter is a property of the sink boundary, not of the
        # lane split, so the shared path applies exactly the same policy.
        publish_policy = PublishPolicy(args.publish_frames)
        shared_publish_skipped = 0

        def _publish_completed_frame(frame) -> None:
            nonlocal shared_publish_skipped
            if not publish_policy.accepts(frame):
                shared_publish_skipped += 1
                return
            assert frame_output is not None
            frame_output.submit(frame)

        pipeline.on_completed_frame = (
            _publish_completed_frame if frame_output is not None else None
        )
        pipeline.on_frame_processed = _fanout_callbacks(
            ("session audit", session_audit),
            image_pipeline.record_packet if image_pipeline is not None else None,
        )

    stop_requested = threading.Event()

    def handle_stop(signum, frame) -> None:
        stop_requested.set()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    print(
        f"Source    : "
        f"{args.replay_pcap if args.replay_pcap else args.interface}"
    )
    print(f"Mode      : {args.mode}")
    print(f"Max stage : {max_stage} (Layer 1-{STAGE_ORDER.index(max_stage) + 2})")
    print(f"EtherType : 0x{ETHER_TYPE:04X}")
    if args.replay_pcap is None:
        print(f"Pcap buf  : {args.pcap_buffer_size} bytes")
    print(f"Working dir: {Path.cwd().resolve()}")
    if archive_root is not None:
        print(f"Archive   : {archive_root.resolve()}")
        print(
            f"Archive policy: root={args.archive_root_policy} "
            f"collision={args.archive_collision_policy}"
        )
    if args.images_root is not None:
        print(f"Images    : {args.images_root.resolve()}")
        print(f"Image policy: {args.image_policy}")
    print(
        f"Publish   : {args.publish_frames} via {args.publish_images}"
        f" (publisher queue={args.publisher_queue_depth})"
    )
    print(
        f"Session audit: {'on' if session_audit is not None else 'off'}"
        f"{'' if args.output_root is not None else ' (needs --output-root)'}"
    )
    if split_by_camera:
        print(
            f"Split cam : on (cam_ids={','.join(str(c) for c in camera_ids)}, "
            f"lane queue={lane_queue_depth}, "
            f"frame-output={args.frame_output_queue_depth})"
        )
        print(
            f"Row CSV    : {'off' if args.no_rows_csv else 'on'} per cam "
            f"(queue={args.csv_queue_depth}, backpressure={csv_backpressure})"
        )
    else:
        print("Split cam : off (single shared worker)")
        print(
            f"Frame output queue: {args.frame_output_queue_depth}"
        )
        print(
            f"Row CSV    : "
            f"{'off' if args.no_rows_csv else 'on'} "
            f"(queue={args.csv_queue_depth}, "
            f"backpressure={csv_backpressure})"
        )
    print("Press Ctrl+C to stop.\n")

    try:
        pipeline.start()
        if args.replay_pcap is None:
            while not stop_requested.wait(0.25):
                pass
    except ModuleNotFoundError:
        print(
            "Scapy is not installed. Install Scapy for live capture or use "
            "--replay-pcap for the dependency-free offline path.",
            file=sys.stderr,
        )
        return 5
    except PermissionError:
        print("Capture permission denied. Run the terminal as Administrator/root.", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"Capture error: {exc}", file=sys.stderr)
        return 4
    finally:
        try:
            pipeline.stop()
            if camera_lane_pool is not None:
                camera_lane_pool.close()
            if frame_output is not None:
                frame_output.close()
            pipeline.print_final_report()
            if storage is not None:
                print("")
                for line in storage.report_lines():
                    print(line)
            if camera_lane_pool is not None:
                print("")
                for line in camera_lane_pool.report_lines():
                    print(line)
            elif image_pipeline is not None:
                # Drain the rows.csv writer before the report so its queue/drop
                # counters describe the whole run rather than the moment before
                # shutdown.  close() is idempotent; the safety net below re-runs it.
                image_pipeline.close()
                print("")
                if frame_output is not None:
                    for line in frame_output.report_lines():
                        print(line)
                    print("")
                for line in image_pipeline.report_lines():
                    print(line)
        finally:
            if camera_lane_pool is not None:
                camera_lane_pool.close()
            if frame_output is not None:
                frame_output.close()
            if session_audit is not None:
                session_audit.close()
            if image_pipeline is not None:
                image_pipeline.close()
            if storage is not None:
                storage.close()

    # A run whose output sink failed every single frame used to exit 0 and look
    # like a success.  Report it, so a wrapper script or CI can see it.
    # A run whose sink failed on every frame used to exit 0 and look like a
    # success.  Each sink is judged on its own, since one broken sink no longer
    # implies the others failed.
    named_dispatchers: list[tuple[str, AsyncCallbackDispatcher]] = []
    if camera_lane_pool is not None:
        for lane in camera_lane_pool.lanes:
            for name, dispatcher in lane.frame_outputs.items():
                named_dispatchers.append((f"cam{lane.camera_id}/{name}", dispatcher))
    elif frame_output is not None:
        named_dispatchers.append(("shared", frame_output))

    broken = [
        name
        for name, dispatcher in named_dispatchers
        if dispatcher.stats.disabled
        or (dispatcher.stats.submitted > 0 and dispatcher.stats.processed == 0)
    ]
    if broken:
        print(
            "Error: output sink(s) published nothing or were disabled after "
            f"repeated failures: {', '.join(broken)}.",
            file=sys.stderr,
        )
        return 7

    return 0

class _NamedCallbackError(RuntimeError):
    def __init__(self, failures):
        self.failures = tuple(failures)

    def __str__(self) -> str:
        return "; ".join(
            f"{name} failed: {exc}" for name, exc in self.failures
        )


def _fanout_callbacks(*callbacks):
    """Run every configured observation/storage callback.

    A failure in one sink must not prevent the other evidence sink from
    receiving the same frame.  The first exception is re-raised only after all
    callbacks have been attempted, preserving the pipeline's existing error
    accounting.
    """

    active = tuple(
        normalized
        for callback in callbacks
        if (normalized := _normalize_fanout_callback(callback)) is not None
    )
    if not active:
        return None

    def invoke(value) -> None:
        failures = []
        for name, callback in active:
            try:
                callback(value)
            except Exception as exc:  # noqa: BLE001 - preserve peer callbacks
                failures.append((name, exc))
        if failures:
            raise _NamedCallbackError(failures)

    return invoke


def _normalize_fanout_callback(callback):
    if isinstance(callback, tuple):
        name, actual_callback = callback
    else:
        actual_callback = callback
        name = getattr(callback, "__name__", callback.__class__.__name__)

    if actual_callback is None:
        return None

    return name, actual_callback


if __name__ == "__main__":
    raise SystemExit(main())
