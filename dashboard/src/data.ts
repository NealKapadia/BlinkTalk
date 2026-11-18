/**
 * Live state for the caregiver console.
 *
 * Normal mode: one SpacetimeDB WebSocket subscription; every insert/update/
 * delete re-snapshots the client cache (batched per animation frame).
 * Demo mode (?demo): a scripted evening so the page can be shown without the
 * backend. Demo never pretends to be live; the header says "Demo".
 */
import { useEffect, useRef, useState } from 'react';
import {
  DbConnection, tables,
  type AgentStep, type Alert, type Briefing, type Contact, type EyeEvent, type HeartSample, type Message, type Patient, type Phrase, type Suggestion,
} from '@bindings';

export interface Snapshot {
  patient?: Patient;
  contacts: Contact[];
  messages: Message[];
  suggestions: Suggestion[];
  alerts: Alert[];
  eyes: EyeEvent[];
  heart: HeartSample[];
  agent: AgentStep[];
  phrases: Phrase[];
  briefings: Briefing[];
}

export interface Actions {
  eye(command: 'left' | 'right' | 'select' | 'toggle' | 'sos'): void;
  setDraft(text: string): void;
  speak(text: string): void;
  replyLatest(text: string): void;
  requestIntent(keywords: string): void;
  cancelSos(): void;
  requestBriefing(hours: number, by: string): void;
  ackAlert(id: bigint, by: string): void;
  addContact(name: string, address: string, relation: string, tier: number): void;
  removeContact(id: bigint): void;
  simulateText(address: string, name: string, body: string): void;
  sendAsPatient(contactId: bigint, body: string): void;
  clearDemo(): void;
}

export type Link = 'connecting' | 'live' | 'offline' | 'demo';

const params = new URLSearchParams(location.search);
export const PATIENT = params.get('patient') ?? 'neal';
const URI = params.get('uri') ?? (import.meta.env.VITE_SPACETIME_URI as string | undefined) ?? 'ws://localhost:3000';
const DB = params.get('db') ?? (import.meta.env.VITE_SPACETIME_DB as string | undefined) ?? 'blinktalk';
export const DEMO = params.has('demo');
export const PATIENT_VIEW = params.get('view') === 'patient' || location.pathname.endsWith('/patient');

const empty: Snapshot = { contacts: [], messages: [], suggestions: [], alerts: [], eyes: [], heart: [], agent: [], phrases: [], briefings: [] };
const byId = <T extends { id: bigint }>(a: T, b: T) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);

function snapshot(conn: DbConnection): Snapshot {
  const mine = <T extends { patient: string }>(rows: Iterable<T>) => [...rows].filter(r => r.patient === PATIENT);
  return {
    patient: conn.db.patient.handle.find(PATIENT) ?? undefined,
    contacts: mine(conn.db.contact.iter()).sort(byId),
    messages: mine(conn.db.message.iter()).sort(byId),
    suggestions: mine(conn.db.suggestion.iter()).sort((a, b) => a.rank - b.rank),
    alerts: mine(conn.db.alert.iter()).sort(byId),
    eyes: mine(conn.db.eye_event.iter()).sort(byId),
    heart: mine(conn.db.heart_sample.iter()).sort(byId),
    agent: mine(conn.db.agent_step.iter()).sort(byId),
    phrases: mine(conn.db.phrase.iter()).sort((a, b) => b.uses - a.uses),
    briefings: mine(conn.db.briefing.iter()).sort(byId),
  };
}

export function useBlinkTalk(): { snap: Snapshot; link: Link; actions: Actions; error?: string } {
  const [snap, setSnap] = useState<Snapshot>(empty);
  const [link, setLink] = useState<Link>(DEMO ? 'demo' : 'connecting');
  const [error, setError] = useState<string>();
  const connRef = useRef<DbConnection | null>(null);
  const demoRef = useRef<ReturnType<typeof startDemo> | null>(null);

  useEffect(() => {
    if (DEMO) {
      demoRef.current = startDemo(setSnap);
      return () => demoRef.current?.stop();
    }
    let frame = 0;
    const refresh = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (connRef.current) setSnap(snapshot(connRef.current));
      });
    };
    const conn = DbConnection.builder()
      .withUri(URI)
      .withDatabaseName(DB)
      .withAutomaticReconnect()
      .onConnect(c => {
        setLink('live');
        c.subscriptionBuilder()
          .onApplied(refresh)
          .subscribe([
            tables.patient, tables.contact, tables.message, tables.suggestion, tables.alert,
            tables.eye_event, tables.heart_sample, tables.agent_step, tables.phrase, tables.briefing,
          ]);
        const t = c.db;
        for (const tbl of [t.patient, t.contact, t.message, t.suggestion, t.alert, t.eye_event, t.heart_sample, t.agent_step, t.phrase, t.briefing] as const) {
          (tbl as any).onInsert(refresh);
          (tbl as any).onDelete(refresh);
          (tbl as any).onUpdate?.(refresh);
        }
      })
      .onConnectError((_ctx, err) => {
        setLink('offline');
        setError(String((err as Error)?.message ?? err));
      })
      .onDisconnect(() => setLink('offline'))
      .onAutomaticReconnect(() => { setLink('live'); refresh(); })
      .build();
    connRef.current = conn;
    return () => { cancelAnimationFrame(frame); conn.disconnect(); };
  }, []);

  const r = () => connRef.current?.reducers;
  const actions: Actions = DEMO && demoRef.current ? demoRef.current.actions : {
    eye: command => r()?.eyeCommand({ patient: PATIENT, command }),
    setDraft: draft => r()?.setDraft({ patient: PATIENT, draft }),
    speak: text => r()?.speak({ patient: PATIENT, text }),
    replyLatest: body => r()?.replyLatest({ patient: PATIENT, body }),
    requestIntent: keywords => r()?.requestIntent({ patient: PATIENT, keywords }),
    cancelSos: () => r()?.cancelSos({ patient: PATIENT }),
    requestBriefing: (hours, by) => r()?.requestBriefing({ patient: PATIENT, hours, requested_by: by }),
    ackAlert: (id, by) => r()?.ackAlert({ id, by }),
    addContact: (name, address, relation, tier) => r()?.addContact({ patient: PATIENT, name, address, relation, tier }),
    removeContact: id => r()?.removeContact({ id }),
    simulateText: (address, name, body) => r()?.receiveMessage({ patient: PATIENT, address, sender_name: name, channel: 'imessage', body }),
    sendAsPatient: (contact_id, body) => r()?.sendMessage({ patient: PATIENT, contact_id, body }),
    clearDemo: () => r()?.clearDemo({ patient: PATIENT }),
  };
  return { snap, link, actions: demoRef.current?.actions ?? actions, error };
}

// ------------------------------------------------------------------ demo script
import { Timestamp } from 'spacetimedb';

function startDemo(set: (s: Snapshot) => void) {
  const now = () => Timestamp.fromDate(new Date());
  let id = 100n;
  const nid = () => ++id;
  const s: Snapshot = structuredClone(empty) as Snapshot;
  s.patient = {
    handle: 'neal', display_name: 'Neal', status: 'idle', draft: '', last_spoken: 'Thank you.',
    heart_bpm: 72, heart_quality: 0.84, heart_out_of_range: 0, last_seen: now(),
  };
  s.contacts = [
    { id: 1n, patient: 'neal', name: 'Mom', address: '+14695550101', relation: 'mother', tier: 1 },
    { id: 2n, patient: 'neal', name: 'Alex', address: '+19725550142', relation: 'brother', tier: 2 },
    { id: 3n, patient: 'neal', name: 'Night nurse', address: 'nurse@example.org', relation: 'care team', tier: 3 },
  ];
  const push = () => set({ ...s, patient: s.patient && { ...s.patient }, contacts: [...s.contacts], messages: [...s.messages], suggestions: [...s.suggestions], alerts: [...s.alerts], eyes: [...s.eyes], heart: [...s.heart], agent: [...s.agent], phrases: [...s.phrases], briefings: [...s.briefings] });
  for (let i = 0; i < 60; i++) s.heart.push({ id: nid(), patient: 'neal', bpm: 70 + Math.round(4 * Math.sin(i / 5) + Math.random() * 3), quality: 0.8, at: now() });
  const msg = (contact_id: bigint, direction: string, body: string, extra: Partial<Message> = {}) =>
    s.messages.push({ id: nid(), patient: 'neal', contact_id, direction, channel: contact_id ? 'imessage' : 'speech', body, status: direction === 'in' ? 'received' : 'sent', assisted: false, created_at: now(), ...extra });
  msg(1n, 'in', 'Are you still awake, sweetheart?');
  msg(1n, 'out', 'Yes. Can you come by?', { assisted: true });
  msg(0n, 'out', 'Thank you.', { status: 'spoken' });
  const step = (step: string, detail: string) => s.agent.push({ id: nid(), patient: 'neal', run_id: detail.slice(1, 5), step, detail, at: now() });
  step('plan', '[a1f3] Text from Mom. Plan: read the conversation and Neal\'s state, check whether caregivers need to know, draft 4 replies.');
  step('tool', '[a1f3] get_conversation({}) -> 3 messages');
  step('tool', '[a1f3] get_patient_state({}) -> {"heart_rate_now":72,"status":"idle","local_time":"Sunday 1:12 AM"}');
  step('reply', '[a1f3] Yes, I am awake. | Very tired today. | Can you come by? | Call me later.');

  const timers: number[] = [];
  const at = (ms: number, fn: () => void) => { if (!PATIENT_VIEW) timers.push(window.setTimeout(() => { fn(); push(); }, ms)); };
  const eye = (command: string) => { s.eyes.push({ id: nid(), patient: 'neal', command, at: now() }); s.eyes = s.eyes.slice(-40); };
  const typeOut = (start: number, text: string) => {
    [...text].forEach((_, i) => at(start + i * 420, () => {
      s.patient!.draft = text.slice(0, i + 1); s.patient!.status = 'composing';
      eye(i % 3 === 0 ? 'right' : i % 3 === 1 ? 'left' : 'select');
    }));
    return start + text.length * 420;
  };
  s.phrases = ["I'd like some water, please.", 'Thank you.', 'Please call the nurse.']
    .map((text, i) => ({ id: nid(), patient: 'neal', text, uses: 5 - i, last_used: now() }));
  if (PATIENT_VIEW) {
    s.suggestions = ['Yes, I am awake.', 'Very tired today.', 'Can you come by?', 'Call me later.']
      .map((text, rank) => ({ id: nid(), patient: 'neal', message_id: 0n, contact_id: 1n, text, rank, source: 'ai', created_at: now() }));
    s.messages = s.messages.filter(m => m.direction === 'in');
  }
  const t = PATIENT_VIEW ? 0 : typeOut(800, "I'd like some water");
  at(t + 600, () => { s.patient!.last_spoken = "I'd like some water, please."; s.patient!.draft = ''; s.patient!.status = 'speaking'; msg(0n, 'out', "I'd like some water, please.", { status: 'spoken' }); eye('select'); });
  at(t + 3000, () => {
    msg(2n, 'in', 'Hey bro, want me to bring dinner after my shift? Pho or tacos? [Photo: A menu board from a pho shop, with the beef pho circled in red marker.]');
    step('plan', '[b7c2] Text from Alex. Plan: read the conversation and Neal\'s state, check whether caregivers need to know, draft 4 replies.');
    step('tool', '[b7c2] get_conversation({}) -> 1 message');
    step('tool', '[b7c2] get_patient_state({}) -> {"heart_rate_now":73,"favourite_phrases":["I\'d like some water, please.","Thank you."]}');
    step('tool', '[b7c2] propose_replies(4 options)');
    step('reply', '[b7c2] Pho, please! | Tacos sound great. | Not hungry tonight. | What time are you coming?');
    s.suggestions = ['Pho, please!', 'Tacos sound great.', 'Not hungry tonight.', 'What time are you coming?']
      .map((text, rank) => ({ id: nid(), patient: 'neal', message_id: 0n, contact_id: 2n, text, rank, source: 'ai', created_at: now() }));
  });
  at(t + 6500, () => { eye('select'); eye('select'); msg(2n, 'out', 'Pho, please!', { assisted: true }); s.suggestions = []; s.patient!.last_spoken = 'Pho, please!'; });
  at(t + 9000, () => { eye('sos'); s.patient!.status = 'sos'; s.patient!.heart_bpm = 118; s.alerts.push({ id: nid(), patient: 'neal', kind: 'sos', detail: 'Five blinks (SOS)', level: 1, notified_level: 1, acked_by: '', created_at: now(), acked_at: undefined }); });
  const beat = window.setInterval(() => {
    const p = s.patient!;
    const target = p.status === 'sos' ? 116 : 72;
    p.heart_bpm = Math.round(p.heart_bpm + (target - p.heart_bpm) * 0.2 + (Math.random() * 4 - 2));
    s.heart.push({ id: nid(), patient: 'neal', bpm: p.heart_bpm, quality: 0.82, at: now() });
    s.heart = s.heart.slice(-90);
    push();
  }, 1000);
  push();

  const latestIn = () => [...s.messages].reverse().find(m => m.direction === 'in');
  const actions: Actions = {
    eye: command => {
      eye(command);
      if (command === 'toggle' && s.patient!.status !== 'sos') s.patient!.status = s.patient!.status === 'paused' ? 'idle' : 'paused';
      if (command === 'sos' && s.patient!.status !== 'sos') {
        s.patient!.status = 'sos';
        s.alerts.push({ id: nid(), patient: 'neal', kind: 'sos', detail: 'Five blinks (SOS)', level: 1, notified_level: 1, acked_by: '', created_at: now(), acked_at: undefined });
        timers.push(window.setTimeout(() => { const a = s.alerts.find(x => x.kind === 'sos' && !x.acked_by); if (a && PATIENT_VIEW) { a.acked_by = 'Mom'; a.acked_at = now(); s.patient!.status = 'idle'; push(); } }, 6000));
      }
      push();
    },
    setDraft: draft => { s.patient!.draft = draft; push(); },
    speak: text => { msg(0n, 'out', text, { status: 'spoken' }); s.patient!.last_spoken = text; push(); },
    replyLatest: body => { const m = latestIn(); if (m) msg(m.contact_id, 'out', body, { assisted: s.suggestions.some(x => x.text === body) }); s.suggestions = []; push(); },
    requestIntent: keywords => {
      timers.push(window.setTimeout(() => {
        const k = keywords.trim();
        const who = /alex|bro/i.test(k) ? 2n : 1n;
        const word = k.replace(/\b(alex|mom|bro)\b/gi, '').trim() || k;
        const drafts: Array<[bigint, string]> = [[who, `Can you bring ${word} tonight?`], [who, `${word[0].toUpperCase()}${word.slice(1)} later, please.`], [who === 1n ? 2n : 1n, `Could you pick up ${word}?`], [who, `No ${word} for now, thanks.`]];
        s.suggestions = drafts.map(([contact_id, text], rank) => ({ id: nid(), patient: 'neal', message_id: 0n, contact_id, text, rank, source: 'ai', created_at: now() }));
        push();
      }, 1200));
    },
    requestBriefing: (hours, by) => {
      const b = { id: nid(), patient: 'neal', hours, status: 'pending', text: '', model: '', requested_by: by || 'caregiver', created_at: now() };
      s.briefings.push(b); push();
      timers.push(window.setTimeout(() => {
        b.status = 'done'; b.model = 'azure:gpt-chat-latest-2';
        b.text = 'Neal had a mostly calm night. Mom checked in just after 1am and he answered “Yes. Can you come by?” using a suggested reply, then asked aloud for water. Alex offered dinner after his shift and Neal chose pho. One SOS at 2:13am was answered by Mom within a minute. His heart rate stayed steady around 72 bpm; it may be worth asking whether he slept after the SOS.';
        push();
      }, 1600));
    },
    cancelSos: () => { for (const a of s.alerts) if (a.kind === 'sos' && !a.acked_by) { a.acked_by = 'patient (cancelled)'; a.acked_at = now(); } s.patient!.status = 'idle'; push(); },
    ackAlert: (aid, by) => { const a = s.alerts.find(x => x.id === aid); if (a) { a.acked_by = by; a.acked_at = now(); } if (s.patient!.status === 'sos') s.patient!.status = 'idle'; push(); },
    addContact: (name, address, relation, tier) => { s.contacts.push({ id: nid(), patient: 'neal', name, address, relation, tier }); push(); },
    removeContact: cid => { s.contacts = s.contacts.filter(c => c.id !== cid); push(); },
    simulateText: (address, name, body) => { let c = s.contacts.find(x => x.address === address); if (!c) { c = { id: nid(), patient: 'neal', name: name || address, address, relation: 'unknown', tier: 0 }; s.contacts.push(c); } msg(c.id, 'in', body); push(); },
    sendAsPatient: (cid, body) => { msg(cid, 'out', body, { assisted: s.suggestions.some(x => x.text === body) }); s.suggestions = []; push(); },
    clearDemo: () => { s.messages = []; s.alerts = []; s.suggestions = []; s.agent = []; s.eyes = []; push(); },
  };
  return { actions, stop: () => { timers.forEach(clearTimeout); clearInterval(beat); } };
}
