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

Claude Code CLI 2.1.226 installed and logged in as `eveli`; headless `claude -p` verified.

**`/pro` escalation measured: 16.1s** - correct answer, correct citation, and *faster than
the local model* (26-29s), because Haiku generates quickly and does its own grep over the
index. Two Windows-specific bugs had to be fixed to get there, both of which would have
broken the preprocessing pipeline identically on its first real run:

1. npm installs `claude` (a bash script, no extension), `claude.cmd` and `claude.ps1`
   alongside the real `bin/claude.exe`. A bare `which("claude")` returns the bash shim,
   which Windows cannot execute - reported as "claude CLI not found", indistinguishable
   from it not being installed.
2. The `.cmd` shim re-parses arguments through `cmd.exe`, which mangles the multi-line
   `--append-system-prompt` into nothing; claude then exits with "Input must be provided...".
   Fixed by launching `bin/claude.exe` directly, so argv survives with no shell involved.

Worth revisiting once the real corpus is indexed: if `/pro` stays both faster and more
accurate, the sensible default may be Claude-first with the local model as the offline
fallback, rather than the other way round. That is a cost/latency tradeoff for Anri to
call - local is free and offline, Claude costs plan usage per question.

## Current state (2026-08-09, live on the laptop)

**Done and verified end-to-end:**
- Access: OpenSSH + cloudflared, both boot services. User is `eveli`, not `george`.
- Toolchain: Python 3.12 (+pymupdf, python-docx, ocrmypdf), Node 24, git, NSSM,
  pandoc, LibreOffice, Tesseract 5 with tessdata_best rus+eng.
- Ollama 0.32.6 with `qwen3:4b-instruct`; Claude Code 2.1.226 logged in as `eveli`.
- Corpus indexed from `D:\LLM_FILES` (56 files, 3047 pages, 16.3M chars, zero
  scanned - every PDF already had a text layer, so no OCR pass was needed).
- Bridge over a Windows named pipe: `/health`, `/prompt` ndjson stream, durable
  pending file written before `done`. `gateway/smoke-test.js` re-checks this.
- Local engine 26-29s per answer; `/pro` Claude escalation 16.1s.
- Deleting a document removes its index entry, extracted text and catalog line.

**Bugs found by testing, all of which would have presented to George as
"the bot doesn't answer" with nothing useful in any log:**

1. `shell:true` on the engine spawn - split the question across argv AND made
   an unescaped Telegram message a command-injection vector.
2. npm's `claude` shims: the extensionless bash one is unlaunchable, and the
   `.cmd` one re-parses arguments through cmd.exe, destroying the multi-line
   system prompt. Fixed by calling `bin/claude.exe` directly.
3. NSSM services run as LocalSystem: Claude's per-user login was invisible, so
   `/pro` would have failed. Fixed with `CLAUDE_CONFIG_DIR` pointing at the
   real store - not a copy, since OAuth refresh rotates tokens and two copies
   would invalidate each other.
4. Ollama was only a per-user Startup entry, so after an unattended reboot the
   bridge would have come up with no engine behind it. Now a boot service, with
   `OLLAMA_MODELS` carried explicitly (models are on D: via a USER variable a
   service cannot see) and bound to loopback.
5. The router prompt embeds the whole catalog: at 56 documents the original
   shape projected to ~12.9k tokens of a 16k window, so Ollama would have
   silently truncated it and made documents unroutable.

## Remaining

1. **Telegram bot** - blocked on a NEW token. The one supplied was the
   production `laamarie_web_dev_bot` with a typo; two pollers on one token
   break each other, which would take down all 16 of Anri's live chats.
   Then: `ops\install.ps1` (reads `secrets.env`, writes `registry.json`,
   registers all three services).
2. **Answer-quality pass** on the full corpus once indexing finishes - the
   first real measure of whether this is useful to George.
3. **Optional:** `0-fed_zakon_№ 185.pdf` is education law, not electrical.
   Anri to decide whether it stays.

## Open questions worth revisiting after real use

- **Claude-first vs local-first.** `/pro` is both faster (16s vs 26-29s) and
  more accurate than the local model on this hardware. If that holds on real
  questions, the sensible default may invert - local becomes the offline
  fallback. It is a cost decision (local is free, Claude spends plan usage),
  not a technical one.
- **Dropping the routing LLM call.** It costs ~13.5s of the budget. Since
  `meta.json` now carries ALIGNED RU/EN keyword pairs, the question could be
  matched lexically against them instead, removing an LLM round trip and the
  query-translation problem in one go. Worth doing only if latency matters
  more than routing quality on the real corpus.
