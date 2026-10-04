"""Independent read-only FGNetFDM v24 decoder/UDP receiver; no Qt dependency.

Frame decoding follows existing src/fg_link/fgnetfdm.py. Only FG supplies physical
pose; a MAVLink state observer must never overwrite it implicitly.
"""
from __future__ import annotations

import math
import socket
import struct
from dataclasses import asdict, dataclass

FRAME_SIZE = 408  # FGNetFDM v24 packet from OMNI existing source


class FGParseError(ValueError):
    pass


@dataclass(frozen=True)
class FGPose:
    lat_deg: float
    lon_deg: float
    alt_msl_m: float
    agl_raw_m: float
    roll_deg: float
    pitch_deg: float
    yaw_deg: float
    vcas_kt: float
    climb_mps: float
    packet_len: int

    def as_payload(self) -> dict:
        return asdict(self)


def parse_fdm_v24(raw: bytes) -> FGPose:
    if len(raw) != FRAME_SIZE:
        raise FGParseError(f"ukuran paket {len(raw)} byte; diharapkan {FRAME_SIZE}")
    version = struct.unpack_from('>I', raw, 0)[0]
    if version != 24:
        raise FGParseError(f"version={version}; diperlukan FGNetFDM v24")
    lon_rad, lat_rad, alt = struct.unpack_from('>ddd', raw, 8)
    agl, roll, pitch, yaw = struct.unpack_from('>ffff', raw, 32)
    vcas, climb = struct.unpack_from('>ff', raw, 68)
    values = (lon_rad, lat_rad, alt, agl, roll, pitch, yaw, vcas, climb)
    if not all(math.isfinite(x) for x in values):
        raise FGParseError('NaN/Infinity dalam frame')
    lat, lon = math.degrees(lat_rad), math.degrees(lon_rad)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise FGParseError('latitude/longitude tidak valid')
    return FGPose(lat, lon, alt, agl, math.degrees(roll), math.degrees(pitch),
                  math.degrees(yaw), vcas, climb * 0.3048, len(raw))


class FGObserver:
    """Binds one *exclusive* local UDP endpoint; no SO_REUSEADDR/REUSEPORT."""
    def __init__(self, port: int, host: str = '127.0.0.1'):
        if not 0 <= port <= 65535 or host != '127.0.0.1':
            raise ValueError('observer menerima UDP localhost dan port valid saja')
        self.host, self.port = host, port
        self.sock = None
        self.rx_total = 0
        self.bad_total = 0

    def __enter__(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.host, self.port))
            sock.setblocking(False)
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        return self

    def drain(self) -> FGPose | None:
        """Drain backlog; report latest valid pose without changing physics."""
        if self.sock is None:
            raise RuntimeError('FGObserver belum dibuka')
        latest = None
        while True:
            try:
                packet, _ = self.sock.recvfrom(4096)
            except BlockingIOError:
                break
            self.rx_total += 1
            try:
                latest = parse_fdm_v24(packet)
            except FGParseError:
                self.bad_total += 1
        return latest

    def __exit__(self, *_):
        if self.sock:
            self.sock.close()
            self.sock = None
