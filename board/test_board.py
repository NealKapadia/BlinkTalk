"""
Replays recordings through the board program and checks the commands it prints.
Runs on the SK-AM62B or any Linux machine (it loads the C library for this CPU):
    python3 board/test_board.py
"""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# in the repo: data/recordings; on the board: /root/blinktalk/recordings
REC = next((p for p in (os.path.join(os.path.dirname(HERE), "data", "recordings"), os.path.join(HERE, "recordings"))
            if os.path.isdir(p)), os.path.join(HERE, "recordings"))


def commands(recording):
    out = subprocess.run([sys.executable, "blinktalk_board.py", "--replay", os.path.join(REC, recording),
                          "--speed", "0", "--log", ""], cwd=HERE, capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, timeout=120, check=True).stdout
    assert "BLINKTALK READY model=eog_c" in out, out
    return [line.split()[2] for line in out.splitlines() if line.startswith("BLINKTALK CMD ")]


class BoardProgram(unittest.TestCase):
    def test_left_looks(self):
        cmds = commands("neal_left_every_3s.csv")
        self.assertGreaterEqual(cmds.count("left"), 18)
        self.assertEqual(cmds.count("right"), 0)

    def test_right_looks(self):
        cmds = commands("neal_right_every_3s.csv")
        self.assertGreaterEqual(cmds.count("right"), 18)
        self.assertEqual(cmds.count("left"), 0)

    def test_single_blinks_are_not_commands(self):
        self.assertEqual(commands("neal_blink_every_3s.csv"), [])

    def test_rest_gives_no_commands(self):
        self.assertEqual(commands("neal_rest_looking_up.csv"), [])

    def test_random_sequence_has_every_command(self):
        cmds = set(commands("neal_random_sequence_1.csv"))
        self.assertTrue({"left", "right", "select", "toggle"} <= cmds, cmds)


if __name__ == "__main__":
    unittest.main()
