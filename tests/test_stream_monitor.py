from taxi_receiver.camera_parser import parse_camera_mode
from taxi_receiver.stream_monitor import StreamMonitor

from .synthetic import make_camera_frame


def test_sequence_gap_dup_ooo():
    monitor = StreamMonitor(report_interval=999, sink=lambda *_: None)

    # 0,1 normal; 1 repeated (dup); 5 skips ahead by 3 (gap); 4 arrives
    # behind the new baseline (ooo).
    seqs = [0, 1, 1, 5, 4]
    for i, seq in enumerate(seqs):
        frame = make_camera_frame(cam_id=0, frame_id=1, row_idx=i, row_seq=seq)
        result = parse_camera_mode(frame.payload)
        monitor.record_camera_result(result)

    cam = monitor.stats.camera(0)
    assert cam.packets == 5
    assert cam.duplicate_packets == 1
    assert cam.sequence_gaps == 3
    assert cam.out_of_order_packets == 1


def test_crc_error_counted_per_camera_and_globally():
    monitor = StreamMonitor(report_interval=999, sink=lambda *_: None)
    frame = make_camera_frame(cam_id=2, frame_id=1, row_idx=0, row_seq=0, corrupt_crc=True)
    result = parse_camera_mode(frame.payload)
    monitor.record_camera_result(result)

    assert monitor.stats.bad_crc == 1
    assert monitor.stats.camera(2).crc_errors == 1
    assert monitor.stats.valid_packets == 0
