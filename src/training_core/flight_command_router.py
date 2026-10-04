"""Validated Flight command transaction path for DEV-015."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import re
import threading
import time
from typing import Any

from .control_authority import AuthorityError, FlightAuthorityManager
from .mavlink_control import (
    ControlError,
    ControlMavlinkTlogRecorder,
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_RESULT_ACCEPTED,
)
from .models import State


SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass
class ControlBinding:
    attempt_id: str
    generation: int
    recorder: Any
    endpoint: ControlMavlinkTlogRecorder
    override_active: bool = False
    last_axis_monotonic_ns: int | None = None
    last_axis_command_id: str | None = None
    last_axis_payload: dict[str, float] | None = None


class FlightCommandRouter:
    def __init__(
        self,
        store,
        *,
        deadman_ms: int = 350,
        max_expiry_ms: int = 500,
        allow_test_force_arm: bool = False,
    ):
        self.store = store
        self.deadman_ns = int(deadman_ms) * 1_000_000
        self.max_expiry_ms = int(max_expiry_ms)
        # DEV015_REV4_TEST_FORCE_ARM
        self.allow_test_force_arm = bool(allow_test_force_arm)
        self.authority = FlightAuthorityManager()
        self._bindings: dict[str, ControlBinding] = {}
        self._last_sequence: dict[tuple[str, str, int], int] = {}
        self._request_utc_ns: dict[str, int] = {}
        self.deadman_releases = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="OmniFlightDeadman",
        )
        self._watchdog.start()

    def register_attempt(
        self,
        *,
        attempt_id: str,
        generation: int,
        recorder,
        endpoint: ControlMavlinkTlogRecorder,
    ) -> ControlBinding:
        binding = ControlBinding(
            attempt_id=attempt_id,
            generation=int(generation),
            recorder=recorder,
            endpoint=endpoint,
        )
        with self._lock:
            if attempt_id in self._bindings:
                raise RuntimeError("duplicate control binding")
            self._bindings[attempt_id] = binding
        return binding

    def grant_principal(self, principal, *, reason: str) -> dict[str, Any]:
        a = principal.assignment
        binding = self._bindings[a.attempt_id]
        lease = self.authority.grant(
            attempt_id=a.attempt_id,
            aircraft_id=a.aircraft_id,
            actor_id=principal.actor_id,
            station_id=principal.station_id,
            generation=a.generation,
            recorder=binding.recorder,
            reason=reason,
        )
        return lease.public()

    async def on_connect(self, ws, principal) -> None:
        lease = self.authority.current(principal.assignment.attempt_id)
        if lease is None:
            return
        await self._send(
            ws,
            {
                "type": "authority",
                "scope": "flight",
                "held": (
                    lease.actor_id == principal.actor_id
                    and lease.station_id == principal.station_id
                ),
                "owner_actor_id": lease.actor_id,
                "owner_station_id": lease.station_id,
                "epoch": lease.epoch,
                "generation": lease.generation,
                "aircraft_id": lease.aircraft_id,
                "attempt_id": lease.attempt_id,
                "deadman_ms": self.deadman_ns // 1_000_000,
                "commands": [
                    "flight_axes",
                    "release_axes",
                    "arm",
                    "disarm",
                ],
            },
        )

    async def on_disconnect(self, principal) -> None:
        attempt_id = principal.assignment.attempt_id
        lease = self.authority.current(attempt_id)
        if (
            lease is not None
            and lease.actor_id == principal.actor_id
            and lease.station_id == principal.station_id
        ):
            self.release_attempt(
                attempt_id,
                reason="authority_client_disconnected",
            )

    def _binding_for(self, principal) -> ControlBinding:
        attempt_id = principal.assignment.attempt_id
        with self._lock:
            binding = self._bindings.get(attempt_id)
        if binding is None:
            raise RuntimeError("control_binding_missing")
        return binding

    def _map_client_monotonic(
        self,
        recorder,
        station_id: str,
        client_mono_ns: int,
    ) -> tuple[int | None, int | None]:
        mapping = getattr(recorder, "_clock", {}).get(station_id)
        if mapping is None:
            return None, None
        return mapping.map_client_monotonic(client_mono_ns)

    def _validate_envelope(
        self,
        principal,
        incoming: dict[str, Any],
        binding: ControlBinding,
    ) -> tuple[str, str, int, int]:
        command_id = incoming.get("command_id")
        name = incoming.get("name")
        generation = incoming.get("generation")
        epoch = incoming.get("authority_epoch")
        sequence = incoming.get("sequence")
        client_mono_ns = incoming.get("client_mono_ns")
        expiry_ms = incoming.get("expiry_ms", 250)

        if not isinstance(command_id, str) or not SAFE_ID.match(command_id):
            raise ValueError("invalid_command_id")
        if name not in {"flight_axes", "release_axes", "arm", "disarm"}:
            raise ValueError("unsupported_flight_command")
        if generation != principal.assignment.generation:
            raise ValueError("generation_mismatch")
        if not isinstance(epoch, int):
            raise ValueError("authority_epoch_missing")
        self.authority.validate(
            attempt_id=principal.assignment.attempt_id,
            actor_id=principal.actor_id,
            station_id=principal.station_id,
            generation=generation,
            epoch=epoch,
        )

        attempt = self.store.attempt(principal.assignment.attempt_id)
        if attempt.state != State.ACTIVE:
            raise ValueError("attempt_not_active")

        if not isinstance(sequence, int) or sequence < 1:
            raise ValueError("invalid_sequence")
        seq_key = (
            principal.assignment.attempt_id,
            principal.station_id,
            int(epoch),
        )
        with self._lock:
            previous = self._last_sequence.get(seq_key, 0)
            if sequence <= previous:
                raise ValueError("stale_command_sequence")

        if not isinstance(client_mono_ns, int):
            raise ValueError("client_time_missing")
        if not isinstance(expiry_ms, int) or not (50 <= expiry_ms <= self.max_expiry_ms):
            raise ValueError("invalid_expiry")
        mapped, uncertainty = self._map_client_monotonic(
            binding.recorder,
            principal.station_id,
            client_mono_ns,
        )
        if mapped is None:
            raise ValueError("clock_unsynchronized")
        now = time.monotonic_ns()
        age_ns = now - mapped
        if age_ns > expiry_ms * 1_000_000:
            raise ValueError("stale_command")
        if age_ns < -100_000_000:
            raise ValueError("command_time_in_future")
        with self._lock:
            self._last_sequence[seq_key] = sequence
        return command_id, str(name), int(epoch), int(sequence)

    def _validate_axes(self, payload: Any) -> dict[str, float]:
        if not isinstance(payload, dict):
            raise ValueError("invalid_axes_payload")
        result = {}
        for key in ("roll", "pitch", "yaw"):
            value = payload.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"invalid_{key}")
            value = float(value)
            if value < -1.0 or value > 1.0:
                raise ValueError(f"{key}_out_of_range")
            result[key] = value
        throttle = payload.get("throttle")
        if not isinstance(throttle, (int, float)) or not math.isfinite(float(throttle)):
            raise ValueError("invalid_throttle")
        throttle = float(throttle)
        if throttle < 0.0 or throttle > 1.0:
            raise ValueError("throttle_out_of_range")
        result["throttle"] = throttle
        return result

    def _record(
        self,
        binding: ControlBinding,
        principal,
        *,
        command_id: str,
        name: str,
        payload: dict[str, Any],
        disposition: str,
        reason: str | None = None,
        applied: bool = False,
    ) -> None:
        binding.recorder.record_command(
            command_id=command_id,
            actor_id=principal.actor_id,
            station_id=principal.station_id,
            scope="flight",
            name=name,
            payload=payload,
            disposition=disposition,
            request_utc_ns=self._request_utc_ns.get(command_id),
            applied_utc_ns=(time.time_ns() if applied else None),
            authority_epoch=(
                self.authority.current(binding.attempt_id).epoch
                if self.authority.current(binding.attempt_id)
                else None
            ),
            reason=reason,
        )

    async def handle(self, ws, principal, incoming: dict[str, Any]) -> None:
        binding = self._binding_for(principal)
        command_id = incoming.get("command_id")
        if isinstance(command_id, str):
            self._request_utc_ns.setdefault(command_id, time.time_ns())
        name = incoming.get("name")
        payload = incoming.get("payload", {})
        try:
            command_id, name, epoch, sequence = self._validate_envelope(
                principal, incoming, binding
            )
        except (ValueError, AuthorityError, RuntimeError) as exc:
            reason = str(exc)
            if isinstance(command_id, str) and isinstance(name, str):
                try:
                    self._record(
                        binding,
                        principal,
                        command_id=command_id,
                        name=name,
                        payload=(payload if isinstance(payload, dict) else {}),
                        disposition="rejected",
                        reason=reason,
                    )
                except Exception:
                    pass
            await self._result(
                ws,
                command_id=(command_id if isinstance(command_id, str) else ""),
                name=(name if isinstance(name, str) else ""),
                status="rejected",
                reason=reason,
            )
            return

        if name == "flight_axes":
            try:
                axes = self._validate_axes(payload)
                self._record(
                    binding,
                    principal,
                    command_id=command_id,
                    name=name,
                    payload=axes,
                    disposition="accepted",
                )
                pwm = binding.endpoint.send_axes(**axes)
                with self._lock:
                    binding.override_active = True
                    binding.last_axis_monotonic_ns = time.monotonic_ns()
                    binding.last_axis_command_id = command_id
                    binding.last_axis_payload = dict(axes)
                self._record(
                    binding,
                    principal,
                    command_id=command_id,
                    name=name,
                    payload={**axes, "pwm_ch1_4": list(pwm[:4])},
                    disposition="forwarded",
                )
                await self._result(
                    ws,
                    command_id=command_id,
                    name=name,
                    status="forwarded",
                    extra={"pwm_ch1_4": list(pwm[:4])},
                )
            except (ValueError, ControlError) as exc:
                reason = str(exc)
                self._record(
                    binding,
                    principal,
                    command_id=command_id,
                    name=name,
                    payload=(payload if isinstance(payload, dict) else {}),
                    disposition="rejected",
                    reason=reason,
                )
                await self._result(
                    ws,
                    command_id=command_id,
                    name=name,
                    status="rejected",
                    reason=reason,
                )
            return

        if name == "release_axes":
            try:
                channels = binding.endpoint.release_override()
                with self._lock:
                    binding.override_active = False
                    binding.last_axis_monotonic_ns = None
                self._record(
                    binding,
                    principal,
                    command_id=command_id,
                    name=name,
                    payload={"released_channels": [1, 2, 3, 4]},
                    disposition="applied",
                    applied=True,
                )
                await self._result(
                    ws,
                    command_id=command_id,
                    name=name,
                    status="applied",
                    extra={"channels": list(channels)},
                )
            except ControlError as exc:
                await self._result(
                    ws,
                    command_id=command_id,
                    name=name,
                    status="rejected",
                    reason=str(exc),
                )
            return

        if name in {"arm", "disarm"}:
            wanted = name == "arm"
            # Never arm against a latched non-zero RC override.  Clear it first.
            try:
                binding.endpoint.release_override()
                with self._lock:
                    binding.override_active = False
                    binding.last_axis_monotonic_ns = None
                self._record(
                    binding,
                    principal,
                    command_id=command_id,
                    name=name,
                    payload={"armed": wanted},
                    disposition="accepted",
                )
                status, reason = await asyncio.to_thread(
                    self._arm_disarm_confirm,
                    binding.endpoint,
                    wanted,
                )
                self._record(
                    binding,
                    principal,
                    command_id=command_id,
                    name=name,
                    payload={"armed": wanted},
                    disposition=status,
                    reason=reason,
                    applied=(status == "applied"),
                )
                await self._result(
                    ws,
                    command_id=command_id,
                    name=name,
                    status=status,
                    reason=reason,
                )
            except ControlError as exc:
                await self._result(
                    ws,
                    command_id=command_id,
                    name=name,
                    status="rejected",
                    reason=str(exc),
                )

    def _arm_disarm_confirm(
        self,
        endpoint: ControlMavlinkTlogRecorder,
        wanted: bool,
    ) -> tuple[str, str | None]:
        ack_token = endpoint.ack_token(MAV_CMD_COMPONENT_ARM_DISARM)
        status_token = endpoint.status_token()
        endpoint.send_arm_disarm(wanted, force=False)
        ack = endpoint.wait_command_ack(
            MAV_CMD_COMPONENT_ARM_DISARM,
            after_counter=ack_token,
            timeout=3.0,
        )

        if ack is not None and ack.result != MAV_RESULT_ACCEPTED:
            time.sleep(0.35)
            texts = endpoint.status_texts_since(status_token, limit=8)
            diagnostic = " | ".join(texts) if texts else "no STATUSTEXT captured"
            normal_reason = f"command_ack_result_{ack.result}: {diagnostic}"

            if self.allow_test_force_arm:
                force_ack_token = endpoint.ack_token(
                    MAV_CMD_COMPONENT_ARM_DISARM
                )
                endpoint.send_arm_disarm(wanted, force=True)
                force_ack = endpoint.wait_command_ack(
                    MAV_CMD_COMPONENT_ARM_DISARM,
                    after_counter=force_ack_token,
                    timeout=3.0,
                )
                action = "arm" if wanted else "disarm"
                if (
                    force_ack is not None
                    and force_ack.result != MAV_RESULT_ACCEPTED
                ):
                    return (
                        "rejected",
                        normal_reason
                        + f"; test_force_{action}_ack_result_{force_ack.result}",
                    )
                if endpoint.wait_armed(wanted, timeout=3.5):
                    return (
                        "applied",
                        f"test_force_{action}_after_regular_failure: "
                        + normal_reason,
                    )
                if (
                    force_ack is not None
                    and force_ack.result == MAV_RESULT_ACCEPTED
                ):
                    return (
                        "accepted",
                        f"test_force_{action}_heartbeat_confirmation_timeout: "
                        + normal_reason,
                    )
                return (
                    "timeout",
                    f"test_force_{action}_ack_timeout: " + normal_reason,
                )

            return "rejected", normal_reason

        if endpoint.wait_armed(wanted, timeout=3.5):
            return "applied", None

        time.sleep(0.2)
        texts = endpoint.status_texts_since(status_token, limit=8)
        diagnostic = " | ".join(texts) if texts else "no STATUSTEXT captured"
        if self.allow_test_force_arm:
            force_ack_token = endpoint.ack_token(
                MAV_CMD_COMPONENT_ARM_DISARM
            )
            endpoint.send_arm_disarm(wanted, force=True)
            force_ack = endpoint.wait_command_ack(
                MAV_CMD_COMPONENT_ARM_DISARM,
                after_counter=force_ack_token,
                timeout=3.0,
            )
            action = "arm" if wanted else "disarm"
            if endpoint.wait_armed(wanted, timeout=3.5):
                return (
                    "applied",
                    f"test_force_{action}_after_confirmation_timeout: "
                    + diagnostic,
                )
            if (
                force_ack is not None
                and force_ack.result != MAV_RESULT_ACCEPTED
            ):
                return (
                    "rejected",
                    f"test_force_{action}_ack_result_{force_ack.result}: "
                    + diagnostic,
                )

        if self.allow_test_force_arm:
            force_ack_token = endpoint.ack_token(
                MAV_CMD_COMPONENT_ARM_DISARM
            )
            endpoint.send_arm_disarm(wanted, force=True)
            force_ack = endpoint.wait_command_ack(
                MAV_CMD_COMPONENT_ARM_DISARM,
                after_counter=force_ack_token,
                timeout=3.0,
            )
            action = "arm" if wanted else "disarm"
            if endpoint.wait_armed(wanted, timeout=3.5):
                return (
                    "applied",
                    f"test_force_{action}_after_confirmation_timeout: "
                    + diagnostic,
                )
            if (
                force_ack is not None
                and force_ack.result != MAV_RESULT_ACCEPTED
            ):
                return (
                    "rejected",
                    f"test_force_{action}_ack_result_{force_ack.result}: "
                    + diagnostic,
                )

        if ack is not None and ack.result == MAV_RESULT_ACCEPTED:
            return (
                "accepted",
                "heartbeat_confirmation_timeout: " + diagnostic,
            )
        return "timeout", "arm_disarm_ack_timeout: " + diagnostic

    def confirm_effect(
        self,
        *,
        attempt_id: str,
        command_id: str,
        principal,
        reason: str,
    ) -> None:
        binding = self._bindings[attempt_id]
        payload = dict(binding.last_axis_payload or {})
        self._record(
            binding,
            principal,
            command_id=command_id,
            name="flight_axes",
            payload=payload,
            disposition="applied",
            reason=reason,
            applied=True,
        )

    def release_attempt(self, attempt_id: str, *, reason: str) -> None:
        with self._lock:
            binding = self._bindings.get(attempt_id)
            if binding is None or not binding.override_active:
                return
            binding.override_active = False
            binding.last_axis_monotonic_ns = None
        try:
            binding.endpoint.release_override()
            if reason == "deadman_timeout":
                self.deadman_releases += 1
            binding.recorder.record_event(
                "flight_override_released",
                {
                    "reason": reason,
                    "deadman_ms": self.deadman_ns // 1_000_000,
                },
                visibility="server",
            )
        except Exception as exc:
            binding.recorder.record_event(
                "flight_override_release_failed",
                {"reason": reason, "error": str(exc)},
                visibility="server",
            )

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(0.05):
            now = time.monotonic_ns()
            expired = []
            with self._lock:
                for attempt_id, binding in self._bindings.items():
                    if (
                        binding.override_active
                        and binding.last_axis_monotonic_ns is not None
                        and now - binding.last_axis_monotonic_ns > self.deadman_ns
                    ):
                        expired.append(attempt_id)
            for attempt_id in expired:
                self.release_attempt(attempt_id, reason="deadman_timeout")

    def shutdown(self) -> None:
        self._stop.set()
        self._watchdog.join(timeout=1.0)
        for attempt_id in list(self._bindings):
            self.release_attempt(attempt_id, reason="router_shutdown")

    async def _send(self, ws, obj: dict[str, Any]) -> None:
        await ws.send(json.dumps(obj, separators=(",", ":"), default=str))

    async def _result(
        self,
        ws,
        *,
        command_id: str,
        name: str,
        status: str,
        reason: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        msg = {
            "type": "command_result",
            "command_id": command_id,
            "name": name,
            "status": status,
            "reason": reason,
            "server_time_unix": time.time(),
        }
        if extra:
            msg.update(extra)
        await self._send(ws, msg)
