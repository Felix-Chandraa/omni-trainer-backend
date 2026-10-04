from pathlib import Path
import ast
import hashlib
import json
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
CORE = BASE / "src" / "training_core"
SOURCE = Path.home() / "Downloads" / "omni_flight-main"
BUS = SOURCE / "Feather-Flight-main" / "src" / "js" / "bus.js"


class Dev016EvidenceTests(unittest.TestCase):
    def test_python_files_compile_structurally(self):
        for name in (
            "evidence_recorder.py",
            "client_evidence_gateway.py",
            "evidence_extensions.py",
            "dev015_live_trial.py",
            "lan_control_server.py",
        ):
            ast.parse((CORE/name).read_text())

    def test_recorder_has_dedicated_input_sent(self):
        t=(CORE/"evidence_recorder.py").read_text()
        self.assertIn("def record_client_input_sent(",t)
        self.assertIn("input_sent",t)

    def test_gateway_keeps_received_state_and_adds_intent(self):
        t=(CORE/"client_evidence_gateway.py").read_text()
        self.assertIn('action == "telemetry_received"',t)
        self.assertIn("record_client_received_state",t)
        self.assertIn('action == "command_intent"',t)
        self.assertIn("record_client_input_sent",t)
        self.assertIn("station_id=principal.station_id",t)
        self.assertIn("actor_id=principal.actor_id",t)

    def test_feather_sends_both_evidence_messages(self):
        t=BUS.read_text()
        self.assertIn("DEV016_RECEIVED_STATE_ACK",t)
        self.assertIn('action: "telemetry_received"',t)
        self.assertIn("DEV016_COMMAND_INTENT_SEND",t)
        self.assertIn('action: "command_intent"',t)

    def test_bin_attachment_before_finalize_live_and_manual(self):
        for name in ("dev015_live_trial.py","lan_control_server.py"):
            t=(CORE/name).read_text()
            a=t.index("attach_ardupilot_bins(recorder, runtime, attempt.id)")
            b=t.index("recorder.finalize(",a)
            self.assertLess(a,b,name)

    def test_bin_helper_copy_and_hash_metadata(self):
        from src.training_core.evidence_extensions import attach_ardupilot_bins
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            pkg=root/"evidence"/"attempt-1"
            (pkg/"server").mkdir(parents=True)
            (pkg/"manifest.json").write_text("{}")
            rawdir=root/"run"/"direct"/"attempt-1"/"plane"/"logs"
            rawdir.mkdir(parents=True)
            raw=rawdir/"00000001.BIN"
            raw.write_bytes(b"OMNI-DATAFLASH-TEST"*100)

            class Fake:
                attempt_id="attempt-1"
                package_dir=pkg

            out=attach_ardupilot_bins(Fake(),root/"run","attempt-1")
            self.assertEqual(len(out),1)
            dst=pkg/"server"/"aircraft.BIN"
            meta=pkg/"server"/"ardupilot-bin.json"
            self.assertTrue(dst.is_file())
            self.assertTrue(meta.is_file())
            doc=json.loads(meta.read_text())
            self.assertEqual(doc["files"][0]["size_bytes"],dst.stat().st_size)
            self.assertEqual(
                doc["files"][0]["sha256"],
                hashlib.sha256(dst.read_bytes()).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
