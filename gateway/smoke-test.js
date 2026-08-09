// End-to-end check of the bridge without Telegram in the way.
//
//   node smoke-test.js "<question>" [socketOrPipe]
//
// Exercises exactly what bot.js does: POST /prompt, read the ndjson event
// stream, and confirm the bridge persisted the reply to the pending store
// before it emitted `done` (that ordering is what makes a reply survive a bot
// restart mid-turn). Prints a pass/fail summary and exits non-zero on failure.
//
// curl cannot speak to a Windows named pipe, which is why this exists.

const http = require('http');
const fs = require('fs');
const path = require('path');

const question = process.argv[2] || 'тест';
const socketPath = process.argv[3] || process.env.BRIDGE_SOCKET ||
                   '\\\\.\\pipe\\standards-bridge';
const pendingDir = process.env.BRIDGE_PENDING_DIR ||
                   path.join(__dirname, 'run', 'pending');

const turnId = `smoke_${Date.now()}`;
const body = { message: question, turnId, chatId: 1, replyTo: 1 };
const seen = [];
let finalText = '';
let pendingSeenBeforeDone = false;

const req = http.request(
  { socketPath, path: '/prompt', method: 'POST',
    headers: { 'Content-Type': 'application/json' } },
  (res) => {
    if (res.statusCode !== 200) {
      console.error(`FAIL: /prompt returned ${res.statusCode}`);
      process.exit(1);
    }
    let buf = '';
    res.on('data', (chunk) => {
      buf += chunk.toString();
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        seen.push(ev.type);
        if (ev.type === 'progress_message') console.log(`  progress: ${ev.text}`);
        if (ev.type === 'done') {
          finalText = ev.text;
          // The bridge writes the pending file BEFORE emitting done; if that
          // ordering ever regresses, a bot restart mid-turn loses the reply.
          pendingSeenBeforeDone =
            fs.existsSync(path.join(pendingDir, `${turnId}.json`));
        }
      }
    });
    res.on('end', () => {
      // A `done` event carrying an error string is still a failure. The first
      // run of this test reported PASS while the engine was actually printing
      // "ask.py: error: unrecognized arguments" - the bridge had split the
      // question across argv. Check the payload, not just the event sequence.
      const looksBroken = /usage:|error:|Traceback|Ошибка|не найден/i.test(finalText);
      const ok = seen.includes('started') && seen.includes('done') &&
                 finalText && !looksBroken;
      if (looksBroken) console.error('\nFAIL: engine returned an error, not an answer');
      console.log(`\nevents      : ${seen.join(' -> ')}`);
      console.log(`pending file: ${pendingSeenBeforeDone ? 'written before done (correct)' : 'MISSING'}`);
      console.log(`answer      :\n${finalText}`);
      if (!ok) { console.error('\nFAIL: incomplete event stream'); process.exit(1); }
      if (!pendingSeenBeforeDone) {
        console.error('\nFAIL: durable-reply file was not written before done');
        process.exit(1);
      }
      // Leave nothing behind for the bot's sweeper to redeliver.
      try { fs.unlinkSync(path.join(pendingDir, `${turnId}.json`)); } catch {}
      console.log('\nPASS');
    });
  });

req.on('error', (e) => {
  console.error(`FAIL: cannot reach bridge at ${socketPath}: ${e.message}`);
  process.exit(1);
});
req.write(JSON.stringify(body));
req.end();
