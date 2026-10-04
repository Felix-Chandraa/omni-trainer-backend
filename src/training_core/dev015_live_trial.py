"""DEV-015 automated real Flight command/authority proof.

Safety envelope:
- one worker only;
- no takeoff target;
- arm is confirmed from MAVLink heartbeat;
- throttle is increased in short stages only until measurable ground movement;
- server deadman releases RC override if command stream stops;
- release + disarm are executed before cleanup.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import json
import math
from pathlib import Path
import socket
import threading
import time
import uuid

import websockets

from .active_start import AuthorizedActiveStart
from .client_evidence_gateway import EvidenceLanTelemetryGateway
from .evidence_recorder import AttemptEvidenceRecorder, AttemptIdentity
from .fg_adapter import FGObserver
from .flight_command_router import FlightCommandRouter
from .mavlink_control import ControlMavlinkTlogRecorder
from .models import Actor, CoreError, Role, State
from .multi_live_trial import evidence, slot_ports_free, wait_tcp_cleanup, worker_argv
from .multi_runtime import (
    RuntimeSlotAllocator,
    extract_arduplane_template,
    find_latest_working_worker_log,
    validate_template,
)
from .omni_launcher_adapter import choose_jsbsim, source_from_root
from .readiness import TrustedReadinessCoordinator
from .session import SessionManager
from .store import Store
from .worker_lifecycle import AttemptWorkerCoordinator
from .evidence_extensions import attach_ardupilot_bins  # DEV016_BIN_ATTACHMENT

KNOT_TO_MPS = 0.514444


def free_tcp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    x = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 6371000.0 * 2.0 * math.atan2(math.sqrt(x), math.sqrt(max(0.0, 1.0 - x)))


def state_dict(p):
    return {
        "lat_deg": p.lat_deg,
        "lon_deg": p.lon_deg,
        "alt_msl_m": p.alt_msl_m,
        "agl_m": p.agl_raw_m,
        "roll_deg": p.roll_deg,
        "pitch_deg": p.pitch_deg,
        "yaw_deg": p.yaw_deg,
        "vcas_mps": None if p.vcas_kt is None else float(p.vcas_kt) * KNOT_TO_MPS,
        "climb_mps": p.climb_mps,
        "frame": "FGNetFDM",
    }


def visual_dict(p):
    return {
        "lat": p.lat_deg,
        "lon": p.lon_deg,
        "alt": p.agl_raw_m,
        "alt_msl": p.alt_msl_m,
        "agl": p.agl_raw_m,
        "roll": p.roll_deg,
        "pitch": p.pitch_deg,
        "yaw": p.yaw_deg,
        "hdg": p.yaw_deg % 360.0,
        "as": None if p.vcas_kt is None else float(p.vcas_kt) * KNOT_TO_MPS,
        "pose_source": "fg",
        "mode": "MANUAL",
    }


async def recv_until(ws, wanted: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = json.loads(
            await asyncio.wait_for(ws.recv(), max(0.1, deadline - time.monotonic()))
        )
        if msg.get("type") == wanted:
            return msg
    raise TimeoutError(f"timeout waiting for {wanted}")


async def recv_command_result(ws, command_id: str, timeout: float = 6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = json.loads(
            await asyncio.wait_for(ws.recv(), max(0.1, deadline - time.monotonic()))
        )
        if msg.get("type") == "command_result" and msg.get("command_id") == command_id:
            return msg
    raise TimeoutError(f"timeout command_result {command_id}")


async def send_command(
    ws,
    *,
    command_id: str,
    name: str,
    generation: int,
    epoch: int,
    sequence: int,
    payload: dict,
):
    await ws.send(
        json.dumps(
            {
                "type": "cmd",
                "protocol": "omni.training.v1",
                "command_id": command_id,
                "name": name,
                "generation": generation,
                "authority_epoch": epoch,
                "sequence": sequence,
                "client_mono_ns": time.monotonic_ns(),
                "expiry_ms": 300,
                "payload": payload,
            }
        )
    )
    return await recv_command_result(ws, command_id)


async def control_client(
    uri: str,
    token: str,
    motion_event: threading.Event,
    armed_event: threading.Event,
    settle_request_event: threading.Event,
    settled_event: threading.Event,
    done_event: threading.Event,
    result: dict,
):
    try:
        async with websockets.connect(uri, max_size=8192) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "protocol": "omni.training.v1",
                        "role": "student",
                        "student_id": "student-1",
                        "token": token,
                    }
                )
            )
            welcome = await recv_until(ws, "welcome")
            authority = await recv_until(ws, "authority")
            result["welcome"] = welcome
            result["authority"] = authority
            generation = int(authority["generation"])
            epoch = int(authority["epoch"])

            # Establish client->server monotonic mapping before commands.
            ping_id = "dev015-clock-1"
            await ws.send(
                json.dumps(
                    {
                        "type": "clock_ping",
                        "ping_id": ping_id,
                        "client_send_mono_ns": time.monotonic_ns(),
                        "client_utc_ms": int(time.time() * 1000),
                    }
                )
            )
            pong = await recv_until(ws, "clock_pong")
            await ws.send(
                json.dumps(
                    {
                        "type": "clock_sample",
                        "ping_id": pong["ping_id"],
                        "client_recv_mono_ns": time.monotonic_ns(),
                    }
                )
            )

            # 1) Wrong epoch must fail closed.
            wrong = await send_command(
                ws,
                command_id="dev015-wrong-epoch",
                name="release_axes",
                generation=generation,
                epoch=epoch + 99,
                sequence=1,
                payload={},
            )
            result["wrong_epoch"] = wrong
            if wrong.get("status") != "rejected" or wrong.get("reason") != "authority_epoch_mismatch":
                raise RuntimeError(f"wrong epoch did not fail closed: {wrong}")

            # 2) Correct neutral axes prove forwarding, then silence proves deadman.
            neutral = await send_command(
                ws,
                command_id="dev015-neutral-before-deadman",
                name="flight_axes",
                generation=generation,
                epoch=epoch,
                sequence=1,
                payload={"roll": 0.0, "pitch": 0.0, "yaw": 0.0, "throttle": 0.0},
            )
            if neutral.get("status") != "forwarded":
                raise RuntimeError(f"neutral axes not forwarded: {neutral}")
            result["neutral_forwarded"] = neutral
            await asyncio.sleep(0.75)

            # 3) Arm must be applied, not merely accepted.
            arm = await send_command(
                ws,
                command_id="dev015-arm",
                name="arm",
                generation=generation,
                epoch=epoch,
                sequence=2,
                payload={},
            )
            result["arm"] = arm
            if arm.get("status") != "applied":
                raise RuntimeError(f"arm not confirmed applied: {arm}")
            armed_event.set()

            # 4) Short, staged ground-roll command stream.
            seq = 3
            started = time.monotonic()
            last_id = None
            while time.monotonic() - started < 5.5 and not motion_event.is_set():
                elapsed = time.monotonic() - started
                throttle = 0.35 if elapsed < 1.5 else (0.50 if elapsed < 3.2 else 0.65)
                last_id = f"dev015-axis-{seq}"
                reply = await send_command(
                    ws,
                    command_id=last_id,
                    name="flight_axes",
                    generation=generation,
                    epoch=epoch,
                    sequence=seq,
                    payload={
                        "roll": 0.0,
                        "pitch": 0.0,
                        "yaw": 0.0,
                        "throttle": throttle,
                    },
                )
                if reply.get("status") != "forwarded":
                    raise RuntimeError(f"axis sample failed: {reply}")
                seq += 1
                await asyncio.sleep(0.04)
            result["last_axis_command_id"] = last_id
            result["motion_seen_by_client"] = motion_event.is_set()

            # Controlled stop: keep an active neutral override with throttle=0
            # while authoritative FG determines that ground motion has settled.
            # This is deliberately before release/disarm so a stale positive
            # throttle value can never remain latched during stopping.
            settle_request_event.set()
            settle_deadline = time.monotonic() + 10.0
            while (
                time.monotonic() < settle_deadline
                and not settled_event.is_set()
            ):
                stop_id = f"dev015-stop-{seq}"
                stop_reply = await send_command(
                    ws,
                    command_id=stop_id,
                    name="flight_axes",
                    generation=generation,
                    epoch=epoch,
                    sequence=seq,
                    payload={
                        "roll": 0.0,
                        "pitch": 0.0,
                        "yaw": 0.0,
                        "throttle": 0.0,
                    },
                )
                if stop_reply.get("status") != "forwarded":
                    raise RuntimeError(
                        f"zero-throttle stop command failed: {stop_reply}"
                    )
                seq += 1
                await asyncio.sleep(0.08)

            result["fg_settled_before_disarm"] = settled_event.is_set()

            release = await send_command(
                ws,
                command_id="dev015-release",
                name="release_axes",
                generation=generation,
                epoch=epoch,
                sequence=seq,
                payload={},
            )
            result["release"] = release
            seq += 1
            if release.get("status") != "applied":
                raise RuntimeError(f"release not applied: {release}")

            # Give the flight stack a short scheduling window after release.
            await asyncio.sleep(0.35)

            disarm = await send_command(
                ws,
                command_id="dev015-disarm",
                name="disarm",
                generation=generation,
                epoch=epoch,
                sequence=seq,
                payload={},
            )
            result["disarm"] = disarm
            if disarm.get("status") != "applied":
                raise RuntimeError(f"disarm not confirmed applied: {disarm}")

            done_event.set()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        done_event.set()


def install_control_endpoint(recorder, port: int):
    endpoint = ControlMavlinkTlogRecorder(
        bind_host="127.0.0.1",
        port=port,
        path=recorder.package_root / "server" / "aircraft.tlog",
        identity=recorder.identity,
    )
    endpoint.start()
    recorder._tlog = endpoint
    recorder.record_event(
        "mavlink_control_tlog_started",
        {
            "port": port,
            "path": "server/aircraft.tlog",
            "bidirectional_control": True,
        },
    )
    return endpoint


def run_trial(base: Path, source_root: Path) -> int:
    source = source_from_root(source_root)
    jsbsim = choose_jsbsim(source, home=Path.home())
    proven = find_latest_working_worker_log(base)
    template = extract_arduplane_template(proven)
    validate_template(template)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    runtime = base / "runtime" / "dev015-live" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    runtime.mkdir(parents=True, exist_ok=False)
    template_json = runtime / "arduplane-template.json"
    template_json.write_text(
        json.dumps(
            {
                "source_worker_log": str(proven),
                "arduplane_argv": list(template),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    store = Store(runtime / "training.sqlite")
    sessions = SessionManager(store)
    workers = AttemptWorkerCoordinator(store, runtime / "workers")
    readiness = TrustedReadinessCoordinator(store, sessions, workers)
    active_gate = AuthorizedActiveStart(store, sessions, workers)
    slots = RuntimeSlotAllocator(runtime / "slot-leases", max_slots=3)
    instructor = Actor("dev015-instructor", Role.INSTRUCTOR)

    recorder = None
    endpoint = None
    router = None
    gateway = None
    attempt = None
    lease = None
    worker_started = False
    accepted = False
    # DEV015_REV7_ACCEPTANCE_SNAPSHOT
    deadman_releases_observed = 0

    try:
        sid = sessions.create_session(instructor, "student-1")
        eid = sessions.create_exercise(instructor, sid, "dev015-flight-control-v1")
        attempt = sessions.create_attempt(instructor, eid, "omni-1")
        lease = slots.claim(
            attempt_id=attempt.id,
            session_id=sid,
            aircraft_id=attempt.aircraft_id,
            preferred_slot=1,
        )
        if not slot_ports_free(lease):
            raise CoreError("slot 1 busy before DEV-015")

        recorder = AttemptEvidenceRecorder(
            runtime / "evidence",
            AttemptIdentity(
                session_id=sid,
                exercise_id=eid,
                attempt_id=attempt.id,
                aircraft_id=attempt.aircraft_id,
                generation=attempt.generation,
            ),
            configuration={
                "dev": "DEV-015",
                "proof": "authorized_ground_roll",
                "flight_commands_enabled": True,
                "takeoff_target": False,
                "deadman_ms": 350,
            },
        )
        recorder.record_lifecycle("Setup", revision=attempt.revision)
        endpoint = install_control_endpoint(recorder, lease.slot.mav_client_udp)

        ws_port = free_tcp_port()
        router = FlightCommandRouter(
            store,
            deadman_ms=350,
            allow_test_force_arm=True,
        )
        gateway = EvidenceLanTelemetryGateway("127.0.0.1", ws_port)
        gateway.set_command_router(router)

        assignment = gateway.register_assignment(
            student_id="student-1",
            session_id=sid,
            attempt_id=attempt.id,
            aircraft_id=attempt.aircraft_id,
            generation=attempt.generation,
            token="dev015-loopback-token",
        )
        principal = gateway.register_evidence_principal(
            assignment,
            recorder,
            station_id="student-station-1",
            training_role="FLIGHT",
        )
        router.register_attempt(
            attempt_id=attempt.id,
            generation=attempt.generation,
            recorder=recorder,
            endpoint=endpoint,
        )

        with FGObserver(lease.slot.fg_udp) as fg:
            health = workers.start(
                instructor,
                attempt.id,
                argv=worker_argv(
                    base,
                    source,
                    jsbsim,
                    template_json,
                    lease,
                    runtime / "direct" / attempt.id,
                ),
                cwd=base,
            )
            worker_started = True

            ev = evidence(source, base, fg, workers, instructor, attempt, lease, 60.0)
            ready, _ = readiness.mark_ready(
                instructor,
                attempt.id,
                ev,
                expected_revision=attempt.revision,
            )
            recorder.record_lifecycle("Ready", revision=ready.revision)

            ev2 = evidence(source, base, fg, workers, instructor, attempt, lease, 15.0)
            active, _ = active_gate.start_active(
                instructor,
                attempt.id,
                ev2,
                expected_revision=ready.revision,
            )
            recorder.record_lifecycle("Active", revision=active.revision)

            if not endpoint.wait_peer(timeout=5.0).peer_ready:
                raise CoreError("control MAVLink peer unavailable")

            authority = router.grant_principal(
                principal,
                reason="DEV-015 assigned student Flight authority",
            )
            recorder.record_event(
                "dev015_authority_ready",
                authority,
                actor_id="dev015-instructor",
                station_id="server",
            )

            gateway.start()

            motion_event = threading.Event()
            armed_event = threading.Event()
            settle_request_event = threading.Event()
            settled_event = threading.Event()
            done_event = threading.Event()
            client_result: dict = {}
            client_thread = threading.Thread(
                target=lambda: asyncio.run(
                    control_client(
                        f"ws://127.0.0.1:{ws_port}",
                        assignment.token,
                        motion_event,
                        armed_event,
                        settle_request_event,
                        settled_event,
                        done_event,
                        client_result,
                    )
                ),
                daemon=True,
                name="Dev015ControlClient",
            )
            release_before = endpoint.release_count
            deadman_before = router.deadman_releases
            client_thread.start()

            baseline = None
            max_distance = 0.0
            states = 0
            settle_samples: list[tuple[float, float, float]] = []
            settle_window_s = 1.5
            settle_distance_m = 0.20
            deadline = time.monotonic() + 32.0
            while time.monotonic() < deadline:
                if not workers.health(instructor, attempt.id).healthy:
                    raise CoreError("worker unhealthy during control proof")
                pose = fg.drain()
                if pose is not None:
                    recorder.record_state(state_dict(pose), source="FGNetFDM")
                    gateway.publish_telemetry(
                        assignment,
                        visual_dict(pose),
                        quality={"pose_source": "fg", "dev": "DEV-015"},
                    )
                    states += 1
                    if armed_event.is_set():
                        if baseline is None:
                            baseline = (pose.lat_deg, pose.lon_deg)
                        else:
                            d = distance_m(
                                baseline,
                                (pose.lat_deg, pose.lon_deg),
                            )
                            max_distance = max(max_distance, d)
                            if d >= 0.75:
                                motion_event.set()

                    if settle_request_event.is_set():
                        now_sample = time.monotonic()
                        settle_samples.append(
                            (now_sample, pose.lat_deg, pose.lon_deg)
                        )
                        cutoff = now_sample - settle_window_s
                        settle_samples = [
                            row
                            for row in settle_samples
                            if row[0] >= cutoff
                        ]
                        if (
                            len(settle_samples) >= 2
                            and settle_samples[-1][0]
                            - settle_samples[0][0]
                            >= settle_window_s * 0.90
                        ):
                            drift = distance_m(
                                (
                                    settle_samples[0][1],
                                    settle_samples[0][2],
                                ),
                                (
                                    settle_samples[-1][1],
                                    settle_samples[-1][2],
                                ),
                            )
                            if drift <= settle_distance_m:
                                settled_event.set()
                if done_event.is_set() and states >= 30:
                    break
                time.sleep(1.0 / 60.0)

            client_thread.join(timeout=2.0)
            if client_thread.is_alive():
                raise CoreError("control client did not finish")
            if client_result.get("error"):
                raise CoreError("control client failed: " + client_result["error"])
            if not motion_event.is_set():
                raise CoreError(
                    f"authorized throttle produced no measurable ground movement; max={max_distance:.3f}m"
                )
            if not client_result.get("fg_settled_before_disarm", False):
                recorder.record_event(
                    "dev015_settle_timeout",
                    {
                        "settle_window_s": settle_window_s,
                        "settle_distance_m": settle_distance_m,
                    },
                    visibility="server",
                )
            if max_distance > 80.0:
                raise CoreError(
                    f"safety envelope exceeded during ground-roll proof: {max_distance:.1f}m"
                )
            if endpoint.release_count <= release_before:
                raise CoreError("release mechanism was not exercised")
            if router.deadman_releases <= deadman_before:
                raise CoreError("server deadman did not release stale axis override")
            deadman_releases_observed = router.deadman_releases
            if deadman_releases_observed < 1:
                raise CoreError(
                    "server deadman was not proven; "
                    f"deadman_releases={deadman_releases_observed}"
                )

            if endpoint.vehicle_status().armed is not False:
                raise CoreError("aircraft not confirmed disarmed at end of proof")

            last_axis = client_result.get("last_axis_command_id")
            if last_axis:
                router.confirm_effect(
                    attempt_id=attempt.id,
                    command_id=last_axis,
                    principal=principal,
                    reason=f"FG ground displacement observed: {max_distance:.3f}m",
                )

            current = store.attempt(attempt.id)
            ended = sessions.transition(
                instructor,
                attempt.id,
                State.ENDED,
                expected_revision=current.revision,
                reason="DEV-015 authorized ground-roll proof complete",
            )
            recorder.record_lifecycle("Ended", revision=ended.revision)

        gateway.stop()
        gateway = None
        deadman_releases_observed = max(
            deadman_releases_observed,
            router.deadman_releases,
        )
        router.shutdown()
        router = None
        workers.stop(instructor, attempt.id, timeout=15.0)
        worker_started = False
        wait_tcp_cleanup(lease, timeout=12.0)
        slots.release(attempt.id)

        # DEV016_BIN_ATTACHMENT: attach DataFlash before index finalization.
        bin_evidence = attach_ardupilot_bins(recorder, runtime, attempt.id)
        print("ARDUPILOT BIN EVIDENCE:", len(bin_evidence), "file(s)")
        index_path = recorder.finalize(status="complete")
        index = json.loads(index_path.read_text())
        package = index_path.parent

        command_meta = index["channels"].get("server/commands", {})
        authority_meta = index["channels"].get("server/authority", {})
        state_meta = index["channels"].get("server/state", {})
        tlog = index.get("mavlink_tlog") or {}

        if command_meta.get("records_written", 0) < 8:
            raise CoreError("too few command evidence records")
        if authority_meta.get("records_written", 0) < 1:
            raise CoreError("authority evidence missing")
        if state_meta.get("records_written", 0) < 30:
            raise CoreError("state evidence too small")
        if tlog.get("frames", 0) < 10:
            raise CoreError("MAVLink tlog too small")
        if not index.get("overall_complete"):
            raise CoreError("evidence package incomplete")

        print(
            "AUTHORITY VERIFIED:",
            "actor=student-1 station=student-station-1 scope=flight",
        )
        print(
            "STALE EPOCH VERIFIED:",
            client_result["wrong_epoch"].get("reason"),
        )
        arm_reason = client_result["arm"].get("reason")
        disarm_reason = client_result["disarm"].get("reason")
        print(
            "ARM/DISARM VERIFIED:",
            f"arm={client_result['arm'].get('status')}",
            f"disarm={client_result['disarm'].get('status')}",
        )
        print(
            "FG SETTLE BEFORE DISARM:",
            client_result.get("fg_settled_before_disarm"),
        )
        if arm_reason:
            print("ARM DIAGNOSTIC:", arm_reason)
        if disarm_reason:
            print("DISARM DIAGNOSTIC:", disarm_reason)
        if (
            isinstance(disarm_reason, str)
            and (
                disarm_reason.startswith(
                    "test_force_disarm_after_regular_failure:"
                )
                or disarm_reason.startswith(
                    "test_force_disarm_after_confirmation_timeout:"
                )
            )
        ):
            print(
                "SITL TEST-ONLY FORCE-DISARM FALLBACK:",
                "USED (manual/product path remains normal-disarm only)",
            )
        if (
            isinstance(arm_reason, str)
            and arm_reason.startswith(
                "test_force_arm_after_regular_failure:"
            )
        ):
            print(
                "SITL TEST-ONLY FORCE-ARM FALLBACK:",
                "USED (manual/product path remains normal-arm only)",
            )
        print(
            "DEADMAN/RELEASE VERIFIED:",
            f"release_count={endpoint.release_count}",
            f"deadman_releases={deadman_releases_observed}",
        )
        print(
            "REAL CONTROL EFFECT:",
            f"ground_displacement={max_distance:.3f}m",
        )
        print(
            "EVIDENCE:",
            f"commands={command_meta.get('records_written')}",
            f"states={state_meta.get('records_written')}",
            f"tlog_frames={tlog.get('frames')}",
        )
        print("EVIDENCE PACKAGE:", package)
        fallback_labels = []
        if (
            isinstance(arm_reason, str)
            and arm_reason.startswith("test_force_arm_")
        ):
            fallback_labels.append("force-arm")
        if (
            isinstance(disarm_reason, str)
            and disarm_reason.startswith("test_force_disarm_")
        ):
            fallback_labels.append("force-disarm")

        if fallback_labels:
            print(
                "STATUS: DEV-015 ACCEPTED "
                "(engineering; SITL test-only "
                + "/".join(fallback_labels)
                + " fallback used)"
            )
        else:
            print("STATUS: DEV-015 ACCEPTED (engineering)")
        print(
            "Proof: authenticated Flight authority -> validated command -> "
            "MAVLink RC override -> ArduPilot/JSBSim -> real FG ground movement."
        )
        accepted = True
        return 0

    except Exception as exc:
        print("STATUS: DEV-015 FAILED:", f"{type(exc).__name__}: {exc}")
        return 151
    finally:
        if router is not None:
            try:
                router.shutdown()
            except Exception:
                pass
        if gateway is not None:
            try:
                gateway.stop()
            except Exception:
                pass
        if endpoint is not None:
            try:
                endpoint.release_override()
            except Exception:
                pass
            try:
                if endpoint.vehicle_status().armed:
                    endpoint.send_arm_disarm(False)
                    endpoint.wait_armed(False, timeout=2.0)
            except Exception:
                pass
        if worker_started and attempt is not None:
            try:
                workers.stop(instructor, attempt.id, timeout=15.0)
            except Exception:
                pass
        if attempt is not None:
            try:
                slots.release(attempt.id)
            except Exception:
                pass
        if recorder is not None and not recorder._finalized:
            try:
                recorder.abort(
                    "DEV-015 accepted" if accepted else "DEV-015 live proof aborted"
                )
            except Exception:
                pass
        store.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--source", required=True)
    a = p.parse_args(argv)
    return run_trial(
        Path(a.base).expanduser().resolve(),
        Path(a.source).expanduser().resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
