from __future__ import annotations

import json
from pathlib import Path
import struct
import tempfile
import time
import unittest

from src.training_core.evidence_recorder import (
    AttemptEvidenceRecorder,
    AttemptIdentity,
    ClockMapping,
    RecorderError,
    _split_mavlink_frames,
)


def identity():
    return AttemptIdentity(
        session_id="S1",
        exercise_id="E1",
        attempt_id="A1",
        aircraft_id="omni-1",
        generation=3,
    )


class Dev012RecorderTests(unittest.TestCase):
    def test_v1_v2_mavlink_frame_split(self):
        v1 = bytes([0xFE, 0, 1, 1, 1, 0, 0xAA, 0xBB])
        v2 = bytes([0xFD, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0xAA, 0xBB])
        frames, skipped = _split_mavlink_frames(v1 + v2)
        self.assertEqual(skipped, 0)
        self.assertEqual(frames, [v1, v2])

    def test_signed_v2_length(self):
        v2 = bytes([0xFD, 0, 1, 0, 1, 1, 1, 0, 0, 0, 0xAA, 0xBB]) + bytes(13)
        frames, skipped = _split_mavlink_frames(v2)
        self.assertEqual(skipped, 0)
        self.assertEqual(frames, [v2])

    def test_identity_is_stamped_on_state(self):
        with tempfile.TemporaryDirectory() as td:
            r = AttemptEvidenceRecorder(Path(td), identity())
            self.assertTrue(r.record_state({"lat": 1.0}, source="fixture"))
            idx = json.loads(r.finalize().read_text())
            seg = Path(td) / "A1" / idx["channels"]["server/state"]["segments"][0]
            rec = json.loads(seg.read_text().splitlines()[0])
            self.assertEqual(rec["identity"]["attempt_id"], "A1")
            self.assertEqual(rec["identity"]["generation"], 3)

    def test_server_truth_and_client_experience_are_separate_channels(self):
        with tempfile.TemporaryDirectory() as td:
            r = AttemptEvidenceRecorder(Path(td), identity())
            r.record_state({"lat": 1}, source="FGNetFDM")
            r.record_client_event(
                station_id="station-1",
                actor_id="student-A",
                role="FLIGHT",
                scope="flight",
                action="input_sample",
                data={"roll": 0.2},
                client_mono_ns=None,
            )
            idx = json.loads(r.finalize().read_text())
            self.assertIn("server/state", idx["channels"])
            self.assertIn("clients/station-1/events", idx["channels"])

    def test_client_received_state_is_separate_from_actions(self):
        with tempfile.TemporaryDirectory() as td:
            r = AttemptEvidenceRecorder(Path(td), identity())
            r.record_client_received_state(
                station_id="station-1",
                actor_id="student-A",
                state={"lat": 1.0},
                source_sequence=10,
                client_mono_ns=None,
            )
            r.record_client_event(
                station_id="station-1",
                actor_id="student-A",
                role="FLIGHT",
                scope="ui",
                action="view_changed",
                data={"view": "FOLLOW"},
                client_mono_ns=None,
            )
            idx = json.loads(r.finalize().read_text())
            self.assertIn("clients/station-1/received_state", idx["channels"])
            self.assertIn("clients/station-1/events", idx["channels"])

    def test_two_students_same_aircraft_remain_attributable(self):
        with tempfile.TemporaryDirectory() as td:
            r = AttemptEvidenceRecorder(Path(td), identity())
            r.record_client_event(
                station_id="station-flight",
                actor_id="student-A",
                role="FLIGHT",
                scope="flight",
                action="input_sample",
                data={"roll": 0.3},
                client_mono_ns=None,
            )
            r.record_client_event(
                station_id="station-payload",
                actor_id="student-B",
                role="PAYLOAD",
                scope="payload",
                action="gimbal_pan",
                data={"deg": 15},
                client_mono_ns=None,
            )
            idx = json.loads(r.finalize().read_text())
            self.assertIn("clients/station-flight/events", idx["channels"])
            self.assertIn("clients/station-payload/events", idx["channels"])

    def test_two_flight_students_actions_remain_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            r = AttemptEvidenceRecorder(Path(td), identity())
            r.record_client_event(
                station_id="left-seat",
                actor_id="student-A",
                role="FLIGHT",
                scope="flight",
                action="input_sample",
                data={"roll": 0.3},
                client_mono_ns=None,
            )
            r.record_client_event(
                station_id="right-seat",
                actor_id="student-B",
                role="FLIGHT",
                scope="flight",
                action="input_sample",
                data={"roll": -0.2},
                client_mono_ns=None,
            )
            idx = json.loads(r.finalize().read_text())
            self.assertEqual(
                idx["channels"]["clients/left-seat/events"]["records_written"], 1
            )
            self.assertEqual(
                idx["channels"]["clients/right-seat/events"]["records_written"], 1
            )

    def test_command_records_requested_vs_applied_times(self):
        with tempfile.TemporaryDirectory() as td:
            r = AttemptEvidenceRecorder(Path(td), identity())
            r.record_command(
                command_id="C1",
                actor_id="student-A",
                station_id="station-A",
                scope="flight",
                name="set_mode",
                payload={"mode": "AUTO"},
                disposition="rejected",
                request_utc_ns=100,
                applied_utc_ns=None,
                reason="no_flight_authority",
            )
            idx = json.loads(r.finalize().read_text())
            seg = Path(td) / "A1" / idx["channels"]["server/commands"]["segments"][0]
            rec = json.loads(seg.read_text().splitlines()[0])
            self.assertEqual(rec["payload"]["disposition"], "rejected")
            self.assertIsNone(rec["payload"]["applied_utc_ns"])

    def test_clock_mapping_stores_uncertainty(self):
        m = ClockMapping("station")
        m.update_ntp_sample(
            client_send_mono_ns=1_000,
            server_recv_mono_ns=2_000,
            server_send_mono_ns=2_100,
            client_recv_mono_ns=1_300,
            server_recv_utc_ns=5_000,
        )
        mapped, uncertainty = m.map_client_monotonic(2_000)
        self.assertIsNotNone(mapped)
        self.assertIsNotNone(uncertainty)
        self.assertGreaterEqual(uncertainty, 1)

    def test_queue_overflow_marks_channel_incomplete_without_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            r = AttemptEvidenceRecorder(Path(td), identity(), queue_size=32)
            # Force a deterministic drop without relying on thread scheduling.
            original = r._writer.q.put_nowait
            def full(_item):
                import queue
                raise queue.Full
            r._writer.q.put_nowait = full
            try:
                ok = r.record_event("overflow-fixture")
                self.assertFalse(ok)
            finally:
                r._writer.q.put_nowait = original
            idx = json.loads(r.finalize().read_text())
            self.assertFalse(idx["overall_complete"])
            self.assertEqual(idx["channels"]["server/events"]["dropped_records"], 1)
            gaps = idx["channels"]["server/events"]["gap_intervals"]
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["reason"], "writer_queue_overflow")

    def test_finalize_hashes_all_evidence_files(self):
        with tempfile.TemporaryDirectory() as td:
            r = AttemptEvidenceRecorder(Path(td), identity())
            r.record_event("x")
            idx = json.loads(r.finalize().read_text())
            files = {x["path"]: x for x in idx["files"]}
            self.assertIn("manifest.json", files)
            self.assertTrue(any(p.endswith(".jsonl") for p in files))
            for meta in files.values():
                self.assertEqual(len(meta["sha256"]), 64)

    def test_finalize_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            r = AttemptEvidenceRecorder(Path(td), identity())
            p1 = r.finalize()
            p2 = r.finalize()
            self.assertEqual(p1, p2)

    def test_no_write_after_finalize(self):
        with tempfile.TemporaryDirectory() as td:
            r = AttemptEvidenceRecorder(Path(td), identity())
            r.finalize()
            with self.assertRaises(RecorderError):
                r.record_event("late")


if __name__ == "__main__":
    unittest.main()
