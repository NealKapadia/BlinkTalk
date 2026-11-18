/**
 * Caregiver briefing ("Catch me up"). A caregiver taps the button and the AI model (llm.ts) turns
 * the last N hours (conversation, alerts and how fast they were answered, heart rate, how much the
 * patient communicated) into a short, warm, plain-language note, like a nurse's shift handoff.
 * Only first names and the patient's own words are sent: no phone numbers.
 * With no model configured, or if the call fails, the hub writes a plain rule-based summary instead.
 */
import { aiConfigured, chatCompletion, modelLabel } from './llm.ts';

export interface Period {
  patientName: string;
  hours: number;
  now: Date;
  messages: Array<{ at: Date; who: string; to?: string; text: string; kind: 'in' | 'out' | 'spoken'; assisted?: boolean }>;
  alerts: Array<{ at: Date; kind: string; ackedBy?: string; ackSeconds?: number; level: number }>;
  heart: number[];          // good-quality bpm samples in the period
  eyeMovements: number;
}

export type Fetch = typeof fetch;

const clock = (d: Date) => d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });

export function periodToText(p: Period): string {
  const lines: string[] = [];
  lines.push(`Patient: ${p.patientName}. Window: last ${p.hours} hours, ending ${p.now.toLocaleString('en-US', { weekday: 'long', hour: 'numeric', minute: '2-digit' })}.`);
  if (p.heart.length) {
    const avg = Math.round(p.heart.reduce((a, b) => a + b, 0) / p.heart.length);
    lines.push(`Heart rate: average ${avg} bpm, range ${Math.min(...p.heart)}-${Math.max(...p.heart)} bpm over ${p.heart.length} readings.`);
  } else lines.push('Heart rate: no readings.');
  lines.push(`Eye movements made on the speller: ${p.eyeMovements}.`);
  lines.push(`Alerts (${p.alerts.length}):`);
  for (const a of p.alerts) {
    lines.push(`- ${clock(a.at)} ${a.kind}${a.ackedBy ? `, answered by ${a.ackedBy}${a.ackSeconds !== undefined ? ` after ${a.ackSeconds}s` : ''}` : ', not answered yet'}${a.level > 1 ? `, escalated to tier ${a.level}` : ''}`);
  }
  lines.push(`Messages (${p.messages.length}), oldest first:`);
  for (const m of p.messages.slice(-60)) {
    const who = m.kind === 'in' ? m.who : m.kind === 'spoken' ? `${p.patientName} (said aloud)` : `${p.patientName} to ${m.to ?? 'someone'}`;
    lines.push(`- ${clock(m.at)} ${who}: ${m.text.slice(0, 240)}${m.assisted ? ' [chosen from AI suggestions]' : ''}`);
  }
  return lines.join('\n');
}

const PROMPT = (data: string) => `You write short handoff notes for the family and caregivers of someone with ALS who communicates only with eye movements.
Write 3 to 5 sentences in plain, warm, factual English for a family member catching up. Cover: who they talked with and what they asked for or said (quote their own words briefly when it matters), any alerts and how quickly they were answered, heart rate if notable, and anything a caregiver should follow up on. Do not invent anything that is not in the data. Do not give medical advice. No headings, no bullet points.

Data:
${data}`;

/** Plain summary used when no model is configured or the call fails. */
export function rulesBriefing(p: Period): string {
  const out = p.messages.filter(m => m.kind !== 'in').length;
  const people = [...new Set(p.messages.filter(m => m.kind === 'in').map(m => m.who))];
  const parts = [`In the last ${p.hours} hours ${p.patientName} sent or said ${out} ${out === 1 ? 'message' : 'messages'}${people.length ? ` and heard from ${people.join(', ')}` : ''}.`];
  if (p.alerts.length) {
    parts.push(p.alerts.map(a => `${a.kind === 'sos' ? 'An SOS' : 'A heart-rate alert'} at ${clock(a.at)} was ${a.ackedBy ? `answered by ${a.ackedBy}${a.ackSeconds !== undefined ? ` in ${a.ackSeconds} seconds` : ''}` : 'not answered yet'}.`).join(' '));
  } else parts.push('There were no alerts.');
  if (p.heart.length) parts.push(`Heart rate averaged ${Math.round(p.heart.reduce((a, b) => a + b, 0) / p.heart.length)} bpm.`);
  const last = [...p.messages].reverse().find(m => m.kind !== 'in');
  if (last) parts.push(`Their latest words: “${last.text.slice(0, 120)}”.`);
  return parts.join(' ');
}

export async function writeBriefing(p: Period, fetchFn: Fetch = fetch): Promise<{ text: string; model: string }> {
  if (!aiConfigured()) return { text: rulesBriefing(p), model: 'rules' };
  try {
    const msg = await chatCompletion(fetchFn, { messages: [{ role: 'user', content: PROMPT(periodToText(p)) }], maxTokens: 700 });
    const text = (msg.content ?? '').replace(/<think>[\s\S]*?<\/think>/g, '').trim();
    if (!text) throw new Error('the model returned no text');
    return { text: text.slice(0, 2000), model: modelLabel() };
  } catch (err) {
    console.error('[briefing] AI unavailable, using a plain summary:', String((err as Error).message ?? err).slice(0, 300));
    return { text: rulesBriefing(p), model: 'rules' };
  }
}
