import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { AgentPorts } from '../src/agent.ts';
import type { Period } from '../src/briefing.ts';

process.env.AI_BASE_URL = 'https://example.services.ai.azure.com/openai/v1/';
process.env.AI_API_KEY = 'test-azure-key';
process.env.AI_MODEL = 'gpt-chat-latest-2';
process.env.AI_VISION_MODEL = '';
const { runAgent, describeImage } = await import('../src/agent.ts');
const { writeBriefing } = await import('../src/briefing.ts');
const { aiConfigured, modelLabel } = await import('../src/llm.ts');

function recorder(replies: Array<unknown | Response>) {
  const calls: Array<{ url: string; headers: Record<string, string>; body: any }> = [];
  const fn = (async (url: string, init: any) => {
    calls.push({ url, headers: init.headers, body: JSON.parse(init.body) });
    const next = replies.shift();
    if (next instanceof Response) return next;
    return new Response(JSON.stringify({ choices: [{ message: next }] }), { status: 200 });
  }) as unknown as typeof fetch;
  return { fn, calls };
}

const ports = (): { ports: AgentPorts; proposed: Array<[string[], string]> } => {
  const proposed: Array<[string[], string]> = [];
  return {
    proposed,
    ports: {
      patientName: 'Neal',
      conversation: () => [{ who: 'Mom', text: 'Are you awake?' }],
      patientState: () => ({ heart_rate_now: 72 }),
      contact: () => ({ name: 'Mom', relation: 'mother', isCaregiver: true }),
      contacts: () => [{ id: 1n, name: 'Mom', relation: 'mother' }],
      alertCaregivers: async () => {},
      propose: async (r, s) => { proposed.push([r, s]); },
      log: async () => {},
    },
  };
};

test('base URL + key + model set: the model is configured for everything', () => {
  assert.equal(aiConfigured(), true);
  assert.equal(modelLabel(), 'gpt-chat-latest-2');
});

test('agent: tool calls go to {AI_BASE_URL}/chat/completions with the model, both auth headers, no sampling fields', async () => {
  const { fn, calls } = recorder([
    { content: null, tool_calls: [{ id: 'a', type: 'function', function: { name: 'get_conversation', arguments: '{}' } }] },
    { content: null, tool_calls: [{ id: 'b', type: 'function', function: { name: 'propose_replies', arguments: JSON.stringify({ replies: ['Yes, I am up.', 'Not yet.', 'Come by later?', 'Love you.'] }) } }] },
  ]);
  const { ports: p, proposed } = ports();
  const out = await runAgent({ kind: 'reply', message: { body: 'Are you awake?' } }, p, fn);
  assert.deepEqual(out, ['Yes, I am up.', 'Not yet.', 'Come by later?', 'Love you.']);
  assert.equal(proposed[0][1], 'ai');
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, 'https://example.services.ai.azure.com/openai/v1/chat/completions');
  assert.equal(calls[0].headers.Authorization, 'Bearer test-azure-key');
  assert.equal(calls[0].headers['api-key'], 'test-azure-key');
  assert.equal(calls[0].body.model, 'gpt-chat-latest-2');
  assert.equal(calls[0].body.max_completion_tokens, 800);
  assert.ok(Array.isArray(calls[0].body.tools));
  for (const k of ['temperature', 'top_p', 'max_tokens']) assert.equal(k in calls[0].body, false, k);
  assert.equal(calls[1].body.messages.at(-1).role, 'tool');
});

test('a server that rejects max_completion_tokens is retried once with max_tokens', async () => {
  const { fn, calls } = recorder([
    new Response(JSON.stringify({ error: { message: "Unsupported parameter: 'max_completion_tokens'" } }), { status: 400 }),
    { content: '{"replies":["Yes.","No.","Later.","Call me?"]}' },
  ]);
  const { ports: p } = ports();
  const out = await runAgent({ kind: 'reply', message: { body: 'Are you awake?' } }, p, fn);
  assert.deepEqual(out, ['Yes.', 'No.', 'Later.', 'Call me?']);
  assert.equal(calls[1].body.max_tokens, 800);
  assert.equal('max_completion_tokens' in calls[1].body, false);
});

test('model outage: built-in replies, never a crash', async () => {
  const { fn } = recorder([new Response('server error', { status: 500 })]);
  const { ports: p, proposed } = ports();
  const out = await runAgent({ kind: 'reply', message: { body: 'Are you awake?' } }, p, fn);
  assert.equal(out.length, 4);
  assert.equal(proposed[0][1], 'fallback');
});

test('photo descriptions use the configured model with the image as a data URL', async () => {
  const { fn, calls } = recorder([{ content: 'Alex holding a birthday cake with candles.' }]);
  const d = await describeImage({ data: Buffer.from([1, 2, 3]), contentType: 'image/jpeg' }, 'Alex', '', fn);
  assert.equal(d, 'Alex holding a birthday cake with candles.');
  assert.equal(calls[0].body.model, 'gpt-chat-latest-2');
  assert.match(calls[0].body.messages[0].content[1].image_url.url, /^data:image\/jpeg;base64,AQID$/);
});

const period: Period = {
  patientName: 'Neal', hours: 3, now: new Date(2026, 10, 11, 21, 0),
  messages: [{ at: new Date(2026, 10, 11, 20, 0), who: 'Mom', text: 'Are you awake?', kind: 'in' }],
  alerts: [], heart: [70, 72], eyeMovements: 12,
};

test('briefings: the same model writes them, labelled with the model name', async () => {
  const { fn, calls } = recorder([{ content: 'Mom checked in at 8 PM. Neal is calm, heart rate around 71.' }]);
  const out = await writeBriefing(period, fn);
  assert.equal(out.model, 'gpt-chat-latest-2');
  assert.match(out.text, /Mom checked in/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://example.services.ai.azure.com/openai/v1/chat/completions');
  assert.match(calls[0].body.messages[0].content, /handoff notes/);
});

test('briefings: model failure falls back to the plain summary', async () => {
  const { fn } = recorder([new Response('nope', { status: 503 })]);
  const out = await writeBriefing(period, fn);
  assert.equal(out.model, 'rules');
  assert.match(out.text, /Neal/);
});
