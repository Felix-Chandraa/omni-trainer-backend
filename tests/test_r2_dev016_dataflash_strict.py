from pathlib import Path
import hashlib
import json
import tempfile
import unittest

from src.training_core.evidence_extensions import attach_ardupilot_bins

class Dev016StrictDataFlashTests(unittest.TestCase):
    def test_eeprom_bin_is_never_attached(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            pkg=base/"evidence"/"attempt-1"
            (pkg/"server").mkdir(parents=True)
            (pkg/"manifest.json").write_text("{}")

            plane=base/"run"/"direct"/"attempt-1"/"plane"
            logs=plane/"logs"
            logs.mkdir(parents=True)
            (plane/"eeprom.bin").write_bytes(b"E"*16384)
            data=b"DATAFLASH"*4096
            (logs/"00000001.BIN").write_bytes(data)

            class Fake:
                package_dir=pkg
                attempt_id="attempt-1"

            out=attach_ardupilot_bins(Fake(),base/"run","attempt-1")
            self.assertEqual([p.name for p in out],["aircraft.BIN"])
            self.assertEqual((pkg/"server"/"aircraft.BIN").read_bytes(),data)
            self.assertFalse((pkg/"server"/"aircraft-001.BIN").exists())

            meta=json.loads((pkg/"server"/"ardupilot-bin.json").read_text())
            self.assertEqual(len(meta["files"]),1)
            self.assertTrue(meta["files"][0]["source_path"].endswith("/plane/logs/00000001.BIN"))
            self.assertNotIn("eeprom.bin",json.dumps(meta))
            self.assertEqual(
                meta["files"][0]["sha256"],
                hashlib.sha256(data).hexdigest(),
            )

    def test_missing_dataflash_fails_closed_even_with_eeprom(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            pkg=base/"evidence"/"attempt-1"
            (pkg/"server").mkdir(parents=True)
            (pkg/"manifest.json").write_text("{}")
            plane=base/"run"/"direct"/"attempt-1"/"plane"
            plane.mkdir(parents=True)
            (plane/"eeprom.bin").write_bytes(b"E"*16384)

            class Fake:
                package_dir=pkg
                attempt_id="attempt-1"

            with self.assertRaises(Exception):
                attach_ardupilot_bins(Fake(),base/"run","attempt-1")

if __name__=="__main__": unittest.main()
