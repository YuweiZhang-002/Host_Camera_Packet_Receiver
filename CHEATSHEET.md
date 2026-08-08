# Host Receiver Cheatsheet

Quick commands for the host-side `taxi_receiver` copy. All paths below are relative to the repository root.

## Bring-up

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements-live.txt
.\.venv\Scripts\python.exe -m taxi_receiver.cli --list
```

## Live capture

```powershell
.\run_receiver.ps1 -Interface "\\Device\\NPF_{YOUR-GUID}"
```

## Live capture without rows.csv

```powershell
.\run_receiver.ps1 -Interface "\\Device\\NPF_{YOUR-GUID}" -NoRowsCsv
```

## Throughput test

```powershell
.\run_receiver.ps1 -Interface "\\Device\\NPF_{YOUR-GUID}" `
  -ImagesRoot .\images `
  -QueueDepth 65536 `
  -FrameOutputQueueDepth 256 `
  -PublishImages process `
  -PublishFrames complete
```

## Loss-tolerant archive capture

```powershell
.\run_receiver.ps1 -Interface "\\Device\\NPF_{YOUR-GUID}" `
  -ImagesRoot .\images `
  -OutputRoot .\archive `
  -ImagePolicy recover-zero-fill `
  -PublishFrames eligible
```

## Offline replay with verification

```powershell
.\verify_s2.ps1 -ReplayPcap .\build\wire.pcapng -OutRoot .\build\s2_verify
```

## Viewer

```powershell
.\run_camera_viewer.ps1
```

## Folder monitor

```powershell
.\monitor_camera_output.ps1 -ImagesRoot .\images
```

## Notes

- Use an elevated PowerShell for real live capture.
- Install Npcap before trying `--list` or `run_receiver.ps1` against a real interface.
- `submit blocked` is the field that tells you whether a sink, not a queue, is the bottleneck.
