"""DEV-002 offline demo and optional *read-only* live attach.

Live receiver only observes packets already forwarded to its dedicated UDP ports.
It does not launch a worker, reconfigure MAVProxy or take control of aircraft.
"""
from __future__ import annotations
import argparse
import json
import math
import socket
import struct
import tempfile
import time
import uuid
from pathlib import Path

from .fg_adapter import FGObserver, parse_fdm_v24
from .mav_observer import MAVObserver, decode_message
from .models import Actor, Role
from .session import SessionManager
from .state_journal import StateJournal
from .store import Store


def make_synthetic_fg() -> bytes:
    raw = bytearray(408)
    struct.pack_into('>I', raw, 0, 24)
    struct.pack_into('>ddd', raw, 8, math.radians(108.2464), math.radians(-7.347), 349.4)
    struct.pack_into('>ffff', raw, 32, 0.4, 0.01, -0.02, math.radians(148.1))
    struct.pack_into('>ff', raw, 68, 42.0, 2.0)
    return bytes(raw)


def offline_demo(root: Path):
    """One independent Session + diagnostic frames, explicitly synthetic."""
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / f'dev002-{uuid.uuid4().hex}.sqlite'
    store = Store(db_path)
    try:
        manager = SessionManager(store)
        instructor = Actor('instructor-demo', Role.INSTRUCTOR)
        sid = manager.create_session(instructor, 'student-demo')
        eid = manager.create_exercise(instructor, sid, 'r2-diagnostic@synthetic-v1')
        attempt = manager.create_attempt(instructor, eid, 'omni-demo-1')
        pose = parse_fdm_v24(make_synthetic_fg())
        class FakeHeartbeat:
            base_mode = 0
            custom_mode = 0
            type = 1
            system_status = 3
            def get_type(self):
                return 'HEARTBEAT'
        mav_state = decode_message(FakeHeartbeat())
        with StateJournal(root, attempt.id, attempt.aircraft_id) as journal:
            fg_row = journal.record('fg_pose', pose.as_payload(), synthetic=True)
            mav_row = journal.record('mavlink_state', mav_state.as_payload(), synthetic=True)
            log_path = journal.path
        print(f'Session={sid}  Exercise={eid}')
        print(f'Attempt={attempt.id}  generation={attempt.generation}  state={attempt.state.value}')
        print(f'FG synthetic pose: {pose.lat_deg:.4f}, {pose.lon_deg:.4f}, alt={pose.alt_msl_m:.1f} m')
        print(f'MAV synthetic HEARTBEAT: armed={mav_state.data["armed"]}')
        print(f'Journal: 2 records, seq={fg_row["seq"]},{mav_row["seq"]}; synthetic=true')
        print(f'Database: {db_path}')
        print(f'JSONL:    {log_path}')
    finally:
        store.close()


def live(root: Path, fg_port: int, mav_port: int, seconds: float):
    if fg_port == mav_port:
        raise ValueError('port FG dan MAVLink harus berbeda')
    if not 1 <= seconds <= 120:
        raise ValueError('durasi harus 1–120 detik')
    attempt_id = 'diagnostic-' + uuid.uuid4().hex
    fg_count = mav_count = 0
    start = time.monotonic()
    # Both endpoints are created exclusively. Busy FG port must fail instead
    # of stealing packets from the production frontend.
    with FGObserver(fg_port) as fg, MAVObserver(mav_port) as mav, StateJournal(root, attempt_id, 'omni-diagnostic') as journal:
        print(f'Read-only listening: FG UDP {fg_port}, MAVLink UDP {mav_port}, selama {seconds:g} detik')
        print('Tidak ada command dan tidak ada permintaan stream. Ctrl+C untuk berhenti.')
        last_fg_record = 0.0
        try:
            while time.monotonic() - start < seconds:
                pose = fg.drain()
                now = time.monotonic()
                if pose is not None and now - last_fg_record >= 1.0 / 30.0:
                    journal.record('fg_pose', pose.as_payload())
                    fg_count += 1
                    last_fg_record = now
                for msg in mav.drain():
                    journal.record('mavlink_state', msg.as_payload())
                    mav_count += 1
                time.sleep(0.01)
        except KeyboardInterrupt:
            print('\nObservasi dihentikan pengguna.')
        print(f'FG valid raw received={fg.rx_total-fg.bad_total}, invalid={fg.bad_total}, recorded={fg_count}')
        print(f'MAVLink received raw={mav.raw_total}, decoded journal records={mav_count}')
        print(f'Journal: {journal.path}')
    if not fg_count and not mav_count:
        print('STATUS: NO DATA. Periksa apakah stream diteruskan ke port tersebut; bukan simulator lulus.')
    elif not fg_count or not mav_count:
        print('STATUS: PARTIAL STREAM. Tidak boleh dianggap feed dual-channel lengkap.')
    else:
        print('STATUS: menerima dua channel dalam observasi; belum validasi flight/training.')


def main():
    parser = argparse.ArgumentParser(description='OMNI R2 DEV-002 telemetry observer (no commands)')
    parser.add_argument('--live', action='store_true', help='observe actual streams, never launch simulator')
    parser.add_argument('--fg-port', type=int, default=15504)
    parser.add_argument('--mav-port', type=int, default=15555)
    parser.add_argument('--seconds', type=float, default=10.0)
    parser.add_argument('--output', type=Path, default=Path('runtime') / 'dev002')
    args = parser.parse_args()
    if not args.live:
        offline_demo(args.output)
        return
    try:
        live(args.output, args.fg_port, args.mav_port, args.seconds)
    except (OSError, RuntimeError, ValueError) as e:
        parser.exit(2, f'ERROR: {e}\nPort mungkin dipakai UI lama. Jangan hentikan proses dengan pkill otomatis.\n')


if __name__ == '__main__':
    main()
