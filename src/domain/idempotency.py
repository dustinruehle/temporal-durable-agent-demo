"""Idempotency helper: a stable key plus a per-key ledger.

Activities are at-least-once, so a retry or a duplicate can run the same logical
effect twice. Deriving a stable key from the input (or from the Workflow Run Id
plus Activity Id) and recording the first result under that key makes the second
execution a no-op that returns the recorded result, so the business effect
happens effectively once.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


def key_for(name: str, payload: dict[str, Any]) -> str:
    """A stable idempotency key for a named effect and its input.

    Stable across dict ordering, because the payload is serialized with sorted
    keys before hashing.
    """
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{name}:{serialized}".encode()).hexdigest()[:16]
    return f"{name}:{digest}"


def once(ledger: dict[str, Any], key: str, effect: Callable[[], T]) -> T:
    """Run effect once per key; on a repeat, return the recorded result.

    The ledger is any dict-like store keyed by the idempotency key. In the demo it
    is Workflow state; in production it would be the adopting team's store.
    """
    if key in ledger:
        return ledger[key]
    result = effect()
    ledger[key] = result
    return result
