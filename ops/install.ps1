# install.ps1 - register the standards gateway as Windows services via NSSM.
#
# Keep this file PURE ASCII. Windows PowerShell 5.1 decodes .ps1 with the system
# ANSI codepage (CP1251 here) unless there is a UTF-8 BOM, and byte 0x94 - which
# appears inside the UTF-8 bytes of an em-dash and of box-drawing characters -
# becomes a smart quote that PowerShell treats as a string delimiter. Russian
# user-facing strings belong in bot.js / ask.py, which Node and Python read as
# UTF-8 unconditionally.
# Run as Administrator, once, after files are in place under C:\Standards.
#
# Prereqs (Phase 1 of PLAN.md): node in PATH, python in PATH, nssm in PATH
#   (winget install nssm  /  choco install nssm)
#
# Services created:
#   standards-bridge  node bridge-server.js  (spawns ask.py per turn)
#   standards-bot     node bot.js            (Telegram polling)
# Both: auto-start at boot, auto-restart on exit (NSSM AppExit Restart with
# throttle), stdout/stderr to rotating logs under C:\Standards\logs.

$ErrorActionPreference = "Stop"

$Root    = "C:\Standards"
$Gateway = Join-Path $Root "gateway"
$BotDir  = Join-Path $Root "bot"
$RunDir  = Join-Path $Gateway "run"
$LogDir  = Join-Path $Root "logs"
$Node    = (Get-Command node).Source
$Pipe    = "\\.\pipe\standards-bridge"

# Telegram token/user id come from a git-ignored env file, one KEY=VALUE per line.
$SecretsFile = Join-Path $Root "secrets.env"
if (-not (Test-Path $SecretsFile)) {
    Write-Error "Create $SecretsFile first with TELEGRAM_BOT_TOKEN=... and ALLOWED_USER_ID=..."
}
$Secrets = @{}
Get-Content $SecretsFile | Where-Object { $_ -match "=" -and $_ -notmatch "^\s*#" } | ForEach-Object {
    $k, $v = $_ -split "=", 2
    $Secrets[$k.Trim()] = $v.Trim()
}

New-Item -ItemType Directory -Force -Path $RunDir, (Join-Path $RunDir "pending"), $LogDir, (Join-Path $BotDir "uploads") | Out-Null

function Install-Svc {
    param($Name, $AppDir, $Script, $EnvVars)
    & nssm stop $Name 2>$null
    & nssm remove $Name confirm 2>$null
    & nssm install $Name $Node $Script
    & nssm set $Name AppDirectory $AppDir
    & nssm set $Name AppStdout (Join-Path $LogDir "$Name.log")
    & nssm set $Name AppStderr (Join-Path $LogDir "$Name.log")
    & nssm set $Name AppRotateFiles 1
    & nssm set $Name AppRotateOnline 1
    & nssm set $Name AppRotateBytes 10485760          # 10MB per file
    & nssm set $Name AppExit Default Restart          # like docker restart: unless-stopped
    & nssm set $Name AppRestartDelay 3000             # 3s backoff
    & nssm set $Name AppThrottle 10000                # min uptime before "successful start"
    & nssm set $Name Start SERVICE_AUTO_START
    & nssm set $Name AppEnvironmentExtra $EnvVars
}

# ---- bridge --------------------------------------------------------------
$askSpawn = '["python","' + ($BotDir -replace '\\', '/') + '/ask.py","-p","{message}"]'
$bridgeEnv = @(
    "BRIDGE_NAME=standards",
    "BRIDGE_SOCKET=$Pipe",
    "BRIDGE_STATE=$RunDir\standards-state.json",
    "BRIDGE_WORKDIR=$BotDir",
    "BRIDGE_PENDING_DIR=$RunDir\pending",
    "BRIDGE_SPAWN=$askSpawn",
    "BRIDGE_OUTPUT=plain",
    "BRIDGE_ENGINE_STATE=$Root\state\ask-history.json",
    "BRIDGE_MAX_RUNTIME_MS=900000",
    "STANDARDS_ROOT=$Root",
    "OLLAMA_URL=http://127.0.0.1:11434",
    "ASK_MODEL=qwen3:4b"
)
Install-Svc "standards-bridge" $Gateway (Join-Path $Gateway "bridge-server.js") $bridgeEnv

# ---- bot -----------------------------------------------------------------
$botEnv = @(
    "TELEGRAM_BOT_TOKEN=$($Secrets['TELEGRAM_BOT_TOKEN'])",
    "ALLOWED_USER_ID=$($Secrets['ALLOWED_USER_ID'])",
    "REGISTRY_PATH=$Gateway\registry.json",
    "RUN_DIR=$RunDir",
    "UPLOADS_DIR=$BotDir\uploads",
    "CATALOG_PATH=$Root\index\catalog.md"
    # STATUS_TEXT is intentionally NOT set here: its Russian default lives in
    # bot.js, which Node reads as UTF-8. Setting it from an ASCII-only .ps1
    # would force mojibake or reintroduce the codepage trap described above.
)
Install-Svc "standards-bot" $Gateway (Join-Path $Gateway "bot.js") $botEnv

& nssm start standards-bridge
& nssm start standards-bot

Write-Host ""
Write-Host "Installed. Use ops\ctl.ps1 status to check, ctl.ps1 logs bot to tail."
