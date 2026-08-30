[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(0, 1)]
    [int]$CameraId,

    [Parameter(Mandatory = $true)]
    [string]$TrainingRoot,

    [Parameter(Mandatory = $true)]
    [string]$HoldoutV1Root,

    [Parameter(Mandatory = $true)]
    [string]$HoldoutV2Root,

    [Parameter(Mandatory = $true)]
    [string]$OutputRoot,

    [string]$PythonExe = '',

    [ValidateSet('full', 'fix_k4', 'fix_k3_k4')]
    [string]$FisheyeConstraint = 'full',

    [ValidateRange(1, 1000)]
    [int]$MinPoses = 15,

    [ValidateRange(1, 1000)]
    [int]$MinViews = 15,

    [ValidateRange(1, 1000)]
    [int]$MinHoldoutViews = 15,

    [switch]$PreflightOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
}
$calibrationCliRoot = Join-Path $repoRoot 'scripts_py\calibration'
$preflightScript = Join-Path $calibrationCliRoot 'preflight_calibration_frames.py'
$calibrationScript = Join-Path $calibrationCliRoot 'calibrate_binary_camera.py'
$validationScript = Join-Path $calibrationCliRoot 'validate_binary_calibration.py'

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

function Get-PgmCount {
    param([string]$Path)
    return @(
        Get-ChildItem -LiteralPath $Path -Filter '*.pgm' -File -Recurse
    ).Count
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
$TrainingRoot = Resolve-RequiredDirectory -Path $TrainingRoot -Label 'Training dataset'
$HoldoutV1Root = Resolve-RequiredDirectory -Path $HoldoutV1Root -Label 'Holdout V1 dataset'
$HoldoutV2Root = Resolve-RequiredDirectory -Path $HoldoutV2Root -Label 'Holdout V2 dataset'
$preflightScript = Resolve-RequiredFile -Path $preflightScript -Label 'Preflight CLI'
$calibrationScript = Resolve-RequiredFile -Path $calibrationScript -Label 'Calibration CLI'
$validationScript = Resolve-RequiredFile -Path $validationScript -Label 'Validation CLI'
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)

$inputCounts = [ordered]@{
    training = Get-PgmCount -Path $TrainingRoot
    holdout_v1 = Get-PgmCount -Path $HoldoutV1Root
    holdout_v2 = Get-PgmCount -Path $HoldoutV2Root
}
foreach ($entry in $inputCounts.GetEnumerator()) {
    if ([int]$entry.Value -eq 0) {
        throw "$($entry.Key) contains no PGM files"
    }
}

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

Assert-FreshOutput -Path $OutputRoot

$preflightRoot = Join-Path $OutputRoot '00_preflight'
$trainingOutputRoot = Join-Path $OutputRoot '01_training'
$holdoutV1OutputRoot = Join-Path $OutputRoot '02_holdout_v1'
$holdoutV2OutputRoot = Join-Path $OutputRoot '03_holdout_v2'
$selectedRoot = Join-Path $preflightRoot 'selected_poses'
$intrinsicPath = Join-Path $trainingOutputRoot "cam${CameraId}_intrinsics.json"
$manifestPath = Join-Path $OutputRoot 'run_manifest.json'

Write-Host 'Intrinsic calibration plan'
Write-Host "  camera          : cam$CameraId"
Write-Host "  training frames : $($inputCounts.training)"
Write-Host "  holdout V1      : $($inputCounts.holdout_v1)"
Write-Host "  holdout V2      : $($inputCounts.holdout_v2)"
Write-Host "  model           : opencv_fisheye ($FisheyeConstraint)"
Write-Host "  Python/OpenCV   : $($versions.python) / $($versions.opencv)"
Write-Host "  repository      : $repositoryHead (dirty=$repositoryDirty)"
Write-Host "  output          : $OutputRoot"

if ($PreflightOnly -or $WhatIfPreference) {
    Write-Host 'PRECHECK PASS: no files were written.' -ForegroundColor Green
    return
}
if (-not $PSCmdlet.ShouldProcess($OutputRoot, 'run intrinsic calibration and two independent holdouts')) {
    return
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$manifest = [ordered]@{
    schema_version = 1
    run_type = 'intrinsic_calibration'
    run_id = Split-Path -Leaf $OutputRoot
    started_utc = [DateTime]::UtcNow.ToString('o')
    status = 'running'
    camera_id = $CameraId
    inputs = [ordered]@{
        training_root = $TrainingRoot
        holdout_v1_root = $HoldoutV1Root
        holdout_v2_root = $HoldoutV2Root
        pgm_counts = $inputCounts
    }
    software = [ordered]@{
        repository_head = $repositoryHead
        repository_dirty = $repositoryDirty
        python = [string]$versions.python
        numpy = [string]$versions.numpy
        opencv = [string]$versions.opencv
    }
    policy = [ordered]@{
        model = 'opencv_fisheye'
        fisheye_constraint = $FisheyeConstraint
        minimum_poses = $MinPoses
        minimum_training_views = $MinViews
        minimum_holdout_views = $MinHoldoutViews
        median_rmse_px = 0.8
        p95_rmse_px = 1.2
        maximum_rmse_px = 1.5
        required_pass_fraction = 0.90
    }
    outputs = [ordered]@{}
    failure = $null
}

try {
    New-Item -ItemType Directory -Path $preflightRoot, $trainingOutputRoot -Force |
        Out-Null

    Invoke-PythonStep -Label 'training preflight' -Arguments @(
        $preflightScript,
        $TrainingRoot,
        '--report', (Join-Path $preflightRoot 'frames.csv'),
        '--montage', (Join-Path $preflightRoot 'montage.png'),
        '--zone-map',
        '--export-selected', $selectedRoot,
        '--min-poses', [string]$MinPoses
    )

    $selectedCount = Get-PgmCount -Path $selectedRoot
    if ($selectedCount -lt $MinViews) {
        throw "Preflight exported only $selectedCount views; need $MinViews"
    }

    $calibrationArgs = @(
        $calibrationScript,
        $selectedRoot,
        '--camera-id', [string]$CameraId,
        '--output', $intrinsicPath,
        '--report', (Join-Path $trainingOutputRoot 'views.csv'),
        '--diagnostics-dir', (Join-Path $trainingOutputRoot 'diagnostics'),
        '--model', 'fisheye',
        '--fov-deg', '120',
        '--min-views', [string]$MinViews,
        '--max-view-rmse-px', '1.5'
    )
    if ($FisheyeConstraint -eq 'fix_k4') {
        $calibrationArgs += '--fisheye-fix-k4'
    }
    elseif ($FisheyeConstraint -eq 'fix_k3_k4') {
        $calibrationArgs += '--fisheye-fix-k3-k4'
    }
    Invoke-PythonStep -Label 'intrinsic solve' -Arguments $calibrationArgs

    $intrinsic = Read-Json -Path $intrinsicPath -Label 'Intrinsic JSON'
    if ([string]$intrinsic.quality.status -ne 'acceptable') {
        throw "Intrinsic quality is not acceptable: $($intrinsic.quality.status)"
    }

    $holdouts = @(
        [pscustomobject]@{Name='V1';Input=$HoldoutV1Root;Output=$holdoutV1OutputRoot},
        [pscustomobject]@{Name='V2';Input=$HoldoutV2Root;Output=$holdoutV2OutputRoot}
    )
    foreach ($holdout in $holdouts) {
        Invoke-PythonStep -Label "holdout $($holdout.Name)" -Arguments @(
            $validationScript,
            $intrinsicPath,
            $holdout.Input,
            '--output-root', $holdout.Output,
            '--sample-count', '30',
            '--min-holdout-views', [string]$MinHoldoutViews,
            '--median-rmse-px', '0.8',
            '--p95-rmse-px', '1.2',
            '--maximum-rmse-px', '1.5',
            '--required-pass-fraction', '0.90'
        )
        $summaryPath = Join-Path $holdout.Output 'holdout_summary.json'
        $summary = Read-Json -Path $summaryPath -Label "Holdout $($holdout.Name) summary"
        if ([string]$summary.status -ne 'pass') {
            throw "Holdout $($holdout.Name) status is not pass: $($summary.status)"
        }
    }

    $manifest.status = 'pass'
    $manifest.outputs = [ordered]@{
        intrinsic_json = $intrinsicPath
        intrinsic_sha256 = (Get-FileHash -LiteralPath $intrinsicPath -Algorithm SHA256).Hash
        holdout_v1_summary = Join-Path $holdoutV1OutputRoot 'holdout_summary.json'
        holdout_v2_summary = Join-Path $holdoutV2OutputRoot 'holdout_summary.json'
    }
    Write-Host 'INTRINSIC CALIBRATION: PASS' -ForegroundColor Green
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
