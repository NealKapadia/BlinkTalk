// Fails if any tracked or staged file contains something that looks like a secret.
// Run: npm run check-secrets   (installed as .git/hooks/pre-commit by scripts/install-hooks.mjs)
import { execSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

const PATTERNS = [
  [/nvapi-[A-Za-z0-9_-]{20,}/, 'NVIDIA API key'],
  [/^\s*(AI_API_KEY|AZURE_OPENAI_API_KEY|PHOTON_API_KEY|PHOTON_PROJECT_SECRET)=\S{8,}/m, 'secret assignment'],
  [/sk-[A-Za-z0-9]{32,}/, 'OpenAI-style key'],
  [/\b[A-Za-z0-9]{70,}JQQJ[A-Za-z0-9]{8,}\b/, 'Azure key'],
  [/\bpho_(?!prj_|org_)[a-z]+_[A-Za-z0-9]{20,}/, 'Photon API key'],
  [/\bAIza[0-9A-Za-z_-]{30,}/, 'Google API key'],
  [/\bAQ\.[A-Za-z0-9_-]{30,}/, 'Google API key'],
  [/-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----/, 'private key'],
];
const files = new Set(
  execSync('git ls-files && git diff --cached --name-only --diff-filter=ACM', { encoding: 'utf8' })
    .split('\n').filter(Boolean)
);
let bad = 0;
for (const f of files) {
  if (/\.(png|jpg|joblib|whl|ico|woff2?)$/i.test(f)) continue;
  let text;
  try { text = readFileSync(f, 'utf8'); } catch { continue; }
  if (f === 'scripts/check-secrets.mjs') continue;
  for (const [re, what] of PATTERNS) {
    const m = text.match(re);
    if (m) { bad++; console.error(`✗ ${f}: looks like a ${what} (${m[0].slice(0, 10)}…)`); }
  }
}
if (bad) { console.error(`\n${bad} possible secret(s). Move them to .env (gitignored).`); process.exit(1); }
console.log(`✓ no secrets in ${files.size} tracked files`);
