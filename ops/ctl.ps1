# ctl.ps1 - docker-compose-style control for the standards gateway.
#
#   ctl.ps1 status                    services + bridge /health + ollama
#   ctl.ps1 start|stop|restart [svc]  svc = bot | bridge | all (default all)
#   ctl.ps1 restart-when-idle         drained restart (port of bot-reload.sh):
#                                     waits for busy=false & queueDepth=0 first
#   ctl.ps1 logs [bot|bridge]         follow the service log
#   ctl.ps1 ask "<question>"          one-shot engine test, bypassing Telegram

param(
    [Parameter(Position = 0)][string]$Cmd = "status",
    [Parameter(Position = 1)][string]$Arg = ""
)

$ErrorActionPreference = "SilentlyContinue"
$Root    = "C:\Standards"
$Gateway = Join-Path $Root "gateway"
$LogDir  = Join-Path $Root "logs"
$Pipe    = "\\.\pipe\standards-bridge"
$Services = @{ bot = "standards-bot"; bridge = "standards-bridge"; ollama = "standards-ollama" }

function Svc-List {
    param($Which)
    if ($Which -eq "bot")    { return @("standards-bot") }
    if ($Which -eq "bridge") { return @("standards-bridge") }
    if ($Which -eq "ollama") { return @("standards-ollama") }
    # ollama first: the bridge is useless without an engine behind it
    return @("standards-ollama", "standards-bridge", "standards-bot")
}

function Bridge-Health {
    $out = & node (Join-Path $Gateway "healthcheck.js") $Pipe 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) { return ($out | ConvertFrom-Json) }
    return $null
}

switch ($Cmd) {
    "status" {
        foreach ($name in $Services.Values) {
            $s = Get-Service $name
            $state = if ($s) { $s.Status } else { "NOT INSTALLED" }
            Write-Host ("{0,-18} {1}" -f $name, $state)
        }
        $h = Bridge-Health
        if ($h) {
            Write-Host ("bridge /health     engine={0} busy={1} queue={2} messages={3}" -f `
                $h.engine, $h.busy, $h.queueDepth, $h.messageCount)
        } else {
            Write-Host "bridge /health     UNREACHABLE"
        }
        $ollama = try { (Invoke-RestMethod "http://127.0.0.1:11434/api/version" -TimeoutSec 3).version } catch { $null }
        Write-Host ("ollama             {0}" -f ($(if ($ollama) { "ok (v$ollama)" } else { "UNREACHABLE" })))
    }
    "start" {
        foreach ($s in (Svc-List $Arg)) { & nssm start $s; Write-Host "started $s" }
    }
    "stop" {
        foreach ($s in (Svc-List $Arg)) { & nssm stop $s; Write-Host "stopped $s" }
    }
    "restart" {
        foreach ($s in (Svc-List $Arg)) { & nssm restart $s; Write-Host "restarted $s" }
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
        foreach ($s in (Svc-List "all")) { & nssm restart $s; Write-Host "restarted $s" }
    }
    "logs" {
        $which = if ($Arg) { $Arg } else { "bot" }
        $file = Join-Path $LogDir ("standards-{0}.log" -f $which)
        Get-Content $file -Tail 50 -Wait
    }
    "ask" {
        if (-not $Arg) { Write-Host 'usage: ctl.ps1 ask "<question>"'; exit 1 }
        $env:STANDARDS_ROOT = $Root
        Push-Location (Join-Path $Root "bot")
        & python ask.py -p $Arg
        Pop-Location
    }
    default {
        Write-Host "commands: status | start | stop | restart [bot|bridge|all] | restart-when-idle | logs [bot|bridge] | ask `"...`""
    }
}
