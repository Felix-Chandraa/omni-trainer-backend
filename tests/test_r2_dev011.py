from __future__ import annotations

import json
from pathlib import Path
import unittest

from src.training_core.lan_protocol import (
    Assignment,
    LanTelemetryGateway,
    PROTOCOL,
    ProtocolError,
    parse_hello,
)


class Dev011ProtocolTests(unittest.TestCase):
    def test_valid_student_hello(self):
        msg = parse_hello(json.dumps({
            "type": "hello",
            "protocol": PROTOCOL,
            "role": "student",
            "student_id": "student-1",
            "token": "secret",
        }))
        self.assertEqual(msg["student_id"], "student-1")

    def test_protocol_and_role_fail_closed(self):
        with self.assertRaises(ProtocolError):
            parse_hello(json.dumps({
                "type": "hello",
                "protocol": "wrong",
                "role": "student",
                "student_id": "student-1",
                "token": "x",
            }))
        with self.assertRaises(ProtocolError):
            parse_hello(json.dumps({
                "type": "hello",
                "protocol": PROTOCOL,
                "role": "instructor",
                "student_id": "student-1",
                "token": "x",
            }))

    def test_token_resolves_server_owned_assignment(self):
        g = LanTelemetryGateway("127.0.0.1", 0)
        a = g.register_assignment(
            student_id="student-2",
            session_id="S2",
            attempt_id="A2",
            aircraft_id="omni-2",
            generation=1,
            token="known-token",
        )
        self.assertEqual(a.aircraft_id, "omni-2")
        self.assertNotIn("token", a.public())
        self.assertEqual(g._authenticate("student-2", "known-token"), a)
        with self.assertRaises(ProtocolError):
            g._authenticate("student-2", "wrong")

    def test_remote_commands_are_disabled(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "src/training_core/lan_protocol.py").read_text()
        self.assertIn("commands_disabled_dev011", text)
        self.assertNotIn("mavutil", text)
        self.assertNotIn("command_long_send", text)

    def test_server_reuses_direct_worker_path(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "src/training_core/lan_session_server.py").read_text()
        self.assertIn("worker_argv(", text)
        self.assertIn("RuntimeSlotAllocator", text)
        self.assertNotIn("run_demo.sh", text)
        self.assertNotIn("sim_vehicle.py", text)
        self.assertNotIn("pkill", text)
        self.assertNotIn("killall", text)

    def test_routing_contains_attempt_aircraft_generation(self):
        a = Assignment(
            student_id="student-3",
            session_id="S3",
            attempt_id="A3",
            aircraft_id="omni-3",
            generation=7,
            token="hidden",
        )
        public = a.public()
        self.assertEqual(public["attempt_id"], "A3")
        self.assertEqual(public["aircraft_id"], "omni-3")
        self.assertEqual(public["generation"], 7)
        self.assertNotIn("token", public)


if __name__ == "__main__":
    unittest.main()
