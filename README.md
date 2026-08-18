# ElectroBrain

A Telegram assistant that answers questions about electrical and fire-safety
standards from a private corpus of ~160 PDF/DOCX documents (Russian + English),
running entirely on one Windows laptop.

Built for George, an electrical revisor who currently spends 4–5 hours hunting
through standards for a single answer.

```
Telegram ──► bot.js ──► bridge-server.js ──► ask.py ──► Ollama (qwen2.5:7b)
                                               │
                                               ├─ index/catalog.md      (which docs?)
                                               ├─ index/docs/*/full.md  (grep + rank)
                                               └─ claude -p haiku       (escalation)
```

**Every answer cites its source** — document title, clause number, and a verbatim
quote. For a revisor an uncited answer is a liability, so both engine prompts
enforce it and refuse rather than guess.

## How it works

**Preprocessing** (one time, plus one click per document change). Free tools do
the heavy lifting: pymupdf classifies each PDF as digital or scanned, OCRmyPDF
(Tesseract `rus+eng`) handles the scans, pandoc handles DOCX. Claude Code CLI
then does cheap text-in/text-out cleanup — clean Markdown with clause numbering
preserved, plus a metadata file with Russian *and* English keywords, which is
what makes a Russian question find an English document.

**Answering** (every message, local). Three stages: qwen picks candidate
documents from a ~160-line catalog and generates bilingual search terms; a
lexical pass scores heading-delimited chunks of just those documents; qwen
answers from the top chunks with mandatory citations. Hard questions, or any
message with an image, escalate to `claude -p`.

**Delivery.** The Telegram gateway is a port of a battle-tested Linux/Docker
bridge (see `gateway/`) to native Windows services under NSSM — durable reply
handoff, drained restarts, and three watchdog layers against long-poll wedges,
all carried over from lessons that cost real outages in the original.

## Layout

| Path | What |
|---|---|
| `bot/ask.py` | The answering engine — routing, retrieval, citation, escalation |
| `gateway/` | Telegram bot + per-project bridge (named pipes on Windows) |
| `pipeline/update.py` | Scan → extract → OCR → cleanup, manifest-driven and resumable |
| `ops/install.ps1` | Registers the NSSM services and the `standards-heal` task |
| `ops/correctness.py` | Asserts facts: is the answer TRUE? A WRONG verdict exits non-zero |
| `ops/eval.py` | Retrieval/citation regression harness — does NOT judge truth |
| `ops/ctl.ps1` | `status` / `start` / `stop` / `restart` / `heal` / `restart-when-idle` / `logs` / `ask` |
| `ops/laptop-setup/` | One-shot SSH + Cloudflare tunnel setup for the laptop |
| `Update-Standards.bat` | George's single button after adding or removing a document |
| `PLAN.md` | Full design, decisions, test results, remaining phases |

Deploys to `C:\Standards\` on the laptop; `index\` doubles as an Obsidian vault
so the corpus stays browsable by hand.

### Health, and why `Get-Service` is not it

Windows reports a service as Running whenever its supervisor holds the slot.
NSSM is that supervisor, so if the `nssm.exe` wrapper dies — which a hung
`nssm restart` invites — the SCM keeps saying Running while the worker is gone,
and `AppExit=Restart` never fires because nothing is left to fire it. That state
once cost 2.5h of silence with all three services showing green.

`ctl.ps1 status` therefore checks the **worker process** and, for the bot, a
heartbeat file it rewrites every 30s only while polling cleanly. Verdicts:

| | |
|---|---|
| `OK` | worker present, and for the bot a fresh heartbeat |
| `ZOMBIE` | service Running, no worker — the silent-downtime state |
| `DUPLICATE` | two workers; for the bot that means duelling `getUpdates` loops |
| `DEAF` | bot alive but heartbeat stale: not reaching Telegram |
| `STARTING` / `STOPPING` | mid-transition, not a failure |

`ctl.ps1 heal` restarts only what is broken, and the `standards-heal` scheduled
task runs it every 5 minutes, so the worst case is ~5 minutes of downtime with
nobody watching. Exit code is 0 only when everything is healthy.

## Setup

1. `ops/laptop-setup/README.md` — remote access (Cloudflare tunnel + SSH)
2. `PLAN.md` → Phase 1 — Ollama, Python, OCR tooling, Node, NSSM, Claude CLI
3. `python pipeline/update.py --scan-only` — review the inventory report first
4. `python pipeline/update.py` — extract, OCR, and build the index
5. `ops/install.ps1` — bring the Telegram gateway up

## Secrets

Nothing sensitive is tracked. `secrets.env` (Telegram bot token) is gitignored.
The Cloudflare tunnel token is passed to `setup-access.ps1` as a command-line
argument rather than stored in the file, so the script itself carries no secret
and can be downloaded straight from this repo.
