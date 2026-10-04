from pathlib import Path
import unittest


class Dev015Rev6RecoveryTests(unittest.TestCase):
    def test_rev5_implementation_survived_late_rev4_run(self):
        root = Path(__file__).resolve().parents[1]
        router = (
            root / "src/training_core/flight_command_router.py"
        ).read_text()
        trial = (
            root / "src/training_core/dev015_live_trial.py"
        ).read_text()

        self.assertIn(
            "endpoint.send_arm_disarm(wanted, force=True)",
            router,
        )
        self.assertIn(
            'action = "arm" if wanted else "disarm"',
            router,
        )
        self.assertIn(
            "settle_request_event.set()",
            trial,
        )
        self.assertIn(
            "settle_samples.append(",
            trial,
        )
        self.assertIn(
            "DISARM DIAGNOSTIC:",
            trial,
        )

    def test_rev5_shutdown_order_is_preserved(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / "src/training_core/dev015_live_trial.py"
        ).read_text()

        settle = text.index("settle_request_event.set()")
        release = text.index(
            'command_id="dev015-release"'
        )
        disarm = text.index(
            'command_id="dev015-disarm"'
        )
        self.assertLess(settle, release)
        self.assertLess(release, disarm)

    def test_manual_server_remains_normal_arm_disarm_only(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / "src/training_core/lan_control_server.py"
        ).read_text()
        self.assertNotIn(
            "allow_test_force_arm=True",
            text,
        )


if __name__ == "__main__":
    unittest.main()
