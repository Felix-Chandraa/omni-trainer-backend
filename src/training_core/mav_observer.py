"""Non-command MAVLink state observer. Importing requires no pymavlink.

Live reads require pymavlink already installed on the user's own simulator env.
No heartbeat request, stream request, arming, mission or MAVLink send occurs.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class MAVState:
    msg_type: str
    data: dict

    def as_payload(self) -> dict:
        return asdict(self)


def decode_message(msg) -> MAVState | None:
    kind = msg.get_type()
    if kind == 'HEARTBEAT':
        return MAVState(kind, {'armed': bool(int(msg.base_mode) & 128),
                               'custom_mode': int(msg.custom_mode),
                               'vehicle_type': int(msg.type),
                               'system_status': int(msg.system_status)})
    if kind == 'GLOBAL_POSITION_INT':
        return MAVState(kind, {'lat_deg': int(msg.lat) / 1e7,
                               'lon_deg': int(msg.lon) / 1e7,
                               'alt_msl_m': int(msg.alt) / 1000.0,
                               'relative_alt_m': int(msg.relative_alt) / 1000.0,
                               'heading_deg': None if int(msg.hdg) == 65535 else int(msg.hdg) / 100.0})
    if kind == 'GPS_RAW_INT':
        return MAVState(kind, {'fix_type': int(msg.fix_type),
                               'satellites_visible': int(msg.satellites_visible)})
    if kind == 'SYS_STATUS':
        return MAVState(kind, {'voltage_battery_mv': int(msg.voltage_battery),
                               'battery_remaining_pct': int(msg.battery_remaining)})
    return None


class MAVObserver:
    """Receive-only localhost UDP endpoint. Never use to dispatch commands."""
    def __init__(self, port: int):
        if not 1 <= port <= 65535:
            raise ValueError('invalid MAVLink port')
        self.port = port
        self.link = None
        self.raw_total = 0

    def __enter__(self):
        try:
            from pymavlink import mavutil
        except ImportError as e:
            raise RuntimeError('pymavlink tidak ada di Python ini. Aktifkan venv simulator dahulu untuk --live.') from e
        # udpin binds but does not transmit; only local endpoint is accepted.
        self.link = mavutil.mavlink_connection(f'udpin:127.0.0.1:{self.port}',
                                               autoreconnect=False)
        return self

    def drain(self, limit: int = 100) -> list[MAVState]:
        if self.link is None:
            raise RuntimeError('MAVObserver belum dibuka')
        result = []
        for _ in range(limit):
            msg = self.link.recv_match(blocking=False)
            if msg is None:
                break
            self.raw_total += 1
            decoded = decode_message(msg)
            if decoded is not None:
                result.append(decoded)
        return result

    def __exit__(self, *_):
        if self.link:
            self.link.close()
            self.link = None
