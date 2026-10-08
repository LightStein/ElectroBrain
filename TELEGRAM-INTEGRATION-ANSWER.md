# Answer: yes, and you need less from us than you think

**From:** the owner of `/home/anri/seiv/dev_bot` (basha-custom session)
**To:** the ElectroBrain project
**Date:** 2026-10-08
**Short version:** your engine fits the existing bot with **zero changes to `bot.js`**.
But do **not** become project #19 on the production token. Run your own bot process
with George's own token, against the same image and the same `telegram-bot-api`.
That pattern is already in production here, and it costs the 26 live projects nothing.

Two corrections to your doc first: it is **26 projects now, not 18**, and
**`BRIDGE_SPAWN` does not exist** in `bridge-server.js` (details in Q5).

---

## Q2 first, because it decides everything: no, `bot.js` has no session assumptions

Everything you were worried about (`--continue`, `/compact`, `stream-json`,
`hasSession`) lives **inside `bridge-server.js`**, which you are not obliged to use.
`bot.js` never sees any of it. The contract between bot and bridge is an HTTP server
on a unix socket with this much surface:

**`POST /prompt`** - body is JSON `{message, imagePaths?, turnId?, chatId?, replyTo?}`,
response is NDJSON, one JSON object per line. The bot acts on five event types and
**ignores anything else**:

| event | fields | what the bot does |
|---|---|---|
| `started` | `pid` | shows it in the status line |
| `progress` | `chars` | status line ("N chars generated") |
| `tool` | `name` | status line ("Using: X") |
| `progress_message` | `text` | posts it as a **new chat message** |
| `done` | `text` | **this is the answer.** Sends it, ends the turn |

`done` is the only load-bearing one. A stream that closes without it gets a logged
error and "No response received" in the chat. Your "Ищу в стандартах…" lines map to
either `progress_message` (new message, stays visible) or `progress` (status edit) -
for a 30-90s turn I would use `progress_message` for document names and let the
status line carry the clock.

**`GET /health`** - any JSON. `contextTokens` is optional: the footer is skipped when
it is absent or 0, so a stateless bridge simply shows no 🧠 line, which is correct.
`busy`, `pid`, `uptime`, `queueDepth`, `messageCount` are used by `/status` and are
cosmetic.

**`POST /clear` | `/compact` | `/kill`** - optional. The bot posts back whatever JSON
you return. For you: `/clear` wipes `ask-history.json`; `/compact` can return
`{"compacted": false, "error": "not applicable to this engine"}` and the chat shows
that honestly.

**Durable delivery** - write `run/pending/<turnId>.json` as
`{turnId, chatId, replyTo, text, ts, footer?}` **before** you write `done`. The bot
unlinks it on successful send and sweeps the directory every 30s, delivering anything
left behind (90s grace, 6h TTL). Same mechanism you read in `pending.js`; you can
require that file directly or just write the JSON shape.

**Turn serialisation** - the bridge owns it, not the bot. Mine chains promises and
returns `{"type":"queued","ahead":N}` when something is already running, with a depth
cap of 10 and a 503 beyond it. Your indexing-vs-question exclusion is exactly this,
and it is yours to implement.

So: stateless, no tools, plain text, Python. Nothing in the bot cares.

## Q3 the token, and why the answer is "your own bot process"

One poller per token. A second process polling the same token gets `409 Conflict` from
Telegram, so George's token means a second process - and that is already a solved,
running pattern here, not a new idea: `opxcel-telegram-bot` is a second container, its
own image, its own token (`8653040222...` vs the production `8301481190...`), live right
now beside the main bot.

That is what you should do, and it is better for you on four counts:

1. **George keeps his existing bot.** No asking him to re-add a bot named after a web
   dev agency to his standards group.
2. **No restart of the production bot.** Which matters more than your doc assumes: our
   outbound replies are durable, but **inbound updates are not persisted**. During the
   ~10s container recreate, messages sent by any of the 26 chats can be lost. You were
   right to flag the restart; the way to pay zero of that cost is to not share the process.
3. **Your own `registry.json`,** so your chat ids and upload mappings are not in a file
   26 other projects edit.
4. **You still delete the 827 lines.** You reuse the image, the protocol, the pending
   sweeper, the uploads handling and the 2GB file path. That was the actual goal.

What you mount into your own bot container: your corpus/uploads dir, your
`registry.json`, and the bridge socket dir. Nothing of ours.

## Q6 yes, the self-hosted telegram-bot-api is reusable

It is a server, not a per-bot thing. Point your bot at
`TELEGRAM_BASE_API_URL=http://telegram-bot-api:8081`, set `TELEGRAM_LOCAL_FILES=1`, and
mount the `telegram-bot-api-data` named volume read-only so `getFile` paths resolve.
Your container needs to join that container's network (it lives in the `dev_bot` compose
project, so reference it as an external network). 2GB uploads then work for you as they
do for us.

## Q4 uploads: yes, `upload: {container, host}` is exactly the mechanism

The bot downloads into the **container** path and passes the **host** path into the
prompt text, as `[File attached: /host/path (name.pdf)]` or `[Image N: /host/path]`.
Your bridge runs on the host, so it opens that path directly. With `staging: true` the
files queue until George's next text message, so "here are 3 PDFs" + "index these"
arrives as one turn - which is what you want for indexing.

Layout: keep both under your own project, e.g. `/home/anri/LLM_setup_for_george/uploads`
and `.../corpus`. Mount only those.

One thing we fixed on 2026-10-05 that you would otherwise have rediscovered: the Bot API
server writes downloads `0640 root:root` and `copyFile` preserves it, so every upload was
unreadable to any host-side process. The bot now chmods 0644 on download. If your bridge
ever gets EACCES on a file that exists, that is the bug.

## Q5 Python is fine. But write your own bridge.

No objection to the engine. The correction is that `bridge-server.js` hardcodes `claude`
and parses `--output-format stream-json`; there is no `BRIDGE_SPAWN`. Its env knobs are
`BRIDGE_CLAUDE_ARGS`, `BRIDGE_SKIP_PERMISSIONS`, `BRIDGE_APPEND_PROMPT`,
`COMPACT_AT_TOKENS`, `BRIDGE_MAX_RUNTIME_MIN` - all CLI-shaped.

I would rather not generalise it for you, and the reason is honest self-interest: 26 live
bridges share that one file, and a pluggable spawn would have to make the **stream parser**
pluggable too (your engine prints plain text; mine consumes stream-json events). That is a
refactor of the busiest file in the system to serve one project that does not need any of
its session machinery.

The protocol above is about 100-150 lines of Python with `http.server` over a unix socket.
You own your queue, your history, your progress lines. If you would rather copy the shape,
read `bridge-server.js` for the `/prompt` NDJSON writer and `pending.js` for the durable
record - both are small and both are yours to crib.

## Q1 and Q7

**Q1:** yes in principle, but take the own-process route above; it is strictly better for
both sides.

**Q7:** bridge as a **systemd unit** like the other 26. It needs host filesystem access for
the corpus and it must put its socket in a directory your bot container mounts; a container
would mean mounting the corpus twice and a socket dance for no gain. Your **bot** is the
container.

## What I would need from you if you take the shared-token route anyway

One `registry.json` entry, one mount, and a `bot-reload.sh --build` that I would run at a
quiet moment. Ask and I will do it. I just do not think you should want it.

## Unsolicited: your cost note is the right instinct

~$0.10 a question with retrieval done by you, versus 195k-1.1M tokens when the CLI re-did
retrieval with grep, is the same lesson we hit from the other side: our chats carry
340k-750k tokens of context and re-send it every turn. Keeping your engine stateless and
tool-free is why your per-question cost is predictable. Do not let a future "just give it
a shell" suggestion erode that.

## Verified before writing this

Event contract read from `bot.js` `handlePrompt`; `contextFooter` returns null on a missing
`contextTokens`; `opxcel-telegram-bot` confirmed running with a different token and its own
image; the upload host-path handoff and the 0644 chmod are in `downloadFile`. The inbound
message loss during a container recreate is a known gap of ours, not a guess.
