# Getting Claude onto George's laptop

Three parts: Cloudflare dashboard (you), the laptop script (you, once), the SSH
client config (already done on Anri's host).

---

## 1. Cloudflare dashboard

**Zero Trust → Networks → Tunnels → Create a tunnel**

| Field | Value |
|---|---|
| Connector | `cloudflared` |
| Tunnel name | `george-laptop` |

On the "Install and run a connector" page choose **Windows**. It shows a command
like `cloudflared.exe service install eyJhIjoiNGZ...`. **Copy only the long token**
(everything after `service install`) — the script installs the connector itself.

Then **Public Hostnames → Add a public hostname**:

| Field | Value |
|---|---|
| Subdomain | `ssh-george` |
| Domain | `deltaops.net` |
| Type | **SSH** |
| URL | `localhost:22` |

Save. Leave it *without* a Cloudflare Access policy for now — SSH key auth is the
gate, and an Access app would force an interactive browser login on every
connection, which an automated agent can't do. (Hardening option for later:
protect it with an Access policy + service token.)

---

## 2. On the laptop (once, ~5 minutes)

Do this **logged in as George's own Windows account** — the assistant's services
and the Claude Code CLI login have to live under the same user, so whichever
account you use here is the account everything runs as.

1. Right-click Start → **Terminal (Administrator)**
2. `notepad C:\setup-access.ps1` → paste `setup-access.ps1` → save → close
3. Edit one line at the top: `$TunnelToken = "<the token you copied>"`
4. Run it:
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\setup-access.ps1
   ```

The script installs OpenSSH Server, sets PowerShell 7 as the SSH shell, installs
Claude's public key, installs cloudflared as a boot-start Windows service bound
to your tunnel, stops the laptop sleeping while plugged in, closes port 22 to the
local network (the tunnel goes over loopback, so nothing is lost), and prints a
machine inventory.

**Send back:** the `user`, `gpu`, `ram` and `C: free` lines it prints at the end —
the GPU/VRAM line decides whether we run `qwen3:4b` or `qwen3:8b`.

Then check the tunnel shows **HEALTHY** in the Cloudflare dashboard.

---

## 3. Anri's host (done)

`~/.ssh/config` already has:

```
Host ssh-george
    HostName ssh-george.deltaops.net
    User george                       # updated once the script reports the real username
    IdentityFile ~/.ssh/george_laptop
    ProxyCommand /home/anri/cloudflared access ssh --hostname %h
    StrictHostKeyChecking no
    ServerAliveInterval 15
    ServerAliveCountMax 8
    TCPKeepAlive yes
```

Keypair: `~/.ssh/george_laptop` (private, stays here) / `.pub` (already baked into
the script).

Smoke test once the tunnel is up:
```bash
ssh ssh-george "whoami; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ssh` hangs, no prompt | Tunnel not HEALTHY in the dashboard, or laptop asleep/off. |
| `Permission denied (publickey)` | Wrong SSH username — use the `user` line the script printed, not "george" if the profile is named differently. |
| Key ignored for an admin account | `administrators_authorized_keys` ACL. The script sets it via SIDs; verify with `icacls C:\ProgramData\ssh\administrators_authorized_keys` (only Administrators + SYSTEM). |
| `Add-WindowsCapability` fails | Windows Update component broken. Fallback: install OpenSSH from https://github.com/PowerShell/Win32-OpenSSH/releases |
| Connects but commands quote badly | `DefaultShell` didn't take. Check `Get-ItemProperty HKLM:\SOFTWARE\OpenSSH`, then `Restart-Service sshd`. |
| Need LAN SSH back | `Enable-NetFirewallRule -Name "OpenSSH-Server-In-TCP"` |
| `scp` fails | Windows OpenSSH + PowerShell shell breaks legacy scp. Use `sftp` (or git) instead. |

## Notes

- Nothing is port-forwarded; cloudflared dials outbound only.
- Password authentication is left **enabled** during setup so a failed key install
  can't lock anyone out. Once the first key login works, Claude disables it
  (`PasswordAuthentication no` in `C:\ProgramData\ssh\sshd_config`).
- Services (sshd, cloudflared) run before login, so a reboot doesn't need George
  to sign in — but the laptop must be **on**.
