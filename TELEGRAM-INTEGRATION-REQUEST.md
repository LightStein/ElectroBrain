# Request: may ElectroBrain use the existing Telegram integration?

**From:** the ElectroBrain project (`/home/anri/LLM_setup_for_george`)
**To:** whoever owns `/home/anri/seiv/dev_bot` (shared `claude-telegram-bot` + the 18 bridges)
**Status:** asking first. Nothing in `seiv/dev_bot` has been modified. We read
`docker-compose.yml`, `registry.json`, `deltaops-server.js`, `claude-bridge.service`
and `pending.js` to understand the conventions, and changed nothing.

We are **not blocked** on this answer — we can run a fully separate stack with
its own token and network. We are asking because duplicating a working
integration is worse than reusing one, if reuse is welcome and safe.

---

## 1. What ElectroBrain is

A Telegram assistant for **George**, an electrical *revisor* (inspector) in
Georgia. He owns ~160 standards documents (PDF/Word, Russian and English:
wiring, fire safety, grounding, lightning protection) and today spends 4-5
hours locating a single clause. 56 documents are indexed so far.

He asks a question in plain Russian; he gets back an answer with **document +
clause number + verbatim quote**. Wrong numbers are safety-critical — he
certifies installations — so an answer whose citation cannot be verified is
suppressed rather than shown.

It currently runs on George's Windows laptop. We want to move it onto this
host because the reason for the laptop (a local GPU model) has been removed,
and the laptop has been unreachable for ~3 weeks.

## 2. How our engine differs from your bridges — read this part first

This is the crux of whether we fit your system at all.

| | your bridges (e.g. `deltaops-server.js`) | ElectroBrain |
|---|---|---|
| engine | `claude --continue` (Claude Code CLI) | **one stateless Anthropic API call** |
| session | `hasSession`, `--continue`, `/compact` every 10 msgs | **none** — no session, no compaction |
| output parsing | `--output-format stream-json`, `content_block_delta` | plain text on stdout |
| per turn | long-lived CLI process, tool loop | one HTTP request, 30-90s |
| host access | `--dangerously-skip-permissions`, shell, repo | **none** — no CLI, no shell, no tools |
| conversation history | the CLI's own session | our own `state/ask-history.json`, last 3 turns |

So ElectroBrain would be a project that **uses none of the machinery your
pattern exists to provide**. If `bot.js` assumes a session-based bridge
anywhere (`/clear` semantics, compaction, `stream-json`), that is exactly what
we need you to tell us.

Retrieval is ours and happens before the API call: lexical scoring over a
Markdown index we build from the documents. The model is given the ~20k tokens
of chunks we selected and **no tools**, which is what keeps a question at
~$0.10 instead of the 195k-1.1M tokens it cost when the CLI re-did retrieval
itself with grep.

## 3. What we need from a Telegram integration

1. **One private group**, one authorised user (George) plus Anri.
2. **Text in, text out.** A Russian question; one reply, typically 300-2000
   characters, Markdown, occasionally longer.
3. **Progress messages during a turn.** A turn takes 30-90s, so the chat needs
   "Ищу в стандартах…" plus which documents are being read, or it looks dead.
   Our bridge writes these to a `.progress` file that the bot tails.
4. **Document uploads → a path on disk.** George adds a standard by sending
   the PDF/DOCX to the chat. We need the saved file's **host path** so the
   indexing pipeline can read it (this is what your `upload: {container, host}`
   mapping already does). Files are typically 1-50 MB; a few are larger, which
   is why your self-hosted `telegram-bot-api` (2 GB) is attractive.
5. **Turn serialisation.** One turn at a time per project. Indexing a new
   document must not run concurrently with a question.
6. **Durable reply delivery.** A finished answer must survive a bot restart
   mid-turn (your `pending.js` + the bot's sweeper already do this; our bridge
   writes to `BRIDGE_PENDING_DIR`).
7. **A few commands:** list documents, clear context, status.

Nice to have, not required: a photo of a panel with a question attached (our
engine sends images as API image blocks).

## 4. What integration would cost you, concretely

If we became project #19 on the shared bot:

- one `registry.json` entry (`name`, `chatId`, `socket`, `upload`)
- one volume mount on `claude-telegram-bot` for our corpus/uploads directory
- a restart of the shared bot container — your documented procedure, but it
  briefly interrupts all 18 live projects
- our bridge as a 19th systemd unit (or a container), running
  `bridge-server.js` with `BRIDGE_SPAWN=["python3", ".../bot/ask.py", "-p",
  "{message}"]`. It is a generalised port of your per-project servers:
  env-driven engine instead of hardcoded `claude`. Node for the bridge, Python
  for the engine.

**The token question.** Your bot container runs a single token
(`laamarie_web_dev_bot`, which also serves production chats). George already
has his own bot in his group. So either his group moves onto your production
token, or a project needs to be able to use its own token — which we assume
means a second bot process. Please tell us which, because it decides this.

## 5. Questions

1. Is it OK to add a 19th project at all, or would you rather we stay separate?
2. Does `bot.js` make assumptions that a stateless bridge would break —
   around `/clear`, compaction, `stream-json`, or session state?
3. Can a project use its own bot token, or is it one token per bot process?
4. Where should the corpus and uploads live to fit the host's layout, and is
   `upload: {container, host}` the right mechanism for handing us a file path?
5. Any objection to a bridge whose engine is Python rather than the Claude CLI?
6. Is the self-hosted `telegram-bot-api` (2 GB files) available to a new
   project, or does it need anything extra?
7. Preference: systemd unit like the other 18, or a container on its own
   network?

## 6. If the answer is no

We build a self-contained Compose stack in our own repo: own network, George's
existing token, own volumes. Nothing of yours is touched or restarted. That
costs us ~827 lines of duplicated bot code we would otherwise delete, and we
would wire up large-file handling ourselves. A "no" is a perfectly fine answer
— we would rather duplicate than destabilise 18 live projects.
