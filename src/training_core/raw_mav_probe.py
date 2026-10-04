"""DEV-006 direct raw MAVLink receive-only probe.

This probe binds a declared loopback UDP output and waits for one HEARTBEAT.
It never sends stream requests, commands, missions, parameters, arming,
mode changes, RC overrides, or any other MAVLink message.
"""
from __future__ import annotations

import argparse
import json
import time


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--conn", default="udpin:127.0.0.1:14555")
    p.add_argument("--timeout", type=float, default=10.0)
    a = p.parse_args(argv)

    if not a.conn.startswith("udpin:127.0.0.1:"):
        print(json.dumps({"ok": False, "error": "loopback UDP input only"}))
        return 2
    if not (0.5 <= a.timeout <= 30):
        print(json.dumps({"ok": False, "error": "timeout out of range"}))
        return 2

    try:
        from pymavlink import mavutil
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"pymavlink unavailable: {type(exc).__name__}"}))
        return 3

    started = time.monotonic()
    link = None
    try:
        link = mavutil.mavlink_connection(a.conn, autoreconnect=False)
        hb = link.wait_heartbeat(timeout=a.timeout)
        if hb is None:
            print(json.dumps({"ok": False, "error": "heartbeat timeout", "conn": a.conn}))
            return 4

        row = {
            "ok": True,
            "conn": a.conn,
            "msg_type": hb.get_type(),
            "system_id": int(link.target_system),
            "component_id": int(link.target_component),
            "vehicle_type": int(hb.type),
            "autopilot": int(hb.autopilot),
            "base_mode": int(hb.base_mode),
            "custom_mode": int(hb.custom_mode),
            "system_status": int(hb.system_status),
            "armed": bool(int(hb.base_mode) & 128),
            "elapsed_s": round(time.monotonic() - started, 3),
            "read_only": True,
        }
        print(json.dumps(row, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}", "conn": a.conn}))
        return 5
    finally:
        if link is not None:
            try:
                link.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
