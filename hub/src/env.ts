/**
 * Loads the repo-root .env (gitignored) without overriding real environment
 * variables, and exposes typed settings. Secrets are never logged.
 */
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

export function loadDotEnv(path = resolve(here, '../../.env')): void {
  if (!existsSync(path)) return;
  for (const raw of readFileSync(path, 'utf8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    } else {
      value = value.replace(/\s+#.*$/, '');            // inline comment: KEY=value   # note
    }
    if (process.env[key] === undefined) process.env[key] = value;
  }
}

loadDotEnv();

const env = (k: string, d = '') => (process.env[k] ?? d).trim();

export const settings = {
  patient: env('BLINKTALK_PATIENT', 'neal'),
  spacetimeUri: env('SPACETIME_URI', 'ws://localhost:3000'),
  spacetimeDb: env('SPACETIME_DB', 'blinktalk'),
  // AI model: any OpenAI-compatible chat-completions endpoint (Azure AI Foundry, or a local Ollama / vLLM)
  aiBaseUrl: env('AI_BASE_URL').replace(/\/$/, ''),
  aiApiKey: env('AI_API_KEY'),
  aiModel: env('AI_MODEL'),
  aiVisionModel: env('AI_VISION_MODEL'),       // optional separate model for photos; defaults to AI_MODEL
  aiTimeoutMs: Number(env('AI_TIMEOUT', '20')) * 1000,
  // SK-AM62B serial console (empty = no board; the patient screen still works with the keyboard)
  boardPort: env('BOARD_PORT'),
  boardDir: env('BOARD_DIR', '/root/blinktalk'),
  boardSensitivity: Math.max(1, Math.min(10, Number(env('BOARD_SENSITIVITY', '5')) || 5)),
  boardLed: env('BOARD_LED') ? Number(env('BOARD_LED')) : undefined,
  boardSwapLR: env('BOARD_SWAP_LR', '0') === '1',
  photonProjectId: env('PHOTON_PROJECT_ID'),
  photonApiKey: env('PHOTON_API_KEY'),
  photonBaseUrl: env('PHOTON_BASE_URL', 'https://api.photon.codes'),
};

export const redact = (s: string) => (s ? `${s.slice(0, 6)}…(${s.length} chars)` : '(not set)');
