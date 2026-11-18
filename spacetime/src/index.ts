/**
 * BlinkTalk backend: a SpacetimeDB module.
 *
 * Every piece of shared state and every rule lives here: patients, their live
 * draft, eye events, heart rate, conversations, AI reply suggestions, caregiver
 * contacts and SOS alerts with a scheduled escalation ladder. The rest of the
 * system is clients that subscribe to it or call its reducers:
 *
 *   hub/board  (Node, WS)     -> eye_command, heartbeat         (from the SK-AM62B)
 *   patient screen (WS)       -> set_draft, speak, reply_latest, request_intent, cancel_sos (+ eye_command from the keyboard)
 *   caregiver dashboard (WS)  -> ack_alert, add_contact, send_message, request_briefing
 *   hub/photon (Node, WS)     -> receive_message, mark_message, ack_alert_by_reply
 *   hub/agent  (Node, WS)     -> add_suggestions, log_agent_step, raise_sos
 *
 * Column and table names are snake_case so SQL queries and the generated client bindings agree.
 */
import { schema, t, table, SenderError, ScheduleAt, type ReducerCtx, type InferSchema } from 'spacetimedb/server';

// ------------------------------------------------------------------ limits
const KEEP_EYE_EVENTS = 120;
const KEEP_HEART_SAMPLES = 240;
const KEEP_AGENT_STEPS = 200;
const HEART_HIGH = 120;
const HEART_LOW = 45;
const HEART_ALERT_RUN = 5;          // consecutive out-of-range good-quality samples
const SOS_ESCALATE_SECONDS = 45n;   // unacknowledged SOS -> next caregiver tier
const SOS_MAX_LEVEL = 3;

const COMMANDS = ['left', 'right', 'select', 'toggle', 'sos'] as const;

// ------------------------------------------------------------------ tables
const patient = table(
  { name: 'patient', public: true },
  {
    handle: t.string().primaryKey(),
    display_name: t.string(),
    status: t.string(),                // idle | composing | speaking | paused | sos
    draft: t.string(),                 // live text being spelled with the eyes
    last_spoken: t.string(),
    heart_bpm: t.u16(),                // 0 = unknown
    heart_quality: t.f32(),
    heart_out_of_range: t.u16(),       // consecutive abnormal samples
    last_seen: t.timestamp(),
  }
);

const contact = table(
  { name: 'contact', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    patient: t.string().index('btree'),
    name: t.string(),
    address: t.string(),               // E.164 phone or email (Photon handle)
    relation: t.string(),
    tier: t.u8(),                      // 0 = not a caregiver, 1 = first call, 2, 3 ...
  }
);

const message = table(
  { name: 'message', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    patient: t.string().index('btree'),
    contact_id: t.u64(),               // 0 = spoken aloud in the room
    direction: t.string(),             // in | out
    channel: t.string(),               // imessage | sms | speech
    body: t.string(),
    status: t.string(),                // queued | sent | failed | received | spoken
    assisted: t.bool(),                // the patient sent an AI suggestion (counts toward glances saved)
    created_at: t.timestamp(),
  }
);

const suggestion = table(
  { name: 'suggestion', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    patient: t.string().index('btree'),
    message_id: t.u64(),               // inbound message these reply to (0 = from an intent)
    contact_id: t.u64(),
    text: t.string(),
    rank: t.u8(),
    source: t.string(),                // ai | fallback
    created_at: t.timestamp(),
  }
);

const intent = table(
  { name: 'intent', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    patient: t.string(),
    contact_id: t.u64(),
    keywords: t.string(),              // e.g. "water later" chosen with a few glances
    status: t.string(),                // pending | done
    created_at: t.timestamp(),
  }
);

const alert = table(
  { name: 'alert', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    patient: t.string().index('btree'),
    kind: t.string(),                  // sos | heart_high | heart_low
    detail: t.string(),
    level: t.u8(),                     // caregiver tier currently being notified
    notified_level: t.u8(),            // highest tier the hub has texted
    acked_by: t.string(),
    created_at: t.timestamp(),
    acked_at: t.option(t.timestamp()),
  }
);

const eye_event = table(
  { name: 'eye_event', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    patient: t.string(),
    command: t.string(),
    at: t.timestamp(),
  }
);

const heart_sample = table(
  { name: 'heart_sample', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    patient: t.string(),
    bpm: t.u16(),
    quality: t.f32(),
    at: t.timestamp(),
  }
);

const phrase = table(
  { name: 'phrase', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    patient: t.string().index('btree'),
    text: t.string(),
    uses: t.u32(),
    last_used: t.timestamp(),
  }
);

const agent_step = table(
  { name: 'agent_step', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    patient: t.string(),
    run_id: t.string(),
    step: t.string(),                  // plan | tool | result | reply | error
    detail: t.string(),
    at: t.timestamp(),
  }
);

const briefing = table(
  { name: 'briefing', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    patient: t.string().index('btree'),
    hours: t.u16(),                    // how far back it covers
    status: t.string(),                // pending | done
    text: t.string(),
    model: t.string(),                 // AI model name, or "rules" when no model is configured
    requested_by: t.string(),
    created_at: t.timestamp(),
  }
);

const sos_timer = table(
  { name: 'sos_timer' },
  {
    scheduled_id: t.u64().primaryKey().autoInc(),
    scheduled_at: t.scheduleAt(),
    alert_id: t.u64(),
  }
);

const spacetimedb = schema({
  patient, contact, message, suggestion, intent, alert,
  eye_event, heart_sample, phrase, agent_step, briefing, sos_timer,
});
export default spacetimedb;

type Ctx = ReducerCtx<InferSchema<typeof spacetimedb>>;

// ------------------------------------------------------------------ helpers
function clean(s: string, max = 1000): string {
  return s.replace(/[\u0000-\u0008\u000b-\u001f]/g, '').trim().slice(0, max);
}

function getPatient(ctx: Ctx, handle: string) {
  const p = ctx.db.patient.handle.find(handle);
  if (!p) throw new SenderError(`unknown patient '${handle}'`);
  return p;
}

function touch(ctx: Ctx, handle: string, changes: Partial<ReturnType<typeof getPatient>>) {
  const p = getPatient(ctx, handle);
  return ctx.db.patient.handle.update({ ...p, ...changes, last_seen: ctx.timestamp });
}

/** Keep only the newest `keep` rows of an append-only table for one patient. */
function prune<R extends { id: bigint; patient: string }>(
  rows: Iterable<R>, patientHandle: string, keep: number, del: (r: R) => void,
) {
  const mine = [...rows].filter(r => r.patient === patientHandle);
  if (mine.length <= keep) return;
  mine.sort((a, b) => (a.id < b.id ? -1 : 1));
  for (const r of mine.slice(0, mine.length - keep)) del(r);
}

function openAlert(ctx: Ctx, handle: string, kind: string) {
  for (const a of ctx.db.alert.patient.filter(handle)) {
    if (a.kind === kind && !a.acked_by) return a;
  }
  return null;
}

function raiseAlert(ctx: Ctx, handle: string, kind: string, detail: string) {
  const existing = openAlert(ctx, handle, kind);
  if (existing) return existing;
  const a = ctx.db.alert.insert({
    id: 0n, patient: handle, kind, detail: clean(detail, 300), level: 1,
    notified_level: 0, acked_by: '', created_at: ctx.timestamp, acked_at: undefined,
  });
  if (kind === 'sos') scheduleEscalation(ctx, a.id);
  return a;
}

function scheduleEscalation(ctx: Ctx, alertId: bigint) {
  const at = ctx.timestamp.microsSinceUnixEpoch + SOS_ESCALATE_SECONDS * 1_000_000n;
  ctx.db.sos_timer.insert({ scheduled_id: 0n, scheduled_at: ScheduleAt.time(at), alert_id: alertId });
}

function countPhrase(ctx: Ctx, handle: string, text: string) {
  for (const ph of ctx.db.phrase.patient.filter(handle)) {
    if (ph.text === text) {
      ctx.db.phrase.id.update({ ...ph, uses: ph.uses + 1, last_used: ctx.timestamp });
      return;
    }
  }
  ctx.db.phrase.insert({ id: 0n, patient: handle, text, uses: 1, last_used: ctx.timestamp });
}

function isSuggestion(ctx: Ctx, handle: string, text: string) {
  for (const s of ctx.db.suggestion.patient.filter(handle)) if (s.text === text) return true;
  return false;
}

function clearSuggestions(ctx: Ctx, handle: string) {
  for (const s of [...ctx.db.suggestion.patient.filter(handle)]) ctx.db.suggestion.id.delete(s.id);
}

// ------------------------------------------------------------------ lifecycle
export const init = spacetimedb.init(ctx => {
  ctx.db.patient.insert({
    handle: 'neal', display_name: 'Neal', status: 'idle', draft: '', last_spoken: '',
    heart_bpm: 0, heart_quality: 0, heart_out_of_range: 0, last_seen: ctx.timestamp,
  });
});

// ------------------------------------------------------------------ patients & contacts
export const register_patient = spacetimedb.reducer(
  { handle: t.string(), display_name: t.string() },
  (ctx, { handle, display_name }) => {
    const h = clean(handle, 40).toLowerCase();
    if (!/^[a-z0-9_-]{1,40}$/.test(h)) throw new SenderError('handle must be letters, digits, - or _');
    const existing = ctx.db.patient.handle.find(h);
    if (existing) {
      ctx.db.patient.handle.update({ ...existing, display_name: clean(display_name, 60) || existing.display_name });
      return;
    }
    ctx.db.patient.insert({
      handle: h, display_name: clean(display_name, 60) || h, status: 'idle', draft: '',
      last_spoken: '', heart_bpm: 0, heart_quality: 0, heart_out_of_range: 0, last_seen: ctx.timestamp,
    });
  }
);

export const add_contact = spacetimedb.reducer(
  { patient: t.string(), name: t.string(), address: t.string(), relation: t.string(), tier: t.u8() },
  (ctx, { patient: handle, name, address, relation, tier }) => {
    getPatient(ctx, handle);
    const addr = clean(address, 120).replace(/[\s()-]/g, '');
    if (!/^(\+\d{8,15}|[^@\s]+@[^@\s]+\.[^@\s]+)$/.test(addr)) {
      throw new SenderError('address must be an E.164 phone (+15551234567) or an email');
    }
    for (const c of ctx.db.contact.patient.filter(handle)) {
      if (c.address === addr) {
        ctx.db.contact.id.update({ ...c, name: clean(name, 60) || c.name, relation: clean(relation, 40), tier });
        return;
      }
    }
    ctx.db.contact.insert({
      id: 0n, patient: handle, name: clean(name, 60) || addr, address: addr,
      relation: clean(relation, 40), tier,
    });
  }
);

export const remove_contact = spacetimedb.reducer({ id: t.u64() }, (ctx, { id }) => {
  ctx.db.contact.id.delete(id);
});

// ------------------------------------------------------------------ live eye + body signals
export const eye_command = spacetimedb.reducer(
  { patient: t.string(), command: t.string() },
  (ctx, { patient: handle, command }) => {
    if (!(COMMANDS as readonly string[]).includes(command)) throw new SenderError(`bad command '${command}'`);
    getPatient(ctx, handle);
    ctx.db.eye_event.insert({ id: 0n, patient: handle, command, at: ctx.timestamp });
    prune(ctx.db.eye_event.iter(), handle, KEEP_EYE_EVENTS, r => ctx.db.eye_event.id.delete(r.id));
    if (command === 'sos') {
      raiseAlert(ctx, handle, 'sos', 'Five blinks (SOS)');
      touch(ctx, handle, { status: 'sos' });
    } else if (command === 'toggle') {
      const p = getPatient(ctx, handle);
      if (p.status !== 'sos') touch(ctx, handle, { status: p.status === 'paused' ? 'idle' : 'paused' });
    } else {
      touch(ctx, handle, {});
    }
  }
);

export const set_draft = spacetimedb.reducer(
  { patient: t.string(), draft: t.string() },
  (ctx, { patient: handle, draft }) => {
    const p = getPatient(ctx, handle);
    const d = clean(draft, 500);
    const keep = p.status === 'sos' || p.status === 'paused';
    touch(ctx, handle, { draft: d, status: keep ? p.status : d ? 'composing' : 'idle' });
  }
);

export const heartbeat = spacetimedb.reducer(
  { patient: t.string(), bpm: t.u16(), quality: t.f32() },
  (ctx, { patient: handle, bpm, quality }) => {
    const p = getPatient(ctx, handle);
    ctx.db.heart_sample.insert({ id: 0n, patient: handle, bpm, quality, at: ctx.timestamp });
    prune(ctx.db.heart_sample.iter(), handle, KEEP_HEART_SAMPLES, r => ctx.db.heart_sample.id.delete(r.id));
    const good = bpm > 0 && quality >= 0.5;
    const abnormal = good && (bpm > HEART_HIGH || bpm < HEART_LOW);
    const run = abnormal ? p.heart_out_of_range + 1 : 0;
    touch(ctx, handle, { heart_bpm: bpm, heart_quality: quality, heart_out_of_range: run });
    if (run === HEART_ALERT_RUN) {
      raiseAlert(ctx, handle, bpm > HEART_HIGH ? 'heart_high' : 'heart_low',
        `Heart rate ${bpm} bpm for ${HEART_ALERT_RUN} readings in a row`);
    }
  }
);

// ------------------------------------------------------------------ speaking & messaging
export const speak = spacetimedb.reducer(
  { patient: t.string(), text: t.string() },
  (ctx, { patient: handle, text }) => {
    const body = clean(text, 500);
    if (!body) return;
    getPatient(ctx, handle);
    ctx.db.message.insert({
      id: 0n, patient: handle, contact_id: 0n, direction: 'out', channel: 'speech', body,
      status: 'spoken', assisted: isSuggestion(ctx, handle, body), created_at: ctx.timestamp,
    });
    countPhrase(ctx, handle, body);
    touch(ctx, handle, { last_spoken: body, draft: '', status: 'speaking' });
  }
);

/** Queue a text to a contact; the hub delivers it over Photon and marks it sent. */
export const send_message = spacetimedb.reducer(
  { patient: t.string(), contact_id: t.u64(), body: t.string() },
  (ctx, { patient: handle, contact_id, body }) => {
    const text = clean(body, 1000);
    if (!text) throw new SenderError('empty message');
    const c = ctx.db.contact.id.find(contact_id);
    if (!c || c.patient !== handle) throw new SenderError('unknown contact');
    ctx.db.message.insert({
      id: 0n, patient: handle, contact_id, direction: 'out', channel: 'imessage', body: text,
      status: 'queued', assisted: isSuggestion(ctx, handle, text), created_at: ctx.timestamp,
    });
    countPhrase(ctx, handle, text);
    clearSuggestions(ctx, handle);
    touch(ctx, handle, { draft: '', last_spoken: text, status: 'idle' });
  }
);

/** Reply to whoever wrote most recently (what picking a suggested reply on the patient screen does). */
export const reply_latest = spacetimedb.reducer(
  { patient: t.string(), body: t.string() },
  (ctx, { patient: handle, body }) => {
    let latest: { id: bigint; contact_id: bigint } | null = null;
    for (const m of ctx.db.message.patient.filter(handle)) {
      if (m.direction === 'in' && (!latest || m.id > latest.id)) latest = m;
    }
    if (!latest) throw new SenderError('no conversation to reply to');
    send_message(ctx, { patient: handle, contact_id: latest.contact_id, body });
  }
);

export const receive_message = spacetimedb.reducer(
  { patient: t.string(), address: t.string(), sender_name: t.string(), channel: t.string(), body: t.string() },
  (ctx, { patient: handle, address, sender_name, channel, body }) => {
    getPatient(ctx, handle);
    const text = clean(body, 2000);
    if (!text) return;
    let contactId = 0n;
    for (const c of ctx.db.contact.patient.filter(handle)) {
      if (c.address === address) contactId = c.id;
    }
    if (contactId === 0n) {
      contactId = ctx.db.contact.insert({
        id: 0n, patient: handle, name: clean(sender_name, 60) || address, address,
        relation: 'unknown', tier: 0,
      }).id;
    }
    clearSuggestions(ctx, handle);
    ctx.db.message.insert({
      id: 0n, patient: handle, contact_id: contactId, direction: 'in', channel: clean(channel, 20) || 'imessage',
      body: text, status: 'received', assisted: false, created_at: ctx.timestamp,
    });
  }
);

export const mark_message = spacetimedb.reducer(
  { id: t.u64(), status: t.string() },
  (ctx, { id, status }) => {
    if (!['sent', 'failed', 'queued'].includes(status)) throw new SenderError('bad status');
    const m = ctx.db.message.id.find(id);
    if (m) ctx.db.message.id.update({ ...m, status });
  }
);

// ------------------------------------------------------------------ AI agent writes
export const add_suggestions = spacetimedb.reducer(
  { patient: t.string(), message_id: t.u64(), contact_id: t.u64(), texts: t.array(t.string()), source: t.string() },
  (ctx, { patient: handle, message_id, contact_id, texts, source }) => {
    getPatient(ctx, handle);
    clearSuggestions(ctx, handle);
    texts.slice(0, 4).forEach((raw, i) => {
      const text = clean(raw, 160);
      if (text) {
        ctx.db.suggestion.insert({
          id: 0n, patient: handle, message_id, contact_id, text, rank: i,
          source: clean(source, 20), created_at: ctx.timestamp,
        });
      }
    });
  }
);

/** Suggestions that each go to a specific person (the agent inferred the recipient). */
export const add_routed_suggestions = spacetimedb.reducer(
  { patient: t.string(), message_id: t.u64(), contact_ids: t.array(t.u64()), texts: t.array(t.string()), source: t.string() },
  (ctx, { patient: handle, message_id, contact_ids, texts, source }) => {
    getPatient(ctx, handle);
    if (contact_ids.length !== texts.length) throw new SenderError('one contact per suggestion');
    clearSuggestions(ctx, handle);
    texts.slice(0, 4).forEach((raw, i) => {
      const text = clean(raw, 160);
      const c = ctx.db.contact.id.find(contact_ids[i]);
      const contact_id = c && c.patient === handle ? c.id : 0n;
      if (text) {
        ctx.db.suggestion.insert({
          id: 0n, patient: handle, message_id, contact_id, text, rank: i,
          source: clean(source, 20), created_at: ctx.timestamp,
        });
      }
    });
  }
);

export const request_intent = spacetimedb.reducer(
  { patient: t.string(), keywords: t.string() },
  (ctx, { patient: handle, keywords }) => {
    getPatient(ctx, handle);
    let contactId = 0n, latest = 0n;
    for (const m of ctx.db.message.patient.filter(handle)) {
      if (m.direction === 'in' && m.id > latest) { latest = m.id; contactId = m.contact_id; }
    }
    ctx.db.intent.insert({
      id: 0n, patient: handle, contact_id: contactId, keywords: clean(keywords, 200),
      status: 'pending', created_at: ctx.timestamp,
    });
  }
);

export const finish_intent = spacetimedb.reducer({ id: t.u64() }, (ctx, { id }) => {
  const i = ctx.db.intent.id.find(id);
  if (i) ctx.db.intent.id.update({ ...i, status: 'done' });
});

export const log_agent_step = spacetimedb.reducer(
  { patient: t.string(), run_id: t.string(), step: t.string(), detail: t.string() },
  (ctx, { patient: handle, run_id, step, detail }) => {
    ctx.db.agent_step.insert({
      id: 0n, patient: handle, run_id: clean(run_id, 40), step: clean(step, 20),
      detail: clean(detail, 600), at: ctx.timestamp,
    });
    prune(ctx.db.agent_step.iter(), handle, KEEP_AGENT_STEPS, r => ctx.db.agent_step.id.delete(r.id));
  }
);

// ------------------------------------------------------------------ caregiver briefing (written by the AI model in the hub)
export const request_briefing = spacetimedb.reducer(
  { patient: t.string(), hours: t.u16(), requested_by: t.string() },
  (ctx, { patient: handle, hours, requested_by }) => {
    getPatient(ctx, handle);
    const h = Math.max(1, Math.min(72, hours));
    ctx.db.briefing.insert({
      id: 0n, patient: handle, hours: h, status: 'pending', text: '', model: '',
      requested_by: clean(requested_by, 60) || 'caregiver', created_at: ctx.timestamp,
    });
    // keep the 10 newest
    const mine = [...ctx.db.briefing.patient.filter(handle)].sort((a, b) => (a.id < b.id ? -1 : 1));
    for (const b of mine.slice(0, Math.max(0, mine.length - 10))) ctx.db.briefing.id.delete(b.id);
  }
);

export const set_briefing = spacetimedb.reducer(
  { id: t.u64(), text: t.string(), model: t.string() },
  (ctx, { id, text, model }) => {
    const b = ctx.db.briefing.id.find(id);
    if (b) ctx.db.briefing.id.update({ ...b, status: 'done', text: clean(text, 2000), model: clean(model, 60) });
  }
);

// ------------------------------------------------------------------ SOS lifecycle
export const raise_sos = spacetimedb.reducer(
  { patient: t.string(), detail: t.string() },
  (ctx, { patient: handle, detail }) => {
    raiseAlert(ctx, handle, 'sos', detail || 'SOS');
    touch(ctx, handle, { status: 'sos' });
  }
);

function ack(ctx: Ctx, id: bigint, by: string) {
  const a = ctx.db.alert.id.find(id);
  if (!a || a.acked_by) return;
  ctx.db.alert.id.update({ ...a, acked_by: clean(by, 60) || 'caregiver', acked_at: ctx.timestamp });
  if (a.kind === 'sos') {
    const p = ctx.db.patient.handle.find(a.patient);
    if (p && p.status === 'sos') ctx.db.patient.handle.update({ ...p, status: 'idle' });
  }
}

export const ack_alert = spacetimedb.reducer(
  { id: t.u64(), by: t.string() },
  (ctx, { id, by }) => ack(ctx, id, by)
);

/** A caregiver texted back "OK" (or similar): acknowledge every open alert for that patient. */
export const ack_alert_by_reply = spacetimedb.reducer(
  { patient: t.string(), address: t.string() },
  (ctx, { patient: handle, address }) => {
    let who = address;
    for (const c of ctx.db.contact.patient.filter(handle)) if (c.address === address) who = c.name;
    for (const a of [...ctx.db.alert.patient.filter(handle)]) if (!a.acked_by) ack(ctx, a.id, who);
  }
);

/** The hub texted every caregiver up to `level`. */
export const mark_alert_notified = spacetimedb.reducer(
  { id: t.u64(), level: t.u8() },
  (ctx, { id, level }) => {
    const a = ctx.db.alert.id.find(id);
    if (a && level > a.notified_level) ctx.db.alert.id.update({ ...a, notified_level: level });
  }
);

/** The patient cancels their own SOS (double blink on the SOS screen). */
export const cancel_sos = spacetimedb.reducer({ patient: t.string() }, (ctx, { patient: handle }) => {
  for (const a of [...ctx.db.alert.patient.filter(handle)]) {
    if (a.kind === 'sos' && !a.acked_by) ack(ctx, a.id, 'patient (cancelled)');
  }
  const p = ctx.db.patient.handle.find(handle);
  if (p && p.status === 'sos') ctx.db.patient.handle.update({ ...p, status: 'idle' });
});

/** Scheduled: an SOS nobody acknowledged moves to the next caregiver tier. */
export const escalate_sos = spacetimedb.reducer(
  { onSchedule: sos_timer },
  { timer: sos_timer.rowType },
  (ctx, { timer }) => {
    const a = ctx.db.alert.id.find(timer.alert_id);
    if (!a || a.acked_by) return;
    if (a.level >= SOS_MAX_LEVEL) return;
    ctx.db.alert.id.update({ ...a, level: a.level + 1 });
    scheduleEscalation(ctx, a.id);
  }
);

export const clear_demo = spacetimedb.reducer({ patient: t.string() }, (ctx, { patient: handle }) => {
  for (const m of [...ctx.db.message.patient.filter(handle)]) ctx.db.message.id.delete(m.id);
  for (const a of [...ctx.db.alert.patient.filter(handle)]) ctx.db.alert.id.delete(a.id);
  clearSuggestions(ctx, handle);
  for (const e of [...ctx.db.eye_event.iter()]) if (e.patient === handle) ctx.db.eye_event.id.delete(e.id);
  for (const s of [...ctx.db.agent_step.iter()]) if (s.patient === handle) ctx.db.agent_step.id.delete(s.id);
  for (const i of [...ctx.db.intent.iter()]) if (i.patient === handle) ctx.db.intent.id.delete(i.id);
  const p = ctx.db.patient.handle.find(handle);
  if (p) ctx.db.patient.handle.update({ ...p, status: 'idle', draft: '', last_spoken: '', heart_out_of_range: 0 });
});

