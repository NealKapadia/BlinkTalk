import { test } from 'vitest';
import assert from 'node:assert/strict';
import { createModuleTestHarness, TestAuth, TestClock } from 'spacetimedb/server/test-utils';
import { ScheduleAt, Timestamp } from 'spacetimedb';
import spacetimedb, * as mod from '../src/index';

function harness() {
  const clock = new TestClock(Timestamp.fromDate(new Date('2026-11-08T03:00:00Z')));
  const h = createModuleTestHarness(spacetimedb, mod as Record<string, unknown>, { clock });
  const auth = TestAuth.internal();
  const run = <T>(fn: (ctx: any) => T) => h.withReducerTx(auth, fn);
  run(ctx => mod.init(ctx, {}));
  return { h, run };
}

test('eye commands are logged and SOS raises one alert with an escalation timer', () => {
  const { h, run } = harness();
  run(ctx => mod.eye_command(ctx, { patient: 'neal', command: 'left' }));
  run(ctx => mod.eye_command(ctx, { patient: 'neal', command: 'sos' }));
  run(ctx => mod.eye_command(ctx, { patient: 'neal', command: 'sos' }));
  assert.equal([...h.db.eye_event.iter()].length, 3);
  const alerts = [...h.db.alert.iter()];
  assert.equal(alerts.length, 1, 'repeat SOS reuses the open alert');
  assert.equal(alerts[0].kind, 'sos');
  assert.equal(h.db.patient.handle.find('neal')!.status, 'sos');
  assert.equal([...h.db.sos_timer.iter()].length, 1);
});

test('three blinks pause and resume; SOS still works while paused', () => {
  const { h, run } = harness();
  run(ctx => mod.eye_command(ctx, { patient: 'neal', command: 'toggle' }));
  assert.equal(h.db.patient.handle.find('neal')!.status, 'paused');
  run(ctx => mod.set_draft(ctx, { patient: 'neal', draft: 'hi' }));
  assert.equal(h.db.patient.handle.find('neal')!.status, 'paused', 'typing state does not unpause');
  run(ctx => mod.eye_command(ctx, { patient: 'neal', command: 'sos' }));
  assert.equal(h.db.patient.handle.find('neal')!.status, 'sos');
  run(ctx => mod.eye_command(ctx, { patient: 'neal', command: 'toggle' }));
  assert.equal(h.db.patient.handle.find('neal')!.status, 'sos', 'toggle never hides an SOS');
  run(ctx => mod.cancel_sos(ctx, { patient: 'neal' }));
  run(ctx => mod.eye_command(ctx, { patient: 'neal', command: 'toggle' }));
  assert.equal(h.db.patient.handle.find('neal')!.status, 'paused');
  run(ctx => mod.eye_command(ctx, { patient: 'neal', command: 'toggle' }));
  assert.equal(h.db.patient.handle.find('neal')!.status, 'idle');
});

test('unacknowledged SOS escalates tier by tier; ack stops it and clears status', () => {
  const { h, run } = harness();
  run(ctx => mod.raise_sos(ctx, { patient: 'neal', detail: 'test' }));
  const a = [...h.db.alert.iter()][0];
  const timer = { scheduled_id: 1n, scheduled_at: ScheduleAt.time(0n), alert_id: a.id };
  run(ctx => mod.escalate_sos(ctx, { timer }));
  assert.equal(h.db.alert.id.find(a.id)!.level, 2);
  run(ctx => mod.ack_alert(ctx, { id: a.id, by: 'Mom' }));
  run(ctx => mod.escalate_sos(ctx, { timer }));
  const after = h.db.alert.id.find(a.id)!;
  assert.equal(after.level, 2, 'no escalation after ack');
  assert.equal(after.acked_by, 'Mom');
  assert.equal(h.db.patient.handle.find('neal')!.status, 'idle');
});

test('caregiver texting back acknowledges alerts by address', () => {
  const { h, run } = harness();
  run(ctx => mod.add_contact(ctx, { patient: 'neal', name: 'Mom', address: '+1 (469) 555-0101', relation: 'mother', tier: 1 }));
  run(ctx => mod.raise_sos(ctx, { patient: 'neal', detail: '' }));
  run(ctx => mod.ack_alert_by_reply(ctx, { patient: 'neal', address: '+14695550101' }));
  assert.equal([...h.db.alert.iter()][0].acked_by, 'Mom');
});

test('inbound text creates a contact; reply_latest queues an iMessage and clears suggestions', () => {
  const { h, run } = harness();
  run(ctx => mod.receive_message(ctx, { patient: 'neal', address: '+14695550199', sender_name: 'Alex', channel: 'imessage', body: 'Want visitors tonight?' }));
  const inbound = [...h.db.message.iter()][0];
  run(ctx => mod.add_suggestions(ctx, { patient: 'neal', message_id: inbound.id, contact_id: inbound.contact_id, texts: ['Yes, come by at 7.', 'Not tonight.', '', 'Bring snacks!', 'extra'], source: 'ai' }));
  assert.equal([...h.db.suggestion.iter()].length, 3, 'blank dropped, max 4 considered');
  run(ctx => mod.reply_latest(ctx, { patient: 'neal', body: 'Yes, come by at 7.' }));
  const out = [...h.db.message.iter()].find(m => m.direction === 'out')!;
  assert.equal(out.status, 'queued');
  assert.equal(out.contact_id, inbound.contact_id);
  assert.equal([...h.db.suggestion.iter()].length, 0);
  assert.equal([...h.db.phrase.iter()][0].uses, 1);
});

test('routed suggestions go to the inferred person; sending one is marked AI-assisted', () => {
  const { h, run } = harness();
  run(ctx => mod.add_contact(ctx, { patient: 'neal', name: 'Alex', address: '+19725550142', relation: 'brother', tier: 2 }));
  const alex = [...h.db.contact.iter()][0];
  run(ctx => mod.add_routed_suggestions(ctx, { patient: 'neal', message_id: 0n, contact_ids: [alex.id, 999n], texts: ['Can you bring pho tonight?', 'Hi'], source: 'ai' }));
  const sugg = [...h.db.suggestion.iter()].sort((a, b) => a.rank - b.rank);
  assert.equal(sugg[0].contact_id, alex.id);
  assert.equal(sugg[1].contact_id, 0n, 'unknown contact falls back to 0');
  run(ctx => mod.send_message(ctx, { patient: 'neal', contact_id: alex.id, body: 'Can you bring pho tonight?' }));
  run(ctx => mod.send_message(ctx, { patient: 'neal', contact_id: alex.id, body: 'typed myself' }));
  const out = [...h.db.message.iter()].sort((a, b) => (a.id < b.id ? -1 : 1));
  assert.deepEqual(out.map(m => m.assisted), [true, false]);
});

test('briefings: request is pending, the hub fills it in, only 10 kept', () => {
  const { h, run } = harness();
  for (let i = 0; i < 12; i++) run(ctx => mod.request_briefing(ctx, { patient: 'neal', hours: 500, requested_by: 'Mom' }));
  const all = [...h.db.briefing.iter()];
  assert.equal(all.length, 10);
  assert.equal(all[0].hours, 72, 'clamped to 72 h');
  assert.equal(all[0].status, 'pending');
  run(ctx => mod.set_briefing(ctx, { id: all[0].id, text: 'Quiet night.', model: 'gpt-chat-latest-2' }));
  const b = h.db.briefing.id.find(all[0].id)!;
  assert.equal(b.status, 'done');
  assert.equal(b.text, 'Quiet night.');
});

test('sustained high heart rate raises one alert; noisy readings do not', () => {
  const { h, run } = harness();
  for (let i = 0; i < 4; i++) run(ctx => mod.heartbeat(ctx, { patient: 'neal', bpm: 140, quality: 0.2 }));
  assert.equal([...h.db.alert.iter()].length, 0);
  for (let i = 0; i < 7; i++) run(ctx => mod.heartbeat(ctx, { patient: 'neal', bpm: 131, quality: 0.8 }));
  const alerts = [...h.db.alert.iter()];
  assert.equal(alerts.length, 1);
  assert.equal(alerts[0].kind, 'heart_high');
});

test('bad input is rejected', () => {
  const { run } = harness();
  assert.throws(() => run(ctx => mod.eye_command(ctx, { patient: 'neal', command: 'jump' })));
  assert.throws(() => run(ctx => mod.add_contact(ctx, { patient: 'neal', name: 'x', address: 'not-a-number', relation: '', tier: 1 })));
  assert.throws(() => run(ctx => mod.eye_command(ctx, { patient: 'nobody', command: 'left' })));
});
