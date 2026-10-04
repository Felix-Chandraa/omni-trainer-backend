"""DEV-002 offline tests; no external simulator, no dependency install."""
import json
import math
import socket
import struct
import tempfile
import unittest
from pathlib import Path

from src.training_core.fg_adapter import FGObserver, FGParseError, parse_fdm_v24
from src.training_core.mav_observer import decode_message
from src.training_core.observer_cli import make_synthetic_fg, offline_demo
from src.training_core.state_journal import StateJournal


class FGTests(unittest.TestCase):
    def test_valid_pose_and_units(self):
        pose = parse_fdm_v24(make_synthetic_fg())
        self.assertAlmostEqual(pose.lat_deg, -7.347)
        self.assertAlmostEqual(pose.lon_deg, 108.2464)
        self.assertAlmostEqual(pose.climb_mps, 0.6096)
        self.assertEqual(pose.packet_len, 408)

    def test_malformed_frames_rejected(self):
        raw = bytearray(make_synthetic_fg())
        with self.assertRaises(FGParseError):
            parse_fdm_v24(raw[:-1])
        struct.pack_into('>I', raw, 0, 23)
        with self.assertRaises(FGParseError):
            parse_fdm_v24(raw)
        raw = bytearray(make_synthetic_fg())
        struct.pack_into('>d', raw, 16, math.nan)
        with self.assertRaises(FGParseError):
            parse_fdm_v24(raw)

    def test_udp_drain_latest_and_invalid_count(self):
        with FGObserver(0) as receiver:  # bind auto-assigned ephemeral loopback port
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                dst = receiver.sock.getsockname()
                sender.sendto(b'invalid', dst)
                sender.sendto(make_synthetic_fg(), dst)
                # UDP arrival on loopback; packets are in socket queue.
                pose = receiver.drain()
                self.assertIsNotNone(pose)
                self.assertEqual((receiver.rx_total, receiver.bad_total), (2, 1))
            finally:
                sender.close()

    def test_exclusive_port_ownership(self):
        with FGObserver(0) as first:
            port = first.sock.getsockname()[1]
            with self.assertRaises(OSError):
                with FGObserver(port):
                    pass


class JournalTests(unittest.TestCase):
    def test_append_only_provenance_and_timing(self):
        with tempfile.TemporaryDirectory() as d:
            with StateJournal(Path(d), 'attempt-1', 'aircraft-1') as journal:
                path = journal.path
                a = journal.record('fg_pose', {'alt_msl_m': 350.0}, synthetic=True)
                b = journal.record('mavlink_state', {'armed': False}, synthetic=True)
            rows = [json.loads(l) for l in path.read_text().splitlines()]
            self.assertEqual([r['seq'] for r in rows], [1, 2])
            self.assertTrue(all(r['attempt_id'] == 'attempt-1' for r in rows))
            self.assertTrue(all(r['synthetic'] for r in rows))
            self.assertLessEqual(a['monotonic_ns'], b['monotonic_ns'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            with StateJournal(Path(d), 'a', 'b') as journal:
                with self.assertRaises(ValueError):
                    journal.record('send_command', {'arm': True})


class MAVTests(unittest.TestCase):
    def test_decoding_is_state_only(self):
        class Heartbeat:
            base_mode, custom_mode, type, system_status = 128, 4, 1, 3
            def get_type(self):
                return 'HEARTBEAT'
        class CommandAck:
            def get_type(self):
                return 'COMMAND_ACK'
        result = decode_message(Heartbeat())
        self.assertTrue(result.data['armed'])
        self.assertIsNone(decode_message(CommandAck()))

    def test_synthetic_attempt_persists_with_no_active_claim(self):
        with tempfile.TemporaryDirectory() as d:
            offline_demo(Path(d))
            records = list(Path(d).glob('diagnostic-*.jsonl'))
            self.assertEqual(len(records), 1)
            rows = [json.loads(x) for x in records[0].read_text().splitlines()]
            self.assertEqual([r['source'] for r in rows], ['fg_pose', 'mavlink_state'])
            self.assertTrue(all(r['synthetic'] for r in rows))


if __name__ == '__main__':
    unittest.main()
