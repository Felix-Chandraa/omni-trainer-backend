from pathlib import Path
import unittest


class Dev015Rev4Tests(unittest.TestCase):
    def test_statustext_capture_is_present(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / "src/training_core/mavlink_control.py"
        ).read_text()
        self.assertIn("msgid == 253", text)
        self.assertIn("status_texts_since", text)
        self.assertIn("StatusText", text)

    def test_force_arm_value_is_test_only_and_explicit(self):
        root = Path(__file__).resolve().parents[1]
        mav = (
            root / "src/training_core/mavlink_control.py"
        ).read_text()
        router = (
            root / "src/training_core/flight_command_router.py"
        ).read_text()
        live = (
            root / "src/training_core/dev015_live_trial.py"
        ).read_text()
        manual = (
            root / "src/training_core/lan_control_server.py"
        ).read_text()

        self.assertIn(
            "21196.0 if force else 0.0",
            mav,
        )
        self.assertIn(
            "allow_test_force_arm: bool = False",
            router,
        )
        self.assertIn(
            "allow_test_force_arm=True",
            live,
        )
        self.assertNotIn(
            "allow_test_force_arm=True",
            manual,
        )

    def test_normal_arm_disarm_is_attempted_before_force_fallback(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / "src/training_core/flight_command_router.py"
        ).read_text()

        normal = text.index(
            "endpoint.send_arm_disarm(wanted, force=False)"
        )
        fallback = text.index(
            "endpoint.send_arm_disarm(wanted, force=True)"
        )
        self.assertLess(normal, fallback)

    def test_force_fallback_is_evidence_visible_for_arm_or_disarm(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / "src/training_core/flight_command_router.py"
        ).read_text()

        # REV5 generalized the REV4 arm-only diagnostic to both actions.
        self.assertIn(
            'action = "arm" if wanted else "disarm"',
            text,
        )
        self.assertIn(
            'f"test_force_{action}_after_regular_failure: "',
            text,
        )
        self.assertIn(
            'f"test_force_{action}_after_confirmation_timeout: "',
            text,
        )

    def test_manual_product_path_cannot_enable_test_fallback(self):
        root = Path(__file__).resolve().parents[1]
        manual = (
            root / "src/training_core/lan_control_server.py"
        ).read_text()
        self.assertNotIn(
            "allow_test_force_arm=True",
            manual,
        )


if __name__ == "__main__":
    unittest.main()
