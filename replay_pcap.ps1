 # Offline replay launcher for host-side regression captures.
 # No Npcap is required in replay mode, but the local .venv must exist.
 # Paths are derived from $PSScriptRoot instead of a fixed drive letter.
param(
    [Parameter(Mandatory = $true)]
    [string]$Pcap,

    [Parameter(Mandatory = $true)]
    [string]$OutputRoot,

    [int]$ExpectedRows = 480,

    [string]$ImagesRoot = '',

    [string]$PythonExe =
        (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
)

$ErrorActionPreference = 'Stop'
$receiverRoot = $PSScriptRoot
Push-Location $receiverRoot
try {
    $receiverArgs = @(
        '-m', 'taxi_receiver.cli',
        '--replay-pcap', $Pcap,
        '--mode', 'camera',
        '--max-stage', 'reassemble',
        '--expected-rows', $ExpectedRows,
        '--output-root', $OutputRoot
    )
    if (-not [string]::IsNullOrWhiteSpace($ImagesRoot)) {
        $receiverArgs += @('--images-root', $ImagesRoot)
    }
    & $PythonExe @receiverArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
