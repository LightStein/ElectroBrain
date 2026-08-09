# George's Standards Assistant — Implementation Plan (v3)

**Goal:** George (electrical revisor) asks questions in Telegram in Russian and gets accurate,
cited answers from his ~160 PDF/DOCX standards documents (electrical wiring, fire systems,
grounding, lightning protection, …) in ≤30s, running on his own laptop.

**Hardware (measured on-site, 2026-08-09):** ASUS laptop, Windows 11 Pro 26200, user `eveli`.
Intel i5-9300H (4c/8t), 15.9GB RAM, 135GB free. **GPU: NVIDIA GTX 1650, 4GB VRAM, driver
555.97, compute 7.5** — a Turing card with no tensor cores, weaker than the 3050/3060
originally assumed, which makes the 4B model choice mandatory rather than merely preferred.

**Status: SSH access live; Phase 1 complete; engine benchmarked on the real machine.**

## Final decisions
- Daily engine: **local `qwen3:4b-instruct`** via Ollama (see benchmark below); **`claude -p` (haiku)** escalation via `/pro`
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
bot/ask.py                 the engine: route (qwen, JSON) -> lexical retrieve
                           (heading chunks, tf*idf-lite, RU stem-prefix matching) -> answer
                           (qwen, mandatory citations) -> NOT_FOUND handling ->
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
- **Finding that survived:** the router can hallucinate doc ids → now validated against the
  catalog, unknown ids ⇒ full-corpus scan.
- The CPU-only timings measured here were superseded by the on-laptop benchmark below, as was
  the "answer needs think=ON" finding — that was an artefact of `qwen3:4b`, and the switch to
  `qwen3:4b-instruct` removes thinking from the picture entirely.

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

## Measured on the target laptop (2026-08-09)

| What | Result |
|---|---|
| `qwen3:4b` generation | 13.7-14.9 tok/s |
| `qwen3:4b` prompt processing | ~150 tok/s |
| **`qwen3:4b-instruct`** generation | **17.9 tok/s** |
| One-line question, `qwen3:4b` | 49s (454-663 tokens, ~all thinking) |
| One-line question, `qwen3:4b-instruct` | **7.3s (30 tokens)** |
| Full `ask.py` pipeline, 2-doc index | **26.5-29.1s** (route ~13.5s + answer ~15s) |

**Decision: `qwen3:4b-instruct` is the daily model.** `qwen3:4b` thinks unconditionally —
neither Ollama's `think: false` nor Qwen's `/no_think` suppresses it — and spends 400-600
tokens reasoning about a one-line question, which at 14 tok/s is ~30-45s of pure overhead.
The `-instruct` variant has no thinking mode at all and answered the same question correctly
in 7.3s. `ASK_THINK_FINAL` now defaults to 0.

Both test questions returned correct answers with the required document + clause + verbatim
quote. End-to-end is at the 30s target, but with little headroom.

**Known optimisation if it proves too slow on the real 160-doc corpus:** the routing LLM call
(~13.5s, half the budget) can be replaced with pure lexical matching of the question against
the RU/EN keyword lists in each `meta.json`. That would need the cleanup agent to emit
*aligned* RU/EN keyword pairs, giving a project-specific translation table and removing the
need for an LLM to do query translation at all. Not built yet - retrieval quality on the real
corpus should decide it.

## Phase 1 complete - installed on the laptop

Ollama 0.32.6 (pre-existing, plus `qwen2.5:7b`/`nomic-embed-text` from earlier experiments),
`qwen3:4b` + `qwen3:4b-instruct` pulled, Python 3.12.10 (+ pymupdf, python-docx, ocrmypdf
17.10), Node 24.19, git 2.55, NSSM, pandoc, Tesseract 5 with **tessdata_best** `rus`+`eng`
(the silent installer ships English only; `best` beats `fast` noticeably on scanned Cyrillic).
Repo cloned to `C:\Standards`.

Still to install: Claude Code CLI (+ login as `eveli`).

## Remaining phases

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
