from pathlib import Path
import unittest


class Dev015Rev5Tests(unittest.TestCase):
    def test_automated_fallback_supports_disarm_too(self):
        root=Path(__file__).resolve().parents[1]
        text=(root/"src/training_core/flight_command_router.py").read_text()
        self.assertIn("endpoint.send_arm_disarm(wanted, force=True)", text)
        self.assertIn('action = "arm" if wanted else "disarm"', text)
        self.assertIn("test_force_{action}_after_regular_failure", text)

    def test_control_client_requests_fg_settle_before_disarm(self):
        root=Path(__file__).resolve().parents[1]
        text=(root/"src/training_core/dev015_live_trial.py").read_text()
        settle=text.index("settle_request_event.set()")
        release=text.index('command_id="dev015-release"')
        disarm=text.index('command_id="dev015-disarm"')
        self.assertLess(settle, release)
        self.assertLess(release, disarm)
        self.assertIn('"throttle": 0.0', text)

    def test_fg_settle_detector_uses_position_not_client_claim(self):
        root=Path(__file__).resolve().parents[1]
        text=(root/"src/training_core/dev015_live_trial.py").read_text()
        self.assertIn("settle_samples.append(", text)
        self.assertIn("distance_m(", text)
        self.assertIn("settle_window_s = 1.5", text)
        self.assertIn("settle_distance_m = 0.20", text)
        self.assertIn("settled_event.set()", text)

    def test_manual_server_still_has_no_test_force_fallback(self):
        root=Path(__file__).resolve().parents[1]
        text=(root/"src/training_core/lan_control_server.py").read_text()
        self.assertNotIn("allow_test_force_arm=True", text)

    def test_disarm_fallback_is_visible_in_output(self):
        root=Path(__file__).resolve().parents[1]
        text=(root/"src/training_core/dev015_live_trial.py").read_text()
        self.assertIn("DISARM DIAGNOSTIC:", text)
        self.assertIn("SITL TEST-ONLY FORCE-DISARM FALLBACK:", text)


if __name__ == "__main__":
    unittest.main()
