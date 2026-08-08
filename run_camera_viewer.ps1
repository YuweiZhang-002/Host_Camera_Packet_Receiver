 # Host-side viewer launcher for archive inspection.
 # No Npcap or elevation is required.
 # Paths are derived from $PSScriptRoot instead of a fixed drive letter.
param(
    [string]$ArchiveRoot = (Join-Path $PSScriptRoot 'images\temp\archive'),

    [string]$Attempt = 'attempt1',

    [string]$Camera = '',

    [int]$RefreshIntervalMs = 50,

    [int]$PollIntervalMs = 50,

    [string]$PythonExe = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
)

$ErrorActionPreference = 'Stop'
$viewerRoot = $PSScriptRoot
Push-Location $viewerRoot
try {
    & $PythonExe -m taxi_receiver.viewer_cli `
        --archive-root $ArchiveRoot `
        --attempt $Attempt `
        --camera $Camera `
        --refresh-interval-ms $RefreshIntervalMs `
        --poll-interval-ms $PollIntervalMs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
