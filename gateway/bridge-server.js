const http = require('http');
const { spawn } = require('child_process');
const { createInterface } = require('readline');
const fs = require('fs');
const path = require('path');

// Generic engine bridge — Windows/Linux port of seiv/dev_bot/bridge-server.js.
// One HTTP server per project, listening on a unix socket (Linux) or named pipe
// (Windows, e.g. \\.\pipe\standards-bridge). Spawns ONE engine process per turn.
//
// Env (per-service configuration; see ops/install.ps1):
//   BRIDGE_NAME          log label, e.g. "standards"
//   BRIDGE_SOCKET        unix socket path OR \\.\pipe\<name>
//   BRIDGE_STATE         JSON state file (session flag + token count)
//   BRIDGE_WORKDIR       cwd for the engine
//   BRIDGE_PROMPT_FILE   --append-system-prompt file (claude mode only)
//   BRIDGE_PENDING_DIR   durable-reply dir shared with the bot
//
// Engine selection (NEW vs the original, which hardcoded `claude`):
//   BRIDGE_SPAWN         JSON array with {message} placeholder, e.g.
//                        ["python","C:/Standards/bot/ask.py","-p","{message}"]
//                        Unset -> original claude behaviour.
//   BRIDGE_OUTPUT        "plain" (stdout text is the reply — use with ask.py)
//                        or "stream-json" (claude --output-format stream-json).
//                        Defaults: plain when BRIDGE_SPAWN is set, else stream-json.
//   BRIDGE_FRESH_ARG     arg appended in plain mode when the turn is fresh
//                        (default "--fresh")
//   BRIDGE_ENGINE_STATE  extra state file /clear should delete (ask.py history)
//   COMPACT_AT_TOKENS    claude mode only; 0 disables (default 850000)

const NAME = process.env.BRIDGE_NAME || 'bridge';
const SOCKET_PATH = process.env.BRIDGE_SOCKET;
const STATE_PATH = process.env.BRIDGE_STATE;
const WORK_DIR = process.env.BRIDGE_WORKDIR;
const IS_PIPE = /^\\\\[.?]\\pipe\\/.test(SOCKET_PATH || '');
const SYSTEM_PROMPT = (() => {
  const f = process.env.BRIDGE_PROMPT_FILE;
  if (f) { try { return fs.readFileSync(f, 'utf8').trim(); } catch {} }
  return process.env.BRIDGE_PROMPT
    || 'You are responding via a Telegram group chat. Keep responses concise.';
})();
const SPAWN_TEMPLATE = (() => {
  const raw = process.env.BRIDGE_SPAWN;
  if (!raw) return null;
  try {
    const arr = JSON.parse(raw);
    if (Array.isArray(arr) && arr.length && arr.every(x => typeof x === 'string')) return arr;
  } catch {}
  console.error(`[${NAME}] BRIDGE_SPAWN is not a JSON string array — refusing to start`);
  process.exit(1);
})();
const OUTPUT_MODE = process.env.BRIDGE_OUTPUT || (SPAWN_TEMPLATE ? 'plain' : 'stream-json');
const FRESH_ARG = process.env.BRIDGE_FRESH_ARG || '--fresh';
const ENGINE_STATE = process.env.BRIDGE_ENGINE_STATE || null;
const PROGRESS_PATH = path.join(WORK_DIR || '.', '.progress');
const { savePending } = require('./pending');

// Never forward harness plumbing (task notifications) to Telegram.
const TASK_NOTIF_RE = /<task-notification>[\s\S]*?<\/task-notification>/g;
function stripSystemNotifications(text) {
  if (!text) return '';
  const t = String(text);
  if (/^\s*(?:Human:\s*)?SYSTEM NOTIFICATION - NOT USER INPUT/.test(t)) return '';
  return t.replace(TASK_NOTIF_RE, '').trim();
}

if (!SOCKET_PATH || !STATE_PATH || !WORK_DIR) {
  console.error(`[${NAME}] BRIDGE_SOCKET, BRIDGE_STATE and BRIDGE_WORKDIR are all required`);
  process.exit(1);
}

const MAX_RUNTIME_MS = Number(process.env.BRIDGE_MAX_RUNTIME_MS || 60 * 60 * 1000);
const PROGRESS_POLL_MS = 1000;
// Claude mode only. In plain mode the engine owns its own history size.
const COMPACT_AT_TOKENS = OUTPUT_MODE === 'plain' ? 0
  : Number(process.env.COMPACT_AT_TOKENS || 850000);
const MAX_QUEUE_DEPTH = 10;

let currentProcess = null;
let currentPid = null;
let startTime = null;
let reaperTimer = null;

// Single-flight serializer — every /prompt + /compact runs in arrival order.
let chain = Promise.resolve();
let queueDepth = 0;

let state = { messageCount: 0, hasSession: false, contextTokens: 0 };

function loadState() {
  try {
    state = JSON.parse(fs.readFileSync(STATE_PATH, 'utf8'));
    console.log(`[${NAME}] Loaded state: ${state.messageCount} messages, hasSession=${state.hasSession}`);
  } catch {
    state = { messageCount: 0, hasSession: false, contextTokens: 0 };
    console.log(`[${NAME}] No state file — first message will start a new conversation.`);
  }
  if (typeof state.contextTokens !== 'number') state.contextTokens = 0;
}

function saveState() {
  try { fs.writeFileSync(STATE_PATH, JSON.stringify(state)); } catch {}
}

function cleanupSocket() {
  if (IS_PIPE) return; // named pipes vanish with the listener; unlink would throw
  try { fs.unlinkSync(SOCKET_PATH); } catch {}
}

function killCurrent(reason) {
  if (currentProcess && currentPid) {
    console.log(`[${NAME}] Killing PID ${currentPid}: ${reason}`);
    // SIGKILL maps to TerminateProcess on Windows — same effect.
    try { process.kill(currentPid, 'SIGKILL'); } catch {}
    currentProcess = null;
    currentPid = null;
    startTime = null;
    if (reaperTimer) { clearTimeout(reaperTimer); reaperTimer = null; }
  }
}

function parseBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', c => chunks.push(c));
    req.on('end', () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString())); }
      catch (e) { reject(e); }
    });
    req.on('error', reject);
  });
}

function makeEnv() {
  const env = { ...process.env };
  delete env.CLAUDECODE;
  return env;
}

function doCompact() {
  return new Promise((resolve) => {
    console.log(`[${NAME}] Running /compact...`);
    const child = spawn('claude', [
      '--continue',
      '--print',
      '--dangerously-skip-permissions',
      '-p', '/compact',
      // shell is safe here and nowhere else in this file: every argument is a
      // fixed literal, none of it comes from a user message, and on Windows a
      // shell is what resolves the `claude` .cmd shim. Only reachable in
      // stream-json (claude) mode; plain-engine mode disables compaction.
    ], { cwd: WORK_DIR, env: makeEnv(), stdio: ['ignore', 'pipe', 'pipe'], shell: process.platform === 'win32' });

    let out = '';
    let timedOut = false;
    child.stdout.on('data', c => { out += c.toString(); });
    child.on('close', (code) => {
      const ok = code === 0 && !timedOut;
      console.log(`[${NAME}] Compact done (code ${code}, ok=${ok}): ${out.substring(0, 200)}`);
      resolve({ ok, code });
    });
    setTimeout(() => {
      timedOut = true;
      try { child.kill(); } catch {}
      console.log(`[${NAME}] Compact subprocess hit 5-min hard timeout — killing.`);
      resolve({ ok: false, code: 124 });
    }, 5 * 60 * 1000);
  });
}

// Build [cmd, ...args] for one turn.
function buildSpawn(message, opts = {}) {
  if (SPAWN_TEMPLATE) {
    const argv = SPAWN_TEMPLATE.map(a => a.replace('{message}', message));
    if (opts.fresh) argv.push(FRESH_ARG);
    return argv;
  }
  const args = [
    '--output-format', 'stream-json',
    '--verbose',
    '--print',
    '--dangerously-skip-permissions',
  ];
  if (state.hasSession && !opts.fresh) args.push('--continue');
  args.push('--append-system-prompt', SYSTEM_PROMPT, '-p', message);
  return ['claude', ...args];
}

async function handlePrompt(req, res) {
  let body;
  try {
    body = await parseBody(req);
  } catch (err) {
    res.writeHead(400, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: `Bad request: ${err.message}` }));
    return;
  }

  if (!body.message) {
    res.writeHead(400, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'message is required' }));
    return;
  }

  if (queueDepth >= MAX_QUEUE_DEPTH) {
    res.writeHead(503, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'queue full', depth: queueDepth, max: MAX_QUEUE_DEPTH }));
    return;
  }

  queueDepth++;
  const myPosition = queueDepth;
  const waitStart = Date.now();

  res.writeHead(200, {
    'Content-Type': 'application/x-ndjson',
    'Transfer-Encoding': 'chunked',
  });
  if (myPosition > 1) {
    console.log(`[${NAME}] Request queued at position ${myPosition} (behind ${myPosition - 1} in-flight)`);
    try { res.write(JSON.stringify({ type: 'queued', ahead: myPosition - 1 }) + '\n'); } catch {}
  }

  const myTurn = chain.then(async () => {
    const waited = Date.now() - waitStart;
    if (waited > 200) {
      console.log(`[${NAME}] Dequeued after ${Math.round(waited / 1000)}s wait`);
      try { res.write(JSON.stringify({ type: 'queued_done', waited_ms: waited }) + '\n'); } catch {}
    }
    await runSpawn(body, res);
  });
  chain = myTurn.catch(() => {});

  try { await myTurn; }
  finally { queueDepth--; }
}

function runSpawn(body, res) {
  return new Promise(async (resolve) => {
    const { message, fresh } = body;

    if (COMPACT_AT_TOKENS > 0 && state.hasSession && state.contextTokens >= COMPACT_AT_TOKENS) {
      console.log(`[${NAME}] Context ${state.contextTokens} tokens >= ${COMPACT_AT_TOKENS}, compacting...`);
      try {
        const compact = await doCompact();
        if (compact.ok) {
          state.contextTokens = 0;
          saveState();
        } else {
          console.warn(`[${NAME}] Compact failed (code ${compact.code}); resetting session — next turn starts fresh.`);
          state.hasSession = false;
          state.contextTokens = 0;
          state.messageCount = 0;
          saveState();
        }
      } catch (err) {
        console.error(`[${NAME}] Compact failed:`, err.message);
        state.hasSession = false;
        state.contextTokens = 0;
        state.messageCount = 0;
        saveState();
      }
    }

    const [cmd, ...args] = buildSpawn(message, { fresh });
    const env = makeEnv();

    try { fs.writeFileSync(PROGRESS_PATH, ''); } catch {}
    let progressOffset = 0;

    console.log(`[${NAME}] Spawning ${cmd} (msg #${state.messageCount + 1}): ${message.substring(0, 100)}...`);

    const child = spawn(cmd, args, {
      cwd: WORK_DIR,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
      // NO shell. With shell:true node concatenates argv into one command
      // string and cmd.exe re-splits it on whitespace, so a question like
      // "какого цвета провод" arrived at the engine as four arguments. Worse,
      // that string is Telegram-supplied and unescaped (node's DEP0190), so a
      // message containing & or | would have been a command-injection vector.
      // Without a shell the array goes straight to CreateProcess intact.
      // BRIDGE_SPAWN must therefore name a real executable (python.exe,
      // node.exe), never a .cmd/.bat shim.
    });

    currentProcess = child;
    currentPid = child.pid;
    startTime = Date.now();

    console.log(`[${NAME}] Engine PID: ${currentPid}`);

    reaperTimer = setTimeout(() => {
      killCurrent(`${Math.round(MAX_RUNTIME_MS / 60000)}-minute timeout`);
    }, MAX_RUNTIME_MS);

    const progressPoll = setInterval(() => {
      try {
        const stat = fs.statSync(PROGRESS_PATH);
        if (stat.size > progressOffset) {
          const fd = fs.openSync(PROGRESS_PATH, 'r');
          const len = stat.size - progressOffset;
          const buf = Buffer.alloc(len);
          fs.readSync(fd, buf, 0, len, progressOffset);
          fs.closeSync(fd);
          progressOffset = stat.size;
          const chunk = buf.toString('utf8');
          for (const line of chunk.split('\n')) {
            const text = line.trim();
            if (!text) continue;
            try {
              res.write(JSON.stringify({ type: 'progress_message', text }) + '\n');
            } catch {}
          }
        } else if (stat.size < progressOffset) {
          progressOffset = 0;
        }
      } catch {
        // .progress not written yet — fine.
      }
    }, PROGRESS_POLL_MS);

    try { res.write(JSON.stringify({ type: 'started', pid: child.pid }) + '\n'); } catch {}

    let fullText = '';
    let plainOut = '';
    let lastChars = 0;
    let lastToolName = '';
    let turnContextTokens = null;

    if (OUTPUT_MODE === 'plain') {
      child.stdout.on('data', c => {
        plainOut += c.toString();
        if (plainOut.length - lastChars >= 200) {
          try { res.write(JSON.stringify({ type: 'progress', chars: plainOut.length }) + '\n'); } catch {}
          lastChars = plainOut.length;
        }
      });
    } else {
      const rl = createInterface({ input: child.stdout });
      rl.on('line', line => {
        if (!line.trim()) return;
        try {
          const event = JSON.parse(line);

          if (event.type === 'content_block_delta') {
            const delta = event.delta;
            if (delta && delta.type === 'text_delta' && delta.text) {
              fullText += delta.text;
              if (fullText.length - lastChars >= 200) {
                try { res.write(JSON.stringify({ type: 'progress', chars: fullText.length }) + '\n'); } catch {}
                lastChars = fullText.length;
              }
            }
          } else if (event.type === 'content_block_start') {
            const content = event.content_block;
            if (content && content.type === 'tool_use') {
              const toolName = content.name || 'unknown';
              if (toolName !== lastToolName) {
                lastToolName = toolName;
                try { res.write(JSON.stringify({ type: 'tool', name: toolName }) + '\n'); } catch {}
              }
            }
          } else if (event.type === 'assistant') {
            // Context size = input side of the LAST top-level API call, NOT the
            // `result` event (whose usage is summed across every call in the
            // turn). Subagent events carry parent_tool_use_id — skip them.
            const au = event.message && event.message.usage;
            if (au && !event.parent_tool_use_id) {
              turnContextTokens = (au.input_tokens || 0)
                + (au.cache_read_input_tokens || 0)
                + (au.cache_creation_input_tokens || 0);
            }
          } else if (event.type === 'result') {
            if (event.result) {
              const cleaned = stripSystemNotifications(event.result);
              if (cleaned) {
                fullText = cleaned;
              } else {
                console.log(`[${NAME}] dropped system-notification result, using streamed text`);
              }
            }
            // Fallback only: with zero assistant events the aggregate equals the
            // single call. Never overwrite a real reading.
            if (turnContextTokens === null && event.usage) {
              const u = event.usage;
              turnContextTokens = (u.input_tokens || 0)
                + (u.cache_read_input_tokens || 0)
                + (u.cache_creation_input_tokens || 0);
            }
          }
        } catch {}
      });
    }

    let stderrBuf = '';
    child.stderr.on('data', chunk => {
      stderrBuf += chunk.toString();
    });

    child.on('close', (code) => {
      const duration = Date.now() - (startTime || Date.now());
      console.log(`[${NAME}] Engine exited with code ${code} after ${Math.round(duration / 1000)}s`);

      if (stderrBuf.trim()) {
        console.log(`[${NAME}] stderr: ${stderrBuf.substring(0, 500)}`);
      }

      if (progressPoll) clearInterval(progressPoll);

      currentProcess = null;
      currentPid = null;
      startTime = null;
      if (reaperTimer) { clearTimeout(reaperTimer); reaperTimer = null; }

      if (OUTPUT_MODE === 'plain') fullText = plainOut.trim();
      const responseText = fullText || (stderrBuf ? `Error: ${stderrBuf.substring(0, 1000)}` : 'No output');

      state.messageCount++;
      state.hasSession = true;
      if (turnContextTokens !== null) {
        state.contextTokens = turnContextTokens;
        console.log(`[${NAME}] Context now ~${turnContextTokens} tokens (compact at ${COMPACT_AT_TOKENS})`);
      }
      saveState();

      try {
        // Persist the finished reply BEFORE writing it to the socket: if the bot
        // died mid-turn the write below lands in a dead socket and the answer
        // would be lost. The bot unlinks this file once delivered.
        savePending(body, responseText);
        res.write(JSON.stringify({
          type: 'done',
          text: responseText,
          duration,
          exitCode: code,
        }) + '\n');
        res.end();
      } catch {}
      resolve();
    });

    child.on('error', (err) => {
      console.error(`[${NAME}] Spawn error:`, err.message);
      if (progressPoll) clearInterval(progressPoll);
      currentProcess = null;
      currentPid = null;
      startTime = null;
      if (reaperTimer) { clearTimeout(reaperTimer); reaperTimer = null; }

      try {
        res.write(JSON.stringify({ type: 'done', text: `Spawn error: ${err.message}`, duration: 0, exitCode: -1 }) + '\n');
        res.end();
      } catch {}
      resolve();
    });
  });
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');

  if (req.method === 'GET' && url.pathname === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      status: 'ok',
      name: NAME,
      engine: SPAWN_TEMPLATE ? SPAWN_TEMPLATE[0] : 'claude',
      busy: !!currentProcess,
      pid: currentPid,
      uptime: startTime ? Math.round((Date.now() - startTime) / 1000) : null,
      queueDepth,
      maxQueueDepth: MAX_QUEUE_DEPTH,
      messageCount: state.messageCount,
      contextTokens: state.contextTokens,
      nextCompactAt: COMPACT_AT_TOKENS > 0
        ? `${Math.max(0, COMPACT_AT_TOKENS - state.contextTokens)} tokens (${state.contextTokens}/${COMPACT_AT_TOKENS})`
        : 'disabled',
    }));
    return;
  }

  if (req.method === 'POST' && url.pathname === '/prompt') {
    handlePrompt(req, res);
    return;
  }

  if (req.method === 'POST' && url.pathname === '/kill') {
    if (currentProcess) {
      const pid = currentPid;
      killCurrent('user requested /kill');
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ killed: true, pid }));
    } else {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ killed: false, message: 'No process running' }));
    }
    return;
  }

  if (req.method === 'POST' && url.pathname === '/clear') {
    state.messageCount = 0;
    state.hasSession = false;
    saveState();
    // Plain-mode engines keep their own conversation history — clear it too.
    if (ENGINE_STATE) { try { fs.unlinkSync(ENGINE_STATE); } catch {} }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ cleared: true }));
    return;
  }

  if (req.method === 'POST' && url.pathname === '/compact') {
    if (OUTPUT_MODE === 'plain') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ compacted: false, error: 'not applicable to this engine' }));
      return;
    }
    if (queueDepth >= MAX_QUEUE_DEPTH) {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'queue full', depth: queueDepth, max: MAX_QUEUE_DEPTH }));
      return;
    }
    queueDepth++;
    const myTurn = chain.then(() => doCompact());
    chain = myTurn.catch(() => {});
    myTurn.then(result => {
      if (result.ok) {
        state.contextTokens = 0;
        saveState();
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ compacted: result.ok, code: result.code }));
    }).catch(err => {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message }));
    }).finally(() => { queueDepth--; });
    return;
  }

  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'Not found' }));
});

cleanupSocket();
loadState();

server.listen(SOCKET_PATH, () => {
  if (!IS_PIPE) { try { fs.chmodSync(SOCKET_PATH, 0o777); } catch {} }
  console.log(`[${NAME}] Listening on ${SOCKET_PATH} (cwd ${WORK_DIR}, engine ${SPAWN_TEMPLATE ? SPAWN_TEMPLATE.join(' ') : 'claude'}, output ${OUTPUT_MODE})`);
});

process.on('SIGTERM', () => {
  console.log(`[${NAME}] SIGTERM received`);
  killCurrent('server shutting down');
  server.close(() => { cleanupSocket(); process.exit(0); });
});

process.on('SIGINT', () => {
  console.log(`[${NAME}] SIGINT received`);
  killCurrent('server shutting down');
  server.close(() => { cleanupSocket(); process.exit(0); });
});
