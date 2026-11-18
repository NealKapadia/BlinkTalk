// Container entrypoint: runs the hub and serves the
// built dashboard + patient screen on $PORT. Restarts the hub if it exits.
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { createReadStream, existsSync, statSync } from 'node:fs';
import { extname, join, normalize, resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const dist = join(root, 'dashboard', 'dist');
const port = Number(process.env.PORT ?? 8080);
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon', '.json': 'application/json' };

createServer((req, res) => {
  const url = new URL(req.url ?? '/', 'http://x');
  if (url.pathname === '/healthz') { res.writeHead(200).end('ok'); return; }
  let file = normalize(join(dist, decodeURIComponent(url.pathname)));
  if (!file.startsWith(dist) || !existsSync(file) || statSync(file).isDirectory()) file = join(dist, 'index.html');
  res.writeHead(200, { 'Content-Type': TYPES[extname(file)] ?? 'application/octet-stream', 'Cache-Control': file.endsWith('index.html') ? 'no-cache' : 'public, max-age=31536000, immutable' });
  createReadStream(file).pipe(res);
}).listen(port, () => console.log(`[web] dashboard on :${port}  (patient screen: /?view=patient)`));

function runHub(delay = 0) {
  setTimeout(() => {
    const hub = spawn(process.execPath, ['--import', 'tsx', 'hub/src/main.ts'], { cwd: root, stdio: 'inherit' });
    hub.on('exit', code => { console.log(`[hub] exited (${code}); restarting in 5 s`); runHub(5000); });
  }, delay);
}
if (process.env.RUN_HUB !== '0') runHub();
