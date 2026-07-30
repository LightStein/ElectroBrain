// Prints a bridge's /health JSON to stdout. Used by ops/ctl.ps1 because curl
// cannot speak to Windows named pipes. Exit 0 = reachable, 1 = not.
//   node healthcheck.js [socketPathOrPipe]
const http = require('http');
const socketPath = process.argv[2] || process.env.BRIDGE_SOCKET || '\\\\.\\pipe\\standards-bridge';

const req = http.get({ socketPath, path: '/health' }, res => {
  const chunks = [];
  res.on('data', c => chunks.push(c));
  res.on('end', () => {
    process.stdout.write(Buffer.concat(chunks).toString() + '\n');
    process.exit(0);
  });
});
req.setTimeout(3000, () => { req.destroy(new Error('timeout')); });
req.on('error', err => {
  console.error(`unreachable: ${err.message}`);
  process.exit(1);
});
