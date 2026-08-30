 # Host-side folder monitor for published images and archives.
 # No Npcap or elevation is required.
 # Paths are derived from $PSScriptRoot instead of a fixed drive letter.
param(
    [Parameter(Mandatory = $true)]
    [string]$ImagesRoot = '',

    [string]$OutputRoot = '',

    [int]$IntervalSeconds = 2
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if ([string]::IsNullOrWhiteSpace($ImagesRoot)) {
    $ImagesRoot = Join-Path $repoRoot 'images'
}
$resolvedImages = [IO.Path]::GetFullPath($ImagesRoot)
$resolvedOutput = if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $null
} else {
    [IO.Path]::GetFullPath($OutputRoot)
}

Write-Host "ImagesRoot = $resolvedImages"
if ($resolvedOutput) {
    Write-Host "OutputRoot = $resolvedOutput"
}
Write-Host 'Press Ctrl+C to stop.'

while ($true) {
    $pgm = @(
        Get-ChildItem -LiteralPath $resolvedImages -Recurse -File `
            -Filter *.pgm -ErrorAction SilentlyContinue
    ).Count
    $imageRaw = @(
        Get-ChildItem -LiteralPath $resolvedImages -Recurse -File `
            -Filter *.raw -ErrorAction SilentlyContinue
    ).Count
    $archiveRaw = if ($resolvedOutput) {
        @(
            Get-ChildItem -LiteralPath $resolvedOutput -Recurse -File `
                -Filter *.raw -ErrorAction SilentlyContinue
        ).Count
    } else {
        0
    }

    Write-Host (
        "$(Get-Date -Format HH:mm:ss) " +
        "PGM=$pgm image_RAW=$imageRaw archive_RAW=$archiveRaw"
    )
    Start-Sleep -Seconds $IntervalSeconds
}
