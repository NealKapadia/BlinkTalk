// npm start: runs the hub (board link, iMessage, AI) and the screens (dashboard + patient screen) together.
// SpacetimeDB must already be running (`spacetime start` in another terminal). Ctrl+C stops both.
import { spawn } from 'node:child_process';

const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
const children = [
  ['hub', ['run', 'hub']],
  ['screens', ['run', 'dashboard']],
].map(([name, args]) => {
  const child = spawn(npm, args, { stdio: ['ignore', 'pipe', 'pipe'], shell: process.platform === 'win32' });
  const prefix = (chunk) => String(chunk).split(/\r?\n/).filter(Boolean).map(l => `[${name}] ${l}`).join('\n') + '\n';
  child.stdout.on('data', c => process.stdout.write(prefix(c)));
  child.stderr.on('data', c => process.stderr.write(prefix(c)));
  child.on('exit', code => { console.log(`[${name}] exited (${code})`); shutdown(code ?? 0); });
  return child;
});

let stopping = false;
function shutdown(code) {
  if (stopping) return;
  stopping = true;
  for (const c of children) if (c.exitCode === null) c.kill();
  setTimeout(() => process.exit(code), 500);
}
process.on('SIGINT', () => shutdown(0));
process.on('SIGTERM', () => shutdown(0));
