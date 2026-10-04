from __future__ import annotations

import struct
import tempfile
import unittest

from src.training_core.control_authority import (
    AuthorityError,
    FlightAuthorityManager,
)
from src.training_core.mavlink_control import (
    IGNORE,
    RELEASE,
    RC_CHANNELS_OVERRIDE_MSG_ID,
    encode_rc_channels_override,
    normalized_to_pwm,
    throttle_to_pwm,
    _frame_parts,
)


class FakeRecorder:
    def __init__(self):
        self.authority = []

    def record_authority(self, **kwargs):
        self.authority.append(kwargs)


class Dev015Tests(unittest.TestCase):
    def test_rc_override_release_semantics_are_not_ignore(self):
        frame = encode_rc_channels_override(
            sequence=7,
            target_system=1,
            target_component=1,
            channels=(
                RELEASE,
                RELEASE,
                RELEASE,
                RELEASE,
                IGNORE,
                IGNORE,
                IGNORE,
                IGNORE,
            ),
        )
        parts = _frame_parts(frame)
        self.assertIsNotNone(parts)
        msgid, _, _, payload = parts
        self.assertEqual(msgid, RC_CHANNELS_OVERRIDE_MSG_ID)
        values = struct.unpack("<8HBB", payload)
        self.assertEqual(values[:4], (0, 0, 0, 0))
        self.assertEqual(values[4:8], (65535, 65535, 65535, 65535))
        self.assertEqual(values[8:], (1, 1))

    def test_axis_mapping_bounds(self):
        self.assertEqual(normalized_to_pwm(-1), 1000)
        self.assertEqual(normalized_to_pwm(0), 1500)
        self.assertEqual(normalized_to_pwm(1), 2000)
        self.assertEqual(throttle_to_pwm(0), 1000)
        self.assertEqual(throttle_to_pwm(1), 2000)

    def test_authority_epoch_invalidates_old_owner_epoch(self):
        m = FlightAuthorityManager()
        r = FakeRecorder()
        a = m.grant(
            attempt_id="A",
            aircraft_id="omni-1",
            actor_id="student-1",
            station_id="station-1",
            generation=1,
            recorder=r,
            reason="initial",
        )
        b = m.grant(
            attempt_id="A",
            aircraft_id="omni-1",
            actor_id="instructor",
            station_id="instructor-station",
            generation=1,
            recorder=r,
            reason="takeover",
        )
        self.assertGreater(b.epoch, a.epoch)
        with self.assertRaises(AuthorityError):
            m.validate(
                attempt_id="A",
                actor_id="student-1",
                station_id="station-1",
                generation=1,
                epoch=a.epoch,
            )

    def test_router_contains_active_generation_freshness_and_deadman_gates(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        router = (root / "src/training_core/flight_command_router.py").read_text()
        authority = (root / "src/training_core/control_authority.py").read_text()

        for marker in (
            "attempt.state != State.ACTIVE",
            "generation_mismatch",
            "stale_command_sequence",
            "clock_unsynchronized",
            "stale_command",
            "deadman_timeout",
        ):
            self.assertIn(marker, router)

        self.assertIn("self.authority.validate(", router)
        self.assertIn(
            "except (ValueError, AuthorityError, RuntimeError)",
            router,
        )
        self.assertIn("lease.epoch != int(epoch)", authority)
        self.assertIn(
            'raise AuthorityError("authority_epoch_mismatch")',
            authority,
        )

    def test_no_arbitrary_mavlink_passthrough(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        text = (root / "src/training_core/flight_command_router.py").read_text()
        self.assertIn('"flight_axes"', text)
        self.assertIn('"release_axes"', text)
        self.assertIn('"arm"', text)
        self.assertIn('"disarm"', text)
        self.assertNotIn("raw_mavlink", text)
        self.assertNotIn("param_set", text)

    def test_live_proof_requires_real_fg_motion(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        text = (root / "src/training_core/dev015_live_trial.py").read_text()
        self.assertIn("ground_displacement", text)
        self.assertIn("motion_event", text)
        self.assertIn("endpoint.vehicle_status().armed is not False", text)


if __name__ == "__main__":
    unittest.main()
