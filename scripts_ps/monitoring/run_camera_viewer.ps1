 # Host-side viewer launcher for archive inspection.
 # No Npcap or elevation is required.
 # Paths are derived from $PSScriptRoot instead of a fixed drive letter.
param(
    [string]$ArchiveRoot = '',

    [string]$Attempt = 'attempt1',

    [string]$Camera = '',

    [int]$RefreshIntervalMs = 50,

    [int]$PollIntervalMs = 50,

    [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop'
$viewerRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if ([string]::IsNullOrWhiteSpace($ArchiveRoot)) {
    $ArchiveRoot = Join-Path $viewerRoot 'images\temp\archive'
}
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = Join-Path $viewerRoot '.venv\Scripts\python.exe'
}
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
