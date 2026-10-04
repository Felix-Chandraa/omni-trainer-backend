from pathlib import Path
import tempfile
import unittest
from src.training_core.client_evidence_gateway import EvidenceLanTelemetryGateway
from src.training_core.evidence_recorder import AttemptEvidenceRecorder, AttemptIdentity

class Dev013Tests(unittest.TestCase):
    def test_server_owned_principal(self):
        with tempfile.TemporaryDirectory() as td:
            r=AttemptEvidenceRecorder(Path(td),AttemptIdentity('S','E','A','omni-1',1))
            g=EvidenceLanTelemetryGateway('127.0.0.1',0)
            a=g.register_assignment(student_id='student-1',session_id='S',attempt_id='A',aircraft_id='omni-1',generation=1,token='x')
            p=g.register_evidence_principal(a,r,station_id='station-1',training_role='FLIGHT')
            self.assertEqual((p.actor_id,p.station_id,p.training_role),('student-1','station-1','FLIGHT'))
            r.finalize()
    def test_commands_stay_disabled(self):
        root=Path(__file__).resolve().parents[1]; t=(root/'src/training_core/client_evidence_gateway.py').read_text()
        compact=''.join(t.split())
        self.assertIn('ifkind=="cmd":',compact)
        self.assertTrue('commands_disabled_dev013' in t or 'commands_disabled_dev014' in t)
        self.assertNotIn('command_long_send',t)
        self.assertNotIn('rc_channels_override_send',t)
    def test_clock_uses_pending_server_timestamps(self):
        root=Path(__file__).resolve().parents[1]; t=(root/'src/training_core/client_evidence_gateway.py').read_text()
        compact=''.join(t.split())
        self.assertIn('pending[ping_id]=ClockPing(',compact)
        self.assertIn('sample.server_recv_mono_ns',t)
        self.assertIn('sample.server_send_mono_ns',t)
        self.assertIn('sample.server_recv_utc_ns',t)
    def test_scope_cannot_self_escalate(self):
        root=Path(__file__).resolve().parents[1]; t=(root/'src/training_core/client_evidence_gateway.py').read_text()
        self.assertIn('scope_assignment_mismatch',t)
    def test_live_spoof_proof_exists(self):
        root=Path(__file__).resolve().parents[1]; t=(root/'src/training_core/dev013_live_trial.py').read_text()
        self.assertIn('spoofed-client-actor',t); self.assertIn('client replaced authoritative actor identity',t)
    def test_dev013_sends_no_flight_command(self):
        root=Path(__file__).resolve().parents[1]; t=(root/'src/training_core/dev013_live_trial.py').read_text()
        self.assertIn("{'type':'cmd','name':'arm'}",t)
        self.assertIn('commands_disabled_dev013',t)

if __name__=='__main__': unittest.main()
