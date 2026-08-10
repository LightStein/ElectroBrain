# ctl.ps1 - docker-compose-style control for the standards gateway.
#
#   ctl.ps1 status                    real health: process + heartbeat + probes
#   ctl.ps1 start|stop|restart [svc]  svc = bot | bridge | ollama | all
#   ctl.ps1 heal                      restart only what is actually unhealthy
#   ctl.ps1 restart-when-idle         drained restart: waits for busy=false
#   ctl.ps1 logs [bot|bridge|ollama]  follow the service log
#   ctl.ps1 ask "<question>"          one-shot engine test, bypassing Telegram
#
# Exit code from `status` and `heal` is 0 only when everything is healthy, so
# the scheduled self-heal task can act on it.
#
# WHY THIS DOES NOT TRUST Get-Service:
# Windows reports a service as Running whenever its supervisor holds the slot.
# NSSM is that supervisor. If the nssm.exe wrapper is killed - which is exactly
# what a hung `nssm restart` tempts you into doing - the SCM keeps saying
# Running while the real process is gone or unsupervised, and AppExit=Restart
# never fires because nothing is left to fire it. That state cost 2.5h of
# silent downtime once. Every check below looks for the actual worker process,
# and `stop` reaps orphans so a later `start` cannot end up with two of them.
#
# WHY sc.exe AND NOT nssm:
# `nssm restart` hung here and left services wedged in StopPending. sc.exe
# talks to the SCM directly and returns; it is also what recovered the wedge.

param(
    [Parameter(Position = 0)][string]$Cmd = "status",
    [Parameter(Position = 1)][string]$Arg = ""
)

$ErrorActionPreference = "SilentlyContinue"
$Root    = "C:\Standards"
$Gateway = Join-Path $Root "gateway"
$LogDir  = Join-Path $Root "logs"
$Pipe    = "\\.\pipe\standards-bridge"
$Heartbeat = Join-Path $Gateway "run\bot.alive"
# The bot refreshes its stamp every 30s while polling cleanly. Three minutes is
# six missed beats: long enough that a slow disk or a GC pause never trips it,
# short enough that George is not staring at a dead bot for long.
$HeartbeatMaxAgeSec = 180

# How to recognise the real worker behind each service. Matching on the command
# line rather than the image name is what tells bot.js and bridge-server.js
# apart - they are both plain node.exe.
$AppSpec = [ordered]@{
    "standards-ollama" = @{ Proc = "ollama.exe"; Match = "" }
    "standards-bridge" = @{ Proc = "node.exe";   Match = "bridge-server.js" }
    "standards-bot"    = @{ Proc = "node.exe";   Match = "bot.js" }
}

function Svc-List {
    param($Which)
    switch ($Which) {
        "bot"    { return @("standards-bot") }
        "bridge" { return @("standards-bridge") }
        "ollama" { return @("standards-ollama") }
    }
    # ollama first: the bridge is useless without an engine behind it, and the
    # bot is useless without the bridge.
    return @("standards-ollama", "standards-bridge", "standards-bot")
}

function Find-AppProcess {
    param([string]$Name)
    $spec = $AppSpec[$Name]
    if (-not $spec) { return @() }
    $procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq $spec.Proc }
    if ($spec.Match) {
        $procs = $procs | Where-Object { $_.CommandLine -and $_.CommandLine -like ("*" + $spec.Match + "*") }
    }
    return @($procs)
}

function Heartbeat-AgeSec {
    $f = Get-Item $Heartbeat -ErrorAction SilentlyContinue
    if (-not $f) { return $null }
    return [int]((Get-Date) - $f.LastWriteTime).TotalSeconds
}

function Bridge-Health {
    $out = & node (Join-Path $Gateway "healthcheck.js") $Pipe 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) { return ($out | ConvertFrom-Json) }
    return $null
}

function Ollama-Version {
    try { return (Invoke-RestMethod "http://127.0.0.1:11434/api/version" -TimeoutSec 3).version }
    catch { return $null }
}

# One verdict per service: OK / STOPPED / ZOMBIE / DEAF / NOT INSTALLED.
# ZOMBIE is the one that matters - it is the state that reads as healthy
# everywhere else in Windows.
function Get-SvcState {
    param([string]$Name)
    $svc = Get-Service $Name -ErrorAction SilentlyContinue
    $procs = Find-AppProcess $Name
    $r = [pscustomobject]@{
        Name    = $Name
        Service = $(if ($svc) { [string]$svc.Status } else { "absent" })
        Procs   = $procs.Count
        Pid     = $(if ($procs.Count -ge 1) { $procs[0].ProcessId } else { 0 })
        Verdict = "OK"
        Note    = ""
    }
    if (-not $svc)                    { $r.Verdict = "NOT INSTALLED"; return $r }
    if ($r.Service -ne "Running")     { $r.Verdict = "STOPPED"; $r.Note = "service is $($r.Service)"; return $r }
    if ($procs.Count -eq 0) {
        $r.Verdict = "ZOMBIE"
        $r.Note = "service says Running but no worker process exists"
        return $r
    }
    if ($procs.Count -gt 1) {
        # Two bots means two getUpdates loops: 409 storms and duplicate replies.
        $r.Verdict = "DUPLICATE"
        $r.Note = "$($procs.Count) worker processes; expected 1"
        return $r
    }
    if ($Name -eq "standards-bot") {
        $age = Heartbeat-AgeSec
        if ($age -eq $null) {
            $r.Verdict = "DEAF"
            $r.Note = "no heartbeat file; bot is running but not polling"
        } elseif ($age -gt $HeartbeatMaxAgeSec) {
            $r.Verdict = "DEAF"
            $r.Note = "heartbeat is ${age}s old (max $HeartbeatMaxAgeSec); alive but not answering Telegram"
        }
    }
    return $r
}

function Svc-Stop {
    param([string]$Name, [switch]$Quiet)
    & sc.exe stop $Name 2>&1 | Out-Null
    for ($i = 0; $i -lt 30; $i++) {
        $s = Get-Service $Name -ErrorAction SilentlyContinue
        if (-not $s -or $s.Status -eq "Stopped") { break }
        Start-Sleep -Seconds 1
    }
    # Reap anything the supervisor left behind. When the wrapper was already
    # dead, the SCM happily reports Stopped while the worker keeps running -
    # and starting the service again would then leave two of them.
    $orphans = Find-AppProcess $Name
    foreach ($p in $orphans) {
        if (-not $Quiet) { Write-Host ("  reaping orphaned pid {0}" -f $p.ProcessId) }
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($orphans.Count) { Start-Sleep -Seconds 2 }
    if (-not $Quiet) { Write-Host "stopped $Name" }
}

function Svc-Start {
    param([string]$Name, [switch]$Quiet)
    & sc.exe start $Name 2>&1 | Out-Null
    # Wait for the worker, not the service: the service reports Running as soon
    # as the supervisor is up, which is the exact lie this script exists to
    # avoid repeating.
    for ($i = 0; $i -lt 30; $i++) {
        if ((Find-AppProcess $Name).Count -ge 1) { break }
        Start-Sleep -Seconds 1
    }
    $st = Get-SvcState $Name
    if (-not $Quiet) {
        if ($st.Verdict -eq "OK" -or $st.Verdict -eq "DEAF") {
            Write-Host ("started {0} (pid {1})" -f $Name, $st.Pid)
        } else {
            Write-Host ("FAILED to start {0}: {1} {2}" -f $Name, $st.Verdict, $st.Note)
        }
    }
}

function Svc-Restart {
    param([string]$Name)
    Svc-Stop $Name
    Svc-Start $Name
}

function Show-Status {
    $bad = 0
    foreach ($name in $AppSpec.Keys) {
        $st = Get-SvcState $name
        if ($st.Verdict -ne "OK") { $bad++ }
        $detail = "service=$($st.Service)"
        if ($st.Pid) { $detail += " pid=$($st.Pid)" }
        if ($st.Note) { $detail += "  <- $($st.Note)" }
        Write-Host ("{0,-18} {1,-14} {2}" -f $st.Name, $st.Verdict, $detail)
    }
    $age = Heartbeat-AgeSec
    Write-Host ("bot heartbeat      {0}" -f $(if ($age -eq $null) { "MISSING" } else { "${age}s ago" }))

    $h = Bridge-Health
    if ($h) {
        Write-Host ("bridge /health     busy={0} queue={1} messages={2}" -f $h.busy, $h.queueDepth, $h.messageCount)
    } else {
        Write-Host "bridge /health     UNREACHABLE"
        $bad++
    }
    $v = Ollama-Version
    if ($v) { Write-Host "ollama             ok (v$v)" } else { Write-Host "ollama             UNREACHABLE"; $bad++ }
    return $bad
}

switch ($Cmd) {
    "status" {
        $bad = Show-Status
        if ($bad) { exit 1 }
    }
    "start"   { foreach ($s in (Svc-List $Arg)) { Svc-Start $s } }
    "stop"    { foreach ($s in (Svc-List $Arg)) { Svc-Stop  $s } }
    "restart" { foreach ($s in (Svc-List $Arg)) { Svc-Restart $s } }
    "heal" {
        # Restart only what is broken, in dependency order. Meant to run
        # unattended from a scheduled task, so it stays quiet when all is well.
        $fixed = @()
        foreach ($name in $AppSpec.Keys) {
            $st = Get-SvcState $name
            if ($st.Verdict -eq "NOT INSTALLED") { continue }
            if ($st.Verdict -ne "OK") {
                Write-Host ("[heal] {0} is {1} ({2}) - restarting" -f $name, $st.Verdict, $st.Note)
                Svc-Restart $name
                $fixed += $name
            }
        }
        # A bridge that answers no longer proves much on its own if the bot in
        # front of it was just replaced; probing after the fact is what catches
        # a restart that came back up broken.
        if (-not (Bridge-Health)) {
            Write-Host "[heal] bridge unreachable after checks - restarting bridge"
            Svc-Restart "standards-bridge"
            $fixed += "standards-bridge"
        }
        if ($fixed.Count -eq 0) { exit 0 }
        Write-Host ("[heal] restarted: {0}" -f ($fixed -join ", "))
        $bad = Show-Status
        if ($bad) { exit 1 }
    }
    "restart-when-idle" {
        # Never restart a bridge mid-turn: it kills the in-flight engine and the
        # user's status message hangs forever. Wait for idle, then restart.
        Write-Host "waiting for bridge to go idle..."
        while ($true) {
            $h = Bridge-Health
            if (-not $h) { Write-Host "bridge unreachable - restarting anyway"; break }
            if (-not $h.busy -and $h.queueDepth -eq 0) { break }
            Write-Host ("  busy={0} queue={1}; rechecking in 15s" -f $h.busy, $h.queueDepth)
            Start-Sleep -Seconds 15
        }
        foreach ($s in (Svc-List "all")) { Svc-Restart $s }
    }
    "logs" {
        $which = if ($Arg) { $Arg } else { "bot" }
        $file = Join-Path $LogDir ("standards-{0}.log" -f $which)
        Get-Content $file -Tail 50 -Wait
    }
    "ask" {
        if (-not $Arg) { Write-Host 'usage: ctl.ps1 ask "<question>"'; exit 1 }
        $env:STANDARDS_ROOT = $Root
        $env:PYTHONIOENCODING = "utf-8"
        $env:PYTHONUTF8 = "1"
        Push-Location (Join-Path $Root "bot")
        & python ask.py -p $Arg
        Pop-Location
    }
    default {
        Write-Host "commands: status | start | stop | restart [bot|bridge|ollama|all] | heal | restart-when-idle | logs [bot|bridge|ollama] | ask `"...`""
    }
}
