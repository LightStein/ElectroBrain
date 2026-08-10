// Telegram bot — Windows/Linux port of seiv/dev_bot/bot.js, stripped to what the
// standards assistant needs. One process, routes chatId -> bridge socket/pipe
// from registry.json, streams the bridge's ndjson progress, delivers durable
// pending replies after restarts. Runs under NSSM (auto-restart on exit — the
// watchdogs below deliberately process.exit(1) to get a clean relaunch, exactly
// like `restart: unless-stopped` did for the Docker original).
//
// Env:
//   TELEGRAM_BOT_TOKEN   (required)
//   ALLOWED_USER_ID      DM allowlist (single numeric user id)
//   REGISTRY_PATH        default ./registry.json
//   RUN_DIR              default ./run   (pending/ lives under it)
//   UPLOADS_DIR          default ./uploads (fallback when registry has no upload dir)
//   CATALOG_PATH         index catalog.md, for /docs (optional)
//   STATUS_TEXT          "thinking" placeholder, default "Ищу в стандартах…"
//   TELEGRAM_BASE_API_URL / TELEGRAM_LOCAL_FILES  self-hosted Bot API (optional)

const TelegramBot = require('node-telegram-bot-api');
const http = require('http');
const fs = require('fs');
const path = require('path');

const TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const ALLOWED_USER_ID = parseInt(process.env.ALLOWED_USER_ID, 10);
const RUN_DIR = process.env.RUN_DIR || path.join(__dirname, 'run');
const UPLOADS_DIR = process.env.UPLOADS_DIR || path.join(__dirname, 'uploads');
const CATALOG_PATH = process.env.CATALOG_PATH || null;
const STATUS_TEXT = process.env.STATUS_TEXT || 'Ищу в стандартах…';

// Per-project routing from registry.json — onboarding a project is a data edit
// there, never a change to this file. Native deployment = one path world, so a
// project's `upload` is a single directory string (no container/host split).
const REGISTRY_PATH = process.env.REGISTRY_PATH || path.join(__dirname, 'registry.json');

const SOCKET_MAP = {};                 // chatId -> bridge socket path / named pipe
const ALLOWED_GROUP_IDS = new Set();   // group chats where any member may use the bot
const STAGING_CHAT_IDS = new Set();    // chats that STAGE uploads for the next text message
const UPLOAD_DIRS = {};                // chatId -> upload dir
let DEFAULT_SOCKET = null;             // first registry entry's socket (single-project convenience)
try {
  const reg = JSON.parse(fs.readFileSync(REGISTRY_PATH, 'utf8'));
  for (const p of (reg.projects || [])) {
    // Coerce to Number: Set.has(msg.chat.id) is a numeric lookup, so a string
    // chatId would silently never match.
    const id = Number(p.chatId);
    if (!Number.isFinite(id)) { console.error(`[registry] bad chatId, skipping: ${JSON.stringify(p)}`); continue; }
    if (p.socket) {
      SOCKET_MAP[id] = p.socket;
      if (!DEFAULT_SOCKET) DEFAULT_SOCKET = p.socket;
    }
    ALLOWED_GROUP_IDS.add(id);
    if (p.staging) STAGING_CHAT_IDS.add(id);
    if (p.upload) UPLOAD_DIRS[id] = p.upload;
  }
  console.log(`[registry] loaded ${ALLOWED_GROUP_IDS.size} projects from ${REGISTRY_PATH}`);
} catch (e) {
  // Fail loud: an empty registry would silently reject every group chat.
  console.error(`[registry] FAILED to load ${REGISTRY_PATH}: ${e.message} — refusing to start with no routing`);
  process.exit(1);
}

const stagedFiles = new Map();    // chatId -> [{ hostPath, name, isImage }]
const stageAckTimers = new Map(); // chatId -> debounce timer for the "staged" ack
function uploadDir(chatId) {
  return UPLOAD_DIRS[chatId] || UPLOADS_DIR;
}

function stageFile(chatId, entry) {
  if (!stagedFiles.has(chatId)) stagedFiles.set(chatId, []);
  stagedFiles.get(chatId).push(entry);
  if (stageAckTimers.has(chatId)) clearTimeout(stageAckTimers.get(chatId));
  stageAckTimers.set(chatId, setTimeout(async () => {
    stageAckTimers.delete(chatId);
    const staged = stagedFiles.get(chatId) || [];
    if (!staged.length) return;
    const names = staged.map(f => f.name).join(', ');
    try {
      await bot.sendMessage(chatId, `📎 ${staged.length} file(s) staged: ${names}\nSend instructions to process them, /files to list, or /discard to clear.`);
    } catch {}
  }, 1500));
}

function clearStaged(chatId) {
  const n = (stagedFiles.get(chatId) || []).length;
  stagedFiles.delete(chatId);
  if (stageAckTimers.has(chatId)) { clearTimeout(stageAckTimers.get(chatId)); stageAckTimers.delete(chatId); }
  return n;
}

if (!TOKEN) { console.error('TELEGRAM_BOT_TOKEN not set'); process.exit(1); }

const BASE_API_URL = process.env.TELEGRAM_BASE_API_URL || 'https://api.telegram.org';
const bot = new TelegramBot(TOKEN, {
  baseApiUrl: BASE_API_URL,
  polling: {
    autoStart: true,
    params: { timeout: 30 },
  },
});
console.log(`[bot] using API server: ${BASE_API_URL}`);

try { fs.mkdirSync(UPLOADS_DIR, { recursive: true }); } catch {}

function getSocket(chatId) {
  return SOCKET_MAP[chatId] || DEFAULT_SOCKET;
}

function isAllowed(msg) {
  // Anyone in an allowed group can use the bot
  if (ALLOWED_GROUP_IDS.has(msg.chat.id)) return true;
  // DM: only the owner
  return msg.from.id === ALLOWED_USER_ID;
}

// HTTP over a unix socket (Linux) or named pipe (Windows) — node's http client
// takes both via `socketPath`, so the transport is a registry string, not code.
function socketRequest(socketPath, method, urlPath, body) {
  return new Promise((resolve, reject) => {
    const options = {
      socketPath,
      path: urlPath,
      method,
      headers: { 'Content-Type': 'application/json' },
    };

    const req = http.request(options, res => {
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => {
        try { resolve({ status: res.statusCode, data: JSON.parse(Buffer.concat(chunks).toString()) }); }
        catch { resolve({ status: res.statusCode, data: Buffer.concat(chunks).toString() }); }
      });
    });

    req.on('error', reject);
    req.setTimeout(5000, () => { req.destroy(new Error('timeout')); });

    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

// Stream response from /prompt endpoint
function streamPrompt(socketPath, body, onEvent) {
  return new Promise((resolve, reject) => {
    const options = {
      socketPath,
      path: '/prompt',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    };

    const req = http.request(options, res => {
      if (res.statusCode === 409 || res.statusCode === 503) {
        const chunks = [];
        res.on('data', c => chunks.push(c));
        res.on('end', () => {
          try {
            const data = JSON.parse(Buffer.concat(chunks).toString());
            reject(new Error(data.error || 'Server busy'));
          } catch {
            reject(new Error('Server busy'));
          }
        });
        return;
      }

      let buffer = '';
      res.on('data', chunk => {
        buffer += chunk.toString();
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const event = JSON.parse(line);
            onEvent(event);
          } catch {}
        }
      });

      res.on('end', () => {
        if (buffer.trim()) {
          try {
            const event = JSON.parse(buffer);
            onEvent(event);
          } catch {}
        }
        resolve();
      });

      res.on('error', reject);
    });

    req.on('error', reject);
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

// Download a file from Telegram into dir/filename.
async function downloadFile(fileId, filename, dir = UPLOADS_DIR) {
  try { fs.mkdirSync(dir, { recursive: true }); } catch {}
  const dest = path.join(dir, filename);

  if (process.env.TELEGRAM_LOCAL_FILES === '1') {
    const f = await bot.getFile(fileId);
    if (!f || !f.file_path) throw new Error('getFile returned no file_path');
    await fs.promises.copyFile(f.file_path, dest);
    return dest;
  }

  const fileStream = fs.createWriteStream(dest);
  const downloadStream = await bot.getFileStream(fileId);
  return new Promise((resolve, reject) => {
    downloadStream.pipe(fileStream);
    fileStream.on('finish', () => resolve(dest));
    fileStream.on('error', reject);
  });
}

// Split long text into chunks for Telegram's 4096 char limit
function splitMessage(text, maxLen = 4000) {
  if (text.length <= maxLen) return [text];
  const chunks = [];
  let remaining = text;
  while (remaining.length > 0) {
    if (remaining.length <= maxLen) {
      chunks.push(remaining);
      break;
    }
    let splitIdx = remaining.lastIndexOf('\n', maxLen);
    if (splitIdx < maxLen * 0.3) {
      splitIdx = remaining.lastIndexOf(' ', maxLen);
    }
    if (splitIdx < maxLen * 0.3) {
      splitIdx = maxLen;
    }
    chunks.push(remaining.substring(0, splitIdx));
    remaining = remaining.substring(splitIdx).trimStart();
  }
  return chunks;
}

// Send response, splitting if needed. `extra` may carry a footer line.
async function sendResponse(chatId, text, replyToId, extra = {}) {
  const { footer = null } = extra;
  const chunks = splitMessage(text);
  if (footer && chunks.length) chunks[chunks.length - 1] += `\n\n${footer}`;
  for (let i = 0; i < chunks.length; i++) {
    const isLast = i === chunks.length - 1;
    const opts = { parse_mode: 'Markdown' };
    if (i === 0 && replyToId) opts.reply_to_message_id = replyToId;
    try {
      await bot.sendMessage(chatId, chunks[i], opts);
    } catch (e) {
      // Only resend as plain text when Telegram REJECTED the Markdown (a parse
      // error) — then the first send definitively did NOT deliver. For ANY other
      // error (429, timeout, socket hiccup) the message may already be delivered;
      // resending would duplicate ("same bot answers 2-3x" bug).
      const emsg = (e && e.message) || String(e);
      const isParseError = /parse entities|can't find end|entities|BUTTON_|reply markup/i.test(emsg)
                           && !/429|too many|timeout|ETIMEDOUT|ESOCKETTIMEDOUT|EFATAL|ECONNRESET/i.test(emsg);
      if (isParseError) {
        try {
          const plainOpts = {};
          if (i === 0 && replyToId) plainOpts.reply_to_message_id = replyToId;
          await bot.sendMessage(chatId, chunks[i], plainOpts);
        } catch (e2) {
          console.error('Failed to send message (plain fallback):', e2.message);
        }
      } else {
        console.error('Send error (not resending, may already be delivered):', emsg);
      }
    }
    // Gentle throttle between chunks: Telegram sustains ~1 msg/s per chat.
    if (chunks.length > 1 && !isLast) {
      await new Promise(r => setTimeout(r, 400));
    }
  }
}

// Handle incoming prompt
async function handlePrompt(chatId, messageId, prompt) {
  const sock = getSocket(chatId);

  let statusMsg;
  try {
    statusMsg = await bot.sendMessage(chatId, STATUS_TEXT, {
      reply_to_message_id: messageId,
    });
  } catch {
    return;
  }

  const startTime = Date.now();
  let lastUpdate = Date.now();
  let progressInfo = '';

  const progressInterval = setInterval(async () => {
    const elapsed = Math.round((Date.now() - startTime) / 1000);
    const now = Date.now();
    if (now - lastUpdate >= 15000) {
      lastUpdate = now;
      let statusText = `${STATUS_TEXT} (${elapsed}s)`;
      if (progressInfo) statusText += `\n${progressInfo}`;
      try {
        await bot.editMessageText(statusText, {
          chat_id: chatId,
          message_id: statusMsg.message_id,
        });
      } catch {}
    }
  }, 5000);

  // Native Telegram "typing..." indicator — auto-clears after ~5s, re-send every 4s.
  const typingInterval = setInterval(() => {
    bot.sendChatAction(chatId, 'typing').catch(() => {});
  }, 4000);
  bot.sendChatAction(chatId, 'typing').catch(() => {});

  // turnId ties this stream to the bridge's durable copy of the finished reply.
  const turnId = `${chatId}_${messageId}_${Date.now()}`.replace(/[^A-Za-z0-9_-]/g, '_');
  const body = { message: prompt, turnId, chatId, replyTo: messageId };
  inFlightTurns.add(turnId);

  let finalText = '';
  let sawDone = false;
  let streamError = null;

  try {
    await streamPrompt(sock, body, (event) => {
      switch (event.type) {
        case 'started':
          progressInfo = `PID: ${event.pid}`;
          break;
        case 'progress':
          progressInfo = `${event.chars} chars generated`;
          break;
        case 'tool':
          progressInfo = `Using: ${event.name}`;
          break;
        case 'progress_message':
          // Engine-authored milestone line from the .progress file — push as a
          // fresh chat message so it stays visible.
          if (event.text) {
            bot.sendMessage(chatId, event.text).catch(() => {});
          }
          break;
        case 'done':
          sawDone = true;
          finalText = event.text;
          break;
      }
    });
  } catch (err) {
    streamError = err.message;
    finalText = `Error: ${err.message}`;
  }

  clearInterval(progressInterval);
  clearInterval(typingInterval);

  const elapsedSec = Math.round((Date.now() - startTime) / 1000);
  if (!sawDone && !streamError) {
    console.error(`[handlePrompt] stream closed without 'done' event: chat=${chatId} msg=${messageId} elapsed=${elapsedSec}s finalTextLen=${finalText.length}`);
  }

  try {
    await bot.deleteMessage(chatId, statusMsg.message_id);
  } catch {}

  try {
    if (finalText) {
      await sendResponse(chatId, finalText, messageId);
    } else {
      console.error(`[handlePrompt] empty finalText fallback: chat=${chatId} msg=${messageId} elapsed=${elapsedSec}s sawDone=${sawDone} streamError=${streamError || 'none'}`);
      await sendResponse(chatId, 'Ответ не получен — сервер прервал соединение. Попробуй ещё раз через минуту.', messageId);
    }
  } finally {
    // Order matters: drop the durable copy first, THEN leave the in-flight set,
    // so the sweeper can never see an unclaimed file for a turn we just sent.
    clearPending(turnId);
    inFlightTurns.delete(turnId);
  }
}

// --- Durable reply handoff -------------------------------------------------
// Bridges persist each finished reply to <RUN_DIR>/pending/<turnId>.json before
// emitting `done` (see pending.js). We deliver from disk every 30s plus once at
// boot, skipping turns this process is actively streaming and anything younger
// than the grace window, so a live send is never duplicated.
const PENDING_DIR = process.env.PENDING_DIR || path.join(RUN_DIR, 'pending');
const PENDING_SWEEP_MS = 30 * 1000;
const PENDING_GRACE_MS = 90 * 1000;        // younger than this = probably still being sent live
const PENDING_TTL_MS = 6 * 60 * 60 * 1000; // older than this = too stale to be useful
const inFlightTurns = new Set();

function clearPending(turnId) {
  try { fs.unlinkSync(path.join(PENDING_DIR, `${turnId}.json`)); } catch {}
}

let sweeping = false;
async function sweepPending(reason) {
  if (sweeping) return;
  sweeping = true;
  try {
    let names;
    try { names = fs.readdirSync(PENDING_DIR); } catch { return; }
    for (const n of names) {
      if (!n.endsWith('.json')) continue;
      const turnId = n.slice(0, -5);
      if (inFlightTurns.has(turnId)) continue;
      const file = path.join(PENDING_DIR, n);
      let st;
      try { st = fs.statSync(file); } catch { continue; }
      const age = Date.now() - st.mtimeMs;
      if (age < PENDING_GRACE_MS) continue;
      let rec;
      try { rec = JSON.parse(fs.readFileSync(file, 'utf8')); }
      catch { try { fs.unlinkSync(file); } catch {} continue; }
      if (age > PENDING_TTL_MS) {
        console.error(`[pending] dropping stale reply ${n} (age ${Math.round(age / 60000)}m)`);
        try { fs.unlinkSync(file); } catch {}
        continue;
      }
      if (!rec.chatId || !rec.text) { try { fs.unlinkSync(file); } catch {} continue; }
      console.log(`[pending] delivering orphaned reply ${n} (age ${Math.round(age / 1000)}s, ${reason})`);
      try {
        await sendResponse(rec.chatId, rec.text, rec.replyTo || undefined, {
          footer: `⏳ доставлено с опозданием (бот перезапускался во время обработки)`,
        });
      } catch (e) {
        console.error(`[pending] delivery failed: ${e.message}`);
      }
      // Unlink either way: a permanent failure must not loop forever.
      try { fs.unlinkSync(file); } catch {}
    }
  } finally {
    sweeping = false;
  }
}
setInterval(() => { sweepPending('sweep').catch(() => {}); }, PENDING_SWEEP_MS);
setTimeout(() => { sweepPending('boot').catch(() => {}); }, 5000);

// Extract text from replied message for context
function getReplyContext(msg) {
  if (!msg.reply_to_message) return '';
  const reply = msg.reply_to_message;
  let context = '';
  if (reply.text) {
    context = reply.text;
  } else if (reply.caption) {
    context = reply.caption;
  }
  if (context) {
    return `[Replying to: "${context.substring(0, 500)}"]\n\n`;
  }
  return '';
}

// Get sender prefix for group chats
function getSenderPrefix(msg) {
  if (msg.chat.type === 'group' || msg.chat.type === 'supergroup') {
    const name = msg.from.first_name + (msg.from.last_name ? ` ${msg.from.last_name}` : '');
    return `[From: ${name}] `;
  }
  return '';
}

// Command: /chatid — debug helper. Intentionally does NOT check isAllowed so it
// works in newly-added groups before they're wired into the registry.
bot.onText(/\/chatid/, async (msg) => {
  try {
    const reply =
      `chat.id: \`${msg.chat.id}\`\n` +
      `chat.type: ${msg.chat.type}\n` +
      `chat.title: ${msg.chat.title || '(none)'}\n` +
      `from: ${msg.from.first_name || ''} ${msg.from.last_name || ''} (id ${msg.from.id})`;
    await bot.sendMessage(msg.chat.id, reply, { parse_mode: 'Markdown' });
  } catch (err) {
    console.error('chatid error:', err.message);
  }
});

// Command: /status
bot.onText(/\/status/, async (msg) => {
  if (!isAllowed(msg)) return;
  const sock = getSocket(msg.chat.id);
  try {
    const { data } = await socketRequest(sock, 'GET', '/health');
    let text = `Status: ${data.status} (engine: ${data.engine || '?'})`;
    if (data.busy) {
      text += `\nBusy: yes (PID ${data.pid}, ${data.uptime}s)`;
    } else {
      text += '\nBusy: no';
    }
    text += `\nMessages: ${data.messageCount}`;
    await bot.sendMessage(msg.chat.id, text);
  } catch (err) {
    await bot.sendMessage(msg.chat.id, `Server unreachable: ${err.message}`);
  }
});

// Command: /kill
bot.onText(/\/kill/, async (msg) => {
  if (!isAllowed(msg)) return;
  const sock = getSocket(msg.chat.id);
  try {
    const { data } = await socketRequest(sock, 'POST', '/kill');
    if (data.killed) {
      await bot.sendMessage(msg.chat.id, `Killed process ${data.pid}`);
    } else {
      await bot.sendMessage(msg.chat.id, data.message || 'No process running');
    }
  } catch (err) {
    await bot.sendMessage(msg.chat.id, `Error: ${err.message}`);
  }
});

// Command: /clear
bot.onText(/\/clear/, async (msg) => {
  if (!isAllowed(msg)) return;
  const sock = getSocket(msg.chat.id);
  try {
    await socketRequest(sock, 'POST', '/clear');
    await bot.sendMessage(msg.chat.id, 'Диалог сброшен. Следующий вопрос начнёт новую беседу.');
  } catch (err) {
    await bot.sendMessage(msg.chat.id, `Error: ${err.message}`);
  }
});

// Command: /pro — force escalation to the strong engine (claude). The text
// handler skips '/' messages, so this must be an explicit command. ask.py
// recognizes the "PRO:" prefix and routes accordingly.
bot.onText(/^\/pro(?:@\w+)?(?:\s+([\s\S]+))?$/, async (msg, match) => {
  if (!isAllowed(msg)) return;
  const q = (match[1] || '').trim();
  if (!q) {
    await bot.sendMessage(msg.chat.id, 'Использование: /pro <вопрос> — отвечает более сильная модель (медленнее).');
    return;
  }
  const prompt = getSenderPrefix(msg) + getReplyContext(msg) + 'PRO: ' + q;
  await handlePrompt(msg.chat.id, msg.message_id, prompt);
});

// Command: /docs — list the document catalog straight from disk (no engine call).
bot.onText(/\/docs/, async (msg) => {
  if (!isAllowed(msg)) return;
  if (!CATALOG_PATH) {
    await bot.sendMessage(msg.chat.id, 'Catalog path not configured.');
    return;
  }
  try {
    // The catalog carries topics and keywords for the router; dumping it raw
    // is ~28k chars of keyword soup across seven Telegram messages. George
    // wants to know WHICH documents exist, so send titles only.
    const catalog = fs.readFileSync(CATALOG_PATH, 'utf8');
    const titles = catalog.split('\n')
      .filter(l => l.startsWith('- '))
      .map(l => (l.split('|')[1] || '').trim())
      .filter(Boolean);
    if (!titles.length) {
      await bot.sendMessage(msg.chat.id, 'Список документов пуст — база ещё не построена.');
      return;
    }
    const list = titles.map((t, i) => `${i + 1}. ${t}`).join('\n');
    await sendResponse(msg.chat.id, `Документов в базе: ${titles.length}\n\n${list}`,
                       msg.message_id);
  } catch (err) {
    await bot.sendMessage(msg.chat.id, `Не могу прочитать каталог: ${err.message}`);
  }
});

// Command: /start
bot.onText(/\/start/, async (msg) => {
  if (!isAllowed(msg)) return;
  await bot.sendMessage(msg.chat.id,
    'Помощник по стандартам готов. Задай вопрос обычным сообщением.\n' +
    'Команды: /docs — список документов, /pro <вопрос> — сильная модель, /clear — сбросить диалог, /status — состояние.');
});

// Command: /files — list staged uploads
bot.onText(/\/files/, async (msg) => {
  if (!isAllowed(msg)) return;
  const staged = stagedFiles.get(msg.chat.id) || [];
  if (!staged.length) {
    await bot.sendMessage(msg.chat.id, 'No files staged.');
    return;
  }
  const list = staged.map((f, i) => `${i + 1}. ${f.name}`).join('\n');
  await bot.sendMessage(msg.chat.id, `${staged.length} file(s) staged:\n${list}\n\nSend instructions to process, or /discard to clear.`);
});

// Command: /discard — clear staged uploads
bot.onText(/\/discard/, async (msg) => {
  if (!isAllowed(msg)) return;
  const n = clearStaged(msg.chat.id);
  await bot.sendMessage(msg.chat.id, n ? `Discarded ${n} staged file(s).` : 'No files staged.');
});

// Telegram cloud Bot API caps getFile at 20MB (self-hosted: 2GB). Guard so the
// polling loop never gets wedged by a big upload.
const TELEGRAM_BOT_API_MAX_BYTES = process.env.TELEGRAM_BASE_API_URL
  ? 2000 * 1024 * 1024
  : 20 * 1024 * 1024;

bot.on('photo', async (msg) => {
  if (!isAllowed(msg)) return;

  const photos = msg.photo;
  const largest = photos[photos.length - 1];
  const timestamp = Date.now();
  const filename = `photo_${timestamp}_${msg.message_id}.jpg`;

  if (largest.file_size && largest.file_size > TELEGRAM_BOT_API_MAX_BYTES) {
    await bot.sendMessage(msg.chat.id,
      `Фото слишком большое (${Math.round(largest.file_size / 1024 / 1024)} MB).`,
      { reply_to_message_id: msg.message_id }).catch(() => {});
    return;
  }

  try {
    const dir = uploadDir(msg.chat.id);
    const savedPath = await downloadFile(largest.file_id, filename, dir);

    if (STAGING_CHAT_IDS.has(msg.chat.id)) {
      stageFile(msg.chat.id, { hostPath: savedPath, name: filename, isImage: true });
      return;
    }

    const caption = msg.caption || 'Что на этом фото? Ответь с учётом стандартов.';
    const prompt = `${getSenderPrefix(msg)}${getReplyContext(msg)}[Image attached: ${savedPath}]\n${caption}`;
    await handlePrompt(msg.chat.id, msg.message_id, prompt);
  } catch (err) {
    await bot.sendMessage(msg.chat.id, `Failed to process photo: ${err.message}`);
  }
});

// Handle document messages
bot.on('document', async (msg) => {
  if (!isAllowed(msg)) return;

  const doc = msg.document;
  const timestamp = Date.now();
  const safeName = (doc.file_name || `file_${timestamp}`).replace(/[^a-zA-Z0-9._-]/g, '_');
  const filename = `${timestamp}_${safeName}`;

  if (doc.file_size && doc.file_size > TELEGRAM_BOT_API_MAX_BYTES) {
    await bot.sendMessage(msg.chat.id,
      `Файл "${doc.file_name || '?'}" слишком большой (${Math.round(doc.file_size / 1024 / 1024)} MB).`,
      { reply_to_message_id: msg.message_id }).catch(() => {});
    return;
  }

  try {
    const dir = uploadDir(msg.chat.id);
    const savedPath = await downloadFile(doc.file_id, filename, dir);

    if (STAGING_CHAT_IDS.has(msg.chat.id)) {
      const ext = path.extname(doc.file_name || '').toLowerCase();
      const imageExts = ['.png', '.jpg', '.jpeg', '.svg'];
      stageFile(msg.chat.id, { hostPath: savedPath, name: doc.file_name || filename, isImage: imageExts.includes(ext) });
      return;
    }

    const caption = msg.caption || `Process this file: ${doc.file_name}`;
    const prompt = `${getSenderPrefix(msg)}${getReplyContext(msg)}[File attached: ${savedPath} (${doc.file_name})]\n${caption}`;
    await handlePrompt(msg.chat.id, msg.message_id, prompt);
  } catch (err) {
    await bot.sendMessage(msg.chat.id, `Failed to process file: ${err.message}`);
  }
});

// Handle text messages (not commands)
bot.on('text', async (msg) => {
  if (!isAllowed(msg)) return;
  if (msg.text.startsWith('/')) return;

  const replyContext = getReplyContext(msg);
  const senderPrefix = getSenderPrefix(msg);

  // Staging chats: if files are queued, attach them all to this instruction
  const staged = stagedFiles.get(msg.chat.id);
  if (staged && staged.length) {
    clearStaged(msg.chat.id);
    const fileList = staged.map((f, i) => f.isImage
      ? `[Image ${i + 1}: ${f.hostPath}]`
      : `[File attached: ${f.hostPath} (${f.name})]`).join('\n');
    const prompt = `${senderPrefix}${replyContext}${fileList}\n${msg.text}`;
    await handlePrompt(msg.chat.id, msg.message_id, prompt);
    return;
  }

  const prompt = senderPrefix + replyContext + msg.text;
  await handlePrompt(msg.chat.id, msg.message_id, prompt);
});

// Polling-recovery watchdog. On a sustained error streak, exit the process and
// let the service manager (NSSM AppExit=Restart) relaunch a fresh single-loop
// instance — an in-process stopPolling()/startPolling() cycle can spiral into
// two overlapping getUpdates loops (409 storms + duplicate replies).
const POLL_ERR_LIMIT = 5;
let pollErrorStreak = 0;
let bailing = false;
// Declared before bailForRestart so an early uncaughtException cannot trip
// over the temporal dead zone on its way out.
let heartbeatTimer = null;

function bailForRestart(reason) {
  if (bailing) return;
  bailing = true;
  console.error(`[watchdog] exiting for a clean restart: ${reason}`);
  // Stop the heartbeat first: from this moment the process is deliberately on
  // its way out, and anything watching from outside should see it go stale
  // even if the exit below is delayed or never happens.
  clearInterval(heartbeatTimer);
  try { fs.unlinkSync(HEARTBEAT_FILE); } catch {}
  try { bot.stopPolling({ cancel: true }); } catch {}
  // The old code waited a full second on a timer before exiting, to let the
  // log line above flush. That is fine when the loop is healthy and worthless
  // when it is not: a blocked loop never runs the timer, and the process sits
  // there alive but deaf, which looks identical to "working" from the service
  // manager's point of view. Exit on the next tick instead, and accept that no
  // in-process guard can cover a fully wedged loop - that is what the
  // heartbeat file above is for, and ops/ctl.ps1 is what acts on it.
  setImmediate(() => process.exit(1));
  setTimeout(() => process.exit(1), 500).unref();
}

function noteLiveActivity() { pollErrorStreak = 0; }
bot.on('message', noteLiveActivity);

// Liveness stamp for outside observers. Windows reports a service as Running
// whenever its supervisor holds the slot, which stayed true here for 2.5h
// while the bot was gone entirely - so service state cannot be the health
// signal. The stamp is only refreshed while the process is BOTH running its
// event loop AND not in a polling-error streak, which makes a stale file mean
// "not answering Telegram" rather than merely "not scheduled recently".
const HEARTBEAT_FILE = process.env.HEARTBEAT_FILE || path.join(RUN_DIR, 'bot.alive');
const HEARTBEAT_MS = 30 * 1000;

function beat() {
  if (bailing || pollErrorStreak !== 0) return;
  try { fs.writeFileSync(HEARTBEAT_FILE, new Date().toISOString()); } catch {}
}

try { fs.mkdirSync(RUN_DIR, { recursive: true }); } catch {}
heartbeatTimer = setInterval(beat, HEARTBEAT_MS);
heartbeatTimer.unref();
beat();

bot.on('polling_error', (err) => {
  pollErrorStreak++;
  console.error(`Polling error (streak ${pollErrorStreak}/${POLL_ERR_LIMIT}):`, err.message);
  if (pollErrorStreak >= POLL_ERR_LIMIT) {
    bailForRestart(`${pollErrorStreak} consecutive polling errors, last: ${err.message}`);
  }
});

bot.on('error', (err) => {
  console.error('Bot error:', err.message);
});

// Safety net for stray promise rejections (e.g. getFileStream rejecting via a
// path that bypasses try/catch — took the original bot silent for 4 days).
process.on('unhandledRejection', (reason) => {
  const msg = reason && (reason.stack || reason.message || String(reason));
  console.error('[unhandledRejection]', msg);
  bailForRestart(`unhandled rejection: ${(reason && reason.message) || String(reason)}`);
});

// Periodic cleanser: the getUpdates long-poll can silently stall (no error, no
// polling_error). Refresh the process every N hours as a floor on staleness.
// DRAINS first: defers while any bridge is mid-turn, rechecking every 30s, so a
// conversation being answered is never frozen. Replies are durable regardless
// (pending sweeper), so even a forced bail at the uptime ceiling is recoverable.
const PERIODIC_RESTART_MS = 60 * 60 * 1000;   // 1 hour
const DRAIN_RETRY_MS = 30 * 1000;             // recheck cadence while deferring
const UPTIME_CEILING_MS = 6 * 60 * 60 * 1000; // hard floor on how stale polling may get

async function anyBridgeBusy() {
  const socks = new Set(Object.values(SOCKET_MAP));
  if (DEFAULT_SOCKET) socks.add(DEFAULT_SOCKET);
  for (const sock of socks) {
    try {
      const { data } = await socketRequest(sock, 'GET', '/health');
      if (data && (data.busy || (data.queueDepth || 0) > 0)) return true;
    } catch { /* dead/unreachable socket = not busy */ }
  }
  return false;
}

const BOOT_TIME = Date.now();
let deferredSince = null;

async function periodicRestart() {
  let busy = false;
  try { busy = await anyBridgeBusy(); } catch {}

  if (busy && Date.now() - BOOT_TIME < UPTIME_CEILING_MS) {
    if (!deferredSince) {
      deferredSince = Date.now();
      console.log('[watchdog] periodic refresh deferred: a bridge is mid-turn');
    }
    setTimeout(periodicRestart, DRAIN_RETRY_MS);
    return;
  }

  if (busy) {
    console.error('[watchdog] uptime ceiling reached with a bridge still busy; restarting anyway (the reply will be redelivered from the pending store)');
  } else if (deferredSince) {
    console.log(`[watchdog] bridges idle after deferring ${Math.round((Date.now() - deferredSince) / 1000)}s; restarting now`);
  }
  bailForRestart('periodic 1h refresh, pre-empting silent long-poll stalls (drained)');
}
setTimeout(periodicRestart, PERIODIC_RESTART_MS);

process.on('uncaughtException', (err) => {
  console.error('[uncaughtException]', err && (err.stack || err.message || String(err)));
  bailForRestart(`uncaught exception: ${(err && err.message) || String(err)}`);
});

console.log('Telegram bot started');
