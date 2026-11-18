/**
 * End-to-end smoke test against a running SpacetimeDB with the blinktalk module published.
 *
 *   spacetime start                                   (terminal 1)
 *   cd spacetime && spacetime publish -y blinktalk    (once)
 *   npm run e2e                                       (starts its own hub; Photon simulated)
 *
 * Plays every role: the board and patient screen (reducer calls over HTTP), the hub (spawned as a
 * child process, Photon simulated, no board port), and the dashboard (WebSocket subscription).
 */
import { spawn } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DbConnection, tables } from '../bindings/index.ts';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const WS = process.env.SPACETIME_URI ?? 'ws://127.0.0.1:3000';
const HTTP = WS.replace(/^ws/, 'http');
const DB = process.env.SPACETIME_DB ?? 'blinktalk';
const P = 'neal';
const MOM = '+14695550101';

let failures = 0;
const ok = (name: string, cond: boolean, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? `  (${extra})` : ''}`);
  if (!cond) failures++;
};
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

async function call(reducer: string, ...args: unknown[]) {
  const res = await fetch(`${HTTP}/v1/database/${DB}/call/${reducer}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(args),
  });
  if (!res.ok) throw new Error(`${reducer}: HTTP ${res.status} ${await res.text()}`);
}
async function sql(q: string): Promise<any[]> {
  const res = await fetch(`${HTTP}/v1/database/${DB}/sql`, { method: 'POST', body: q });
  const [r] = await res.json();
  const names = r.schema.elements.map((e: any) => e.name?.some ?? e.name);
  return r.rows.map((row: any[]) => Object.fromEntries(names.map((n: string, i: number) => [n, row[i]])));
}
async function until<T>(what: string, fn: () => T | undefined | null | false, ms = 15000): Promise<T | undefined> {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const v = fn();
    if (v) return v as T;
    await sleep(200);
  }
  console.log(`      timed out waiting for ${what}`);
  return undefined;
}

async function main() {
  // ---------- dashboard role: live subscription
  const conn = await new Promise<DbConnection>((res, rej) => {
    DbConnection.builder().withUri(WS).withDatabaseName(DB)
      .onConnect(c => c.subscriptionBuilder().onApplied(() => res(c)).subscribe([
        tables.patient, tables.contact, tables.message, tables.suggestion, tables.alert,
        tables.eye_event, tables.heart_sample, tables.agent_step, tables.phrase, tables.briefing,
      ]))
      .onConnectError((_c, e) => rej(e)).build();
  });
  ok('dashboard subscribes over WebSocket', true);
  await call('clear_demo', P);
  for (const c of [...conn.db.contact.iter()]) if (c.patient === P) await call('remove_contact', Number(c.id));
  await sleep(300);
  ok('init reducer created patient "neal"', !!conn.db.patient.handle.find(P));

  // ---------- hub role: spawn it
  const hubLog: string[] = [];
  const hub = spawn(process.execPath, ['--import', 'tsx', 'hub/src/main.ts'], {
    cwd: root,
    env: { ...process.env, SPACETIME_URI: WS, SPACETIME_DB: DB, PHOTON_API_KEY: '', PHOTON_PROJECT_ID: '', AI_BASE_URL: process.env.E2E_AI_BASE_URL ?? '', AI_API_KEY: process.env.E2E_AI_API_KEY ?? '', AI_MODEL: process.env.E2E_AI_MODEL ?? '', BOARD_PORT: '' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  hub.stdout.on('data', d => hubLog.push(String(d)));
  hub.stderr.on('data', d => hubLog.push(String(d)));
  ok('hub starts and connects', !!(await until('hub ready', () => hubLog.join('').includes('[hub] ready'), 30000)));

  // ---------- board + patient screen: eye commands, heart rate, live draft
  await call('eye_command', P, 'right');
  await call('eye_command', P, 'select');
  await call('set_draft', P, "I'd like some wa");
  const live = await until('live draft', () => conn.db.patient.handle.find(P)?.draft === "I'd like some wa");
  ok('patient draft reaches the dashboard live', !!live);
  ok('eye movements recorded', [...conn.db.eye_event.iter()].filter(e => e.patient === P).length >= 2);
  for (let i = 0; i < 3; i++) await call('heartbeat', P, 72 + i, 0.85);
  await until('heart', () => conn.db.patient.handle.find(P)?.heart_bpm === 74);
  ok('heart rate stored', conn.db.patient.handle.find(P)?.heart_bpm === 74);
  await call('speak', P, "I'd like some water, please.");
  await until('speech', () => [...conn.db.message.iter()].some(m => m.channel === 'speech'));
  ok('spoken phrase logged + phrase learned', [...conn.db.phrase.iter()].some(p => p.text.startsWith("I'd like")));

  // ---------- caregiver + inbound text -> agent -> suggestions
  conn.reducers.addContact({ patient: P, name: 'Mom', address: '+1 (469) 555-0101', relation: 'mother', tier: 1 });
  await until('contact', () => [...conn.db.contact.iter()].some(c => c.address === MOM));
  ok('dashboard adds a caregiver (address normalised)', [...conn.db.contact.iter()].some(c => c.address === MOM && c.tier === 1));
  await call('receive_message', P, MOM, '', 'imessage', 'Want me to bring dinner tonight?');
  const sugg = await until('agent suggestions', () => {
    const s = [...conn.db.suggestion.iter()].filter(x => x.patient === P);
    return s.length >= 2 ? s : null;
  }, 60000);
  ok('agent run produces reply suggestions', !!sugg, sugg ? `${sugg.length} from ${sugg[0].source}: ${sugg.map(s => s.text).join(' | ')}` : '');
  ok('agent steps logged for the dashboard', [...conn.db.agent_step.iter()].some(s => s.step === 'plan'));
  const viaSql = await sql(`SELECT * FROM suggestion WHERE patient = '${P}'`);
  ok('suggestions readable over SQL', viaSql.length === (sugg?.length ?? -1));

  // ---------- patient sends a reply -> hub delivers via Photon (simulated)
  await call('reply_latest', P, sugg?.[0]?.text ?? 'Yes please.');
  const sent = await until('delivery', () => [...conn.db.message.iter()].find(m => m.direction === 'out' && m.channel === 'imessage' && m.status === 'sent'));
  ok('reply queued by the patient, delivered by hub, marked sent', !!sent);
  ok('hub printed the outbound text', hubLog.join('').includes(`[photon:sim] -> ${MOM}`));
  ok('suggestions cleared after replying', [...conn.db.suggestion.iter()].filter(s => s.patient === P).length === 0);

  // ---------- AI expand from keywords
  await call('request_intent', P, 'water later');
  const exp = await until('intent suggestions', () => {
    const s = [...conn.db.suggestion.iter()].filter(x => x.patient === P);
    return s.length >= 2 ? s : null;
  }, 60000);
  ok('AI expand turns keywords into replies', !!exp, exp?.map(s => s.text).join(' | '));

  // ---------- SOS: triple blink -> alert -> caregiver texted -> ack -> cleared
  await call('eye_command', P, 'sos');
  const alert = await until('sos alert', () => [...conn.db.alert.iter()].find(a => a.patient === P && a.kind === 'sos' && !a.acked_by));
  ok('triple blink raises SOS', !!alert && conn.db.patient.handle.find(P)?.status === 'sos');
  const notified = await until('notify', () => (alert && conn.db.alert.id.find(alert.id)?.notified_level === 1));
  ok('hub texted tier-1 caregiver and recorded it', !!notified && hubLog.join('').includes('[alert] sos L1 -> Mom'));
  await call('ack_alert_by_reply', P, MOM);
  await until('ack', () => alert && conn.db.alert.id.find(alert.id)?.acked_by === 'Mom');
  ok('caregiver "OK" acknowledges SOS', alert ? conn.db.alert.id.find(alert.id)?.acked_by === 'Mom' : false);
  ok('patient status back to idle', conn.db.patient.handle.find(P)?.status === 'idle');

  // ---------- caregiver briefing (AI model in the hub; plain summary without one)
  conn.reducers.requestBriefing({ patient: P, hours: 12, requested_by: 'e2e' });
  const brief = await until('briefing', () => [...conn.db.briefing.iter()].find(b => b.patient === P && b.status === 'done'), 30000);
  ok('"Catch me up" briefing written by the hub', !!brief && brief.text.length > 20, brief ? `${brief.model}: ${brief.text.slice(0, 90)}…` : '');

  if (process.env.E2E_ESCALATION === '1') {
    await call('raise_sos', P, 'escalation test');
    const a2 = await until('sos 2', () => [...conn.db.alert.iter()].find(a => a.detail === 'escalation test'));
    const esc = await until('escalation', () => a2 && (conn.db.alert.id.find(a2.id)?.level ?? 0) >= 2, 60000);
    ok('scheduled reducer escalates unanswered SOS after 45 s', !!esc);
    if (a2) await call('ack_alert', Number(a2.id), 'e2e');
  }

  hub.kill();
  conn.disconnect();
  console.log(failures ? `\n${failures} check(s) failed` : '\nAll end-to-end checks passed');
  if (failures) console.log('--- hub log ---\n' + hubLog.join('').slice(-3000));
  process.exit(failures ? 1 : 0);
}

main().catch(err => { console.error(err); process.exit(1); });
