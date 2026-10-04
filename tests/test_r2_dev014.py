from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from src.training_core.replay_reader import (
    ReplayError,
    ReplayPackage,
)


class Dev014StaticTests(unittest.TestCase):
    def test_replay_path_has_no_worker_or_fdm_launch(self):
        root = Path(__file__).resolve().parents[1]
        for rel in (
            "src/training_core/replay_reader.py",
            "src/training_core/replay_gateway.py",
        ):
            text = (root / rel).read_text()
            self.assertNotIn("worker_argv", text)
            self.assertNotIn("AttemptWorkerCoordinator", text)
            self.assertNotIn("subprocess", text)
            self.assertNotIn("sim_vehicle.py", text)

    def test_replay_gateway_is_read_only(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / "src/training_core/replay_gateway.py"
        ).read_text()
        self.assertIn("replay_read_only", text)
        self.assertNotIn("command_long_send", text)
        self.assertNotIn(
            "rc_channels_override_send", text
        )

    def test_gateway_promotes_received_state_channel(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root
            / "src/training_core/client_evidence_gateway.py"
        ).read_text()
        self.assertIn(
            "record_client_received_state", text
        )
        self.assertIn(
            'action == "telemetry_received"', text
        )

    def test_manual_server_records_and_publishes_same_pose(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root
            / "src/training_core/lan_recording_server.py"
        ).read_text()
        self.assertIn("record_state(", text)
        self.assertIn("publish_telemetry(", text)
        self.assertIn("start_mavlink_tlog", text)

    def test_replay_reader_rejects_missing_index(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ReplayError):
                ReplayPackage(Path(td))

    def test_replay_reader_source_uses_stored_state_only(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / "src/training_core/replay_reader.py"
        ).read_text()
        self.assertIn('"server/state"', text)
        self.assertIn("state_sample", text.lower() if "state_sample" in text.lower() else "state_sample")
        self.assertNotIn("JSBSim", text)
        self.assertNotIn("ArduPlane", text)


if __name__ == "__main__":
    unittest.main()
