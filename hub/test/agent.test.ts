import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { AgentPorts } from '../src/agent.ts';

// a local model server (Ollama-style): no API key needed; real environment variables win over .env
process.env.AI_BASE_URL = 'http://localhost:11434/v1';
process.env.AI_API_KEY = '';
process.env.AI_MODEL = 'local-test-model';
const { runAgent, parseRepliesFromText, resolveContact } = await import('../src/agent.ts');

function fakePorts() {
  const calls = { alerts: [] as string[], proposed: [] as Array<[string[], string]>, logs: [] as string[], routes: [] as Array<bigint[] | undefined> };
  const ports: AgentPorts = {
    patientName: 'Neal',
    conversation: () => [{ who: 'Mom', text: 'Are you awake?' }],
    patientState: () => ({ heart_rate_now: 72 }),
    contact: () => ({ name: 'Mom', relation: 'mother', isCaregiver: true }),
    contacts: () => [{ id: 1n, name: 'Mom', relation: 'mother' }, { id: 2n, name: 'Alex', relation: 'brother' }],
    alertCaregivers: async why => { calls.alerts.push(why); },
    propose: async (r, s, to) => { calls.proposed.push([r, s]); calls.routes.push(to); },
    log: async (step, d) => { calls.logs.push(`${step}:${d}`); },
  };
  return { ports, calls };
}

function scriptedFetch(responses: unknown[]) {
  const bodies: any[] = [];
  const fn = (async (_url: string, init: any) => {
    bodies.push(JSON.parse(init.body));
    const next = responses.shift();
    if (next instanceof Response) return next;
    return new Response(JSON.stringify({ choices: [{ message: next }] }), { status: 200 });
  }) as unknown as typeof fetch;
  return { fn, bodies };
}

const tc = (name: string, args: object, id = name) => ({ id, type: 'function', function: { name, arguments: JSON.stringify(args) } });

test('multi-step tool run: reads context, then proposes 4 replies', async () => {
  const { ports, calls } = fakePorts();
  const { fn, bodies } = scriptedFetch([
    { content: null, tool_calls: [tc('get_conversation', {}), tc('get_patient_state', {})] },
    { content: null, tool_calls: [tc('propose_replies', { replies: ['Yes, I am awake.', 'Very tired today.', 'Can you visit?', 'Call me later.'] })] },
  ]);
  const replies = await runAgent({ kind: 'reply', message: { body: 'Are you awake?' } }, ports, fn);
  assert.deepEqual(replies, ['Yes, I am awake.', 'Very tired today.', 'Can you visit?', 'Call me later.']);
  assert.equal(calls.proposed[0][1], 'ai');
  assert.equal(bodies.length, 2);
  assert.equal(bodies[0].model, 'local-test-model');
  assert.equal(bodies[0].tools.length, 6, 'tools offered');
  assert.equal(bodies[1].messages.filter((m: any) => m.role === 'tool').length, 2, 'tool results fed back');
  assert.ok(calls.logs.some(l => l.startsWith('plan:')));
});

test('agent can alert caregivers once, then still proposes replies', async () => {
  const { ports, calls } = fakePorts();
  const { fn } = scriptedFetch([
    { content: null, tool_calls: [tc('alert_caregivers', { reason: 'Nurse says Neal fell' }), tc('alert_caregivers', { reason: 'again' }, 'b')] },
    { content: null, tool_calls: [tc('propose_replies', { replies: ['I am okay.', 'Please come now.', 'Call Mom.', 'Thank you.'] })] },
  ]);
  await runAgent({ kind: 'reply', message: { body: 'Neal fell out of bed, is someone coming?' } }, ports, fn);
  assert.deepEqual(calls.alerts, ['Nurse says Neal fell']);
  assert.equal(calls.proposed.length, 1);
});

test('model outage falls back to built-in replies', async () => {
  const { ports, calls } = fakePorts();
  const { fn } = scriptedFetch([new Response('upstream down', { status: 503 })]);
  const replies = await runAgent({ kind: 'reply', message: { body: 'Want me to bring dinner?' } }, ports, fn);
  assert.equal(calls.proposed[0][1], 'fallback');
  assert.equal(replies.length, 4);
  assert.ok(calls.logs.some(l => l.startsWith('fallback:')));
});

test('4xx on tools retries in JSON mode', async () => {
  const { ports } = fakePorts();
  const { fn, bodies } = scriptedFetch([
    new Response('tools unsupported', { status: 400 }),
    { content: '<think>hmm</think>{"replies":["Yes","No","Later","Why?"]}' },
  ]);
  const replies = await runAgent({ kind: 'reply', message: { body: 'Dinner at 6?' } }, ports, fn);
  assert.deepEqual(replies, ['Yes', 'No', 'Later', 'Why?']);
  assert.equal(bodies[1].tools, undefined);
});

test('intent expansion produces full sentences', async () => {
  const { ports, calls } = fakePorts();
  const { fn } = scriptedFetch([
    { content: null, tool_calls: [tc('propose_replies', { replies: ['I would love some water.', 'Water later, please.', 'Is there cold water?', 'Water and a straw.'] })] },
  ]);
  await runAgent({ kind: 'intent', keywords: 'water' }, ports, fn);
  assert.equal(calls.proposed[0][0].length, 4);
});

test('reply parser handles JSON, numbered lists and thinking tags', () => {
  assert.deepEqual(parseRepliesFromText('{"replies":["a","b"]}'), ['a', 'b']);
  assert.deepEqual(parseRepliesFromText('<think>x</think>1. Yes\n2. No\n3. Later'), ['Yes', 'No', 'Later']);
});

test('keywords naming a person: the agent routes each message to the right contact', async () => {
  const { ports, calls } = fakePorts();
  const { fn, bodies } = scriptedFetch([
    { content: null, tool_calls: [tc('list_contacts', {})] },
    { content: null, tool_calls: [tc('propose_messages', { messages: [
      { to: 'Alex', text: 'Can you bring pho tonight?' }, { to: 'my brother', text: 'Pho after your shift?' },
      { to: 'Mom', text: 'Alex is bringing pho.' }, { to: 'nobody', text: 'Pho please.' }] })] },
  ]);
  const out = await runAgent({ kind: 'intent', keywords: 'alex pho' }, ports, fn);
  assert.equal(out.length, 4);
  assert.deepEqual(calls.routes[0], [2n, 2n, 1n, 0n]);
  assert.ok(JSON.parse(bodies[1].messages.find((m: any) => m.role === 'tool').content).some((c: any) => c.name === 'Alex'));
});

test('contact matching handles names, relations and nicknames', () => {
  const people = [{ id: 1n, name: 'Mom', relation: 'mother' }, { id: 2n, name: 'Alex Kim', relation: 'brother' }];
  assert.equal(resolveContact('alex', people), 2n);
  assert.equal(resolveContact('mum', people), 1n);
  assert.equal(resolveContact('my brother', people), 2n);
  assert.equal(resolveContact('pizza guy', people), undefined);
});

test('photo: the vision model gets the image as a data URL and returns a short description', async () => {
  const { describeImage, photoTag } = await import('../src/agent.ts');
  const { fn, bodies } = scriptedFetch([{ content: '<think>x</think>A steaming bowl of pho with basil and lime.' }]);
  const d = await describeImage({ data: Buffer.from([1, 2, 3]), contentType: 'image/png' }, 'Alex', 'dinner!', fn);
  assert.equal(d, 'A steaming bowl of pho with basil and lime.');
  const parts = bodies[0].messages[0].content;
  assert.equal(parts[1].type, 'image_url');
  assert.match(parts[1].image_url.url, /^data:image\/png;base64,AQID$/);
  assert.match(parts[0].text, /Alex/);
  assert.equal(photoTag(d), '[Photo: A steaming bowl of pho with basil and lime.]');
  assert.equal(await describeImage({ data: Buffer.from([1]), contentType: 'application/pdf' }, 'Alex', '', fn), null, 'non-images skipped');
});
