"""DEV-015 bidirectional MAVLink control endpoint sharing the authoritative tlog socket.

The existing worker's MAVProxy output is bidirectional.  This endpoint owns
that one UDP socket, records every inbound MAVLink frame in MAVProxy-style
.tlog format, learns the current peer/vehicle identity from HEARTBEAT, and sends
only validated control frames back to the same peer.

Only two outbound message families are implemented in DEV-015:
- RC_CHANNELS_OVERRIDE (70) for continuous Flight axes;
- COMMAND_LONG (76) for arm/disarm.

This module intentionally does not expose arbitrary MAVLink passthrough.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
import os
import queue
import select
from pathlib import Path
import socket
import struct
import threading
import time
from typing import Callable, Iterable

from .evidence_recorder import RecorderError, _split_mavlink_frames


MAVLINK_V1_MAGIC = 0xFE
MAV_MODE_FLAG_SAFETY_ARMED = 0x80
MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_RESULT_ACCEPTED = 0

RC_CHANNELS_OVERRIDE_MSG_ID = 70
RC_CHANNELS_OVERRIDE_CRC_EXTRA = 124
COMMAND_LONG_MSG_ID = 76
COMMAND_LONG_CRC_EXTRA = 152

HEARTBEAT_MSG_ID = 0
MISSION_ITEM_MSG_ID = 39
MISSION_REQUEST_MSG_ID = 40
MISSION_REQUEST_LIST_MSG_ID = 43
MISSION_COUNT_MSG_ID = 44
MISSION_ACK_MSG_ID = 47
MISSION_REQUEST_INT_MSG_ID = 51
MISSION_ITEM_INT_MSG_ID = 73
COMMAND_ACK_MSG_ID = 77
STATUSTEXT_MSG_ID = 253

MISSION_TRANSACTION_MSG_IDS = frozenset({
    MISSION_ITEM_MSG_ID,
    MISSION_REQUEST_MSG_ID,
    MISSION_REQUEST_LIST_MSG_ID,
    MISSION_COUNT_MSG_ID,
    MISSION_ACK_MSG_ID,
    MISSION_REQUEST_INT_MSG_ID,
    MISSION_ITEM_INT_MSG_ID,
})

RELEASE = 0
IGNORE = 65535


class ControlError(RuntimeError):
    pass


def _crc_accumulate(byte: int, crc: int) -> int:
    tmp = (byte ^ (crc & 0xFF)) & 0xFF
    tmp = (tmp ^ ((tmp << 4) & 0xFF)) & 0xFF
    return (
        ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4))
        & 0xFFFF
    )


def mavlink_x25(data: bytes, crc_extra: int) -> int:
    crc = 0xFFFF
    for b in data:
        crc = _crc_accumulate(b, crc)
    return _crc_accumulate(int(crc_extra) & 0xFF, crc)


def encode_mavlink_v1(
    *,
    sequence: int,
    system_id: int,
    component_id: int,
    message_id: int,
    payload: bytes,
    crc_extra: int,
) -> bytes:
    if not (0 <= len(payload) <= 255):
        raise ValueError("MAVLink v1 payload too large")
    header = bytes(
        (
            len(payload),
            sequence & 0xFF,
            system_id & 0xFF,
            component_id & 0xFF,
            message_id & 0xFF,
        )
    )
    crc = mavlink_x25(header + payload, crc_extra)
    return bytes((MAVLINK_V1_MAGIC,)) + header + payload + struct.pack("<H", crc)


def encode_rc_channels_override(
    *,
    sequence: int,
    target_system: int,
    target_component: int,
    channels: Iterable[int],
    source_system: int = 255,
    source_component: int = 190,
) -> bytes:
    values = tuple(int(x) for x in channels)
    if len(values) != 8:
        raise ValueError("RC override requires exactly channels 1..8")
    if any(x < 0 or x > 65535 for x in values):
        raise ValueError("RC override channel outside uint16")
    # Wire ordering from common.xml generated headers:
    # chan1..chan8 uint16 first, then target_system/component.
    payload = struct.pack(
        "<8HBB",
        *values,
        int(target_system),
        int(target_component),
    )
    return encode_mavlink_v1(
        sequence=sequence,
        system_id=source_system,
        component_id=source_component,
        message_id=RC_CHANNELS_OVERRIDE_MSG_ID,
        payload=payload,
        crc_extra=RC_CHANNELS_OVERRIDE_CRC_EXTRA,
    )


def encode_command_long(
    *,
    sequence: int,
    target_system: int,
    target_component: int,
    command: int,
    params: Iterable[float],
    confirmation: int = 0,
    source_system: int = 255,
    source_component: int = 190,
) -> bytes:
    p = tuple(float(x) for x in params)
    if len(p) != 7:
        raise ValueError("COMMAND_LONG requires seven params")
    payload = struct.pack(
        "<7fHBBB",
        *p,
        int(command),
        int(target_system),
        int(target_component),
        int(confirmation),
    )
    return encode_mavlink_v1(
        sequence=sequence,
        system_id=source_system,
        component_id=source_component,
        message_id=COMMAND_LONG_MSG_ID,
        payload=payload,
        crc_extra=COMMAND_LONG_CRC_EXTRA,
    )


def normalized_to_pwm(value: float) -> int:
    v = max(-1.0, min(1.0, float(value)))
    return int(round(1500.0 + 500.0 * v))


def throttle_to_pwm(value: float) -> int:
    v = max(0.0, min(1.0, float(value)))
    return int(round(1000.0 + 1000.0 * v))


@dataclass(frozen=True)
class VehicleStatus:
    peer_ready: bool
    target_system: int
    target_component: int
    armed: bool | None
    custom_mode: int | None
    last_heartbeat_monotonic_ns: int | None


@dataclass(frozen=True)
class CommandAck:
    counter: int
    command: int
    result: int
    monotonic_ns: int


@dataclass(frozen=True)
class StatusText:
    counter: int
    severity: int
    text: str
    monotonic_ns: int


@dataclass(frozen=True)
class RoutedMavlinkFrame:
    """One inbound MAVLink frame observed by the single socket owner."""

    counter: int
    message_id: int
    system_id: int
    component_id: int
    payload: bytes
    frame: bytes
    utc_ns: int
    monotonic_ns: int


@dataclass
class _OutboundRequest:
    frame: bytes
    done: threading.Event = field(default_factory=threading.Event)
    sent: int | None = None
    error: Exception | None = None


class MavlinkMailbox:
    """Cursor-based, non-consuming view of routed MAVLink frames."""

    def __init__(
        self,
        endpoint: "ControlMavlinkTlogRecorder",
        message_ids: Iterable[int],
        *,
        after_counter: int,
        on_close: Callable[["MavlinkMailbox"], None] | None = None,
    ):
        ids = frozenset(int(x) for x in message_ids)
        if not ids:
            raise ValueError("mailbox requires at least one MAVLink message id")
        self._endpoint = endpoint
        self.message_ids = ids
        self._cursor = int(after_counter)
        self._on_close = on_close
        self._closed = False

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def closed(self) -> bool:
        return self._closed

    def wait(
        self,
        timeout: float = 3.0,
        *,
        predicate: Callable[[RoutedMavlinkFrame], bool] | None = None,
    ) -> RoutedMavlinkFrame | None:
        if self._closed:
            raise ControlError("MAVLink mailbox is closed")
        frame = self._endpoint.wait_routed_frame(
            self.message_ids,
            after_counter=self._cursor,
            timeout=timeout,
            predicate=predicate,
        )
        if frame is not None:
            self._cursor = frame.counter
        return frame

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        callback = self._on_close
        self._on_close = None
        if callback is not None:
            callback(self)

    def __enter__(self) -> "MavlinkMailbox":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# CR015_SINGLE_IO_OWNER
# DEV015_REV4_STATUSTEXT
def _frame_parts(frame: bytes) -> tuple[int, int, int, bytes] | None:
    """Return (msgid, sysid, compid, payload) for MAVLink v1/v2."""
    if not frame:
        return None
    if frame[0] == 0xFE:
        if len(frame) < 8:
            return None
        plen = frame[1]
        if len(frame) < 8 + plen:
            return None
        return frame[5], frame[3], frame[4], frame[6 : 6 + plen]
    if frame[0] == 0xFD:
        if len(frame) < 12:
            return None
        plen = frame[1]
        if len(frame) < 12 + plen:
            return None
        msgid = frame[7] | (frame[8] << 8) | (frame[9] << 16)
        return msgid, frame[5], frame[6], frame[10 : 10 + plen]
    return None


class ControlMavlinkTlogRecorder:
    """Authoritative .tlog recorder plus a narrowly-scoped outbound adapter."""

    def __init__(
        self,
        *,
        bind_host: str,
        port: int,
        path: Path,
        identity,
        source_system: int = 255,
        source_component: int = 190,
        frame_history_limit: int = 2048,
    ):
        self.bind_host = bind_host
        self.port = int(port)
        self.path = Path(path)
        self.identity = identity
        self.source_system = int(source_system)
        self.source_component = int(source_component)

        self.frames = 0
        self.datagrams = 0
        self.skipped_bytes = 0
        self.first_utc_ns: int | None = None
        self.last_utc_ns: int | None = None

        self._sock: socket.socket | None = None
        self._fp = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: Exception | None = None
        self._state = threading.Condition()
        self._peer: tuple[str, int] | None = None
        self._target_system = 1
        self._target_component = 1
        self._armed: bool | None = None
        self._custom_mode: int | None = None
        self._last_heartbeat_ns: int | None = None
        self._ack_counter = 0
        self._acks: dict[int, CommandAck] = {}
        self._status_counter = 0
        self._status_texts: deque[StatusText] = deque(maxlen=64)
        self._tx_sequence = 0
        self._send_lock = threading.Lock()

        # CR-015: this endpoint owns both directions of the UDP socket.
        self._outbound: queue.Queue[_OutboundRequest] = queue.Queue()
        self._wake_read: socket.socket | None = None
        self._wake_write: socket.socket | None = None
        self._owner_thread_ident: int | None = None
        self._last_send_thread_ident: int | None = None

        self._frame_counter = 0
        self._frame_history: deque[RoutedMavlinkFrame] = deque(
            maxlen=max(1, int(frame_history_limit))
        )
        self._mission_mailbox: MavlinkMailbox | None = None

        self.sent_frames = 0
        self.release_count = 0
        self.last_rc_channels: tuple[int, ...] | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise ControlError("MAVLink control endpoint already started")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        wake_read = None
        wake_write = None
        try:
            sock.bind((self.bind_host, self.port))
            sock.setblocking(False)
            wake_read, wake_write = socket.socketpair()
            wake_read.setblocking(False)
            wake_write.setblocking(False)
            fp = self.path.open("wb", buffering=0)
        except Exception:
            sock.close()
            if wake_read is not None:
                wake_read.close()
            if wake_write is not None:
                wake_write.close()
            raise

        self._stop.clear()
        self._error = None
        self._sock = sock
        self._wake_read = wake_read
        self._wake_write = wake_write
        self._fp = fp
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"OmniControlTlog:{self.port}",
        )
        self._thread.start()

    @property
    def io_owner_thread_ident(self) -> int | None:
        return self._owner_thread_ident

    @property
    def last_send_thread_ident(self) -> int | None:
        return self._last_send_thread_ident

    def _wake_owner(self) -> None:
        wake = self._wake_write
        if wake is None:
            return
        try:
            wake.send(b"\0")
        except (BlockingIOError, OSError):
            pass

    def _drain_wakeup(self) -> None:
        wake = self._wake_read
        if wake is None:
            return
        while True:
            try:
                if not wake.recv(4096):
                    return
            except BlockingIOError:
                return
            except OSError:
                return

    def _send_from_owner(self, frame: bytes) -> int:
        if threading.get_ident() != self._owner_thread_ident:
            raise ControlError("MAVLink socket send attempted outside I/O owner")
        sock = self._sock
        with self._state:
            peer = self._peer
        if sock is None or peer is None:
            raise ControlError("MAVLink adapter not ready: no learned peer")

        for _ in range(5):
            try:
                sent = sock.sendto(frame, peer)
                self.sent_frames += 1
                self._last_send_thread_ident = threading.get_ident()
                return sent
            except BlockingIOError:
                select.select([], [sock], [], 0.05)
        raise ControlError("MAVLink UDP send remained blocked")

    def _drain_outbound(self) -> None:
        while True:
            try:
                request = self._outbound.get_nowait()
            except queue.Empty:
                return
            try:
                request.sent = self._send_from_owner(request.frame)
            except Exception as exc:
                request.error = exc
            finally:
                request.done.set()

    def _fail_pending_outbound(self, error: Exception) -> None:
        while True:
            try:
                request = self._outbound.get_nowait()
            except queue.Empty:
                return
            request.error = error
            request.done.set()

    def _submit_outbound(self, frame: bytes, *, timeout: float = 1.0) -> int:
        if self._stop.is_set():
            raise ControlError("MAVLink control endpoint is stopping")
        thread = self._thread
        if thread is None or not thread.is_alive():
            raise ControlError("MAVLink control endpoint is not running")
        if threading.get_ident() == self._owner_thread_ident:
            return self._send_from_owner(frame)

        request = _OutboundRequest(bytes(frame))
        self._outbound.put(request)
        self._wake_owner()
        if not request.done.wait(timeout=max(0.01, float(timeout))):
            raise ControlError("MAVLink outbound owner queue timeout")
        if request.error is not None:
            if isinstance(request.error, ControlError):
                raise request.error
            raise ControlError(f"MAVLink outbound send failed: {request.error}")
        if request.sent is None:
            raise ControlError("MAVLink outbound send completed without result")
        return request.sent

    def _run(self) -> None:
        assert self._sock is not None and self._fp is not None
        self._owner_thread_ident = threading.get_ident()
        try:
            while not self._stop.is_set():
                readers = [self._sock]
                if self._wake_read is not None:
                    readers.append(self._wake_read)
                try:
                    ready, _, _ = select.select(readers, [], [], 0.2)
                except OSError:
                    if self._stop.is_set():
                        break
                    raise

                if self._wake_read is not None and self._wake_read in ready:
                    self._drain_wakeup()
                    if self._stop.is_set():
                        break
                    self._drain_outbound()

                if self._sock in ready:
                    while not self._stop.is_set():
                        try:
                            data, peer = self._sock.recvfrom(65535)
                        except BlockingIOError:
                            break
                        except OSError:
                            if self._stop.is_set():
                                break
                            raise

                        utc_ns = time.time_ns()
                        self.datagrams += 1
                        frames, skipped = _split_mavlink_frames(data)
                        self.skipped_bytes += skipped
                        with self._state:
                            self._peer = peer

                        for frame in frames:
                            self._fp.write(struct.pack(">Q", utc_ns // 1000))
                            self._fp.write(frame)
                            self.frames += 1
                            if self.first_utc_ns is None:
                                self.first_utc_ns = utc_ns
                            self.last_utc_ns = utc_ns

                            # Existing state observers run first; the routed
                            # frame is then published to non-consuming mailboxes.
                            self._observe_frame(frame)
                            self._route_frame(frame, utc_ns=utc_ns)

                self._drain_outbound()
        except Exception as exc:
            self._error = exc
        finally:
            self._fail_pending_outbound(
                ControlError("MAVLink I/O owner stopped before send completed")
            )
            with self._state:
                self._state.notify_all()

    def _route_frame(
        self,
        frame: bytes,
        *,
        utc_ns: int,
    ) -> RoutedMavlinkFrame | None:
        parts = _frame_parts(frame)
        if parts is None:
            return None
        msgid, sysid, compid, payload = parts
        with self._state:
            self._frame_counter += 1
            routed = RoutedMavlinkFrame(
                counter=self._frame_counter,
                message_id=int(msgid),
                system_id=int(sysid),
                component_id=int(compid),
                payload=bytes(payload),
                frame=bytes(frame),
                utc_ns=int(utc_ns),
                monotonic_ns=time.monotonic_ns(),
            )
            self._frame_history.append(routed)
            self._state.notify_all()
        return routed

    def _observe_frame(self, frame: bytes) -> None:
        parts = _frame_parts(frame)
        if parts is None:
            return
        msgid, sysid, compid, payload = parts
        now = time.monotonic_ns()
        with self._state:
            if msgid == HEARTBEAT_MSG_ID and len(payload) >= 9:  # HEARTBEAT
                custom_mode = struct.unpack_from("<I", payload, 0)[0]
                base_mode = payload[6]
                self._target_system = int(sysid) or self._target_system
                self._target_component = int(compid) or self._target_component
                self._armed = bool(base_mode & MAV_MODE_FLAG_SAFETY_ARMED)
                self._custom_mode = int(custom_mode)
                self._last_heartbeat_ns = now
                self._state.notify_all()
            elif msgid == COMMAND_ACK_MSG_ID and len(payload) >= 3:  # COMMAND_ACK
                command = struct.unpack_from("<H", payload, 0)[0]
                result = payload[2]
                self._ack_counter += 1
                self._acks[int(command)] = CommandAck(
                    counter=self._ack_counter,
                    command=int(command),
                    result=int(result),
                    monotonic_ns=now,
                )
                self._state.notify_all()
            elif msgid == STATUSTEXT_MSG_ID and len(payload) >= 2:  # STATUSTEXT
                severity = int(payload[0])
                raw = payload[1:51]
                text = raw.split(b"\0", 1)[0].decode(
                    "utf-8", errors="replace"
                ).strip()
                if text:
                    self._status_counter += 1
                    self._status_texts.append(
                        StatusText(
                            counter=self._status_counter,
                            severity=severity,
                            text=text,
                            monotonic_ns=now,
                        )
                    )
                    self._state.notify_all()

    def frame_token(self) -> int:
        with self._state:
            return self._frame_counter

    def _check_dispatch_cursor_locked(self, after_counter: int) -> None:
        if not self._frame_history:
            return
        oldest = self._frame_history[0].counter
        if int(after_counter) < oldest - 1:
            raise ControlError(
                "MAVLink dispatcher history overrun; transaction cursor is stale"
            )

    def frames_since(
        self,
        after_counter: int,
        *,
        message_ids: Iterable[int] | None = None,
    ) -> list[RoutedMavlinkFrame]:
        ids = None if message_ids is None else frozenset(int(x) for x in message_ids)
        with self._state:
            self._check_dispatch_cursor_locked(after_counter)
            return [
                frame
                for frame in self._frame_history
                if frame.counter > int(after_counter)
                and (ids is None or frame.message_id in ids)
            ]

    def wait_routed_frame(
        self,
        message_ids: Iterable[int],
        *,
        after_counter: int,
        timeout: float = 3.0,
        predicate: Callable[[RoutedMavlinkFrame], bool] | None = None,
    ) -> RoutedMavlinkFrame | None:
        ids = frozenset(int(x) for x in message_ids)
        if not ids:
            raise ValueError("wait_routed_frame requires message ids")
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._state:
            while True:
                self._check_dispatch_cursor_locked(after_counter)
                for frame in self._frame_history:
                    if frame.counter <= int(after_counter):
                        continue
                    if frame.message_id not in ids:
                        continue
                    if predicate is None or predicate(frame):
                        return frame

                if self._error is not None:
                    raise ControlError(f"MAVLink I/O owner failed: {self._error}")
                if self._stop.is_set():
                    raise ControlError("MAVLink control endpoint stopped")

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._state.wait(timeout=min(0.1, remaining))

    def open_mailbox(
        self,
        message_ids: Iterable[int],
        *,
        after_counter: int | None = None,
    ) -> MavlinkMailbox:
        cursor = self.frame_token() if after_counter is None else int(after_counter)
        return MavlinkMailbox(
            self,
            message_ids,
            after_counter=cursor,
        )

    def _release_mission_mailbox(self, mailbox: MavlinkMailbox) -> None:
        with self._state:
            if self._mission_mailbox is mailbox:
                self._mission_mailbox = None

    def open_mission_mailbox(self) -> MavlinkMailbox:
        """Open the single mission transaction mailbox for this aircraft."""
        with self._state:
            current = self._mission_mailbox
            if current is not None and not current.closed:
                raise ControlError("mission transaction already active")
            mailbox = MavlinkMailbox(
                self,
                MISSION_TRANSACTION_MSG_IDS,
                after_counter=self._frame_counter,
                on_close=self._release_mission_mailbox,
            )
            self._mission_mailbox = mailbox
            return mailbox

    def vehicle_status(self) -> VehicleStatus:
        with self._state:
            return VehicleStatus(
                peer_ready=self._peer is not None,
                target_system=self._target_system,
                target_component=self._target_component,
                armed=self._armed,
                custom_mode=self._custom_mode,
                last_heartbeat_monotonic_ns=self._last_heartbeat_ns,
            )

    def wait_peer(self, timeout: float = 5.0) -> VehicleStatus:
        deadline = time.monotonic() + timeout
        with self._state:
            while self._peer is None and time.monotonic() < deadline:
                self._state.wait(timeout=min(0.2, max(0.0, deadline - time.monotonic())))
            status = self.vehicle_status()
        if not status.peer_ready:
            raise ControlError("MAVLink control peer not learned from worker")
        return status

    def ack_token(self, command: int) -> int:
        with self._state:
            ack = self._acks.get(int(command))
            return 0 if ack is None else ack.counter

    def wait_command_ack(
        self,
        command: int,
        *,
        after_counter: int,
        timeout: float = 3.0,
    ) -> CommandAck | None:
        deadline = time.monotonic() + timeout
        with self._state:
            while time.monotonic() < deadline:
                ack = self._acks.get(int(command))
                if ack is not None and ack.counter > after_counter:
                    return ack
                self._state.wait(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
        return None

    def wait_armed(self, armed: bool, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._state:
            while time.monotonic() < deadline:
                if self._armed is bool(armed) or self._armed == bool(armed):
                    return True
                self._state.wait(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
        return False

    def status_token(self) -> int:
        with self._state:
            return self._status_counter

    def status_texts_since(
        self,
        counter: int,
        *,
        limit: int = 8,
    ) -> list[str]:
        with self._state:
            rows = [
                x.text
                for x in self._status_texts
                if x.counter > int(counter)
            ]
        return rows[-max(1, int(limit)):]

    def _next_tx_seq_locked(self) -> int:
        seq = self._tx_sequence
        self._tx_sequence = (self._tx_sequence + 1) & 0xFF
        return seq

    def send_raw(self, frame: bytes) -> int:
        # Preserve the legacy synchronous contract while the actual socket I/O
        # is performed by the single owner thread.
        with self._send_lock:
            return self._submit_outbound(bytes(frame))

    def send_rc_channels(self, channels: Iterable[int]) -> tuple[int, ...]:
        values = tuple(int(x) for x in channels)
        status = self.wait_peer(timeout=0.0)
        with self._send_lock:
            frame = encode_rc_channels_override(
                sequence=self._next_tx_seq_locked(),
                target_system=status.target_system,
                target_component=status.target_component,
                channels=values,
                source_system=self.source_system,
                source_component=self.source_component,
            )
            self._submit_outbound(frame)
        self.last_rc_channels = values
        return values

    def send_axes(
        self,
        *,
        roll: float,
        pitch: float,
        throttle: float,
        yaw: float,
    ) -> tuple[int, ...]:
        channels = (
            normalized_to_pwm(roll),
            normalized_to_pwm(pitch),
            throttle_to_pwm(throttle),
            normalized_to_pwm(yaw),
            IGNORE,
            IGNORE,
            IGNORE,
            IGNORE,
        )
        return self.send_rc_channels(channels)

    def release_override(self) -> tuple[int, ...]:
        # For channels 1..8, 0 RELEASES that channel; UINT16_MAX only ignores
        # the field and would leave an old override active.
        channels = (
            RELEASE,
            RELEASE,
            RELEASE,
            RELEASE,
            IGNORE,
            IGNORE,
            IGNORE,
            IGNORE,
        )
        result = self.send_rc_channels(channels)
        self.release_count += 1
        return result

    def send_arm_disarm(
        self,
        armed: bool,
        *,
        force: bool = False,
    ) -> None:
        status = self.wait_peer(timeout=0.0)
        force_value = 21196.0 if force else 0.0
        with self._send_lock:
            frame = encode_command_long(
                sequence=self._next_tx_seq_locked(),
                target_system=status.target_system,
                target_component=status.target_component,
                command=MAV_CMD_COMPONENT_ARM_DISARM,
                params=(
                    1.0 if armed else 0.0,
                    force_value,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                ),
                source_system=self.source_system,
                source_component=self.source_component,
            )
            self._submit_outbound(frame)

    def stop(self) -> None:
        self._stop.set()
        self._wake_owner()

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        if thread is not None and thread.is_alive():
            raise RecorderError("MAVLink control I/O owner did not stop")

        self._fail_pending_outbound(
            ControlError("MAVLink control endpoint stopped")
        )

        for attr in ("_sock", "_wake_read", "_wake_write"):
            sock = getattr(self, attr)
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
                setattr(self, attr, None)

        if self._fp is not None:
            self._fp.flush()
            os.fsync(self._fp.fileno())
            self._fp.close()
            self._fp = None

        mailbox = None
        with self._state:
            mailbox = self._mission_mailbox
            self._mission_mailbox = None
            self._state.notify_all()
        if mailbox is not None:
            mailbox.close()

        if self._error is not None:
            raise RecorderError(
                f"MAVLink control/tlog endpoint failed: {self._error}"
            )
