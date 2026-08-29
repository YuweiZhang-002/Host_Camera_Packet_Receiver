# Binary Intrinsic and Stereo Calibration Runbook

[Chinese version](calibration_pipeline.zh-CN.md)

The receiver and calibration stages have separate evidence boundaries. Live
capture converts EtherType `0x88B5` row packets into PGM files and sidecar
metadata. Calibration consumes those files offline; it does not run on the
capture hot path. A receiver PASS therefore does not imply an intrinsic PASS,
and an intrinsic PASS does not imply a publishable stereo transform.

## Environment

```powershell
$repo = 'D:\prg\prg_cam_host' # replace on another host
$python = Join-Path $repo '.venv\Scripts\python.exe'

Set-Location $repo
& $python -m pip install `
  -r .\requirements-live.txt `
  -r .\requirements-calibration.txt

& $python -c `
  "import cv2,numpy; print('OpenCV',cv2.__version__,'NumPy',numpy.__version__)"
```

The calibration requirements intentionally constrain OpenCV to `>=4.8,<5`.
The release audit used Python 3.14.6, NumPy 2.5.1 and OpenCV 4.14.0.

## Capture contract

Use a fresh directory for every Training, Holdout V1 and Holdout V2 run. A
stereo dataset must contain `cam0/` and `cam1/`; PGM files must retain their
sidecar frame/timestamp metadata. Do not append a holdout to a training run.

```powershell
$repo = 'D:\prg\prg_cam_host'
$python = Join-Path $repo '.venv\Scripts\python.exe'
$captureRoot = 'D:\camera_runs\20260829_stereo_train' # fresh path

Set-Location $repo
.\run_receiver.ps1 `
  -Interface '\Device\NPF_{REPLACE-WITH-REAL-GUID}' `
  -ImagesRoot $captureRoot `
  -ExpectedRows 480 `
  -QueueDepth 65536 `
  -FrameOutputQueueDepth 256 `
  -CameraIds '0,1' `
  -SplitByCamera on `
  -ImagePolicy strict `
  -PublishFrames complete `
  -PublishImages process `
  -PublisherQueueDepth 256 `
  -SessionAudit off `
  -PythonExe $python
```

Only parameters implemented by this repository's `run_receiver.ps1` are used
above. FPGA-workspace flags such as `-CrcMode`, `-BitOrder` and
`-CsvQueueDepth` are not parameters of this launcher.

## Intrinsic workflow

Prepare three independent camera directories: Training, Holdout V1 and
Holdout V2. Then run the write-free check:

```powershell
& .\scripts_ps\run_intrinsic_calibration.ps1 `
  -CameraId 0 `
  -TrainingRoot 'D:\camera_runs\cam0_train\cam0' `
  -HoldoutV1Root 'D:\camera_runs\cam0_holdout_v1\cam0' `
  -HoldoutV2Root 'D:\camera_runs\cam0_holdout_v2\cam0' `
  -OutputRoot 'D:\camera_runs\audit\cam0_intrinsic_run01' `
  -PythonExe $python `
  -FisheyeConstraint full `
  -MinPoses 15 `
  -MinViews 15 `
  -MinHoldoutViews 15 `
  -PreflightOnly
```

Remove `-PreflightOnly` to execute:

```text
complete-grid/pose preflight
  -> one selected frame per pose
  -> cv2.fisheye.calibrate
  -> intrinsic JSON and per-view CSV
  -> fixed-K/D Holdout V1
  -> fixed-K/D Holdout V2
  -> run_manifest.json
```

The wrapper requires `quality.status=acceptable` and both holdout summaries to
report `status=pass`. Its explicit validation gates are the current code
defaults: median 0.8 px, P95 1.2 px, maximum 1.5 px and pass fraction 0.90.
These are frozen engineering gates, not universal OpenCV constants.

For a new 120-degree/120-degree rig, calibrate both cameras again. Do not reuse
the former 120-degree/160-degree K/D pair. Both intrinsics must use the same
object-point index set; the stereo code rejects a mismatch rather than taking
an implicit intersection.

## Stereo extrinsic workflow

Inputs are four independent dual-camera runs: Static, Training, Holdout V1 and
Holdout V2, plus two independently accepted intrinsic JSON files.

```powershell
& .\scripts_ps\run_extrinsic_calibration.ps1 `
  -StaticRoot 'D:\camera_runs\stereo_static' `
  -TrainingRoot 'D:\camera_runs\stereo_train' `
  -HoldoutV1Root 'D:\camera_runs\stereo_holdout_v1' `
  -HoldoutV2Root 'D:\camera_runs\stereo_holdout_v2' `
  -Cam0Intrinsic 'D:\camera_runs\intrinsics\cam0_intrinsics.json' `
  -Cam1Intrinsic 'D:\camera_runs\intrinsics\cam1_intrinsics.json' `
  -OutputRoot 'D:\camera_runs\audit\stereo_run01' `
  -PythonExe $python `
  -PairingMode quasi_static_episode_minimum `
  -MinPairs 15 `
  -PreflightOnly
```

Remove `-PreflightOnly` only after the paths, image counts, Python/OpenCV and
exact intrinsic point-set check pass. The execution order is:

```text
Static -> frozen stillness thresholds
Training -> episode-minimum timestamp pairs -> fixed-K/D R/t solve
Holdout V1 -> new pairs -> fixed-R/t validation
Holdout V2 -> new pairs -> fixed-R/t validation
```

For a handheld board, capture 15-20 real stops, remain still for 2-3 seconds
at each stop, and move quickly between stops. Episode-minimum mode assigns one
independent sample to each stop instead of giving hundreds of adjacent frames
independent weight.

The wrapper deliberately does not pass `--allow-limited` or
`--allow-limited-extrinsics`. A limited result remains diagnostic evidence and
is not promoted into an acceptable release. A low stereo RMS alone does not
prove physical validity; dispersion, depth drift, rectified vertical error
and both holdouts must also pass.

## Live pose monitor

Run this in a second PowerShell window while the receiver writes cam0 images;
use a third window with `cam1` substituted:

```powershell
$liveRoot = Join-Path $captureRoot '_live_audit'
New-Item -ItemType Directory -Force -Path $liveRoot | Out-Null

& $python .\preflight_calibration_frames.py `
  (Join-Path $captureRoot 'cam0') `
  --watch `
  --poll-interval 1 `
  --min-poses 15 `
  --zone-map `
  --report (Join-Path $liveRoot 'cam0_frames.csv')
```

Single-camera distinct poses are not stereo `selected_pose_pairs`; the latter
exists only after timestamp matching and shared visibility checks.

## Output and failure policy

Both wrappers support `-PreflightOnly` and `-WhatIf`, refuse a non-empty output
root, and write `run_manifest.json`. Preserve a failed output directory and
use a new run ID instead of clearing it.

If Windows reports that script execution is disabled, do not change the
machine-wide policy. Use a process-local override:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts_ps\run_intrinsic_calibration.ps1 `
  -PreflightOnly <the remaining required parameters>
```

Core calibration exit meanings are:

| Exit | Meaning |
|---:|---|
| 0 | Step passed |
| 2 | Invalid input, JSON, OpenCV or data format |
| 3 | Insufficient data or validation gate failure |
| 4 | Limited stereo solve not allowed for publication |

Before publication:

```powershell
Set-Location $repo
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'Regression failed' }
git status --short
git diff --check
```
