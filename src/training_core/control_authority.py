"""Flight authority leases for DEV-015."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import threading
import time
from typing import Any


class AuthorityError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthorityLease:
    attempt_id: str
    aircraft_id: str
    scope: str
    actor_id: str
    station_id: str
    generation: int
    epoch: int
    granted_monotonic_ns: int
    reason: str

    def public(self) -> dict[str, Any]:
        return asdict(self)


class FlightAuthorityManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._by_attempt: dict[str, AuthorityLease] = {}
        self._epoch: dict[str, int] = {}

    def grant(
        self,
        *,
        attempt_id: str,
        aircraft_id: str,
        actor_id: str,
        station_id: str,
        generation: int,
        recorder,
        reason: str,
        force_new_epoch: bool = False,
    ) -> AuthorityLease:
        with self._lock:
            previous = self._by_attempt.get(attempt_id)
            if (
                not force_new_epoch
                and previous is not None
                and previous.actor_id == actor_id
                and previous.station_id == station_id
                and previous.generation == int(generation)
            ):
                return previous
            epoch = self._epoch.get(attempt_id, 0) + 1
            self._epoch[attempt_id] = epoch
            lease = AuthorityLease(
                attempt_id=attempt_id,
                aircraft_id=aircraft_id,
                scope="flight",
                actor_id=actor_id,
                station_id=station_id,
                generation=int(generation),
                epoch=epoch,
                granted_monotonic_ns=time.monotonic_ns(),
                reason=reason,
            )
            self._by_attempt[attempt_id] = lease

        recorder.record_authority(
            scope="flight",
            previous_actor_id=(None if previous is None else previous.actor_id),
            new_actor_id=actor_id,
            epoch=epoch,
            reason=reason,
        )
        return lease

    def current(self, attempt_id: str) -> AuthorityLease | None:
        with self._lock:
            return self._by_attempt.get(attempt_id)

    def validate(
        self,
        *,
        attempt_id: str,
        actor_id: str,
        station_id: str,
        generation: int,
        epoch: int,
    ) -> AuthorityLease:
        with self._lock:
            lease = self._by_attempt.get(attempt_id)
        if lease is None:
            raise AuthorityError("no_flight_authority")
        if lease.generation != int(generation):
            raise AuthorityError("authority_generation_mismatch")
        if lease.epoch != int(epoch):
            raise AuthorityError("authority_epoch_mismatch")
        if lease.actor_id != actor_id or lease.station_id != station_id:
            raise AuthorityError("not_flight_authority_owner")
        return lease

    def revoke(
        self,
        *,
        attempt_id: str,
        recorder,
        reason: str,
    ) -> AuthorityLease | None:
        with self._lock:
            previous = self._by_attempt.pop(attempt_id, None)
            if previous is None:
                return None
            epoch = self._epoch.get(attempt_id, previous.epoch) + 1
            self._epoch[attempt_id] = epoch
        recorder.record_authority(
            scope="flight",
            previous_actor_id=previous.actor_id,
            new_actor_id=None,
            epoch=epoch,
            reason=reason,
        )
        return previous
