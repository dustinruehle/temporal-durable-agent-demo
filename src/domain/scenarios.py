"""Scenario profiles loaded from datasets/.

A profile names, per scenario, how many times a given skill fails before
succeeding, whether a follow-up is armed, and the trigger words that map a plain
customer message to a scenario. The profiles are the source of truth for the
mock's failure injection; the mock reads the relevant counts through the system
prompt directive so the JSON stays authoritative.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

# datasets/ sits at the repo root, two levels up from this file's package.
_DATASETS = Path(__file__).resolve().parents[2] / "datasets"

# Known scenarios, in the order the dashboard picker lists them.
SCENARIO_FILES = ["happy", "deferred_follow_up", "handoff"]
DEFAULT_SCENARIO = "happy"


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    triggers: tuple[str, ...]
    fail_counts: dict[str, int]
    follow_up: bool
    max_price: int

    def financing_fail_count(self) -> int:
        return int(self.fail_counts.get("check_financing_status", 0))


@cache
def load_scenario(name: str) -> Scenario:
    path = _DATASETS / f"{name}.json"
    data = json.loads(path.read_text())
    return Scenario(
        name=data["name"],
        description=data["description"],
        triggers=tuple(data.get("triggers", [])),
        fail_counts=dict(data.get("fail_counts", {})),
        follow_up=bool(data.get("follow_up", False)),
        max_price=int(data.get("max_price", 15000)),
    )


def all_scenarios() -> list[Scenario]:
    return [load_scenario(n) for n in SCENARIO_FILES]


def classify(message: str) -> str:
    """Map a plain customer message to a scenario name by trigger words.

    Deterministic and order-stable: the first scenario whose trigger appears
    wins, otherwise the default. The dashboard scenario picker overrides this by
    passing an explicit scenario, so classification only matters for a bare curl.
    """
    lowered = message.lower()
    for name in SCENARIO_FILES:
        scenario = load_scenario(name)
        if any(trigger in lowered for trigger in scenario.triggers):
            return name
    return DEFAULT_SCENARIO
