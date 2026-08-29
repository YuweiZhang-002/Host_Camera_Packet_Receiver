# DIVERGENCE

This file records every phase-2 change made to the copied host repository so the differences from the source workspace can be audited later.

| File | Change | Why |
|---|---|---|
| `requirements-live.txt` | Added `pytest` next to `scapy`. | The host-only copy must be able to run its verification suite from a fresh `.venv` without extra manual package installation. |
| `tests/test_pcap_stdlib.py` | Replaced `Path(__file__).resolve().parents[6]` with marker-based repository-root discovery and added explicit `skipif` guards for the two external pcapng regressions. | The original tests depend on repository-external capture artifacts; the magic parent depth no longer works in the host-only layout. |
| `run_receiver.ps1` | Removed fixed drive-letter paths, derived defaults from `$PSScriptRoot`, and switched the Python executable default to the local `.venv`. | Make live capture launchable from the new root without the source machine's path layout. |
| `verify_s2.ps1` | Removed fixed drive-letter paths, derived the Python executable default from `$PSScriptRoot`, and kept the script rooted in the host repo. | Keep the A/B verifier portable in the new copy. |
| `replay_pcap.ps1` | Removed fixed drive-letter paths and switched the Python executable default to the local `.venv`. | Make offline replay self-contained in the host repo. |
| `run_camera_viewer.ps1` | Replaced the absolute default archive root with a `$PSScriptRoot`-relative default and switched to the local `.venv`. | The viewer launcher must not depend on `<ReceiverRoot>`. |
| `monitor_camera_output.ps1` | Replaced the hard-coded root with `$PSScriptRoot`-relative defaults. | The folder monitor should work in the copied repository without any drive-specific path. |
| `taxi_receiver/camera_viewer.py` | Replaced the absolute default archive root with a repository-relative default path. | Keep the viewer code host-side and portable. |
| `README.md` | Added a new English top-level README. | Provide the host-side overview, quick start, CLI reference, troubleshooting, status, and local measurement notes. |
| `README.zh-CN.md` | Added a Chinese top-level README with the same content and top link back to the English file. | Satisfy the bilingual documentation requirement. |
| `CHEATSHEET.md` | Added a sanitized quick-reference sheet with relative commands only. | Preserve the host-side operator shortcuts without machine-specific paths. |
| `docs/notes/p11_python_receiver_performance_optimization.md` | Added a local-measurement note with an explicit non-universal disclaimer. | Preserve the performance rationale while marking the numbers as machine-specific. |
| `.gitignore` | Added the requested runtime and environment ignores. | Keep the host repo clean during live capture, replay, and pytest runs. |
| `.gitignore` | Added `.pytest_tmp/` for the in-repo pytest basetemp used by verification. | Keep local test runs from polluting `git status`. |
| `LICENSE` | Added a new MIT license file in the host repository only. | The source workspace was left unchanged, per phase-2 requirements. |
| Repository scan | No vendored third-party source code was copied into the host repo; `scapy` and `pytest` are declared as runtime/test dependencies only. | Satisfy the no-vendored-code check and keep third-party code out of the repository tree. |

## Calibration publication extension (2026-08-29)

| File or group | Change | Why |
|---|---|---|
| `calibrate_binary_camera*.py`, `preflight_calibration_frames.py`, `validate_binary_calibration.py` | Copied the intrinsic-calibration CLI entry points from FPGA workspace commit `7c6f9d1`. | The host repository is the public owner of image processing and calibration; the FPGA repository only links to it. |
| `build_stereo_pairs.py`, `calibrate_binary_stereo.py`, `validate_binary_extrinsics.py` | Copied the stereo pairing, fixed-K/D solve, and independent holdout entry points from commit `7c6f9d1`. | Publish the complete host-side extrinsic workflow without calibration data or historical Attempt outputs. |
| `taxi_receiver/*calibration*.py`, `taxi_receiver/extrinsic_*.py`, `taxi_receiver/stereo_*.py` | Copied the corresponding implementation modules from commit `7c6f9d1`. | Keep CLI and implementation code in the same independently testable repository. |
| Seven calibration/stereo test files | Copied the calibration regression suite; the source baseline produced `57 passed`. | Preserve point-set, calibration, stereo-pairing, and validation invariants during migration. |
| `requirements-calibration.txt` | Added NumPy and pinned OpenCV to `<5`. | OpenCV 5.0.0.93 has a documented multi-view fisheye regression in this project; this is a dependency constraint, not vendored code. |
| `scripts_ps/run_intrinsic_calibration.ps1` | Added a portable preflight/training/V1/V2 runner with dry-run behavior and a run manifest. | Replace Attempt-specific command fragments with one public entry point. |
| `scripts_ps/run_extrinsic_calibration.ps1` | Added a portable static-threshold/training/V1/V2 runner that enforces matching intrinsic point sets. | Make the fixed-intrinsic stereo workflow reproducible while retaining all quality gates. |
| `docs/calibration_pipeline*.md` | Added English and Chinese calibration runbooks with complete PowerShell examples and evidence boundaries. | Explain inputs, output ownership, quality statuses, and why numerical solve success is not physical release. |
| `README.zh-CN.md`, `COPY_MANIFEST.md` | Reconstructed text that had been stored as mojibake. | Restore readable public documentation without changing receiver behavior. |
