"""Mock backend: synthetic data and the pure logic behind each skill.

The skill Activities in skills.py are thin wrappers over these pure functions, so
the same logic runs whether a skill executes as a Workflow Activity, a Standalone
Activity, or a plain function inside the stage 0 harness. Synthetic data only: no
real names, addresses, VINs, or PII.
"""

from __future__ import annotations

from dataclasses import dataclass

# A small synthetic inventory. Prices are round numbers so scenario assertions
# are obvious. Ids are opaque tokens, not VINs.
_INVENTORY = [
    {"id": "VH-1001", "make": "Maker A", "model": "Hatch", "year": 2021, "price": 12000},
    {"id": "VH-1002", "make": "Maker B", "model": "Sedan", "year": 2020, "price": 14500},
    {"id": "VH-1003", "make": "Maker C", "model": "SUV", "year": 2022, "price": 19000},
    {"id": "VH-1004", "make": "Maker A", "model": "Coupe", "year": 2019, "price": 9500},
    {"id": "VH-1005", "make": "Maker D", "model": "Van", "year": 2023, "price": 26000},
]


@dataclass
class Vehicle:
    id: str
    make: str
    model: str
    year: int
    price: int


def find_inventory(max_price: int | None = None, body_style: str | None = None) -> list[dict]:
    """Return matching vehicles, cheapest first. Deterministic."""
    rows = list(_INVENTORY)
    if max_price is not None:
        rows = [r for r in rows if r["price"] <= max_price]
    if body_style:
        rows = [r for r in rows if r["model"].lower() == body_style.lower()]
    rows.sort(key=lambda r: r["price"])
    return rows


def book_inspection(vehicle_id: str, when: str) -> dict:
    """Confirm an inspection slot. Deterministic confirmation id from inputs."""
    confirmation = f"INSP-{vehicle_id}-{abs(hash((vehicle_id, when))) % 100000:05d}"
    return {"vehicle_id": vehicle_id, "when": when, "confirmation": confirmation}


def financing_status(application_id: str) -> dict:
    """Return a financing decision for a synthetic application id."""
    # Deterministic pseudo-decision keyed on the id, so the same id always maps
    # to the same status.
    bucket = abs(hash(application_id)) % 3
    status = ["approved", "pending_review", "needs_documents"][bucket]
    return {"application_id": application_id, "status": status}


class SkillError(Exception):
    """Raised by a skill when the backend cannot satisfy the request.

    The message is safe to return to the model as a tool result so the model can
    choose a different approach.
    """
