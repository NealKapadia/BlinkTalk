/**
 * Lists the serial ports on this computer, to find the SK-AM62B console for BOARD_PORT in .env.
 *   npm run ports
 * The board's console is the micro-USB "UART" port; on Windows it is one of the COM ports that appear
 * when you plug it in (usually the lowest-numbered of the group, e.g. COM12).
 */
import { listSerialPorts } from '../hub/src/board.ts';

listSerialPorts().then(ports => {
  if (!ports.length) console.log("No serial ports found. Plug in the board's UART micro-USB cable and power the board.");
  for (const p of ports) console.log(`${p.path.padEnd(16)} ${p.description}`);
}).catch(err => {
  console.error(`Could not list serial ports: ${(err as Error).message}`);
  console.error('On Windows, Device Manager > Ports (COM & LPT) shows them too.');
  process.exit(1);
});
