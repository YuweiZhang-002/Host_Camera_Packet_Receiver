# Host repository migration manifest

This manifest records what was moved from the FPGA development workspace into the public host-only repository. It is evidence of provenance and scope, not permission to copy experiment data.

## Repository identity

| Field | Value |
|---|---|
| Source workspace | `<PRIVATE_INTEGRATION_WORKSPACE>` (historical import source; not required after clone) |
| Host repository | `D:\prg\blank_project\Host_Camera_Packet_Receiver` (example cold-start path) |
| Source closeout commit | `7c6f9d1` |
| Host base commit | `a991ef0` |
| Publication branch | `feat/calibration-pipeline-publication` |
| Calibration migration date | 2026-08-29 |

The absolute paths above identify the migration workstation only. Public commands use repository-relative paths.

## Included scope

- `taxi_receiver/`: packet format, capture, parser, monitor, reassembler, storage, publisher isolation, viewer, and calibration implementation modules.
- Root receiver, replay, verification, calibration and analysis entry points.
- `tests/`: synthetic receiver regressions and public calibration regressions.
- `requirements-live.txt` and `requirements-calibration.txt`.
- Documentation, BSD 3-Clause license, bilingual third-party notices, quick
  reference, bilingual software requirements, divergence record, and this
  manifest.

## Calibration extension

Intrinsic CLI files: `preflight_calibration_frames.py`, `calibrate_binary_camera.py`, `calibrate_binary_camera_refill.py`, and `validate_binary_calibration.py`.

Extrinsic CLI files: `build_stereo_pairs.py`, `calibrate_binary_stereo.py`, and `validate_binary_extrinsics.py`.

Implementation modules: `binary_calibration.py`, `calibration_config.py`, `calibration_refill.py`, `calibration_validation.py`, `extrinsic_config.py`, `extrinsic_validation.py`, `stereo_calibration.py`, and `stereo_pairs.py` under `taxi_receiver/`.

Public wrappers and runbooks: `scripts_ps/calibration/run_intrinsic_calibration.ps1`, `scripts_ps/calibration/run_extrinsic_calibration.ps1`, and `docs/calibration_pipeline*.md`.

## Explicit exclusions

- FPGA/RTL, XDC, XPR, DCP, bitstream, LTX, Vivado cache, implementation runs or hardware-server artifacts.
- MCU firmware, PIO sources, UF2 files or MCU build trees.
- Historical `images/`, `build/attempt*`, PCAP/PCAPNG, PGM/RAW, rows CSV, calibration JSON, K/D, R/t, operator paths or interface GUIDs.
- Python virtual environments, caches and downloaded package contents.
- Third-party FPGA Ethernet/TAXI source trees.

## Verification evidence

| Check | Result |
|---|---|
| Host baseline before calibration extension | `157 passed, 2 skipped` |
| Migrated calibration subset | `57 passed` |
| Combined public candidate | `216 passed, 2 skipped` |
| Calibration CLI `--help` smoke test | 7/7 passed |
| Python used for calibration smoke test | 3.14.6 |
| NumPy | 2.5.1 |
| OpenCV | 4.14.0 |
| Historical data copied | none |
| Vendored third-party source copied | none |

The two skipped receiver tests depend on repository-external capture artifacts and state their skip reason. A live Npcap run is hardware- and privilege-dependent and must be performed on the publication machine.

## Publication rule

Only reviewed source, tests, documentation, and dependency declarations may be committed. Run outputs remain immutable local evidence and must not be added with `git add -A`. Review `git status --short`, `git diff --cached --stat`, and the staged path list before every public push.
