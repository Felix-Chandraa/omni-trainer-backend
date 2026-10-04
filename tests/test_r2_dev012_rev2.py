import unittest

from src.training_core.actor_identity import actor_identifier
from src.training_core.models import Actor, Role


class Dev012Rev2Tests(unittest.TestCase):
    def test_actor_identifier_uses_actual_core_actor_contract(self):
        actor = Actor("dev012-identity-probe", Role.INSTRUCTOR)
        self.assertFalse(hasattr(actor, "actor_id"))
        self.assertEqual(
            actor_identifier(actor),
            "dev012-identity-probe",
        )

    def test_live_trial_does_not_reference_missing_actor_id_attribute(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        text = (root/"src/training_core/recording_live_trial.py").read_text()
        self.assertNotIn("instructor.actor_id", text)
        self.assertIn("actor_identifier(instructor)", text)


if __name__ == "__main__":
    unittest.main()
