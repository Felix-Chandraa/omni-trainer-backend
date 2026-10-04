from __future__ import annotations

from typing import Any


class ActorIdentityError(RuntimeError):
    pass


def actor_identifier(actor: Any) -> str:
    '''Resolve the existing training-core Actor into the evidence schema actor_id.

    The evidence schema intentionally uses the neutral key "actor_id", while
    the older core Actor model may use a different attribute name. This
    boundary adapter avoids changing the core model or hard-coding one field
    name into recorder/live-trial code.
    '''
    for field_name in ("actor_id", "id", "name", "subject", "user_id"):
        value = getattr(actor, field_name, None)
        if isinstance(value, str) and value:
            return value

    field_map = getattr(actor, "__dataclass_fields__", None)
    if field_map:
        for field_name in field_map:
            if field_name == "role":
                continue
            value = getattr(actor, field_name, None)
            if isinstance(value, str) and value:
                return value

    raise ActorIdentityError(
        f"unsupported Actor identity contract: {type(actor).__name__}"
    )
