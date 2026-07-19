"""Accès au catalogue des ressources RTE (re-export depuis config pour commodité)."""
from __future__ import annotations

from ..config import RteResource, load_settings


def enabled_resources() -> list[RteResource]:
    return load_settings().enabled_rte_resources()


def resource_by_name(name: str) -> RteResource | None:
    for r in load_settings().rte_resources:
        if r.name == name:
            return r
    return None
