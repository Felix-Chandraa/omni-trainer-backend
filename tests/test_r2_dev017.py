from pathlib import Path
import unittest
from src.training_core.dev017_replay_overlay import ActuatorSample, enrich_records

class Dev017ReplayTests(unittest.TestCase):
    def test_actuator_fields_merge_by_authoritative_utc(self):
        recs=[
            {"time":{"server_utc_ns":10_100_000_000},"payload":{"lat":1}},
            {"time":{"server_utc_ns":10_600_000_000},"payload":{"lat":2}},
        ]
        samples=[
            ActuatorSample(10.0,1400,1500,1100,1600,5),
            ActuatorSample(10.5,1600,1450,1700,1400,55),
        ]
        self.assertEqual(enrich_records(recs,samples),2)
        self.assertEqual(recs[0]["payload"]["srv1"],1400)
        self.assertEqual(recs[0]["payload"]["throttle"],5)
        self.assertEqual(recs[1]["payload"]["srv1"],1600)
        self.assertEqual(recs[1]["payload"]["srv4"],1400)
        self.assertEqual(recs[1]["payload"]["throttle"],55)

    def test_pose_truth_is_not_replaced(self):
        recs=[{"time":{"server_utc_ns":10_100_000_000},"payload":{"lat":-7.3,"lon":108.2,"roll":3.0}}]
        samples=[ActuatorSample(10.0,1400,1500,1100,1600,20)]
        enrich_records(recs,samples)
        self.assertEqual(recs[0]["payload"]["lat"],-7.3)
        self.assertEqual(recs[0]["payload"]["lon"],108.2)
        self.assertEqual(recs[0]["payload"]["roll"],3.0)

    def test_long_tlog_gap_is_not_fabricated(self):
        recs=[{"time":{"server_utc_ns":20_000_000_000},"payload":{}}]
        samples=[ActuatorSample(10.0,1400,1500,1100,1600,20)]
        self.assertEqual(enrich_records(recs,samples),0)
        self.assertNotIn("srv1",recs[0]["payload"])

class Dev017StaticTests(unittest.TestCase):
    def test_ground_patch_is_omni_only(self):
        src=(Path.home()/"Downloads"/"omni_flight-main"/"omnitrainer-sitl"/"ardupilot"/"libraries"/"SITL"/"SIM_JSBSim.cpp").read_text()
        self.assertIn("DEV017_OMNI_GROUND_SPAWN",src)
        self.assertIn('strstr(jsbsim_model, "Omni-Trainer")',src)
        self.assertIn("omni_ground_spawn ? 0.365f : 1.3f",src)
        self.assertIn("omni_ground_spawn ? 0.0f : 13.0f",src)

    def test_replay_wrapper_never_launches_fdm(self):
        root=Path.home()/"omni-r2-dev001-hyAZEv"
        text=(root/"run_dev017_replay.sh").read_text()
        self.assertIn("run_dev014_replay.sh",text)
        self.assertNotIn("sim_vehicle.py",text)
        self.assertNotIn("JSBSim",text)
        self.assertNotIn("arduplane",text.lower())

if __name__=="__main__": unittest.main()
