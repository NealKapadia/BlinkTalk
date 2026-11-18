import { test } from 'node:test';
import assert from 'node:assert/strict';
import { BoardSession, parseBoardLine, startCommand, type BoardLine, type ConsolePort } from '../src/board.ts';

test('parses the board program output', () => {
  assert.deepEqual(parseBoardLine('BLINKTALK CMD left t=12.40 delay_ms=900'), { kind: 'command', command: 'left', delayMs: 900 });
  assert.deepEqual(parseBoardLine('\x1b[0mroot@am62xx:~# BLINKTALK CMD sos t=1.00 delay_ms=2010\r'), { kind: 'command', command: 'sos', delayMs: 2010 });
  assert.deepEqual(parseBoardLine('BLINKTALK HR 74 good q=0.86'), { kind: 'heart', bpm: 74, status: 'good', quality: 0.86 });
  assert.deepEqual(parseBoardLine('BLINKTALK HR -- no_pulse'), { kind: 'heart', bpm: null, status: 'no_pulse', quality: 0 });
  assert.equal(parseBoardLine('BLINKTALK READY model=eog_c features=72 sensitivity=5')?.kind, 'ready');
  assert.equal(parseBoardLine('BLINKTALK CMD dance'), null, 'unknown commands are dropped');
  assert.equal(parseBoardLine('root@am62xx:~# cd /root/blinktalk && python3 blinktalk_board.py'), null, 'shell echo is not a board line');
});

test('start command carries the settings', () => {
  assert.equal(startCommand({ dir: '/root/blinktalk', sensitivity: 5, swapLR: false }),
    'cd /root/blinktalk && python3 blinktalk_board.py --sensitivity 5');
  assert.equal(startCommand({ dir: '/root/blinktalk', sensitivity: 7, led: 12, swapLR: true }),
    'cd /root/blinktalk && python3 blinktalk_board.py --sensitivity 7 --led 12 --swap-lr');
});

function fakeBoard() {
  const written: string[] = [];
  let feed: (s: string) => void = () => {};
  const port: ConsolePort = {
    write: t => { written.push(t); },
    onData: cb => { feed = cb; },
    onClose: () => {},
    close: () => {},
  };
  const timers: Array<{ at: number; fn: () => void }> = [];
  let now = 0;
  const clock = {
    now: () => now,
    setTimeout: ((fn: () => void, ms: number) => { timers.push({ at: now + ms, fn }); return 0; }) as unknown as typeof setTimeout,
  };
  const advance = (ms: number) => {               // fire due timers in time order, including ones they set
    const end = now + ms;
    for (;;) {
      const due = timers.filter(x => x.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      timers.splice(timers.indexOf(due), 1);
      now = due.at;
      due.fn();
    }
    now = end;
  };
  const lines: BoardLine[] = [];
  const status: string[] = [];
  const session = new BoardSession(port, { dir: '/root/blinktalk', sensitivity: 5, swapLR: false },
    { line: l => lines.push(l), status: s => status.push(s) }, clock);
  return { session, written, feed: (s: string) => feed(s), advance, lines, status };
}

test('a quiet console: the hub starts the eye-command model itself', () => {
  const b = fakeBoard();
  assert.deepEqual(b.written, ['\n']);
  b.feed('root@am62xx-evm:~# ');
  b.advance(2500);
  b.advance(1000);
  assert.ok(b.written.includes('\x03'), 'stops whatever was running');
  assert.ok(b.written.includes('cd /root/blinktalk && python3 blinktalk_board.py --sensitivity 5\n'));
  b.feed('BLINKTALK READY model=eog_c features=72 sensitivity=5\nBLINKTALK CMD right t=3.1 delay_ms=850\n');
  assert.deepEqual(b.lines.map(l => l.kind), ['ready', 'command']);
  b.advance(30000);
  assert.ok(!b.status.some(s => s.startsWith('no answer')), 'no timeout warning once it runs');
});

test('already running: left alone, lines forwarded', () => {
  const b = fakeBoard();
  b.feed('BLINKTALK HR 71 good q=0.90\nBLINKTALK CMD sel');
  b.feed('ect t=9.0 delay_ms=2000\n');
  b.advance(5000);
  assert.ok(!b.written.includes('\x03'), 'not restarted');
  assert.deepEqual(b.lines, [
    { kind: 'heart', bpm: 71, status: 'good', quality: 0.9 },
    { kind: 'command', command: 'select', delayMs: 2000 },
  ]);
});

test('board reboots: logs in as root and starts the model again', () => {
  const b = fakeBoard();
  b.feed('BLINKTALK HR 71 good q=0.90\n');
  b.feed('\r\nam62xx-evm login: ');
  assert.ok(b.written.includes('root\n'));
  b.advance(2500);
  assert.ok(b.written.includes('cd /root/blinktalk && python3 blinktalk_board.py --sensitivity 5\n'));
});

test('no answer from the board program: a clear hint, once', () => {
  const b = fakeBoard();
  b.advance(2500 + 25000 + 10);
  assert.equal(b.status.filter(s => s.startsWith('no answer')).length, 1);
  assert.match(b.status.at(-1)!, /\/root\/blinktalk/);
});
