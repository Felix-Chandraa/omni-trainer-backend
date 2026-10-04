"""Automated DEV-014 replay proof using the latest real DEV-013 evidence."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import socket
import time

import websockets

from .lan_protocol import PROTOCOL
from .replay_gateway import ReplayGateway
from .replay_reader import ReplayPackage


def latest_evidence(base: Path) -> Path:
    patterns = [
        "runtime/dev013-live/*/evidence/*/index.json",
        "runtime/dev012/*/evidence/*/index.json",
    ]
    candidates = []
    for pattern in patterns:
        candidates.extend(base.glob(pattern))
    if not candidates:
        raise RuntimeError(
            "no DEV-013/012 evidence package found"
        )
    return max(
        candidates,
        key=lambda p: p.stat().st_mtime,
    ).parent


def free_port() -> int:
    s = socket.socket(
        socket.AF_INET, socket.SOCK_STREAM
    )
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def ardupilot_processes() -> set[tuple[int, str]]:
    result = set()
    proc = Path("/proc")
    for item in proc.iterdir():
        if not item.name.isdigit():
            continue
        try:
            raw = (
                item / "cmdline"
            ).read_bytes().replace(b"\0", b" ")
            cmd = raw.decode(
                errors="replace"
            ).strip()
        except Exception:
            continue
        lower = cmd.lower()
        if "arduplane" in lower or "jsbsim" in lower:
            result.add((int(item.name), cmd))
    return result


async def _hello(ws, token: str) -> dict:
    await ws.send(
        json.dumps(
            {
                "type": "hello",
                "protocol": PROTOCOL,
                "role": "student",
                "student_id": "replay-viewer",
                "token": token,
            }
        )
    )
    welcome = json.loads(
        await asyncio.wait_for(ws.recv(), 5)
    )
    if welcome.get("type") != "welcome":
        raise RuntimeError(
            f"unexpected welcome: {welcome}"
        )
    return welcome


async def readonly_probe(
    uri: str,
    token: str,
) -> dict:
    """Prove aircraft commands are rejected on an isolated connection.

    The probe doesn't depend on replay telemetry ordering. Accelerated
    trajectory frames may arrive before the error and are simply ignored.
    """
    result = {
        "welcome": None,
        "error_code": None,
        "telemetry_seen": 0,
    }
    async with websockets.connect(
        uri, max_size=8192
    ) as ws:
        result["welcome"] = await _hello(
            ws, token
        )
        await ws.send(
            json.dumps(
                {
                    "type": "cmd",
                    "name": "arm",
                }
            )
        )
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            timeout = max(
                0.1,
                min(
                    2.0,
                    deadline - time.monotonic(),
                ),
            )
            try:
                msg = json.loads(
                    await asyncio.wait_for(
                        ws.recv(), timeout
                    )
                )
            except asyncio.TimeoutError:
                continue
            if msg.get("type") == "error":
                result["error_code"] = msg.get(
                    "code"
                )
                return result
            if msg.get("type") == "telemetry":
                result["telemetry_seen"] += 1
        return result


async def stream_probe(
    uri: str,
    token: str,
    max_frames: int,
) -> dict:
    """Prove the stored trajectory streams without sending any command."""
    result = {
        "welcome": None,
        "frames": [],
        "replay_end": False,
    }
    async with websockets.connect(
        uri, max_size=8192
    ) as ws:
        result["welcome"] = await _hello(
            ws, token
        )
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            timeout = max(
                0.1,
                min(
                    3.0,
                    deadline - time.monotonic(),
                ),
            )
            try:
                msg = json.loads(
                    await asyncio.wait_for(
                        ws.recv(), timeout
                    )
                )
            except asyncio.TimeoutError:
                continue
            if msg.get("type") == "telemetry":
                result["frames"].append(msg)
                if (
                    len(result["frames"])
                    >= max_frames
                ):
                    return result
            elif msg.get("type") == "replay_end":
                result["replay_end"] = True
                return result
            elif msg.get("type") == "error":
                raise RuntimeError(
                    "unexpected replay stream error: "
                    f"{msg.get('code')}"
                )
        return result


async def run_probes(
    uri: str,
    token: str,
    max_frames: int,
) -> dict:
    readonly = await readonly_probe(uri, token)
    if (
        readonly.get("error_code")
        != "replay_read_only"
    ):
        raise RuntimeError(
            "replay command path did not fail closed; "
            f"got={readonly.get('error_code')!r}, "
            f"telemetry_seen={readonly.get('telemetry_seen')}"
        )
    stream = await stream_probe(
        uri, token, max_frames
    )
    return {
        "readonly": readonly,
        "stream": stream,
    }


def run_trial(
    base: Path,
    evidence: Path | None,
) -> int:
    package_path = (
        evidence
        if evidence is not None
        else latest_evidence(base)
    )
    before = ardupilot_processes()
    package = ReplayPackage(package_path)
    port = free_port()
    gateway = ReplayGateway(
        package,
        host="127.0.0.1",
        port=port,
        token="dev014-replay-token",
        speed=200.0,
        loop_playback=False,
    )
    try:
        gateway.start()
        wanted = min(
            50, max(10, len(package.frames))
        )
        result = asyncio.run(
            run_probes(
                f"ws://127.0.0.1:{port}",
                gateway.token,
                wanted,
            )
        )
    finally:
        gateway.stop()
    after = ardupilot_processes()

    readonly = result["readonly"]
    stream = result["stream"]
    frames = stream["frames"]
    if len(frames) < min(
        10, len(package.frames)
    ):
        raise RuntimeError(
            f"too few replay frames: {len(frames)}"
        )
    if (
        readonly["error_code"]
        != "replay_read_only"
    ):
        raise RuntimeError(
            "replay command path did not fail closed"
        )
    seqs = [x["seq"] for x in frames]
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        raise RuntimeError(
            "replay sequence not monotonic"
        )
    if before != after:
        raise RuntimeError(
            "replay changed ArduPlane/JSBSim process set"
        )

    print(
        "REPLAY PACKAGE:",
        package.root,
    )
    print(
        "INTEGRITY:",
        f"verified_files={package.integrity.verified_files}",
        f"warnings={len(package.integrity.warnings)}",
    )
    for warning in package.integrity.warnings:
        print("  warning:", warning)
    print(
        "READ-ONLY PROBE:",
        f"rejected={readonly['error_code']}",
        f"telemetry_before_reject={readonly['telemetry_seen']}",
    )
    print(
        "REPLAY STREAM:",
        f"source_frames={len(package.frames)}",
        f"received={len(frames)}",
        f"duration={package.duration_s:.3f}s",
    )
    print(
        "COMMAND FAIL-CLOSED VERIFIED:",
        "replay_read_only",
    )
    print(
        "NO FDM RE-RUN VERIFIED:",
        "ArduPlane/JSBSim process set unchanged",
    )
    print(
        "STATUS: DEV-014A REPLAY CORE ACCEPTED "
        "(engineering)"
    )
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--evidence")
    a = p.parse_args(argv)
    return run_trial(
        Path(a.base).expanduser().resolve(),
        (
            Path(a.evidence).expanduser().resolve()
            if a.evidence
            else None
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
