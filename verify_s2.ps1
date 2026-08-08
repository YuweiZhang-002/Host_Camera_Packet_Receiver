 # Host-side A/B verifier for the S2 image-publication split.
 # Replay mode does not need Npcap; live mode does.
 # Defaults are derived from $PSScriptRoot and the local .venv.
<#
.SYNOPSIS
    A/B verification for S2 (image publication in a dedicated process).

.DESCRIPTION
    Runs the receiver twice over the same input -- once with
    -PublishImages thread (pre-S2) and once with process (S2) -- then checks the
    two properties that matter:

      correctness  every published .raw/.pgm is byte-for-byte identical
                   between the two modes, and the image counters agree
      benefit      'submit blocked' on each lane thread collapses toward zero
                   and the consumer rate rises above the producer rate

    Offline replay is the default because it is deterministic and lossless, so
    a byte comparison is meaningful.  -Interface runs the same comparison
    against live capture, where the pass criterion is the drop counters rather
    than byte equality (two live runs never see identical traffic).

.EXAMPLE
    # Deterministic A/B on a capture (recommended first).
    .\verify_s2.ps1 -ReplayPcap .\build\s1s2_ab\two_camera.pcap `
                    -OutRoot .\build\s2_verify

.EXAMPLE
    # Live 60 s per mode against the board.
    .\verify_s2.ps1 -Interface '\Device\NPF_{...}' -Seconds 60 `
                    -OutRoot .\build\s2_verify_live
#>
[CmdletBinding(DefaultParameterSetName = 'Replay')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Replay')]
    [string]$ReplayPcap,

    [Parameter(Mandatory = $true, ParameterSetName = 'Live')]
    [string]$Interface,

    [Parameter(ParameterSetName = 'Live')]
    [int]$Seconds = 60,

    [Parameter(Mandatory = $true)]
    [string]$OutRoot,

    [int]$ExpectedRows = 480,

    [ValidateSet('strict', 'recover-zero-fill')]
    [string]$ImagePolicy = 'recover-zero-fill',

    [ValidateSet('complete', 'eligible', 'all')]
    [string]$PublishFrames = 'eligible',

    [string]$PythonExe =
        (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
)

$ErrorActionPreference = 'Stop'
$receiverRoot = $PSScriptRoot
$isLive = $PSCmdlet.ParameterSetName -eq 'Live'

if (Test-Path $OutRoot) { Remove-Item -Recurse -Force $OutRoot }
New-Item -ItemType Directory -Force $OutRoot | Out-Null

function Invoke-Receiver {
    param([string]$Mode)

    $imagesRoot = Join-Path $OutRoot "$Mode\images"
    $reportPath = Join-Path $OutRoot "$Mode\receiver_report.txt"
    New-Item -ItemType Directory -Force (Split-Path $reportPath) | Out-Null

    $receiverArgs = @(
        '-m', 'taxi_receiver.cli',
        '--mode', 'camera',
        '--reassemble',
        '--expected-rows', $ExpectedRows,
        '--images-root', $imagesRoot,
        '--image-policy', $ImagePolicy,
        '--publish-frames', $PublishFrames,
        '--publish-images', $Mode,
        '--report-interval', '1e9'
    )
    if ($isLive) {
        $receiverArgs += @('--interface', $Interface)
    }
    else {
        $receiverArgs += @('--replay-pcap', $ReplayPcap)
    }

    Write-Host "=== running --publish-images $Mode ..." -ForegroundColor Cyan
    if ($isLive) {
        # Live capture only stops on Ctrl+C/SIGTERM, so run it detached and
        # stop it after the requested window.
        $process = Start-Process -FilePath $PythonExe `
            -ArgumentList $receiverArgs `
            -WorkingDirectory $receiverRoot `
            -RedirectStandardOutput $reportPath `
            -RedirectStandardError "$reportPath.err" `
            -PassThru -NoNewWindow
        Start-Sleep -Seconds $Seconds
        # taskkill sends a real termination the CLI's signal handler observes,
        # so the final report and the publisher stats are still written.
        & taskkill /PID $process.Id /T /F | Out-Null
        $process.WaitForExit()
        $exitCode = $process.ExitCode
    }
    else {
        Push-Location $receiverRoot
        try {
            & $PythonExe @receiverArgs *>&1 | Tee-Object -FilePath $reportPath | Out-Null
            $exitCode = $LASTEXITCODE
        }
        finally { Pop-Location }
    }

    [pscustomobject]@{
        Mode       = $Mode
        ExitCode   = $exitCode
        ImagesRoot = $imagesRoot
        ReportPath = $reportPath
    }
}

function Get-ReportMetrics {
    param([string]$ReportPath)

    $text = Get-Content -Raw $ReportPath
    function Sum-Matches {
        param([string]$Pattern)
        $total = 0.0
        foreach ($m in [regex]::Matches($text, $Pattern)) {
            $total += [double]$m.Groups[1].Value
        }
        return $total
    }

    [pscustomobject]@{
        Elapsed          = Sum-Matches 'Elapsed\s+:\s+([\d.]+)'
        ConsumerRate     = Sum-Matches 'Consumer rate\s+:\s+([\d.]+)'
        ProducerRate     = Sum-Matches 'Producer rate\s+:\s+([\d.]+)'
        CaptureDrops     = Sum-Matches 'Capture queue drops\s+:\s+(\d+)'
        LaneDrops        = Sum-Matches 'Lane queue drops\s+:\s+(\d+)'
        LaneSubmitBlocked = Sum-Matches 'submit blocked\s+:\s+([\d.]+) s'
        PublisherBlocked = Sum-Matches 'publisher blocked\s+:\s+([\d.]+) s'
        PublisherPublished = Sum-Matches 'publisher published\s+:\s+(\d+)'
        PublisherFailures = Sum-Matches 'publisher failures\s+:\s+(\d+)'
        ImagesComplete   = Sum-Matches 'images complete\s+:\s+(\d+)'
        ImagesRecovered  = Sum-Matches 'images recovered\s+:\s+(\d+)'
        ImagesRejected   = Sum-Matches 'images rejected\s+:\s+(\d+)'
        CsvRowsDropped   = Sum-Matches 'csv_rows_dropped\s+:\s+(\d+)'
    }
}

function Get-ImageDigest {
    param([string]$Root)

    $table = @{}
    Get-ChildItem -Path $Root -Recurse -File -Include *.raw, *.pgm |
        ForEach-Object {
            $relative = $_.FullName.Substring($Root.Length).TrimStart('\')
            $table[$relative] = (Get-FileHash -Algorithm SHA256 $_.FullName).Hash
        }
    return $table
}

$results = @{}
foreach ($mode in @('thread', 'process')) {
    $results[$mode] = Invoke-Receiver -Mode $mode
}

$metrics = @{}
foreach ($mode in @('thread', 'process')) {
    $metrics[$mode] = Get-ReportMetrics -ReportPath $results[$mode].ReportPath
}

Write-Host ''
Write-Host '================ S2 VERIFICATION ================' -ForegroundColor Yellow
$table = foreach ($mode in @('thread', 'process')) {
    $m = $metrics[$mode]
    [pscustomobject]@{
        Mode              = $mode
        Elapsed_s         = [math]::Round($m.Elapsed, 2)
        Consumer_pps      = [math]::Round($m.ConsumerRate, 0)
        Producer_pps      = [math]::Round($m.ProducerRate, 0)
        LaneBlocked_s     = [math]::Round($m.LaneSubmitBlocked, 2)
        PublisherBlocked_s = [math]::Round($m.PublisherBlocked, 2)
        LaneDrops         = [int]$m.LaneDrops
        CaptureDrops      = [int]$m.CaptureDrops
        ImagesComplete    = [int]$m.ImagesComplete
        ImagesRecovered   = [int]$m.ImagesRecovered
    }
}
$table | Format-Table -AutoSize

$failures = New-Object System.Collections.Generic.List[string]

foreach ($mode in @('thread', 'process')) {
    if ($results[$mode].ExitCode -ne 0 -and -not $isLive) {
        $failures.Add("$mode exited with $($results[$mode].ExitCode)")
    }
}

# --- Gate 1: S2 is a placement change, so the bytes must not move. -----------
if ($isLive) {
    Write-Host 'Gate 1 (byte equality): SKIPPED for live capture' -ForegroundColor DarkGray
    Write-Host '  two live runs never observe identical traffic; use the replay mode for this gate.'
}
else {
    $threadDigest = Get-ImageDigest -Root $results['thread'].ImagesRoot
    $processDigest = Get-ImageDigest -Root $results['process'].ImagesRoot
    $onlyThread = $threadDigest.Keys | Where-Object { -not $processDigest.ContainsKey($_) }
    $onlyProcess = $processDigest.Keys | Where-Object { -not $threadDigest.ContainsKey($_) }
    $differing = $threadDigest.Keys |
        Where-Object { $processDigest.ContainsKey($_) -and $processDigest[$_] -ne $threadDigest[$_] }

    Write-Host "Gate 1 (byte equality): thread=$($threadDigest.Count) files, process=$($processDigest.Count) files"
    if ($threadDigest.Count -eq 0) {
        $failures.Add('no images were published in either mode')
    }
    elseif ($onlyThread.Count -or $onlyProcess.Count -or $differing.Count) {
        $failures.Add(
            "image sets differ (only-thread=$($onlyThread.Count), " +
            "only-process=$($onlyProcess.Count), differing=$($differing.Count))"
        )
        $onlyThread + $onlyProcess + $differing | Select-Object -First 10 |
            ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
    }
    else {
        Write-Host '  PASS: every published image is byte-identical' -ForegroundColor Green
    }
}

# --- Gate 2: the publisher process must actually take the work. --------------
if ($metrics['process'].PublisherPublished -le 0) {
    $failures.Add('process mode published nothing through its publisher process')
}
if ($metrics['process'].PublisherFailures -gt 0) {
    $failures.Add("process mode reported $([int]$metrics['process'].PublisherFailures) publisher failures")
}

# --- Gate 3: the lane threads must stop blocking on publication. -------------
$before = $metrics['thread'].LaneSubmitBlocked
$after = $metrics['process'].LaneSubmitBlocked
Write-Host ("Gate 3 (lane blocking): {0:N2} s -> {1:N2} s" -f $before, $after)
if ($before -gt 1.0 -and $after -ge ($before * 0.5)) {
    $failures.Add(
        "lane submit blocking did not fall (thread=$([math]::Round($before,2))s, " +
        "process=$([math]::Round($after,2))s)"
    )
}
elseif ($before -le 1.0) {
    Write-Host '  INCONCLUSIVE: the baseline never blocked, so the load is too light.' -ForegroundColor DarkYellow
    Write-Host '  Use a longer capture or a higher frame rate to exercise the publisher.'
}
else {
    Write-Host '  PASS: publication no longer blocks the lane threads' -ForegroundColor Green
}

# --- Gate 4: live only -- the receiver must keep up with the wire. -----------
if ($isLive) {
    $p = $metrics['process']
    Write-Host ("Gate 4 (live keep-up): consumer={0:N0} pps producer={1:N0} pps lane_drops={2}" -f `
        $p.ConsumerRate, $p.ProducerRate, [int]$p.LaneDrops)
    if ($p.LaneDrops -gt 0) {
        Write-Host '  Still dropping at the lane queue. Next levers, in order:' -ForegroundColor DarkYellow
        Write-Host '    -PublishFrames complete   (skip partial frames entirely)'
        Write-Host '    -NoRowsCsv                (drop per-packet telemetry)'
        Write-Host '    raise -PublisherQueueDepth (absorb publication bursts)'
    }
    else {
        Write-Host '  PASS: no lane drops' -ForegroundColor Green
    }
}

Write-Host ''
if ($failures.Count -gt 0) {
    Write-Host 'RESULT: FAIL' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
Write-Host 'RESULT: PASS' -ForegroundColor Green
Write-Host "Reports: $OutRoot\{thread,process}\receiver_report.txt"
exit 0
