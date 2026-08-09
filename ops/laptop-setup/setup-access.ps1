# setup-access.ps1 - remote-access setup for George's Windows 11 laptop.
#
#   1. OpenSSH Server (PowerShell 7 as the SSH shell)
#   2. Claude's public key installed for this account
#   3. cloudflared installed as a Windows service for your tunnel
#   4. Power settings so the laptop stays reachable
#   5. Prints an inventory (GPU/VRAM, RAM, disk, username)
#
# HOW TO RUN - do NOT copy-paste this file's contents. Download it, so
# the bytes arrive exactly as written:
#
#   [Net.ServicePointManager]::SecurityProtocol = 'Tls12'
#   $u = 'https://raw.githubusercontent.com/LightStein/ElectroBrain' +
#        '/main/ops/laptop-setup/setup-access.ps1.example'
#   Invoke-WebRequest $u -OutFile C:\setup-access.ps1 -UseBasicParsing
#   powershell -ExecutionPolicy Bypass -File C:\setup-access.ps1 `
#              -TunnelToken "<paste token from Cloudflare dashboard>"
#
# Nothing is exposed to the internet: cloudflared dials OUT and reaches
# sshd on localhost only. No router or port-forwarding changes.
#
# TWO HARD RULES FOR EDITING THIS FILE, both learned the hard way:
#
# 1. PURE ASCII. No em-dashes, box-drawing, ellipses or Cyrillic.
#    PowerShell 5.1 decodes .ps1 with the system ANSI codepage (CP1251
#    on a Russian Windows) unless the file has a UTF-8 BOM, and Win11
#    Notepad saves UTF-8 WITHOUT one. Byte 0x94 - inside the UTF-8 bytes
#    of both "-" (E2 80 94) and box-drawing (E2 94 80) - then decodes to
#    a right double quote, which PowerShell treats as a string
#    delimiter, and the file stops parsing.
#
# 2. EVERY LINE UNDER 72 CHARACTERS. A terminal that soft-wraps at 73
#    columns turns into hard newlines when copied, splitting long lines
#    mid-word. That silently corrupted the tunnel token and the SSH key
#    on the first run. ops/check-encoding.sh enforces both rules.

param(
    # Paste from the Cloudflare dashboard. Passing it as an argument
    # keeps the long token out of the file, where a wrapped paste would
    # break it.
    [string]$TunnelToken = ""
)

# Assembled from short pieces so no line here can exceed the 72-char
# limit. Verified against the real key by ops/check-encoding.sh.
$PublicKey = "ssh-ed25519 " +
    "AAAAC3NzaC1lZDI1NTE5AAAAIDCl1bFz2nqR4lOd" +
    "j+VVT6gkjfVikL6ZVTLG6WeyU0E/" +
    " claude@deltaops-george-laptop"

$ErrorActionPreference = "Stop"
function Step($n, $t) {
    Write-Host "`n=== $n. $t" -ForegroundColor Cyan
}
function Ok($t)   { Write-Host "    [ok] $t" -ForegroundColor Green }
function Warn($t) { Write-Host "    [!!] $t" -ForegroundColor Yellow }
function Die($t)  {
    Write-Host "`nFATAL: $t" -ForegroundColor Red
    exit 1
}

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($id)
$adminRole = [Security.Principal.WindowsBuiltInRole]::Administrator
if (-not $principal.IsInRole($adminRole)) {
    Die "Run this from an ADMINISTRATOR terminal."
}

# Integrity checks. A wrapped paste corrupts these silently otherwise:
# the key install "succeeds" and SSH simply never authenticates.
if (-not $TunnelToken) {
    Die "No tunnel token. Re-run with: -TunnelToken ""eyJ..."""
}
if ($TunnelToken -match '\s') {
    Die "Tunnel token contains whitespace - the paste was wrapped."
}
if ($TunnelToken.Length -lt 40) {
    Die "Tunnel token looks truncated ($($TunnelToken.Length) chars)."
}
if ($PublicKey -notmatch '^ssh-ed25519 [A-Za-z0-9+/=]{68} \S+$') {
    Die "Public key is malformed - this file was altered."
}

$SshUser = $env:USERNAME
Write-Host "Setting up remote access for user: $SshUser"

# --- 1. OpenSSH Server ------------------------------------------
Step 1 "OpenSSH Server"
$cap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" |
       Select-Object -First 1
if ($cap.State -ne "Installed") {
    Add-WindowsCapability -Online -Name $cap.Name | Out-Null
    Ok "installed $($cap.Name)"
} else {
    Ok "already installed"
}
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
Ok "sshd running, starts at boot"

# --- 2. PowerShell 7 as the SSH shell ---------------------------
# Windows OpenSSH defaults to cmd.exe. PowerShell 7 gives proper UTF-8
# (this project is full of Russian text) and sane remote quoting.
Step 2 "SSH default shell"
$pwsh = "C:\Program Files\PowerShell\7\pwsh.exe"
if (-not (Test-Path $pwsh)) {
    try {
        Write-Host "    installing PowerShell 7 via winget"
        winget install --id Microsoft.PowerShell --silent `
            --accept-package-agreements --accept-source-agreements `
            --disable-interactivity | Out-Null
    } catch {
        Warn "winget install failed: $($_.Exception.Message)"
    }
}
if (Test-Path $pwsh) {
    $shell = $pwsh
} else {
    $shell = "$env:SystemRoot\System32\WindowsPowerShell" +
             "\v1.0\powershell.exe"
    Warn "PowerShell 7 unavailable, using Windows PowerShell 5.1"
}
# New-Item -Force on an EXISTING registry key wipes its values.
if (-not (Test-Path "HKLM:\SOFTWARE\OpenSSH")) {
    New-Item -Path "HKLM:\SOFTWARE\OpenSSH" | Out-Null
}
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell `
    -Value $shell -PropertyType String -Force | Out-Null
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" `
    -Name DefaultShellCommandOption -Value "-Command" `
    -PropertyType String -Force | Out-Null
Ok "shell = $shell"

# --- 3. Authorized key ------------------------------------------
# Windows sshd quirk: keys for ADMIN accounts must live in
# C:\ProgramData\ssh\administrators_authorized_keys, not the user
# profile. Install into both so it works either way.
#
# Rewrites rather than appends, so fragments left by a corrupted
# earlier run are cleaned out. Any legitimate third-party key is kept.
Step 3 "Public key"

function Set-AuthorizedKey($path) {
    $keep = @()
    if (Test-Path $path) {
        foreach ($line in (Get-Content $path)) {
            $t = $line.Trim()
            if (-not $t) { continue }
            # our key, in any form, including wrapped fragments
            if ($t -like "*claude@deltaops-george-laptop*") { continue }
            # not a key at all
            $keyRe = '^(ssh|ecdsa)-[a-z0-9-]+ [A-Za-z0-9+/=]+'
            if ($t -notmatch $keyRe) { continue }
            # an ed25519 blob is exactly 68 base64 chars; anything else
            # is the front half of a line that got wrapped
            if ($t -match '^ssh-ed25519 (\S+)') {
                if ($matches[1].Length -ne 68) { continue }
            }
            $keep += $line
        }
    }
    $keep += $PublicKey
    # ascii, NOT utf8: PowerShell 5.1 writes a BOM for utf8, and a BOM
    # makes sshd ignore the first key. SSH keys are pure ASCII anyway.
    Set-Content -Path $path -Value $keep -Encoding ascii
    return $keep.Count
}

$userKeys = Join-Path $env:USERPROFILE ".ssh\authorized_keys"
New-Item -ItemType Directory -Force -Path (Split-Path $userKeys) |
    Out-Null
$n = Set-AuthorizedKey $userKeys
Ok "user file: $userKeys ($n key(s))"

$adminKeys = "$env:ProgramData\ssh\administrators_authorized_keys"
$n = Set-AuthorizedKey $adminKeys
# ACL must be Administrators+SYSTEM only or sshd ignores the file.
# SIDs, not names: this laptop runs a non-English Windows.
icacls.exe $adminKeys /inheritance:r /grant "*S-1-5-32-544:F" `
    /grant "*S-1-5-18:F" | Out-Null
Ok "admin file: $adminKeys ($n key(s), ACL locked)"
Restart-Service sshd

# --- 4. cloudflared ---------------------------------------------
Step 4 "cloudflared tunnel service"
$cfDir = "C:\Program Files\cloudflared"
$cfExe = Join-Path $cfDir "cloudflared.exe"
if (-not (Test-Path $cfExe)) {
    New-Item -ItemType Directory -Force -Path $cfDir | Out-Null
    $url = "https://github.com/cloudflare/cloudflared/releases" +
           "/latest/download/cloudflared-windows-amd64.exe"
    Write-Host "    downloading cloudflared"
    [Net.ServicePointManager]::SecurityProtocol = 'Tls12'
    Invoke-WebRequest -Uri $url -OutFile $cfExe -UseBasicParsing
    Ok "downloaded to $cfExe"
} else {
    Ok "already present"
}

# A pre-existing service may be bound to a stale or broken token.
if (Get-Service cloudflared -ErrorAction SilentlyContinue) {
    Warn "removing existing cloudflared service"
    & $cfExe service uninstall 2>&1 | Out-Null
    Start-Sleep -Seconds 2
}
& $cfExe service install $TunnelToken 2>&1 | Out-Null
Start-Sleep -Seconds 3
Set-Service -Name cloudflared -StartupType Automatic
Start-Service cloudflared -ErrorAction SilentlyContinue
$cf = Get-Service cloudflared -ErrorAction SilentlyContinue
if ($cf -and $cf.Status -eq "Running") {
    Ok "cloudflared service running"
} else {
    Warn "not running. Check: Get-Service cloudflared"
}

# --- 5. Stay awake ----------------------------------------------
# A sleeping laptop kills the tunnel. On AC only; battery behaviour
# is left alone.
Step 5 "Power settings (AC only)"
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 20
# SUB_BUTTONS / lid-close-action, 0 = do nothing
$subButtons = "4f971e89-eebd-4455-a8de-9e59040e7347"
$lidAction = "5ca83367-6e45-459f-a27b-476b1d01c936"
powercfg /setacvalueindex SCHEME_CURRENT $subButtons $lidAction 0
powercfg /setactive SCHEME_CURRENT
Ok "plugged in: never sleeps, lid close ignored"

# --- 6. Firewall ------------------------------------------------
# cloudflared reaches sshd over loopback, which Windows Firewall never
# filters, so the inbound rule only exposes port 22 to whatever wifi
# he is on. Re-enable with:
#   Enable-NetFirewallRule -Name "OpenSSH-Server-In-TCP"
Step 6 "Firewall"
Disable-NetFirewallRule -Name "OpenSSH-Server-In-TCP" `
    -ErrorAction SilentlyContinue
Ok "port 22 closed to the local network (tunnel unaffected)"

# --- 7. Inventory -----------------------------------------------
Step 7 "Machine inventory"
$os = Get-CimInstance Win32_OperatingSystem
$cpu = (Get-CimInstance Win32_Processor).Name
$ram = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$disk = Get-PSDrive C | Select-Object -ExpandProperty Free
Write-Host "    user     : $SshUser"
Write-Host "    windows  : $($os.Caption) $($os.Version)"
Write-Host "    cpu      : $cpu"
Write-Host "    ram      : $ram GB"
Write-Host "    C: free  : $([math]::Round($disk/1GB,1)) GB"
try {
    $q = "name,memory.total,driver_version"
    $gpu = & nvidia-smi --query-gpu=$q --format=csv,noheader
    Write-Host "    gpu      : $gpu"
} catch {
    $g = (Get-CimInstance Win32_VideoController |
          Where-Object { $_.Name -match "NVIDIA" }).Name
    Write-Host "    gpu      : $g  (nvidia-smi not in PATH)"
}

Write-Host ""
Write-Host "==========================================="
Write-Host " Done. Send Anri:"
Write-Host "   - the SSH username above -> $SshUser"
Write-Host "   - the gpu / ram / disk lines"
Write-Host " Then check the tunnel is HEALTHY in Cloudflare."
Write-Host "==========================================="
