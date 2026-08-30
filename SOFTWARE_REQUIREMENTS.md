# Host Camera Packet Receiver software requirements

This document is the software-environment contract for a cold-start clone of the Host repository. It deliberately separates five operating modes because an offline PCAP replay does not need the same software as live capture or calibration.

## Requirement labels

- **Required**: the named workflow cannot run without it.
- **Stage-specific**: install it only for the named workflow.
- **Observed**: the version was present in the 2026-08-30 closeout environment; this is reproducibility evidence, not automatically a minimum.
- **Minimum not verified**: the repository does not contain evidence for a safe numeric minimum. Do not replace this label with an estimate.

## Validated closeout snapshot

| Component | Repository requirement | Observed closeout version | Scope |
|---|---:|---:|---|
| Windows | Required for the documented Npcap and PowerShell live flow | Windows NT 10.0.26200.0, 64-bit | Live capture and supplied wrappers |
| Windows PowerShell | 5.1 or PowerShell 7 according to the public runbooks | 5.1.26100.9168 Desktop | `scripts_ps/` |
| Git | Required for clone, provenance, and clean-state checks; minimum not verified | 2.54.0.windows.1 | Repository governance |
| CPython | **3.10 or newer** | 3.14.6, 64-bit | All Python entry points |
| Scapy | Declared but unpinned in `requirements-live.txt` | 2.7.0 | Live capture |
| pytest | Declared but unpinned in `requirements-live.txt` | 9.1.1 | Regression tests |
| NumPy | `>=1.24` | 2.5.1 | Calibration |
| OpenCV Python headless | `>=4.8,<5` | package 4.14.0.94; `cv2` 4.14.0 | Calibration |
| Npcap | Required for live Windows capture; exact/minimum version not verified | installed version not recorded | Live capture only |
| Tk/Tcl Python support | Required only for the camera viewer; minimum not verified | not version-frozen | Viewer only |
| Wireshark | Optional; minimum not verified | not version-frozen | EtherType `0x88B5` inspection |

Python 3.10 is a source-level minimum: the repository uses PEP 604 union syntax such as `X | None`. The OpenCV upper bound is intentional; project regression evidence records a multi-view fisheye problem with OpenCV 5.0.0.93. The observed versions above should be recorded in each run manifest, but Scapy, pytest, Npcap, Git, and PowerShell must not be described as having a numeric minimum unless a later tested policy establishes one.

## Dependency matrix

| Workflow | Python | Additional software/packages | Npcap/admin capture rights |
|---|---|---|---|
| Offline PCAP replay and standard-library parsing | Required | Repository source; no live driver | No |
| Live packet capture | Required | `requirements-live.txt`, Npcap | Yes |
| Automated tests | Required | `pytest` from `requirements-live.txt` | No for synthetic/offline tests |
| CSV/PGM receiver output | Required | Live dependencies when reading a NIC | Yes for live capture |
| Camera viewer | Required | Python build with Tk/Tcl | No for archived images |
| Intrinsic/extrinsic calibration | Required | `requirements-calibration.txt` | No after PGM acquisition |
| Packet inspection outside Python | Optional | Wireshark | Npcap normally installed with capture support |

## Clean installation

Use a fresh clone and a repository-local virtual environment. Replace only the angle-bracket path.

```powershell
$host = '<HOST_REPOSITORY_ROOT>'
$basePython = 'C:\Users\<USER>\AppData\Local\Programs\Python\Python314\python.exe'

if (!(Test-Path -LiteralPath $host -PathType Container)) {
  throw "Host repository does not exist: $host"
}
if (!(Test-Path -LiteralPath $basePython -PathType Leaf)) {
  throw "Python executable does not exist: $basePython"
}

Set-Location $host
& $basePython -m venv .venv
$python = Join-Path $host '.venv\Scripts\python.exe'
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $host 'requirements-live.txt')
& $python -m pip install -r (Join-Path $host 'requirements-calibration.txt')
```

The example Python path is a placeholder, not a project invariant. Locate the interpreter with `Get-Command python -All` or the Python launcher, then record the resolved executable in the run manifest. Do not copy the closeout user's absolute Python path into a public automation script.

## PRECHECK / dry run

Run this before acquiring data. The array materialization avoids the PowerShell empty-pipeline problem when a package or executable is absent.

```powershell
$host = '<HOST_REPOSITORY_ROOT>'
$python = Join-Path $host '.venv\Scripts\python.exe'
$requiredFiles = @(
  'requirements-live.txt'
  'requirements-calibration.txt'
  'run_receiver.ps1'
  'taxi_receiver\cli.py'
)
$inventory = @(
  foreach ($relativePath in $requiredFiles) {
    $fullPath = Join-Path $host $relativePath
    [pscustomobject]@{
      Path = $fullPath
      Exists = Test-Path -LiteralPath $fullPath -PathType Leaf
    }
  }
)
$inventory | Format-Table -AutoSize
if (@($inventory | Where-Object { -not $_.Exists }).Count -gt 0) {
  throw 'Host software precheck failed: required files are missing'
}

& $python --version
& $python -c "import sys,scapy,pytest,numpy,cv2; print(sys.executable); print('scapy',scapy.__version__); print('pytest',pytest.__version__); print('numpy',numpy.__version__); print('cv2',cv2.__version__)"
```

The calibration wrappers support `-PreflightOnly` and `-WhatIf`. Use those modes before any long solve or creation of a formal audit directory. The live receiver is intentionally long-running and does not have the same dry-run switch; its safe preflight is the interface-list command below plus an explicit fresh output root.

## Live capture validation

Npcap is a machine-level driver, not a Python wheel. Install it separately, open a PowerShell session with capture permission when required by local policy, and enumerate interfaces:

```powershell
$host = '<HOST_REPOSITORY_ROOT>'
$python = Join-Path $host '.venv\Scripts\python.exe'
Set-Location $host
& $python -m taxi_receiver.cli --list
if ($LASTEXITCODE -ne 0) {
  throw "Npcap/interface preflight failed with exit code $LASTEXITCODE"
}
```

Copy the exact `\Device\NPF_{GUID}` string printed by this command. Never leave `<actual interface>` or a stale GUID in a launch command: Windows error 123 indicates an invalid adapter string, while zero ingress with a valid-looking command can indicate the wrong NIC, insufficient permissions, an inactive physical link, or missing Npcap support.

## Test and calibration validation

```powershell
$host = '<HOST_REPOSITORY_ROOT>'
$python = Join-Path $host '.venv\Scripts\python.exe'
Set-Location $host

& $python -m pytest -q
$testExit = $LASTEXITCODE
if ($testExit -ne 0) {
  throw "Host regression suite failed with exit code $testExit"
}

& (Join-Path $host 'scripts_ps\calibration\run_intrinsic_calibration.ps1') `
  -CameraId 0 `
  -TrainingRoot '<TRAINING_CAM0_ROOT>' `
  -HoldoutV1Root '<HOLDOUT_V1_CAM0_ROOT>' `
  -HoldoutV2Root '<HOLDOUT_V2_CAM0_ROOT>' `
  -OutputRoot '<FRESH_INTRINSIC_AUDIT_ROOT>' `
  -PythonExe $python `
  -PreflightOnly `
  -WhatIf
```

Replace every angle-bracket path before running. Inspect the complete current parameter table with `Get-Help .\scripts_ps\calibration\run_intrinsic_calibration.ps1 -Full` and the bilingual calibration runbook. Repeat the same preflight pattern for `run_extrinsic_calibration.ps1`. A generated JSON file is not by itself a PASS: inspect its `status`, quality failures, and the wrapper exit code.

## Filesystem and evidence requirements

- Use a new timestamped run directory. The wrappers reject non-empty formal output roots; do not work around this by deleting historical evidence.
- Keep `.venv/`, caches, PGM/PCAP/CSV runs, K/D, R/t, and interface GUIDs out of the public repository unless a specifically reviewed fixture is approved.
- The repository does not establish numeric RAM, CPU, free-disk, or NIC-throughput minima. Record the actual machine, NIC, queue settings, packet counters, free disk, and peak queues for the run instead of claiming an unsupported minimum.
- `run_manifest.json` should bind the run ID to Git HEAD/dirty state, Python executable and package versions, interface GUID, capture root, camera IDs, relevant K/D and R/t hashes, parameters, and final status.

## Licensing boundary

Installing dependencies does not place them under this repository's BSD 3-Clause license. Review [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) before redistribution. Npcap in particular is installed under its own terms; the public repository must not copy its installer, DLLs, or SDK merely because a local workstation uses them.
