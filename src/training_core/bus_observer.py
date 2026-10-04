"""R2 DEV-003: passive WebSocket F1/UI-bus subscriber, standard library only.

Source is an existing UI-normalized stream; NEVER claim direct raw FG/MAVLink validation.
No write to simulator, process management, UDP bind, external web, or commands.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import time
import uuid

from .models import Actor, Role
from .session import SessionManager
from .store import Store

_WS_MAGIC = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
MAX_MESSAGE = 1 << 20
DISPLAY_FIELDS = frozenset({
    'lat','lon','alt','alt_msl','agl','roll','pitch','yaw','hdg','as','gs',
    'pose_source','armed','mode','bat','bat_pct','fuel_pct','sat','gps_fix_type',
    'home_lat','home_lon','home_alt','vehicle_type',
})


class BusError(Exception):
    pass


class BusClient:
    """Basic RFC6455 text client for loopback F1 bus, with bounded input buffer.

    Works even when system Python lacks `websockets` (no dependency install).
    Text, continuation, server ping/pong and close frames are supported.
    """
    def __init__(self, port: int = 8765):
        if not 1 <= port <= 65535:
            raise ValueError('bus port harus 1..65535')
        self.port = port
        self.sock: socket.socket | None = None
        self.buf = bytearray()
        self.frag = bytearray()
        self.fragmenting = False

    def __enter__(self):
        self.sock = socket.create_connection(('127.0.0.1', self.port), timeout=2)
        try:
            nonce = base64.b64encode(secrets.token_bytes(16)).decode('ascii')
            request = ('GET / HTTP/1.1\r\n'
                       f'Host: 127.0.0.1:{self.port}\r\n'
                       'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                       f'Sec-WebSocket-Key: {nonce}\r\n'
                       'Sec-WebSocket-Version: 13\r\n\r\n')
            self.sock.sendall(request.encode('ascii'))
            end = time.monotonic() + 2
            while b'\r\n\r\n' not in self.buf:
                if len(self.buf) > 16384:
                    raise BusError('HTTP handshake terlalu besar')
                self._recv_until(end)
            idx = self.buf.index(b'\r\n\r\n') + 4
            head = bytes(self.buf[:idx])
            del self.buf[:idx]
            lines = head.decode('iso-8859-1').split('\r\n')
            if ' 101 ' not in lines[0] and not lines[0].endswith(' 101'):
                raise BusError(f'WebSocket upgrade gagal: {lines[0]}')
            headers = {}
            for line in lines[1:]:
                if ':' in line:
                    name, value = line.split(':', 1)
                    headers[name.strip().lower()] = value.strip()
            expected = base64.b64encode(hashlib.sha1((nonce + _WS_MAGIC).encode()).digest()).decode()
            if headers.get('sec-websocket-accept') != expected:
                raise BusError('WebSocket accept tidak cocok')
            if headers.get('upgrade', '').lower() != 'websocket':
                raise BusError('WebSocket upgrade header tidak valid')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _recv_until(self, deadline: float):
        assert self.sock is not None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('menunggu frame')
        self.sock.settimeout(remaining)
        try:
            data = self.sock.recv(65536)
        except socket.timeout as e:
            raise TimeoutError('menunggu frame') from e
        if not data:
            raise BusError('bus menutup koneksi')
        self.buf.extend(data)
        if len(self.buf) > MAX_MESSAGE + 16384:
            raise BusError('buffer melewati batas')

    def _fill(self, needed: int, deadline: float):
        while len(self.buf) < needed:
            self._recv_until(deadline)

    def _send_control(self, opcode: int, payload: bytes = b''):
        # RFC6455 requires ALL client frames masked; only protocol pong/close used.
        if self.sock is None or len(payload) > 125:
            return
        mask = secrets.token_bytes(4)
        encoded = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + encoded)

    def recv_text(self, timeout: float = 0.5) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._fill(2, deadline)
            except TimeoutError:
                return None
            b0, b1 = self.buf[0], self.buf[1]
            fin, op, masked = bool(b0 & 0x80), b0 & 0x0f, bool(b1 & 0x80)
            if masked:
                raise BusError('server mengirim frame masked')
            n = b1 & 127
            extra = 2 if n == 126 else 8 if n == 127 else 0
            try:
                self._fill(2 + extra, deadline)
            except TimeoutError:
                return None
            if extra:
                n = struct.unpack('!H' if extra == 2 else '!Q', self.buf[2:2+extra])[0]
            if n > MAX_MESSAGE:
                raise BusError('frame melebihi ukuran yang diizinkan')
            hdr = 2 + extra
            try:
                self._fill(hdr + n, deadline)
            except TimeoutError:
                return None
            payload = bytes(self.buf[hdr:hdr+n])
            del self.buf[:hdr+n]
            if op == 0x8:
                raise BusError('bus mengirim close frame')
            if op == 0x9:
                self._send_control(0xA, payload)
                continue
            if op == 0xA:
                continue
            if op == 0x1:
                if self.fragmenting:
                    raise BusError('fragmentasi text tidak valid')
                self.frag = bytearray(payload)
                self.fragmenting = not fin
            elif op == 0x0 and self.fragmenting:
                self.frag.extend(payload)
                if len(self.frag) > MAX_MESSAGE:
                    raise BusError('text melebihi ukuran')
                self.fragmenting = not fin
            else:
                raise BusError(f'opcode tidak didukung: {op}')
            if fin:
                msg = self.frag.decode('utf-8')
                self.frag.clear()
                return msg

    def __exit__(self, *_):
        if self.sock is not None:
            self.sock.close()
            self.sock = None


def parse_bus_telemetry(raw: str) -> dict | None:
    """Never trust unvalidated WebSocket content or invent absent telemetry fields."""
    try:
        env = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(env, dict) or env.get('type') != 'telemetry':
        return None
    data = env.get('data')
    if not isinstance(data, dict):
        return None
    return {k: data[k] for k in DISPLAY_FIELDS if k in data and
            (data[k] is None or type(data[k]) in (bool, int, float, str))}


class DiagnosticJournal:
    """New file per invocation. Not production training evidence."""
    def __init__(self, folder: Path, attempt_id: str):
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f'bus-{uuid.uuid4().hex}.jsonl'
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.file = os.fdopen(fd, 'w', encoding='utf-8')
        self.seq = 0
        self.attempt_id = attempt_id

    def record(self, kind: str, data: dict):
        self.seq += 1
        row = {'schema': 'omni.r2.bus_observation.v1',
               'seq': self.seq, 'attempt_id': self.attempt_id,
               'source': 'existing_f1_ui_bus', 'synthetic': False,
               'utc_ns': time.time_ns(), 'monotonic_ns': time.monotonic_ns(),
               'kind': kind, 'data': data}
        self.file.write(json.dumps(row, allow_nan=False, sort_keys=True) + '\n')
        self.file.flush()
        return row

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()


def observe(root: Path, port: int, seconds: float, *, client_class=BusClient) -> int:
    if not 1 <= seconds <= 120:
        raise ValueError('durasi live harus 1–120 detik')
    output = root / 'runtime' / 'dev003'
    output.mkdir(parents=True, exist_ok=True)
    db = output / f'dev003-{uuid.uuid4().hex}.sqlite'
    store = Store(db)
    try:
        manager = SessionManager(store)
        teacher = Actor('instructor-diagnostic', Role.INSTRUCTOR)
        session_id = manager.create_session(teacher, 'student-diagnostic')
        exercise_id = manager.create_exercise(teacher, session_id, 'r2-bus-observation@diagnostic')
        attempt = manager.create_attempt(teacher, exercise_id, 'omni-bus-diagnostic')
    finally:
        store.close()
    print(f'Diagnostic Session={session_id}')
    print(f'Diagnostic Attempt={attempt.id} state={attempt.state.value} (TIDAK Active)')
    print(f'Database={db}')
    try:
        with client_class(port) as bus:
            print(f'Connected: ws://127.0.0.1:{port}/ (subscriber read-only)')
            print('Mendengarkan telemetry existing, tanpa command. Ctrl+C untuk stop.')
            start, last_sample = time.monotonic(), 0.0
            first_received = last_received = None
            gap_open = None
            total = samples = flagged_fg = reported_status = 0
            with DiagnosticJournal(output, attempt.id) as journal:
                print(f'Journal={journal.path}')
                while time.monotonic() - start < seconds:
                    try:
                        raw = bus.recv_text(min(0.5, max(.01, seconds - (time.monotonic()-start))))
                    except BusError as e:
                        journal.record('disconnect', {'reason': str(e)})
                        print('BUS DISCONNECTED:', e)
                        break
                    now = time.monotonic()
                    if raw is not None:
                        data = parse_bus_telemetry(raw)
                        if data is not None:
                            total += 1
                            if first_received is None: first_received = now
                            last_received = now
                            if gap_open is not None:
                                journal.record('gap_end', {'duration_s': round(now-gap_open, 3)})
                                gap_open = None
                            if data.get('pose_source') == 'fg' and data.get('lat') is not None and data.get('lon') is not None:
                                flagged_fg += 1
                            if data.get('armed') is not None or data.get('mode') is not None:
                                reported_status += 1
                            if now-last_sample >= 0.1:
                                try:
                                    journal.record('telemetry', data)
                                    samples += 1
                                    last_sample = now
                                    if samples == 1 or samples % 25 == 0:
                                        print(f'  sample={samples} lat={data.get("lat")} lon={data.get("lon")} '
                                              f'alt_msl={data.get("alt_msl")} pose_source={data.get("pose_source")} '
                                              f'armed={data.get("armed")} mode={data.get("mode")}')
                                except (TypeError, ValueError):
                                    journal.record('invalid_payload', {'reason': 'JSON non-finite or invalid'})
                    if last_received is not None and now - last_received >= 2.0 and gap_open is None:
                        gap_open = now
                        journal.record('gap_start', {'silence_after_last_telemetry_s': round(now-last_received, 3)})
                if gap_open is not None:
                    journal.record('gap_end', {'reason': 'observer_stopped', 'duration_s': round(time.monotonic()-gap_open,3)})
                print(f'SUMMARY: received={total}, journal_samples={samples}, pose_source_fg={flagged_fg}, reported_status={reported_status}')
                print(f'Journal={journal.path}')
            if not total:
                print('STATUS: BUS CONNECTED / NO TELEMETRY. Periksa simulator dan F1 bus.')
                return 3
            if not flagged_fg:
                print('STATUS: UI TELEMETRY RECEIVED; FG physical pose tidak terbukti dari pose_source.')
                return 4
            print('STATUS: LIVE UI BUS + FG POSE FLAG OBSERVED; direct raw FG/MAVLink NOT YET VERIFIED.')
            return 0
    except (ConnectionError, OSError, TimeoutError, BusError) as e:
        print(f'STATUS: BUS UNAVAILABLE: {e}')
        print('Jalankan demo simulator existing di terminal lain hingga WebSocket F1 :8765 aktif.')
        print('Tidak ada proses simulator yang dihentikan atau port production diambil alih.')
        return 2


def main():
    parser = argparse.ArgumentParser(description='OMNI R2 DEV-003 existing F1 bus observer (read-only)')
    parser.add_argument('--bus-port', type=int, default=8765)
    parser.add_argument('--seconds', type=float, default=15)
    args = parser.parse_args()
    try:
        raise SystemExit(observe(Path('.'), args.bus_port, args.seconds))
    except KeyboardInterrupt:
        print('\nObserver dihentikan pengguna; simulator existing tetap berjalan.')


if __name__ == '__main__':
    main()

