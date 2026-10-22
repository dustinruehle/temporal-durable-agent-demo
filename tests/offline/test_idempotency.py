"""Cross-cutting: idempotency.

Activities are at-least-once. A stable idempotency key derived from the input
plus a per-key ledger means executing the same logical send twice yields a single
business effect, so at-least-once plus idempotency is effectively-once.
"""

from __future__ import annotations

from domain import idempotency


def test_same_key_executes_effect_once():
    ledger: dict[str, int] = {}
    calls = {"n": 0}

    def effect() -> str:
        calls["n"] += 1
        return f"result-{calls['n']}"

    key = idempotency.key_for("send_customer_message", {"conversation_id": "c1", "seq": 7})

    first = idempotency.once(ledger, key, effect)
    second = idempotency.once(ledger, key, effect)

    # The effect ran exactly once; the second call returns the recorded result.
    assert calls["n"] == 1
    assert first == second


def test_different_keys_execute_independently():
    ledger: dict[str, int] = {}
    calls = {"n": 0}

    def effect() -> int:
        calls["n"] += 1
        return calls["n"]

    idempotency.once(ledger, idempotency.key_for("s", {"seq": 1}), effect)
    idempotency.once(ledger, idempotency.key_for("s", {"seq": 2}), effect)

    assert calls["n"] == 2


def test_key_is_stable_for_same_input():
    a = idempotency.key_for("send_customer_message", {"conversation_id": "c1", "seq": 7})
    b = idempotency.key_for("send_customer_message", {"seq": 7, "conversation_id": "c1"})
    assert a == b
