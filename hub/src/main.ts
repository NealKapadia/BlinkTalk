/**
 * BlinkTalk hub: connects SpacetimeDB to the outside world.
 *   - Board: starts the eye-command model on the SK-AM62B over its serial console and forwards
 *     eye commands and heart rate (board.ts)
 *   - Photon: inbound iMessages -> receive_message; queued outbound -> delivered -> mark_message
 *   - Alerts: SOS / heart-rate alerts -> texts to caregivers, tier by tier as SpacetimeDB escalates
 *   - Agent: every inbound text or eye-picked intent -> AI run -> suggestions
 */
import type { Alert, Message, Intent } from '../../bindings/index.ts';
import { settings, redact } from './env.ts';
import { connect, contactById, contactsOf, conversation, recentHeart, topPhrases, type Conn } from './db.ts';
import { startPhoton, type Messenger } from './photon.ts';
import { runAgent, describeImage, photoTag, type AgentPorts, type Task } from './agent.ts';
import { writeBriefing, type Period } from './briefing.ts';
import { aiConfigured } from './llm.ts';
import { runBoardLink } from './board.ts';
import type { Briefing } from '../../bindings/index.ts';

const P = settings.patient;
const ACK_WORDS = /^\s*(ok(ay)?|on it|coming|got it|here|omw|on my way|i'?m here|yes|👍)\b/i;

function displayName(conn: Conn): string {
  return conn.db.patient.handle.find(P)?.display_name ?? P;
}

function openAlerts(conn: Conn): Alert[] {
  return [...conn.db.alert.iter()].filter(a => a.patient === P && !a.acked_by);
}

// ------------------------------------------------------------------ agent wiring
function portsFor(conn: Conn, contactId: bigint, messageId: bigint): AgentPorts {
  const name = displayName(conn);
  return {
    patientName: name,
    conversation: () =>
      conversation(conn, P, contactId).map(m => ({
        who: m.direction === 'in' ? (contactById(conn, m.contact_id)?.name ?? 'them') : name,
        text: m.body,
        at: m.created_at.toDate().toISOString(),
      })),
    patientState: () => {
      const p = conn.db.patient.handle.find(P);
      const hr = recentHeart(conn, P);
      return {
        status: p?.status,
        heart_rate_now: p?.heart_bpm || null,
        heart_rate_recent: hr.slice(-10),
        local_time: new Date().toLocaleString('en-US', { weekday: 'long', hour: 'numeric', minute: '2-digit' }),
        open_alerts: openAlerts(conn).map(a => a.kind),
        favourite_phrases: topPhrases(conn, P),
      };
    },
    contact: () => {
      const c = contactById(conn, contactId);
      return { name: c?.name ?? 'Unknown sender', relation: c?.relation ?? 'unknown', isCaregiver: (c?.tier ?? 0) > 0 };
    },
    contacts: () => contactsOf(conn, P).map(c => ({ id: c.id, name: c.name, relation: c.relation })),
    alertCaregivers: async reason => {
      await conn.reducers.raiseSos({ patient: P, detail: `Agent: ${reason}`.slice(0, 280) });
    },
    propose: async (replies, source, to) => {
      if (to && to.length === replies.length) {
        const ids = to.map(id => (id === 0n ? contactId : id));
        await conn.reducers.addRoutedSuggestions({ patient: P, message_id: messageId, contact_ids: ids, texts: replies, source });
      } else {
        await conn.reducers.addSuggestions({ patient: P, message_id: messageId, contact_id: contactId, texts: replies, source });
      }
    },
    log: async (step, detail) => {
      console.log(`[agent] ${step}: ${detail}`);
      await conn.reducers.logAgentStep({ patient: P, run_id: detail.slice(1, 9), step, detail });
    },
  };
}

let agentQueue: Promise<unknown> = Promise.resolve();
function enqueueAgent(conn: Conn, task: Task, contactId: bigint, messageId: bigint) {
  agentQueue = agentQueue
    .then(() => runAgent(task, portsFor(conn, contactId, messageId)))
    .catch(err => console.error('[agent] run failed', err));
}

// ------------------------------------------------------------------ caregiver alerts
const ALERT_HEAD: Record<string, string> = {
  sos: '🚨 SOS',
  heart_high: '⚠️ Heart rate high',
  heart_low: '⚠️ Heart rate low',
};

async function notifyAlert(conn: Conn, msgr: Messenger, a: Alert) {
  if (a.patient !== P || a.acked_by || a.level <= a.notified_level) return;
  const name = displayName(conn);
  const p = conn.db.patient.handle.find(P);
  const maxTier = a.level;
  const targets = contactsOf(conn, P).filter(c => c.tier >= 1 && c.tier <= maxTier && c.tier > a.notified_level);
  const hr = p?.heart_bpm ? ` Heart rate ${p.heart_bpm} bpm.` : '';
  const escalated = a.level > 1 ? ` No one has answered yet, so BlinkTalk is now reaching tier ${a.level}.` : '';
  const ask = ' Reply OK when you are on it.';
  const body = `${ALERT_HEAD[a.kind] ?? 'Alert'} from ${name} (BlinkTalk): ${a.detail}.${hr}${escalated}${ask}`;
  for (const c of targets) {
    if (!msgr.canReach(c.address)) {
      console.warn(`[alert] ${c.name} (${c.address}) has not texted the BlinkTalk line yet, so Photon cannot reach them.`);
      continue;
    }
    try {
      await msgr.send(c.address, body);
      console.log(`[alert] ${a.kind} L${a.level} -> ${c.name}`);
    } catch (err) {
      console.error(`[alert] could not text ${c.name}`, err);
    }
  }
  await conn.reducers.markAlertNotified({ id: a.id, level: maxTier });
}

// ------------------------------------------------------------------ outbound delivery
const inFlight = new Set<bigint>();
async function deliver(conn: Conn, msgr: Messenger, m: Message) {
  if (m.patient !== P || m.direction !== 'out' || m.status !== 'queued' || m.channel === 'speech') return;
  if (inFlight.has(m.id)) return;
  inFlight.add(m.id);
  try {
    const c = contactById(conn, m.contact_id);
    if (!c) throw new Error('contact missing');
    await msgr.send(c.address, m.body);
    await conn.reducers.markMessage({ id: m.id, status: 'sent' });
  } catch (err) {
    console.error('[photon] delivery failed', err);
    await conn.reducers.markMessage({ id: m.id, status: 'failed' });
  } finally {
    inFlight.delete(m.id);
  }
}

// ------------------------------------------------------------------ caregiver briefing input
function periodFor(conn: Conn, hours: number): Period {
  const now = new Date();
  const since = now.getTime() - hours * 3600_000;
  const recent = <T extends { patient: string }>(rows: Iterable<T>, at: (r: T) => Date) =>
    [...rows].filter(r => r.patient === P && at(r).getTime() >= since);
  const name = (id: bigint) => contactById(conn, id)?.name?.split(' ')[0] ?? 'someone';
  return {
    patientName: displayName(conn),
    hours,
    now,
    messages: recent(conn.db.message.iter(), m => m.created_at.toDate())
      .sort((a, b) => (a.id < b.id ? -1 : 1))
      .map(m => ({
        at: m.created_at.toDate(),
        who: name(m.contact_id),
        to: m.direction === 'out' ? name(m.contact_id) : undefined,
        text: m.body,
        kind: m.channel === 'speech' ? 'spoken' as const : m.direction === 'in' ? 'in' as const : 'out' as const,
        assisted: m.assisted,
      })),
    alerts: recent(conn.db.alert.iter(), a => a.created_at.toDate()).map(a => ({
      at: a.created_at.toDate(),
      kind: a.kind,
      ackedBy: a.acked_by || undefined,
      ackSeconds: a.acked_at ? Math.round((a.acked_at.toDate().getTime() - a.created_at.toDate().getTime()) / 1000) : undefined,
      level: a.level,
    })),
    heart: recent(conn.db.heart_sample.iter(), h => h.at.toDate()).filter(h => h.quality >= 0.5).map(h => h.bpm),
    eyeMovements: recent(conn.db.eye_event.iter(), e => e.at.toDate()).length,
  };
}

// ------------------------------------------------------------------ main
async function main() {
  console.log(`[hub] patient=${P} spacetime=${settings.spacetimeUri}/${settings.spacetimeDb}`);
  console.log(aiConfigured()
    ? `[hub] AI: ${settings.aiModel} at ${settings.aiBaseUrl} key=${redact(settings.aiApiKey)} (replies, photos, briefings)`
    : '[hub] AI: none configured (AI_BASE_URL); using built-in replies and plain summaries');
  const conn = await connect();
  console.log('[hub] connected to SpacetimeDB');

  const recentAcks = new Map<string, number>();
  let msgr: Messenger;
  msgr = await startPhoton(async ({ address, body, channel, attachmentIds }) => {
    if (attachmentIds?.length) {
      const sender = contactsOf(conn, P).find(c => c.address === address)?.name ?? 'Someone';
      const tags: string[] = [];
      for (const id of attachmentIds.slice(0, 3)) {
        try {
          const image = await msgr.download(id);
          const description = await describeImage(image, sender, body);
          console.log(`[photo] ${sender}: ${description ?? '(no description)'}`);
          tags.push(photoTag(description));
        } catch (err) {
          console.error('[photo] could not read attachment', err);
          tags.push(photoTag(null));
        }
      }
      body = [body, ...tags].filter(Boolean).join(' ');
    }
    if (ACK_WORDS.test(body) && openAlerts(conn).length) {
      recentAcks.set(address, Date.now());
      await conn.reducers.ackAlertByReply({ patient: P, address });
    }
    await conn.reducers.receiveMessage({ patient: P, address, sender_name: '', channel, body });
  });

  // Ignore history already in the database at startup; act only on new rows.
  const seenMessage = new Set([...conn.db.message.iter()].map(m => m.id));
  const seenIntent = new Set([...conn.db.intent.iter()].map(i => i.id));

  conn.db.message.onInsert((_ctx, m: Message) => {
    if (m.patient !== P) return;
    if (m.direction === 'out') { void deliver(conn, msgr, m); return; }
    if (seenMessage.has(m.id)) return;
    seenMessage.add(m.id);
    const c = contactById(conn, m.contact_id);
    const acked = c && Date.now() - (recentAcks.get(c.address) ?? 0) < 5000;
    if (acked) return;
    enqueueAgent(conn, { kind: 'reply', message: m }, m.contact_id, m.id);
  });
  conn.db.message.onUpdate((_ctx, _old, m: Message) => void deliver(conn, msgr, m));

  conn.db.intent.onInsert((_ctx, i: Intent) => {
    if (i.patient !== P || i.status !== 'pending' || seenIntent.has(i.id)) return;
    seenIntent.add(i.id);
    agentQueue = agentQueue
      .then(() => runAgent({ kind: 'intent', keywords: i.keywords }, portsFor(conn, i.contact_id, 0n)))
      .then(() => conn.reducers.finishIntent({ id: i.id }))
      .catch(err => console.error('[agent] intent failed', err));
  });

  // Caregiver briefings: answer every pending request, including ones made while the hub was down.
  const briefingsSeen = new Set<bigint>();
  const handleBriefing = (b: Briefing) => {
    if (b.patient !== P || b.status !== 'pending' || briefingsSeen.has(b.id)) return;
    briefingsSeen.add(b.id);
    console.log(`[briefing] writing the last ${b.hours} h for ${b.requested_by}…`);
    void (async () => {
      let out: { text: string; model: string };
      try {
        out = await writeBriefing(periodFor(conn, b.hours));
      } catch (err) {
        console.error('[briefing] could not gather the data', err);
        out = { text: 'BlinkTalk could not put together a summary just now. Please try again in a minute.', model: 'rules' };
      }
      console.log(`[briefing] ${out.model}: ${out.text.slice(0, 120)}…`);
      await conn.reducers.setBriefing({ id: b.id, text: out.text, model: out.model });
    })().catch(err => console.error('[briefing] failed to save', err));
  };
  conn.db.briefing.onInsert((_ctx, b: Briefing) => handleBriefing(b));
  for (const b of conn.db.briefing.iter()) handleBriefing(b);

  conn.db.alert.onInsert((_ctx, a: Alert) => void notifyAlert(conn, msgr, a));
  conn.db.alert.onUpdate((_ctx, _old, a: Alert) => void notifyAlert(conn, msgr, a));

  // Catch up on anything left pending while the hub was down.
  for (const m of conn.db.message.iter()) void deliver(conn, msgr, m);
  for (const a of conn.db.alert.iter()) void notifyAlert(conn, msgr, a);

  startBoard(conn);
  console.log('[hub] ready');
}

// ------------------------------------------------------------------ board (SK-AM62B)
function startBoard(conn: Conn) {
  if (!settings.boardPort) {
    console.log('[board] BOARD_PORT not set: no board link (the patient screen still works with the keyboard)');
    return;
  }
  runBoardLink(settings.boardPort, {
    dir: settings.boardDir, sensitivity: settings.boardSensitivity, led: settings.boardLed, swapLR: settings.boardSwapLR,
  }, {
    status: msg => console.log(`[board] ${msg}`),
    line: l => {
      switch (l.kind) {
        case 'command':
          console.log(`[board] ${l.command}${l.delayMs !== undefined ? ` (${l.delayMs} ms after the movement)` : ''}`);
          conn.reducers.eyeCommand({ patient: P, command: l.command }).catch(err => console.error('[board] eye_command failed', err));
          break;
        case 'heart':
          if (l.bpm) conn.reducers.heartbeat({ patient: P, bpm: l.bpm, quality: l.quality }).catch(err => console.error('[board] heartbeat failed', err));
          break;
        case 'ready': console.log(`[board] model running: ${l.detail}`); break;
        case 'error': console.error(`[board] ERROR ${l.detail}`); break;
        case 'alert': console.log(`[board] ${l.detail} (SpacetimeDB raises the caregiver alert from the heart-rate readings)`); break;
        default: if (l.kind !== 'info' || !/^(blink|led|device)/.test(l.detail)) console.log(`[board] ${l.kind}: ${l.detail}`);
      }
    },
  });
}

main().catch(err => {
  console.error('[hub] fatal', err);
  process.exit(1);
});
