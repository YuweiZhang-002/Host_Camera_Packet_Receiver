[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$StaticRoot,

    [Parameter(Mandatory = $true)]
    [string]$TrainingRoot,

    [Parameter(Mandatory = $true)]
    [string]$HoldoutV1Root,

    [Parameter(Mandatory = $true)]
    [string]$HoldoutV2Root,

    [Parameter(Mandatory = $true)]
    [string]$Cam0Intrinsic,

    [Parameter(Mandatory = $true)]
    [string]$Cam1Intrinsic,

    [Parameter(Mandatory = $true)]
    [string]$OutputRoot,

    [string]$PythonExe = '',

    [ValidateSet(
        'stationary',
        'quasi_static_local_minimum',
        'quasi_static_episode_minimum'
    )]
    [string]$PairingMode = 'quasi_static_episode_minimum',

    [ValidateRange(1, 1000)]
    [int]$MinPairs = 15,

    [ValidateRange(1, 100000)]
    [int]$MinStaticFrames = 200,

    [ValidateRange(1, 100)]
    [int]$WindowFrames = 5,

    [ValidateRange(0.001, 10000.0)]
    [double]$MaxCenterDtMs = 33.5,

    [ValidateRange(0.001, 1000.0)]
    [double]$MaxPredictedMotionPx = 0.75,

    [ValidateRange(0.000001, 1000.0)]
    [double]$MaxMotionRatePxPerMs = 0.02,

    [ValidateRange(1, 10000)]
    [int]$QuasiEpisodeGapFrames = 10,

    [ValidateRange(0.001, 10000.0)]
    [double]$MinCam0EdgeMarginPx = 12.0,

    [switch]$PreflightOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
}
$calibrationCliRoot = Join-Path $repoRoot 'scripts_py\calibration'
$pairScript = Join-Path $calibrationCliRoot 'build_stereo_pairs.py'
$solveScript = Join-Path $calibrationCliRoot 'calibrate_binary_stereo.py'
$validationScript = Join-Path $calibrationCliRoot 'validate_binary_extrinsics.py'

$repositoryHead = 'UNVERIFIED'
$repositoryDirty = $null
if ($null -ne (Get-Command git -ErrorAction SilentlyContinue)) {
    $headLines = @(& git -c "safe.directory=$repoRoot" -C $repoRoot rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -eq 0 -and $headLines.Count -gt 0) {
        $repositoryHead = [string]$headLines[0]
        $repositoryDirty = @(
            & git -c "safe.directory=$repoRoot" -C $repoRoot status --porcelain
        ).Count -gt 0
    }
}

function Resolve-RequiredFile {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label does not exist: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Resolve-RequiredDirectory {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label does not exist: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Assert-DualCameraDataset {
    param([string]$Path, [string]$Label)
    $resolved = Resolve-RequiredDirectory -Path $Path -Label $Label
    $counts = [ordered]@{}
    foreach ($camera in 'cam0', 'cam1') {
        $cameraRoot = Join-Path $resolved $camera
        Resolve-RequiredDirectory -Path $cameraRoot -Label "$Label $camera" |
            Out-Null
        $counts[$camera] = @(
            Get-ChildItem -LiteralPath $cameraRoot -Filter '*.pgm' -File -Recurse
        ).Count
        if ([int]$counts[$camera] -eq 0) {
            throw "$Label $camera contains no PGM files: $cameraRoot"
        }
    }
    return [pscustomobject]@{Path=$resolved;Counts=$counts}
}

function Assert-FreshOutput {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Output root exists but is not a directory: $Path"
    }
    if (@(Get-ChildItem -LiteralPath $Path -Force).Count -ne 0) {
        throw "Output root is not empty; choose a new run directory: $Path"
    }
}

function Invoke-PythonStep {
    param([string]$Label, [string[]]$Arguments)
    Write-Host "[$Label] $PythonExe $($Arguments -join ' ')"
    & $PythonExe @Arguments
    $stepExit = $LASTEXITCODE
    if ($stepExit -ne 0) {
        throw "$Label failed with exit code $stepExit"
    }
}

function Read-Json {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label was not generated: $Path"
    }
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 |
        ConvertFrom-Json
}

$PythonExe = Resolve-RequiredFile -Path $PythonExe -Label 'Python executable'
$Cam0Intrinsic = Resolve-RequiredFile -Path $Cam0Intrinsic -Label 'CAM0 intrinsic'
$Cam1Intrinsic = Resolve-RequiredFile -Path $Cam1Intrinsic -Label 'CAM1 intrinsic'
$pairScript = Resolve-RequiredFile -Path $pairScript -Label 'Stereo pairing CLI'
$solveScript = Resolve-RequiredFile -Path $solveScript -Label 'Stereo solve CLI'
$validationScript = Resolve-RequiredFile -Path $validationScript -Label 'Extrinsic validation CLI'
$staticData = Assert-DualCameraDataset -Path $StaticRoot -Label 'Static dataset'
$trainingData = Assert-DualCameraDataset -Path $TrainingRoot -Label 'Training dataset'
$holdoutV1Data = Assert-DualCameraDataset -Path $HoldoutV1Root -Label 'Holdout V1 dataset'
$holdoutV2Data = Assert-DualCameraDataset -Path $HoldoutV2Root -Label 'Holdout V2 dataset'
$StaticRoot = $staticData.Path
$TrainingRoot = $trainingData.Path
$HoldoutV1Root = $holdoutV1Data.Path
$HoldoutV2Root = $holdoutV2Data.Path
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)

$versionLines = @(& $PythonExe -c (
    'import sys,cv2,numpy; ' +
    'print(sys.version.split()[0]); ' +
    'print(numpy.__version__); ' +
    'print(cv2.__version__)'
))
if ($LASTEXITCODE -ne 0) {
    throw 'Calibration imports failed. Install requirements-calibration.txt into this Python environment.'
}
if ($versionLines.Count -ne 3) {
    throw "Unexpected Python version probe output: $($versionLines -join ' | ')"
}
$versions = [pscustomobject]@{
    python = $versionLines[0]
    numpy = $versionLines[1]
    opencv = $versionLines[2]
}

Push-Location -LiteralPath $repoRoot
try {
    & $PythonExe -c (
        'import sys; from pathlib import Path; ' +
        'from taxi_receiver.extrinsic_config import validate_intrinsic_pair; ' +
        'validate_intrinsic_pair(Path(sys.argv[1]), Path(sys.argv[2]))'
    ) $Cam0Intrinsic $Cam1Intrinsic
    if ($LASTEXITCODE -ne 0) {
        throw 'The two intrinsic JSON files are not a valid identical-point-set pair.'
    }
}
finally {
    Pop-Location
}

Assert-FreshOutput -Path $OutputRoot

$staticAudit = Join-Path $OutputRoot '00_static'
$trainingPairsRoot = Join-Path $OutputRoot '01_training_pairs'
$solveRoot = Join-Path $OutputRoot '02_training_solve'
$holdoutV1PairsRoot = Join-Path $OutputRoot '03_holdout_v1_pairs'
$holdoutV1ValidationRoot = Join-Path $OutputRoot '04_holdout_v1_validation'
$holdoutV2PairsRoot = Join-Path $OutputRoot '05_holdout_v2_pairs'
$holdoutV2ValidationRoot = Join-Path $OutputRoot '06_holdout_v2_validation'
$stillnessPath = Join-Path $staticAudit 'stillness_config.json'
$extrinsicsPath = Join-Path $solveRoot 'cam0_to_cam1_extrinsics.json'
$manifestPath = Join-Path $OutputRoot 'run_manifest.json'

Write-Host 'Extrinsic calibration plan'
Write-Host "  pairing mode : $PairingMode"
Write-Host "  static       : cam0=$($staticData.Counts.cam0) cam1=$($staticData.Counts.cam1)"
Write-Host "  training     : cam0=$($trainingData.Counts.cam0) cam1=$($trainingData.Counts.cam1)"
Write-Host "  holdout V1   : cam0=$($holdoutV1Data.Counts.cam0) cam1=$($holdoutV1Data.Counts.cam1)"
Write-Host "  holdout V2   : cam0=$($holdoutV2Data.Counts.cam0) cam1=$($holdoutV2Data.Counts.cam1)"
Write-Host "  minimum pairs: $MinPairs"
Write-Host "  Python/OpenCV: $($versions.python) / $($versions.opencv)"
Write-Host "  repository   : $repositoryHead (dirty=$repositoryDirty)"
Write-Host "  output       : $OutputRoot"

if ($PreflightOnly -or $WhatIfPreference) {
    Write-Host 'PRECHECK PASS: no files were written.' -ForegroundColor Green
    return
}
if (-not $PSCmdlet.ShouldProcess($OutputRoot, 'run stereo solve and two independent holdouts')) {
    return
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$manifest = [ordered]@{
    schema_version = 1
    run_type = 'extrinsic_calibration'
    run_id = Split-Path -Leaf $OutputRoot
    started_utc = [DateTime]::UtcNow.ToString('o')
    status = 'running'
    inputs = [ordered]@{
        static_root = $StaticRoot
        training_root = $TrainingRoot
        holdout_v1_root = $HoldoutV1Root
        holdout_v2_root = $HoldoutV2Root
        cam0_intrinsic = $Cam0Intrinsic
        cam0_intrinsic_sha256 = (Get-FileHash -LiteralPath $Cam0Intrinsic -Algorithm SHA256).Hash
        cam1_intrinsic = $Cam1Intrinsic
        cam1_intrinsic_sha256 = (Get-FileHash -LiteralPath $Cam1Intrinsic -Algorithm SHA256).Hash
    }
    software = [ordered]@{
        repository_head = $repositoryHead
        repository_dirty = $repositoryDirty
        python = [string]$versions.python
        numpy = [string]$versions.numpy
        opencv = [string]$versions.opencv
    }
    policy = [ordered]@{
        pairing_mode = $PairingMode
        minimum_pairs = $MinPairs
        minimum_static_frames = $MinStaticFrames
        window_frames = $WindowFrames
        max_center_dt_ms = $MaxCenterDtMs
        max_predicted_motion_px = $MaxPredictedMotionPx
        max_motion_rate_px_per_ms = $MaxMotionRatePxPerMs
        quasi_episode_gap_frames = $QuasiEpisodeGapFrames
        minimum_cam0_edge_margin_px = $MinCam0EdgeMarginPx
        median_cross_rmse_px = 0.8
        p95_cross_rmse_px = 1.2
        maximum_cross_rmse_px = 1.5
        required_pass_fraction = 0.90
    }
    outputs = [ordered]@{}
    failure = $null
}

function Invoke-Pairing {
    param([string]$Label, [string]$DatasetRoot, [string]$PairOutputRoot)
    Invoke-PythonStep -Label $Label -Arguments @(
        $pairScript,
        $DatasetRoot,
        '--cam0-intrinsics', $Cam0Intrinsic,
        '--cam1-intrinsics', $Cam1Intrinsic,
        '--output-root', $PairOutputRoot,
        '--stillness-config', $stillnessPath,
        '--pairing-mode', $PairingMode,
        '--max-center-dt-ms', [string]$MaxCenterDtMs,
        '--max-predicted-motion-px', [string]$MaxPredictedMotionPx,
        '--max-motion-rate-px-per-ms', [string]$MaxMotionRatePxPerMs,
        '--quasi-episode-gap-frames', [string]$QuasiEpisodeGapFrames,
        '--min-cam0-edge-margin-px', [string]$MinCam0EdgeMarginPx,
        '--min-pairs', [string]$MinPairs
    )
    $summary = Read-Json -Path (Join-Path $PairOutputRoot 'pairing_summary.json') -Label "$Label summary"
    if ([string]$summary.status -ne 'ready') {
        throw "$Label status is not ready: $($summary.status)"
    }
}

function Invoke-HoldoutValidation {
    param([string]$Label, [string]$PairsRoot, [string]$ValidationRoot)
    Invoke-PythonStep -Label $Label -Arguments @(
        $validationScript,
        $extrinsicsPath,
        (Join-Path $PairsRoot 'pairs.csv'),
        '--pairing-summary', (Join-Path $PairsRoot 'pairing_summary.json'),
        '--cam0-intrinsics', $Cam0Intrinsic,
        '--cam1-intrinsics', $Cam1Intrinsic,
        '--stillness-config', $stillnessPath,
        '--output-root', $ValidationRoot,
        '--min-holdout-pairs', [string]$MinPairs,
        '--median-rmse-px', '0.8',
        '--p95-rmse-px', '1.2',
        '--maximum-rmse-px', '1.5',
        '--required-pass-fraction', '0.90'
    )
    $summary = Read-Json -Path (Join-Path $ValidationRoot 'holdout_summary.json') -Label "$Label summary"
    if ([string]$summary.status -ne 'pass') {
        throw "$Label status is not pass: $($summary.status)"
    }
}

try {
    Invoke-PythonStep -Label 'static stillness' -Arguments @(
        $pairScript,
        $StaticRoot,
        '--cam0-intrinsics', $Cam0Intrinsic,
        '--cam1-intrinsics', $Cam1Intrinsic,
        '--output-root', $staticAudit,
        '--estimate-stillness-only',
        '--window-frames', [string]$WindowFrames,
        '--min-static-frames', [string]$MinStaticFrames
    )
    Read-Json -Path $stillnessPath -Label 'Stillness configuration' | Out-Null

    Invoke-Pairing -Label 'training pairing' -DatasetRoot $TrainingRoot -PairOutputRoot $trainingPairsRoot

    Invoke-PythonStep -Label 'stereo solve' -Arguments @(
        $solveScript,
        (Join-Path $trainingPairsRoot 'pairs.csv'),
        '--pairing-summary', (Join-Path $trainingPairsRoot 'pairing_summary.json'),
        '--cam0-intrinsics', $Cam0Intrinsic,
        '--cam1-intrinsics', $Cam1Intrinsic,
        '--stillness-config', $stillnessPath,
        '--output', $extrinsicsPath,
        '--report', (Join-Path $solveRoot 'training_pairs.csv'),
        '--min-pairs', [string]$MinPairs
    )
    $extrinsics = Read-Json -Path $extrinsicsPath -Label 'Extrinsics JSON'
    if ([string]$extrinsics.quality.status -ne 'acceptable') {
        throw "Extrinsic quality is not acceptable: $($extrinsics.quality.status)"
    }

    Invoke-Pairing -Label 'holdout V1 pairing' -DatasetRoot $HoldoutV1Root -PairOutputRoot $holdoutV1PairsRoot
    Invoke-HoldoutValidation -Label 'holdout V1 validation' -PairsRoot $holdoutV1PairsRoot -ValidationRoot $holdoutV1ValidationRoot

    Invoke-Pairing -Label 'holdout V2 pairing' -DatasetRoot $HoldoutV2Root -PairOutputRoot $holdoutV2PairsRoot
    Invoke-HoldoutValidation -Label 'holdout V2 validation' -PairsRoot $holdoutV2PairsRoot -ValidationRoot $holdoutV2ValidationRoot

    $manifest.status = 'pass'
    $manifest.outputs = [ordered]@{
        stillness_config = $stillnessPath
        training_pairs = Join-Path $trainingPairsRoot 'pairs.csv'
        extrinsics_json = $extrinsicsPath
        extrinsics_sha256 = (Get-FileHash -LiteralPath $extrinsicsPath -Algorithm SHA256).Hash
        holdout_v1_summary = Join-Path $holdoutV1ValidationRoot 'holdout_summary.json'
        holdout_v2_summary = Join-Path $holdoutV2ValidationRoot 'holdout_summary.json'
    }
    Write-Host 'EXTRINSIC CALIBRATION: PASS' -ForegroundColor Green
}
catch {
    $manifest.status = 'fail'
    $manifest.failure = $_.Exception.Message
    throw
}
finally {
    $manifest.completed_utc = [DateTime]::UtcNow.ToString('o')
    $manifest | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $manifestPath -Encoding UTF8
    Write-Host "Manifest: $manifestPath"
}
