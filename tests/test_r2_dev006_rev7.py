from __future__ import annotations

from pathlib import Path
import unittest


class Dev006Rev7Tests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.mav = (root / "src/training_core/raw_mav_probe.py").read_text()
        self.live = (root / "src/training_core/omni_live_trial.py").read_text()

    def test_raw_probe_uses_declared_second_udp_output(self):
        self.assertIn('default="udpin:127.0.0.1:14555"', self.mav)
        self.assertIn('a.conn.startswith("udpin:127.0.0.1:")', self.mav)
        self.assertNotIn('default="tcp:127.0.0.1:5762"', self.mav)

    def test_live_trial_uses_udp14555_for_raw_mav_evidence(self):
        self.assertIn("MAV_RX_PORT = 14555", self.live)
        self.assertIn('"--conn", f"udpin:127.0.0.1:{MAV_RX_PORT}"', self.live)
        fg_tail = self.live[self.live.index('print("RAW FG OK:"'):]
        self.assertNotIn('tcp_port_free("127.0.0.1", 5762)', fg_tail)

    def test_receive_port_guard_runs_before_worker_start(self):
        token = 'udp_port_free("127.0.0.1", MAV_RX_PORT)'
        self.assertIn(token, self.live)
        self.assertLess(self.live.index(token), self.live.index("coord.start("))

    def test_probe_has_no_mavlink_write_calls(self):
        forbidden = (
            "command_long_send",
            "param_set_send",
            "mission_item",
            "rc_channels_override_send",
            "set_mode_send",
            "arducopter_arm",
            "arducopter_disarm",
        )
        for token in forbidden:
            self.assertNotIn(token, self.mav)


if __name__ == "__main__":
    unittest.main()
