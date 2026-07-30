// Durable reply handoff between a bridge and the Telegram bot.
//
// The bot process restarts on its own (hourly anti-wedge refresh, polling
// watchdog, unhandled rejection). When that happens mid-turn, the open
// /prompt stream dies with it: the bridge finishes the work, writes its
// `done` event into a dead socket, and the finished answer is gone.
//
// Fix: the bridge persists the finished reply to <pending>/<turnId>.json
// BEFORE writing it to the socket. The bot deletes that file once it has sent
// the reply, and sweeps the directory every 30s (and at boot) to deliver
// anything left behind. One file per turn, written by the bridge, unlinked by
// the bot — no read-modify-write race.
//
// turnId/chatId/replyTo come from the bot in the /prompt body. A request
// without them (curl, tests) just skips persistence.

const fs = require('fs');
const path = require('path');

const PENDING_DIR = process.env.BRIDGE_PENDING_DIR || path.join(__dirname, 'run', 'pending');

function safeId(id) {
  return String(id).replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 120);
}

function savePending(body, text) {
  try {
    if (!body || !body.turnId || !body.chatId || !text) return null;
    fs.mkdirSync(PENDING_DIR, { recursive: true });
    const file = path.join(PENDING_DIR, `${safeId(body.turnId)}.json`);
    const tmp = `${file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify({
      turnId: body.turnId,
      chatId: body.chatId,
      replyTo: body.replyTo || null,
      text,
      ts: Date.now(),
    }));
    // Atomic swap so the bot never reads a half-written record.
    fs.renameSync(tmp, file);
    try { fs.chmodSync(file, 0o666); } catch {}
    return file;
  } catch (e) {
    console.error(`[pending] save failed: ${e.message}`);
    return null;
  }
}

module.exports = { savePending, PENDING_DIR };
