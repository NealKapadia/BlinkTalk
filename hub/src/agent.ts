/**
 * BlinkTalk agent. The model calls go through llm.ts (Azure AI Foundry or a local model).
 *
 * Every glance costs the patient seconds, so the agent turns an incoming text
 * (or 2-3 keywords picked with the eyes) into four complete, first-person
 * replies the patient can send with one double blink. It is not a chatbot: it
 * never answers on the patient's behalf. It plans, calls tools against the live
 * SpacetimeDB state, decides whether caregivers need to know something, and
 * only then proposes replies. Every step is written to the agent_step table so
 * caregivers can watch it work.
 *
 * With a local model (AI_BASE_URL=http://localhost:11434/v1) the whole system runs with no
 * internet except the messaging channel.
 */
import { randomUUID } from 'node:crypto';
import { aiConfigured, chatCompletion, type Fetch, type ToolCall } from './llm.ts';
export type { Fetch } from './llm.ts';

// ------------------------------------------------------------------ ports
/** What the agent can read and do. Implemented over SpacetimeDB in main.ts, faked in tests. */
export interface AgentPorts {
  patientName: string;
  conversation(): Array<{ who: string; text: string; at?: string }>;
  patientState(): Record<string, unknown>;
  contact(): { name: string; relation: string; isCaregiver: boolean };
  /** Everyone the patient can text (for routing a message to the right person). */
  contacts(): Array<{ id: bigint; name: string; relation: string }>;
  alertCaregivers(reason: string): Promise<void>;
  /** Replies; `to[i]` is the contact for replies[i] (omitted = whoever the conversation is with). */
  propose(replies: string[], source: 'ai' | 'fallback', to?: bigint[]): Promise<void>;
  log(step: string, detail: string): Promise<void>;
}

export type Task =
  | { kind: 'reply'; message: { body: string } }
  | { kind: 'intent'; keywords: string };

// ------------------------------------------------------------------ deterministic fallback
export function fallbackReplies(task: Task): string[] {
  if (task.kind === 'intent') {
    const k = task.keywords.trim();
    const cap = k.charAt(0).toUpperCase() + k.slice(1);
    return [`${cap}, please.`, `I need ${k}.`, `Can we talk about ${k}?`, `Yes, ${k}.`];
  }
  const t = task.message.body.toLowerCase();
  if (/\?\s*$/.test(t) || /\b(are|do|can|will|would|should|is)\s+you\b/.test(t)) {
    if (/\b(ok|okay|alright|doing|feel)/.test(t)) return ["I'm okay, thanks.", 'Not great today.', 'Can you come by?', 'Call me later.'];
    if (/\b(visit|come|stop by|tonight|tomorrow|bring)\b/.test(t)) return ['Yes, please come.', 'Not today, sorry.', 'Tomorrow is better.', 'What time?'];
    return ['Yes.', 'No.', 'Maybe later.', 'Can you call me?'];
  }
  return ['Thank you.', 'Love you.', 'Can you call me?', 'Talk soon.'];
}

// ------------------------------------------------------------------ model client
type ChatMsg =
  | { role: 'system' | 'user'; content: string }
  | { role: 'assistant'; content: string | null; tool_calls?: ToolCall[] }
  | { role: 'tool'; tool_call_id: string; content: string };


const TOOLS = [
  {
    type: 'function',
    function: {
      name: 'get_conversation',
      description: 'Recent messages between the patient and this contact (plus what the patient said aloud), oldest first.',
      parameters: { type: 'object', properties: {} },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_patient_state',
      description: "The patient's live state: heart rate trend, current status, local time, open alerts, phrases they use most.",
      parameters: { type: 'object', properties: {} },
    },
  },
  {
    type: 'function',
    function: {
      name: 'alert_caregivers',
      description: "Text the patient's caregivers. Only for a genuine safety concern: the message reports an emergency, or the patient's state looks medically worrying.",
      parameters: { type: 'object', properties: { reason: { type: 'string' } }, required: ['reason'] },
    },
  },
  {
    type: 'function',
    function: {
      name: 'list_contacts',
      description: 'People the patient can text, with their relation (mother, brother, nurse...).',
      parameters: { type: 'object', properties: {} },
    },
  },
  {
    type: 'function',
    function: {
      name: 'propose_messages',
      description: 'Finish (for keywords): exactly 4 complete messages, each addressed to the person it is for. Use names from list_contacts.',
      parameters: {
        type: 'object',
        properties: {
          messages: {
            type: 'array',
            items: { type: 'object', properties: { to: { type: 'string' }, text: { type: 'string' } }, required: ['to', 'text'] },
          },
        },
        required: ['messages'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'propose_replies',
      description: 'Finish: give exactly 4 short first-person replies the patient can send with one selection.',
      parameters: {
        type: 'object',
        properties: { replies: { type: 'array', items: { type: 'string' } } },
        required: ['replies'],
      },
    },
  },
] as const;

function systemPrompt(name: string): string {
  return [
    `You are the communication agent inside BlinkTalk. ${name} cannot speak or type; they control a screen with eye movements, and every selection costs several seconds.`,
    'You never message anyone yourself. You prepare 4 reply options that sound like the patient: first person, warm, plain, at most 12 words each.',
    'Make the 4 options meaningfully different (for example yes / no / not now / a question back). Use facts from the tools; never invent plans, names or medical facts.',
    'Workflow: 1) call get_conversation and get_patient_state; 2) call alert_caregivers only if there is a genuine safety concern; 3) call propose_replies.',
    'Text like "[Photo: ...]" is a description of a photo they were sent; reply to the photo naturally (e.g. "That looks delicious!").',
  ].join('\n');
}

function taskPrompt(task: Task, contact: { name: string; relation: string }): string {
  if (task.kind === 'intent') {
    return `The patient selected these keywords with their eyes: "${task.keywords}". Work out who each message is for (a name or relation in the keywords, otherwise ${contact.name}): call list_contacts, read the conversation, then call propose_messages with 4 complete messages, each addressed to the right person. Offer different people if the keywords are ambiguous.`;
  }
  return `New message from ${contact.name} (${contact.relation || 'unknown relation'}): "${task.message.body}". Follow the workflow.`;
}

export function stripThinking(s: string): string {
  return s.replace(/<think>[\s\S]*?<\/think>/g, '').trim();
}

export function parseRepliesFromText(s: string): string[] | null {
  const text = stripThinking(s);
  const m = text.match(/\{[\s\S]*\}/);
  if (m) {
    try {
      const j = JSON.parse(m[0]);
      if (Array.isArray(j.replies)) return j.replies.map(String);
    } catch { /* fall through */ }
  }
  const lines = text.split('\n').map(l => l.replace(/^\s*(\d+[.)]|[-*•])\s*/, '').replace(/^"|"$/g, '').trim()).filter(Boolean);
  return lines.length >= 2 ? lines.slice(0, 4) : null;
}

export const agentConfigured = aiConfigured;

async function chat(fetchFn: Fetch, messages: ChatMsg[], useTools: boolean): Promise<{ content: string | null; tool_calls?: ToolCall[] }> {
  return chatCompletion(fetchFn, { messages, tools: useTools ? TOOLS : undefined, maxTokens: 800 });
}

function clampReplies(replies: unknown[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const r of replies) {
    const t = stripThinking(String(r)).replace(/\s+/g, ' ').trim().slice(0, 140);
    if (t && !seen.has(t.toLowerCase())) { seen.add(t.toLowerCase()); out.push(t); }
  }
  return out.slice(0, 4);
}

/** Map a name the model wrote ("Alex", "mom", "my brother") to a contact id. */
export function resolveContact(to: string, contacts: Array<{ id: bigint; name: string; relation: string }>): bigint | undefined {
  const t = to.toLowerCase().replace(/^(my|to)\s+/, '').trim();
  if (!t) return undefined;
  const exact = contacts.find(c => c.name.toLowerCase() === t || c.relation.toLowerCase() === t);
  if (exact) return exact.id;
  const alias: Record<string, string[]> = { mother: ['mom', 'mum', 'mama'], father: ['dad', 'papa'], brother: ['bro'], sister: ['sis'] };
  const loose = contacts.find(c =>
    t.startsWith(c.name.toLowerCase().split(' ')[0]) || c.name.toLowerCase().startsWith(t) ||
    (alias[c.relation.toLowerCase()] ?? []).includes(t));
  return loose?.id;
}

// ------------------------------------------------------------------ photos (vision model)
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;

/**
 * Describe a photo someone texted the patient, in one or two plain sentences, so it
 * can be shown on their screen and used as context for replies. Uses the same
 * vision-capable model (image as a data URL). Returns null if no model is reachable.
 */
export async function describeImage(
  image: { data: Buffer; contentType: string }, senderName: string, caption: string, fetchFn: Fetch = fetch,
): Promise<string | null> {
  if (!agentConfigured() || !image.contentType.startsWith('image/') || image.data.length > MAX_IMAGE_BYTES) return null;
  const prompt = `${senderName} texted this photo to someone who cannot move or speak${caption ? `, with the words "${caption}"` : ''}. ` +
    'Describe what matters in it in one or two short, warm, concrete sentences (people, food, places, text in the image). No preamble.';
  try {
    const msg = await chatCompletion(fetchFn, {
      vision: true, maxTokens: 160,
      messages: [{ role: 'user', content: [
        { type: 'text', text: prompt },
        { type: 'image_url', image_url: { url: `data:${image.contentType};base64,${image.data.toString('base64')}` } },
      ] }],
    });
    const text = stripThinking(msg.content ?? '').replace(/\s+/g, ' ').trim();
    return text ? text.slice(0, 280) : null;
  } catch {
    return null;
  }
}

/** "[Photo: …]" is how a described photo travels through SpacetimeDB; the screens render it. */
export const photoTag = (description: string | null) => (description ? `[Photo: ${description}]` : '[Photo]');

// ------------------------------------------------------------------ the run
export async function runAgent(task: Task, ports: AgentPorts, fetchFn: Fetch = fetch): Promise<string[]> {
  const runId = randomUUID().slice(0, 8);
  const log = (step: string, detail: string) => ports.log(step, `[${runId}] ${detail}`);
  const contact = ports.contact();
  let alerted = false;

  await log('plan', task.kind === 'reply'
    ? `Text from ${contact.name}. Plan: read the conversation and ${ports.patientName}'s state, check whether caregivers need to know, draft 4 replies.`
    : `${ports.patientName} picked "${task.keywords}". Plan: read the conversation, expand into 4 full replies.`);

  const finish = async (replies: string[], source: 'ai' | 'fallback') => {
    await ports.propose(replies, source);
    await log('reply', replies.join(' | '));
    return replies;
  };
  const finishFallback = async (why: string) => {
    await log('fallback', why);
    return finish(fallbackReplies(task), 'fallback');
  };

  if (!agentConfigured()) return finishFallback('No AI model configured (AI_BASE_URL); using built-in replies.');

  const messages: ChatMsg[] = [
    { role: 'system', content: systemPrompt(ports.patientName) },
    { role: 'user', content: taskPrompt(task, contact) },
  ];
  let useTools = true;

  for (let turn = 0; turn < 6; turn++) {
    let msg;
    try {
      msg = await chat(fetchFn, messages, useTools);
    } catch (err: any) {
      if (useTools && err?.status && err.status >= 400 && err.status < 500) {
        await log('retry', `Tool calling rejected (${err.status}); retrying in JSON mode.`);
        useTools = false;
        messages.push({ role: 'user', content: `Tools are unavailable. Context: conversation=${JSON.stringify(ports.conversation())} state=${JSON.stringify(ports.patientState())}. Reply ONLY with JSON: {"replies":["","","",""]}` });
        continue;
      }
      return finishFallback(`AI model unavailable (${String(err?.message ?? err).slice(0, 160)}).`);
    }

    const calls = msg.tool_calls ?? [];
    if (!calls.length) {
      const parsed = parseRepliesFromText(msg.content ?? '');
      const replies = parsed ? clampReplies(parsed) : [];
      if (replies.length >= 2) return finish(replies, 'ai');
      messages.push({ role: 'assistant', content: msg.content ?? '' });
      messages.push({ role: 'user', content: 'Call propose_replies with exactly 4 replies now.' });
      continue;
    }

    messages.push({ role: 'assistant', content: msg.content ?? null, tool_calls: calls });
    for (const call of calls) {
      let args: any = {};
      try { args = JSON.parse(call.function.arguments || '{}'); } catch { /* keep {} */ }
      let result: unknown;
      switch (call.function.name) {
        case 'get_conversation':
          result = ports.conversation();
          break;
        case 'get_patient_state':
          result = ports.patientState();
          break;
        case 'list_contacts':
          result = ports.contacts().map(c => ({ name: c.name, relation: c.relation }));
          break;
        case 'propose_messages': {
          const items = (Array.isArray(args.messages) ? args.messages : []).filter((m: any) => m && m.text);
          const texts = clampReplies(items.map((m: any) => m.text));
          if (texts.length >= 2) {
            const people = ports.contacts();
            const to = texts.map(t => {
              const m = items.find((x: any) => stripThinking(String(x.text)).replace(/\s+/g, ' ').trim().slice(0, 140) === t);
              return resolveContact(String(m?.to ?? ''), people) ?? 0n;
            });
            await log('tool', `propose_messages(${texts.map((t, i) => `${people.find(p => p.id === to[i])?.name ?? 'reply'}: ${t}`).join(' | ').slice(0, 300)})`);
            await ports.propose(texts, 'ai', to);
            await log('reply', texts.join(' | '));
            return texts;
          }
          result = { error: 'need 4 messages with to and text' };
          break;
        }
        case 'alert_caregivers':
          if (!alerted) { await ports.alertCaregivers(String(args.reason ?? 'Agent safety concern')); alerted = true; }
          result = { sent: true };
          break;
        case 'propose_replies': {
          const replies = clampReplies(Array.isArray(args.replies) ? args.replies : []);
          if (replies.length >= 2) {
            await log('tool', `propose_replies(${replies.length} options)`);
            return finish(replies, 'ai');
          }
          result = { error: 'need 4 non-empty replies' };
          break;
        }
        default:
          result = { error: `unknown tool ${call.function.name}` };
      }
      await log('tool', `${call.function.name}(${JSON.stringify(args).slice(0, 120)}) -> ${JSON.stringify(result).slice(0, 200)}`);
      messages.push({ role: 'tool', tool_call_id: call.id, content: JSON.stringify(result) });
    }
  }
  return finishFallback('Agent did not finish in 6 steps.');
}
