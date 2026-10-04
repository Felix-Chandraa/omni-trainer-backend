"""R2 core types. No Qt, network listener, or simulator side effects."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class State(str, Enum):
    SETUP = "Setup"
    READY = "Ready"
    ACTIVE = "Active"
    PAUSED = "Paused"
    INTERRUPTED = "Interrupted"
    ENDED = "Ended"


class Role(str, Enum):
    INSTRUCTOR = "instructor"
    STUDENT = "student"


class Scope(str, Enum):
    FLIGHT = "flight"
    PAYLOAD = "payload"
    BOTH = "both"


@dataclass(frozen=True)
class Actor:
    id: str
    role: Role


@dataclass(frozen=True)
class Attempt:
    id: str
    exercise_id: str
    aircraft_id: str
    generation: int
    state: State
    flight_owner: str
    payload_owner: str
    authority_epoch: int
    revision: int


class CoreError(ValueError):
    """The requested transition or operation is not permitted."""
