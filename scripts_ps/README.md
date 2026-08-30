# PowerShell entry points

Run these scripts from any PowerShell working directory; every script resolves
the repository root from `$PSScriptRoot`.

| Category | Entry point | Responsibility |
|---|---|---|
| Capture | `capture/run_receiver.ps1` | Live Npcap ingress, parsing, per-camera lanes, reassembly and publication |
| Capture | `capture/replay_pcap.ps1` | Deterministic offline replay of a PCAP/PCAPNG capture |
| Monitoring | `monitoring/monitor_camera_output.ps1` | Read-only image/archive counter |
| Monitoring | `monitoring/run_camera_viewer.ps1` | Viewer for an existing archive |
| Diagnostics | `diagnostics/verify_s2.ps1` | A/B comparison of thread and process image publishers |
| Calibration | `calibration/run_intrinsic_calibration.ps1` | Intrinsic preflight, solve, holdout V1 and holdout V2 |
| Calibration | `calibration/run_extrinsic_calibration.ps1` | Stereo stillness, pairing, fixed-K/D solve and two holdouts |

The `taxi_receiver` Python package owns reusable behavior.  These PowerShell
files only validate paths, compose CLI arguments, preserve run evidence and
propagate process exit codes.  PowerShell script files use the `.ps1`
extension; `.ps2` is not a project file type.

Minimal clone check:

```powershell
$hostRoot = 'D:\prg\blank_project\Host_Camera_Packet_Receiver'
$receiver = Join-Path $hostRoot 'scripts_ps\capture\run_receiver.ps1'
& $receiver -Interface '<Npcap device GUID>' -WhatIf
```

If a script offers `-PreflightOnly`, use it before a real calibration run.
Never infer experiment PASS merely from `$LASTEXITCODE -eq 0`; read the JSON
`status` and quality fields emitted by the corresponding Python stage.
