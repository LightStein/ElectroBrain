# George's Standards Assistant — Implementation Plan (v3)

**Goal:** George (electrical revisor) asks questions in Telegram in Russian and gets accurate,
cited answers from his ~160 PDF/DOCX standards documents (electrical wiring, fire systems,
grounding, lightning protection, …) in ≤30s, running on his own laptop.

**Hardware:** ASUS gaming laptop — Intel CPU, NVIDIA 4GB VRAM (RTX 3050 laptop), 16GB DDR4-2400, Windows.

**Status: gateway ported and integration-tested on Anri's host. Waiting on SSH access to the laptop.**

## Final decisions
- Daily engine: **local `qwen3:4b`** via Ollama; **`claude -p` (haiku)** escalation via `/pro`
  command, image/file messages, or (optional) auto-escalation on NOT_FOUND.
- Preprocessing: free tools (pymupdf, OCRmyPDF/Tesseract rus+eng, pandoc) extract text;
  **Claude Code CLI** (Anri's account) does text-in/text-out cleanup into the index.
- Telegram: **port of Anri's seiv/dev_bot gateway** (bot.js + bridge-server.js), run **natively
  on Windows under NSSM** — no Docker, no WSL. Sockets → named pipes (`\\.\pipe\standards-bridge`).
  Ops UX via `ops\ctl.ps1` (status / start / stop / restart / restart-when-idle / logs / ask).
- Language: Russian. `index\` doubles as an Obsidian vault.

## What's already built (this repo)

```
gateway/bridge-server.js   port of seiv bridge: engine-configurable (BRIDGE_SPAWN JSON argv
                           template + BRIDGE_OUTPUT=plain|stream-json), unix socket OR named
                           pipe, keeps: queue serializer, reaper, .progress tailing, pending
                           persistence, context-token lesson, /health /prompt /kill /clear /compact
gateway/bot.js             port of seiv bot: registry routing, durable pending sweeper, Markdown
                           fallback send, watchdogs (poll-error bail, unhandledRejection,
                           drained hourly refresh -> NSSM restarts). Stripped project keyboards.
                           Added /pro (escalation), /docs (catalog from disk). Russian UX texts.
gateway/pending.js         unchanged logic, env-configurable dir
gateway/healthcheck.js     /health over named pipe (curl can't) — used by ctl.ps1
gateway/registry.json      one project "standards"; chatId filled in after /chatid
bot/ask.py                 the engine: route (qwen, think=off, JSON) -> lexical retrieve
                           (heading chunks, tf*idf-lite, RU stem-prefix matching) -> answer
                           (qwen, think=ON, mandatory citations) -> NOT_FOUND handling ->
                           claude escalation (PRO:/images/auto). Stdlib-only. History file.
bot/pro-prompt.md          system prompt for the claude escalation (grep/read the index, cite)
pipeline/update.py         scan (hash+classify text/scanned/mixed) -> extract (pymupdf/pandoc)
                           -> OCR (ocrmypdf rus+eng, --redo-ocr for mixed) -> cleanup (claude -p
                           per doc) -> removals. Manifest-driven, resumable, --scan-only report.
pipeline/cleanup-prompt.md claude instructions: full.md + meta.json + catalog line; never guess
                           numbers; [неразборчиво] markers; quality flag
Update-Standards.bat       George's one click -> pipeline/update.py
ops/install.ps1            NSSM services (auto-start, auto-restart, rotating logs), reads
                           secrets.env (TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID)
ops/ctl.ps1                compose-like control incl. drained restart (port of bot-reload.sh)
```

### Verified by tests (on Anri's host, CPU-only)
- bridge: /health, /prompt ndjson stream (started → progress_message → done), pending file
  written before `done`, plain-engine spawn. (Unix socket; named pipe is the same node API.)
- ask.py end-to-end on a 2-doc fake index with real `qwen3:4b`:
  - route: correct doc + good bilingual search terms
  - retrieve: exact clause chunk found
  - answer: correct, cited («квота» + п. + document), Russian, with verification footer
- **Findings:** (1) final answer needs think=ON — with think=off qwen3:4b wrongly said
  NOT_FOUND on an answerable question; ASK_THINK_FINAL=1 is the default. (2) router can
  hallucinate doc ids → now validated against the catalog, unknown ids ⇒ full-corpus scan.
- CPU timings here (no GPU): route ~40-60s, answer(think) ~1.5-10min. On the 4GB GPU with the
  model fully resident expect ~5-15s route + ~10-30s answer. **Benchmark on-site**; if thinking
  is too slow there, the lever is ASK_THINK_FINAL=0 + retuning the answer prompt (must re-test:
  think=off currently degrades accuracy).

## Answer contract (non-negotiable)
Every answer cites document title + clause number + verbatim quote and ends with
"_Проверь в первоисточнике._" — enforced by both engine prompts.

## Laptop layout (deploy target)

```
C:\Standards\
├── raw\                     ← George's originals (the ONLY folder he touches)
├── work\{extracted,ocred}\
├── index\catalog.md + docs\<id>\{full.md, meta.json}     ← Obsidian vault
├── state\{manifest.json, inventory_report.md, ask-history.json}
├── bot\{ask.py, pro-prompt.md, uploads\}
├── gateway\{bot.js, bridge-server.js, pending.js, healthcheck.js, registry.json, run\}
├── pipeline\{update.py, cleanup-prompt.md}
├── ops\{install.ps1, ctl.ps1}
├── logs\
├── secrets.env              ← TELEGRAM_BOT_TOKEN=..., ALLOWED_USER_ID=...
└── Update-Standards.bat
```

## Remaining phases (need SSH — ~2 days out)

### Phase 0 — Access (Anri, ~30 min)
OpenSSH Server + cloudflared tunnel on the laptop; Anri verifies login.
First commands: `nvidia-smi`, disk space, Windows version, power settings (lid ≠ sleep).

### Phase 1 — Environment (remote, ~1-2h)
Ollama for Windows + `qwen3:4b` (bench `qwen3:8b` once for the record); Python 3.12 +
`pip install pymupdf python-docx`; pandoc; OCRmyPDF + Tesseract (rus+eng traineddata);
Node.js LTS; NSSM (winget); Claude Code CLI + login (Anri's account); git-copy this repo
to C:\Standards; `npm install` in gateway\.
Benchmark: 5 Russian standards questions → confirm think=ON timing fits ~30s.

### Phase 2 — Inventory (remote, ~30 min + Anri review)
`python pipeline\update.py --scan-only` → review `state\inventory_report.md`
(text/scanned/mixed counts, page volume, weird files) before burning OCR/Claude time.

### Phase 3 — Extraction + OCR + cleanup (remote, unattended, resumable)
`python pipeline\update.py --limit 10` first → QA those 10 by hand (scanned RU, scanned EN,
digital, table-heavy) → tune cleanup-prompt.md if needed → run the rest (may span days if
Claude plan limits pause it; manifest makes re-runs free). Re-OCR the "quality: poor" list.

### Phase 4 — Gateway live (remote, ~2h)
Create bot via BotFather (privacy mode OFF for group use), fill secrets.env; George's group
→ /chatid → registry.json; `ops\install.ps1`; test battery of ~20 real questions from George
(colors, heights, distances, sections; follow-ups; /pro; a photo → escalation path).

### Phase 5 — Handover (remote, ~1h)
Autostart sanity (services boot without login? NSSM = yes, it's a service), Ollama autostart,
one-page Russian cheat-sheet for George (raw\ folder, Update-Standards.bat, /docs /pro /clear),
Obsidian pointed at index\ (optional).

## Risks / open items
- **4GB VRAM + think=ON latency** — measured only on CPU so far; on-site benchmark decides.
  Escape hatches: think=off + prompt retune, or `/pro`-style claude as default engine (zero
  architecture change either way).
- OCR quality on worst scans → QA sample + per-doc visual fallback (individually affordable).
- Preprocessing volume vs Claude plan limits → resumable loop, spans days if needed.
- Laptop uptime: bot lives only while the laptop is on (services run pre-login though).
- node-telegram-bot-api long-poll wedges (seiv lessons) → all three watchdog layers ported.
