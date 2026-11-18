/**
 * Board link: the SK-AM62B's serial console (the same micro-USB cable PuTTY uses) -> SpacetimeDB.
 *
 * The hub opens BOARD_PORT (e.g. COM12), logs in as root if the board has just booted, and starts
 * the board program itself, so there is nothing to click and the C eye-command model is always the
 * one that runs:
 *
 *     cd /root/blinktalk && python3 blinktalk_board.py --sensitivity 5 [--led 12] [--swap-lr]
 *
 * If the board is already printing BLINKTALK lines it is left running. Every line it prints is parsed:
 *     BLINKTALK CMD left|right|select|toggle|sos ...  -> eye_command reducer
 *     BLINKTALK HR 74 good q=0.86                      -> heartbeat reducer (SpacetimeDB raises heart alerts)
 *     BLINKTALK READY / INFO / ALERT / ERROR ...       -> hub log
 * If the cable is unplugged or the board reboots, the link reopens the port and starts the program again.
 */

export const BOARD_COMMANDS = ['left', 'right', 'select', 'toggle', 'sos'] as const;
export type BoardCommand = (typeof BOARD_COMMANDS)[number];

export type BoardLine =
  | { kind: 'command'; command: BoardCommand; delayMs?: number }
  | { kind: 'heart'; bpm: number | null; status: string; quality: number }
  | { kind: 'ready'; detail: string }
  | { kind: 'info' | 'alert' | 'error' | 'summary'; detail: string };

const ANSI = /\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\r/g;

/** Parse one console line. Returns null for anything that is not a BLINKTALK line (prompt, echo, boot log). */
export function parseBoardLine(raw: string): BoardLine | null {
  const text = raw.replace(ANSI, '');
  const at = text.indexOf('BLINKTALK ');
  if (at < 0) return null;
  const parts = text.slice(at).trim().split(/\s+/);
  const detail = parts.slice(2).join(' ');
  switch (parts[1]) {
    case 'CMD': {
      const command = parts[2] as BoardCommand;
      if (!BOARD_COMMANDS.includes(command)) return null;
      const delay = parts.find(p => p.startsWith('delay_ms='));
      return { kind: 'command', command, delayMs: delay ? Number(delay.slice(9)) : undefined };
    }
    case 'HR': {
      const q = parts.find(p => p.startsWith('q='));
      const bpm = /^\d+$/.test(parts[2] ?? '') ? Number(parts[2]) : null;
      return { kind: 'heart', bpm, status: parts[3] ?? '', quality: q ? Number(q.slice(2)) : 0 };
    }
    case 'READY': return { kind: 'ready', detail };
    case 'ALERT': return { kind: 'alert', detail };
    case 'ERROR': return { kind: 'error', detail };
    case 'SUMMARY': return { kind: 'summary', detail };
    case 'INFO': return { kind: 'info', detail };
    default: return null;
  }
}

export interface BoardOptions {
  dir: string;              // folder on the board that holds blinktalk_board.py
  sensitivity: number;      // 1 strict .. 10 sensitive
  led?: number;             // PPG LED current code
  swapLR: boolean;          // electrodes mirrored
}

/** The shell command the hub types on the board's console. */
export function startCommand(o: BoardOptions): string {
  const args = [`--sensitivity ${o.sensitivity}`];
  if (o.led !== undefined) args.push(`--led ${o.led}`);
  if (o.swapLR) args.push('--swap-lr');
  return `cd ${o.dir} && python3 blinktalk_board.py ${args.join(' ')}`;
}

/** Anything that can carry the console bytes: a serial port in real use, a fake in tests. */
export interface ConsolePort {
  write(text: string): void;
  onData(cb: (chunk: string) => void): void;
  onClose(cb: (why: string) => void): void;
  close(): void;
}

export interface BoardEvents {
  line(l: BoardLine): void;
  status(message: string): void;
}

/**
 * Talks to one board over one console. Pure logic (no serial library), so it can be tested with a fake port.
 * Timers are injectable for the same reason.
 */
export class BoardSession {
  private buf = '';
  private running = false;           // saw BLINKTALK output since the last (re)start
  private startedAt = 0;
  private timers: ReturnType<typeof setTimeout>[] = [];

  constructor(
    private port: ConsolePort,
    private opts: BoardOptions,
    private on: BoardEvents,
    private clock: { setTimeout: typeof setTimeout; now: () => number } = { setTimeout, now: Date.now },
    private probeMs = 2500,
    private readyTimeoutMs = 25000,
  ) {
    port.onData(chunk => this.feed(chunk));
    port.write('\n');                                        // wake the console / show the prompt
    this.later(this.probeMs, () => { if (!this.running && !this.startedAt) this.start('starting the eye-command model'); });
  }

  private later(ms: number, fn: () => void) { this.timers.push(this.clock.setTimeout(fn, ms)); }

  /** Stop whatever runs on the console, then start the board program. */
  start(why: string) {
    this.running = false;
    this.startedAt = this.clock.now();
    this.on.status(`${why}: ${startCommand(this.opts)}`);
    this.port.write('\x03');
    this.later(600, () => this.port.write('\n'));
    this.later(800, () => this.port.write(`${startCommand(this.opts)}\n`));
    const started = this.startedAt;
    this.later(this.readyTimeoutMs, () => {
      if (!this.running && this.startedAt === started) {
        this.on.status(`no answer from blinktalk_board.py after ${this.readyTimeoutMs / 1000}s. Is the board software in ${this.opts.dir}? (docs/BOARD_SETUP.md)`);
      }
    });
  }

  private feed(chunk: string) {
    this.buf += chunk;
    const tail = this.buf.replace(ANSI, '').trimEnd();
    if (tail.endsWith('login:')) {                          // the board just booted
      this.buf = '';
      this.on.status('board booted: logging in as root');
      this.port.write('root\n');
      this.later(1500, () => this.start('board booted'));
      return;
    }
    let nl: number;
    while ((nl = this.buf.indexOf('\n')) >= 0) {
      const raw = this.buf.slice(0, nl);
      this.buf = this.buf.slice(nl + 1);
      const line = parseBoardLine(raw);
      if (!line) continue;
      if (line.kind === 'error') this.running = false;
      else this.running = true;
      this.on.line(line);
    }
    if (this.buf.length > 4096) this.buf = this.buf.slice(-1024);   // a prompt with no newline, boot noise
  }

  /** Change the sensitivity of the running program (it reads "sens N" from the console). */
  setSensitivity(level: number) {
    this.opts.sensitivity = level;
    this.port.write(`sens ${level}\n`);
  }

  close() {
    for (const t of this.timers) clearTimeout(t);
    this.port.close();
  }
}

// ------------------------------------------------------------------ real serial port
/** Open a serial port as a ConsolePort (115200 8N1, the AM62B console settings). */
export async function openSerial(path: string): Promise<ConsolePort> {
  const { SerialPort } = await import('serialport');
  const sp = new SerialPort({ path, baudRate: 115200, autoOpen: false });
  await new Promise<void>((resolve, reject) => sp.open(err => (err ? reject(err) : resolve())));
  return {
    write: text => { sp.write(text); },
    onData: cb => { sp.on('data', (b: Buffer) => cb(b.toString('utf8'))); },
    onClose: cb => {
      sp.on('close', () => cb('port closed'));
      sp.on('error', (e: Error) => cb(e.message));
    },
    close: () => { if (sp.isOpen) sp.close(); },
  };
}

export async function listSerialPorts(): Promise<Array<{ path: string; description: string }>> {
  const { SerialPort } = await import('serialport');
  return (await SerialPort.list()).map(p => ({
    path: p.path,
    description: [p.manufacturer, (p as { friendlyName?: string }).friendlyName ?? p.pnpId].filter(Boolean).join(' '),
  }));
}

/**
 * Keep a board session alive on `path`: reopen the port every few seconds when it is missing or lost
 * (cable unplugged, PuTTY holding it), and start a fresh session each time.
 */
export function runBoardLink(path: string, opts: BoardOptions, on: BoardEvents, retryMs = 5000): { stop(): void } {
  let stopped = false;
  let session: BoardSession | null = null;
  let warned = '';
  const connect = async () => {
    if (stopped) return;
    try {
      const port = await openSerial(path);
      warned = '';
      on.status(`connected to the board on ${path}`);
      port.onClose(why => {
        session = null;
        on.status(`lost the board on ${path} (${why}); retrying`);
        if (!stopped) setTimeout(connect, retryMs);
      });
      session = new BoardSession(port, opts, on);
    } catch (err) {
      const why = String((err as Error).message ?? err);
      if (why !== warned) {
        on.status(`cannot open ${path} (${why}). Close PuTTY if it is open, and check the COM number (npm run ports).`);
        warned = why;
      }
      if (!stopped) setTimeout(connect, retryMs);
    }
  };
  void connect();
  return { stop() { stopped = true; session?.close(); } };
}
