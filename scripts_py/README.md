# Python command-line tools

The Python files in this directory are thin, human-facing command-line entry
points.  Algorithms and stateful receiver components remain under
`taxi_receiver/` so imports, unit tests and multiprocessing targets have one
stable package owner.

| Category | Entry point | Implementation owner |
|---|---|---|
| Analysis | `analysis/analyze_camera_archive.py` | Standalone archive/CSV audit |
| Analysis | `analysis/analyze_rows_csv.py` | `taxi_receiver.packet_format` plus streaming CSV audit |
| Intrinsic | `calibration/preflight_calibration_frames.py` | `taxi_receiver.binary_calibration` detector and pose gate |
| Intrinsic | `calibration/calibrate_binary_camera.py` | `taxi_receiver.binary_calibration` |
| Intrinsic | `calibration/calibrate_binary_camera_refill.py` | `taxi_receiver.calibration_refill` |
| Intrinsic | `calibration/validate_binary_calibration.py` | `taxi_receiver.calibration_validation` |
| Extrinsic | `calibration/build_stereo_pairs.py` | `taxi_receiver.stereo_pairs` |
| Extrinsic | `calibration/calibrate_binary_stereo.py` | `taxi_receiver.stereo_calibration` |
| Extrinsic | `calibration/validate_binary_extrinsics.py` | `taxi_receiver.extrinsic_validation` |

Each entry point inserts the cloned repository root into `sys.path`, so a file
may be invoked by absolute path without first changing directory:

```powershell
$python = 'D:\prg\blank_project\Host_Camera_Packet_Receiver\.venv\Scripts\python.exe'
$cli = 'D:\prg\blank_project\Host_Camera_Packet_Receiver\scripts_py\calibration\preflight_calibration_frames.py'
& $python $cli '<capture-root>\cam0\*.pgm' --zone-map --min-poses 15
```
