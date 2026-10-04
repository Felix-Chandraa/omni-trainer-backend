from __future__ import annotations

import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.training_core.mavlink_control import (
    COMMAND_ACK_MSG_ID,
    HEARTBEAT_MSG_ID,
    MISSION_COUNT_MSG_ID,
    MISSION_ITEM_INT_MSG_ID,
    ControlError,
    ControlMavlinkTlogRecorder,
    RC_CHANNELS_OVERRIDE_MSG_ID,
    _frame_parts,
    encode_mavlink_v1,
)


def free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


def frame(msgid: int, payload: bytes = b"", sequence: int = 0) -> bytes:
    return encode_mavlink_v1(
        sequence=sequence,
        system_id=1,
        component_id=1,
        message_id=msgid,
        payload=payload,
        crc_extra=0,
    )


def heartbeat(*, armed: bool = False, sequence: int = 0) -> bytes:
    payload = struct.pack(
        "<IBBBBB",
        16,
        1,
        3,
        0x80 if armed else 0,
        4,
        3,
    )
    return frame(HEARTBEAT_MSG_ID, payload, sequence)


class EndpointHarness:
    def __init__(self, history_limit: int = 2048):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_udp_port()
        self.endpoint = ControlMavlinkTlogRecorder(
            bind_host="127.0.0.1",
            port=self.port,
            path=Path(self.tmp.name) / "aircraft.tlog",
            identity=object(),
            frame_history_limit=history_limit,
        )
        self.vehicle = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.vehicle.bind(("127.0.0.1", 0))
        self.vehicle.settimeout(1.0)

    def start(self):
        self.endpoint.start()
        self.vehicle.sendto(heartbeat(sequence=1), ("127.0.0.1", self.port))
        self.endpoint.wait_peer(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while self.endpoint.frame_token() < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        if self.endpoint.frame_token() < 1:
            raise AssertionError("heartbeat was not routed")
        return self

    def close(self):
        try:
            self.endpoint.stop()
        finally:
            self.vehicle.close()
            self.tmp.cleanup()


class CR015MavlinkDispatcherTests(unittest.TestCase):
    def test_outbound_socket_send_runs_on_io_owner_thread(self):
        h = EndpointHarness().start()
        try:
            caller = threading.get_ident()
            h.endpoint.send_rc_channels(
                (1500, 1500, 1000, 1500, 65535, 65535, 65535, 65535)
            )
            data, _ = h.vehicle.recvfrom(4096)
            parts = _frame_parts(data)
            self.assertIsNotNone(parts)
            self.assertEqual(parts[0], RC_CHANNELS_OVERRIDE_MSG_ID)

            self.assertIsNotNone(h.endpoint.io_owner_thread_ident)
            self.assertEqual(
                h.endpoint.last_send_thread_ident,
                h.endpoint.io_owner_thread_ident,
            )
            self.assertNotEqual(h.endpoint.last_send_thread_ident, caller)
        finally:
            h.close()

    def test_interleaved_telemetry_mission_and_ack_are_non_consuming(self):
        h = EndpointHarness().start()
        try:
            start = h.endpoint.frame_token()
            ack_start = h.endpoint.ack_token(400)
            mission = h.endpoint.open_mission_mailbox()

            attitude = frame(30, b"\0" * 28, 2)
            count = frame(MISSION_COUNT_MSG_ID, struct.pack("<HBB", 2, 1, 1), 3)
            vfr = frame(74, b"\0" * 20, 4)
            item = frame(MISSION_ITEM_INT_MSG_ID, b"\0" * 38, 5)
            ack = frame(COMMAND_ACK_MSG_ID, struct.pack("<HB", 400, 0), 6)
            hb = heartbeat(armed=True, sequence=7)

            h.vehicle.sendto(
                attitude + count + vfr + item + ack + hb,
                ("127.0.0.1", h.port),
            )

            first = mission.wait(timeout=1.0)
            second = mission.wait(timeout=1.0)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertEqual(first.message_id, MISSION_COUNT_MSG_ID)
            self.assertEqual(second.message_id, MISSION_ITEM_INT_MSG_ID)
            mission.close()

            command_ack = h.endpoint.wait_command_ack(
                400,
                after_counter=ack_start,
                timeout=1.0,
            )
            self.assertIsNotNone(command_ack)
            self.assertEqual(command_ack.result, 0)

            deadline = time.monotonic() + 1.0
            rows = []
            while time.monotonic() < deadline:
                rows = h.endpoint.frames_since(start)
                if len(rows) >= 6:
                    break
                time.sleep(0.005)

            self.assertEqual(
                [x.message_id for x in rows[:6]],
                [30, 44, 74, 73, 77, 0],
            )
            self.assertEqual(len(rows), 6)
            self.assertTrue(h.endpoint.wait_armed(True, timeout=1.0))
        finally:
            h.close()

    def test_mission_mailbox_is_exclusive_and_starts_at_current_cursor(self):
        h = EndpointHarness().start()
        try:
            h.vehicle.sendto(
                frame(MISSION_COUNT_MSG_ID, struct.pack("<HBB", 99, 1, 1), 8),
                ("127.0.0.1", h.port),
            )
            deadline = time.monotonic() + 1.0
            token = h.endpoint.frame_token()
            while token < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
                token = h.endpoint.frame_token()
            self.assertGreaterEqual(token, 2)

            first = h.endpoint.open_mission_mailbox()
            with self.assertRaises(ControlError):
                h.endpoint.open_mission_mailbox()

            self.assertIsNone(first.wait(timeout=0.05))

            h.vehicle.sendto(
                frame(MISSION_COUNT_MSG_ID, struct.pack("<HBB", 2, 1, 1), 9),
                ("127.0.0.1", h.port),
            )
            fresh = first.wait(timeout=1.0)
            self.assertIsNotNone(fresh)
            self.assertEqual(struct.unpack_from("<H", fresh.payload, 0)[0], 2)

            first.close()
            second = h.endpoint.open_mission_mailbox()
            second.close()
        finally:
            h.close()

    def test_stale_dispatch_cursor_fails_closed_after_history_overrun(self):
        h = EndpointHarness(history_limit=2).start()
        try:
            cursor = h.endpoint.frame_token()
            datagram = b"".join(
                frame(30 + i, b"\0" * 4, 20 + i)
                for i in range(4)
            )
            h.vehicle.sendto(datagram, ("127.0.0.1", h.port))
            deadline = time.monotonic() + 1.0
            while h.endpoint.frame_token() < cursor + 4 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertGreaterEqual(h.endpoint.frame_token(), cursor + 4)
            with self.assertRaisesRegex(ControlError, "history overrun"):
                h.endpoint.frames_since(cursor)
        finally:
            h.close()

    def test_waiter_is_cancelled_when_endpoint_stops(self):
        h = EndpointHarness().start()
        errors = []
        entered = threading.Event()

        def waiter():
            try:
                entered.set()
                h.endpoint.wait_routed_frame(
                    {MISSION_COUNT_MSG_ID},
                    after_counter=h.endpoint.frame_token(),
                    timeout=5.0,
                )
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=waiter)
        t.start()
        self.assertTrue(entered.wait(0.5))
        time.sleep(0.03)
        h.endpoint.stop()
        t.join(timeout=1.0)
        try:
            self.assertFalse(t.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ControlError)
        finally:
            h.vehicle.close()
            h.tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
