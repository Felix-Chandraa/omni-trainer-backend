from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from src.training_core.evidence_recorder import (
    AttemptEvidenceRecorder,
    AttemptIdentity,
)


class Dev014Rev2Tests(unittest.TestCase):
    def test_received_state_is_real_recorder_channel(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            recorder = AttemptEvidenceRecorder(
                root,
                AttemptIdentity(
                    session_id="S",
                    exercise_id="E",
                    attempt_id="A",
                    aircraft_id="omni-1",
                    generation=1,
                ),
            )
            ok = recorder.record_client_received_state(
                station_id="student-station-1",
                actor_id="student-1",
                state={
                    "lat": -7.347,
                    "lon": 108.2464,
                    "alt_msl": 349.4,
                    "roll": 1.2,
                    "pitch": -0.5,
                    "yaw": 87.0,
                },
                source_sequence=123,
                client_mono_ns=None,
                client_utc_ns=None,
            )
            self.assertTrue(ok)
            index = json.loads(recorder.finalize().read_text())
            channel = "clients/student-station-1/received_state"
            self.assertIn(channel, index["channels"])
            self.assertEqual(
                index["channels"][channel]["records_written"], 1
            )

            segment = (
                root
                / "A"
                / index["channels"][channel]["segments"][0]
            )
            row = json.loads(segment.read_text().splitlines()[0])
            self.assertEqual(
                row["payload"]["actor_id"], "student-1"
            )
            self.assertEqual(
                row["payload"]["source_sequence"], 123
            )
            self.assertEqual(
                row["payload"]["state"]["yaw"], 87.0
            )

    def test_gateway_command_path_is_semantically_fail_closed(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root
            / "src/training_core/client_evidence_gateway.py"
        ).read_text()
        compact = "".join(text.split())
        self.assertIn('ifkind=="cmd":', compact)
        self.assertTrue(
            "commands_disabled_dev013" in text
            or "commands_disabled_dev014" in text
        )
        self.assertNotIn("command_long_send", text)
        self.assertNotIn("rc_channels_override_send", text)
        self.assertNotIn("set_mode_send", text)

    def test_clock_sync_uses_pending_server_timestamps_semantically(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root
            / "src/training_core/client_evidence_gateway.py"
        ).read_text()
        compact = "".join(text.split())
        self.assertIn(
            "pending[ping_id]=ClockPing(",
            compact,
        )
        self.assertIn(
            "sample.server_recv_mono_ns",
            text,
        )
        self.assertIn(
            "sample.server_send_mono_ns",
            text,
        )
        self.assertIn(
            "sample.server_recv_utc_ns",
            text,
        )

    def test_replay_core_never_imports_worker_launcher(self):
        root = Path(__file__).resolve().parents[1]
        for rel in (
            "src/training_core/replay_reader.py",
            "src/training_core/replay_gateway.py",
        ):
            text = (root / rel).read_text()
            self.assertNotIn("worker_argv", text)
            self.assertNotIn("AttemptWorkerCoordinator", text)
            self.assertNotIn("subprocess", text)


if __name__ == "__main__":
    unittest.main()
