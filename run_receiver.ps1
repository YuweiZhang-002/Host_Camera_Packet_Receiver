 # Host-side live receiver launcher for taxi_receiver.
 # Requires an elevated shell and Npcap when using a real interface.
 # Defaults are derived from $PSScriptRoot and the local .venv.
param(
    [Parameter(Mandatory = $true)]
    [string]$Interface,

    # Optional.  The frame archive (image.raw + packets.csv + summary.csv per
    # frame) is evidence tooling, not the image path, and it is the most
    # expensive sink in the receiver.  Leave it unset for a throughput run;
    # --images-root alone still produces RAW/PGM images and rows.csv.
    [string]$OutputRoot = '',

    [int]$ExpectedRows = 480,

    [int]$QueueDepth = 65536,

    [int]$FrameOutputQueueDepth = 256,

    [string]$ImagesRoot = (Join-Path $PSScriptRoot 'images'),

    [ValidateSet('strict', 'recover-zero-fill')]
    [string]$ImagePolicy = 'strict',

    [int]$MaxMissingRows = 4,

    [int]$MaxConsecutiveMissing = 2,

    # S1 per-camera lanes.  'off' restores the single shared worker and is what
    # an A/B comparison against the old behaviour needs.
    [ValidateSet('auto', 'on', 'off')]
    [string]$SplitByCamera = 'auto',

    [string]$CameraIds = '0,1,2,3',

    # 'complete' skips partial frames entirely: they cost a full serialisation
    # plus a recovery assessment in every sink and are then rejected anyway.
    [ValidateSet('complete', 'eligible', 'all')]
    [string]$PublishFrames = 'complete',

    # S2.  'process' runs image publication in a dedicated process per camera;
    # 'thread' is the pre-S2 behaviour and is the A/B baseline.  run001 measured
    # each lane thread blocked 34 s of 65 s on its in-thread publisher.
    [ValidateSet('process', 'thread')]
    [string]$PublishImages = 'process',

    [int]$PublisherQueueDepth = 256,

    # session_audit.csv is written synchronously on the consumer thread and is
    # nearly a subset of rows.csv.  'auto' means off for live capture.
    [ValidateSet('auto', 'on', 'off')]
    [string]$SessionAudit = 'auto',

    # frame_id restarts near zero at every board power-on, so reusing an
    # archive root makes every frame collide with an earlier run.
    [ValidateSet('run-subdir', 'require-empty', 'reuse')]
    [string]$ArchiveRootPolicy = 'run-subdir',

    [ValidateSet('suffix', 'error')]
    [string]$ArchiveCollisionPolicy = 'suffix',

    [switch]$NoRowsCsv,

    [string]$PythonExe =
        (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
)

$ErrorActionPreference = 'Stop'
$receiverRoot = $PSScriptRoot
$ImagesRoot = [IO.Path]::GetFullPath($ImagesRoot)

$receiverArgs = @(
    '-m', 'taxi_receiver.cli',
    '--interface', $Interface,
    '--mode', 'camera',
    '--max-stage', 'reassemble',
    '--expected-rows', $ExpectedRows,
    '--image-policy', $ImagePolicy,
    '--max-missing-rows', $MaxMissingRows,
    '--max-consecutive-missing', $MaxConsecutiveMissing,
    '--queue-depth', $QueueDepth,
    '--frame-output-queue-depth', $FrameOutputQueueDepth,
    '--images-root', $ImagesRoot,
    '--split-by-camera', $SplitByCamera,
    '--camera-ids', $CameraIds,
    '--publish-frames', $PublishFrames,
    '--publish-images', $PublishImages,
    '--publisher-queue-depth', $PublisherQueueDepth,
    '--session-audit', $SessionAudit
)
if (-not [string]::IsNullOrWhiteSpace($OutputRoot)) {
    $receiverArgs += @(
        '--output-root', $OutputRoot,
        '--archive-root-policy', $ArchiveRootPolicy,
        '--archive-collision-policy', $ArchiveCollisionPolicy
    )
}
if ($NoRowsCsv) {
    $receiverArgs += '--no-rows-csv'
}

Push-Location $receiverRoot
try {
    & $PythonExe @receiverArgs
    $exitCode = $LASTEXITCODE
    # 7 = every submitted frame failed in the output sink, or a sink was
    # disabled after repeated failures.  Surface it instead of hiding a total
    # publication outage behind a normal-looking Final Report.
    if ($exitCode -eq 7) {
        Write-Warning 'Receiver reported an output-sink failure (exit 7).'
    }
    exit $exitCode
}
finally {
    Pop-Location
}
