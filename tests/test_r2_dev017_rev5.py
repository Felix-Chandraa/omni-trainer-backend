from pathlib import Path
import unittest

from src.training_core.dev017_replay_overlay import ActuatorSample, enrich_records


class Dev017Rev5Tests(unittest.TestCase):
    def test_nested_state_receives_actuators(self):
        rows=[{
            "time":{"server_utc_ns":10_100_000_000},
            "payload":{
                "sequence":1,
                "state":{
                    "lat_deg":-7.3,
                    "lon_deg":108.2,
                    "roll_deg":0.0,
                }
            }
        }]
        samples=[ActuatorSample(10.0,1400,1500,1100,1600,25)]
        n=enrich_records(rows,samples)
        self.assertEqual(n,1)
        st=rows[0]["payload"]["state"]
        self.assertEqual(st["srv1"],1400)
        self.assertEqual(st["srv2"],1500)
        self.assertEqual(st["srv4"],1600)
        self.assertEqual(st["throttle"],25)
        self.assertNotIn("srv1",rows[0]["payload"])
        self.assertEqual(st["lat_deg"],-7.3)

    def test_replay_reader_exports_actuators(self):
        root=Path.home()/"omni-r2-dev001-hyAZEv"
        t=(root/"src/training_core/replay_reader.py").read_text()
        for k in ("srv1","srv2","srv3","srv4","throttle"):
            self.assertIn(f'"{k}": state.get("{k}")',t)

    def test_ground_frontend_clearance_remains_zero(self):
        cfg=(
            Path.home()/"Downloads"/"omni_flight-main"/
            "Feather-Flight-main/src/js/config.js"
        ).read_text()
        self.assertIn("groundClearanceMeters: 0.0",cfg)


if __name__=="__main__":
    unittest.main()
