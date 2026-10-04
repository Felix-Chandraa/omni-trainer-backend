from __future__ import annotations

from dataclasses import fields
from pathlib import Path
import unittest

from src.training_core.fg_adapter import FGPose


class Dev006Rev6Tests(unittest.TestCase):
    def test_fgpose_contract_uses_yaw_not_heading(self):
        names = {f.name for f in fields(FGPose)}
        self.assertIn("yaw_deg", names)
        self.assertNotIn("heading_deg", names)

    def test_live_trial_uses_existing_fgpose_field(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "src/training_core/omni_live_trial.py").read_text()
        self.assertNotIn("first_pose.heading_deg", text)
        self.assertIn('"yaw_deg": first_pose.yaw_deg', text)

    def test_fgpose_payload_contains_attitude_contract(self):
        pose = FGPose(
            lat_deg=-7.346914,
            lon_deg=108.2463725,
            alt_msl_m=349.0,
            agl_raw_m=0.0,
            roll_deg=1.0,
            pitch_deg=2.0,
            yaw_deg=327.66,
            vcas_kt=0.0,
            climb_mps=0.0,
            packet_len=408,
        )
        payload = pose.as_payload()
        self.assertEqual(payload["yaw_deg"], 327.66)
        self.assertEqual(payload["roll_deg"], 1.0)
        self.assertEqual(payload["pitch_deg"], 2.0)
        self.assertNotIn("heading_deg", payload)


if __name__ == "__main__":
    unittest.main()
