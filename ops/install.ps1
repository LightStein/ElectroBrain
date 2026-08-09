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

# Absolute interpreter path. NSSM services run as LocalSystem, whose PATH does
# not include a per-user Python install - and on this machine a bare "python"
# resolves to the Microsoft Store stub, which is not an interpreter at all.
$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python -or $Python -like "*WindowsApps*") {
    $Python = (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Filter python.exe `
               -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
}
if (-not $Python) { Write-Error "Could not find a real python.exe" }

# Claude Code stores its login per user, so a LocalSystem service sees "Not
# logged in" and /pro escalation dies silently. CLAUDE_CONFIG_DIR points the
# service at the interactive user's own config directory, which keeps ONE live
# credential store: copying .credentials.json instead would let the two refresh
# OAuth tokens independently and invalidate each other. The directory needs a
# .claude.json inside it (the CLI keeps its own at ~/.claude.json).
$ClaudeCfg = "$env:USERPROFILE\.claude"
if ((Test-Path "$env:USERPROFILE\.claude.json") -and
    -not (Test-Path "$ClaudeCfg\.claude.json")) {
    Copy-Item "$env:USERPROFILE\.claude.json" "$ClaudeCfg\.claude.json"
}

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

# Generate registry.json from secrets.env so there is ONE file to fill in.
# Hand-editing a chat id into JSON in a second place is exactly the kind of
# step that gets forgotten, and the failure mode is silent: the bot starts
# fine and ignores every message from the unlisted chat.
$ChatId = $Secrets['TELEGRAM_CHAT_ID']
if ($ChatId) {
    $registry = @{
        projects = @(@{
            name   = "standards"
            chatId = [int64]$ChatId
            socket = $Pipe
            upload = (Join-Path $BotDir "uploads")
        })
    } | ConvertTo-Json -Depth 5
    Set-Content (Join-Path $Gateway "registry.json") -Value $registry -Encoding utf8
    Write-Host "registry.json written for chat $ChatId"
} else {
    Write-Host "WARNING: no TELEGRAM_CHAT_ID in secrets.env - registry.json left as-is"
}

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

# ---- ollama --------------------------------------------------------------
# Ollama ships only as a per-user tray app started from Startup, so after an
# unattended reboot (Windows update, power cut) the bridge service comes up
# with no engine behind it and every question fails. Register it as a real
# service so it is up before anyone logs in.
#
# Two settings must be carried explicitly: OLLAMA_MODELS is a USER variable
# here (models live on D:), invisible to a LocalSystem service, which would
# otherwise find no models at all. OLLAMA_HOST binds loopback only - the
# engine has no business being reachable from the wifi.
$Ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $Ollama) {
    $Ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
}
if (Test-Path $Ollama) {
    $models = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS","User")
    if (-not $models) { $models = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS","Machine") }
    $ollamaEnv = @(
        "OLLAMA_HOST=127.0.0.1:11434",
        # Keep the model resident: default is a 5-minute idle unload, which
        # makes the first question after a quiet spell pay a cold load.
        # Nothing else on this machine wants the GPU.
        "OLLAMA_KEEP_ALIVE=-1"
    )
    if ($models) { $ollamaEnv += "OLLAMA_MODELS=$models" }

    & nssm stop standards-ollama 2>$null
    & nssm remove standards-ollama confirm 2>$null
    & nssm install standards-ollama $Ollama serve
    & nssm set standards-ollama AppStdout (Join-Path $LogDir "standards-ollama.log")
    & nssm set standards-ollama AppStderr (Join-Path $LogDir "standards-ollama.log")
    & nssm set standards-ollama AppRotateFiles 1
    & nssm set standards-ollama AppRotateOnline 1
    & nssm set standards-ollama AppRotateBytes 10485760
    & nssm set standards-ollama AppExit Default Restart
    & nssm set standards-ollama AppRestartDelay 3000
    & nssm set standards-ollama Start SERVICE_AUTO_START
    & nssm set standards-ollama AppEnvironmentExtra $ollamaEnv

    # Stop the tray app from starting a SECOND server that fights for 11434.
    $startup = [Environment]::GetFolderPath("Startup")
    Get-ChildItem $startup -Filter "*llama*" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Move-Item $_.FullName "$($_.FullName).disabled-by-electrobrain" -Force
            Write-Host "disabled Startup entry: $($_.Name)"
        }
    & nssm start standards-ollama
} else {
    Write-Host "WARNING: ollama.exe not found - install it, then re-run this script"
}

# ---- bridge --------------------------------------------------------------
$askSpawn = '["' + ($Python -replace '\\', '/') + '","' +
            ($BotDir -replace '\\', '/') + '/ask.py","-p","{message}"]'
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
    "STANDARDS_RAW=D:\LLM_FILES",
    "OLLAMA_URL=http://127.0.0.1:11434",
    "ASK_MODEL=qwen3:4b-instruct",
    "CLAUDE_CONFIG_DIR=$ClaudeCfg",
    # Without these Python writes stdout in the Windows ANSI codepage while
    # node decodes the pipe as UTF-8, so every Russian answer reached Telegram
    # as "?????". It never showed up in testing because the manual test
    # commands all set PYTHONIOENCODING by hand - the service did not.
    "PYTHONIOENCODING=utf-8",
    "PYTHONUTF8=1"
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
