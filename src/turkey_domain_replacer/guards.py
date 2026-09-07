from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, TypeVar


class UnsafePlan(RuntimeError):
    pass


class HasApex(Protocol):
    apex: str


T = TypeVar("T", bound=HasApex)


def require_complete(complete: bool) -> None:
    if not complete:
        raise UnsafePlan("provider evidence is incomplete")


def require_distinct(old_domain: str, new_domain: str) -> None:
    if old_domain == new_domain:
        raise UnsafePlan("old and new domains must be distinct")


def require_exact_zone(zones: Iterable[T], apex: str) -> T:
    matches = [zone for zone in zones if zone.apex == apex]
    if len(matches) != 1:
        raise UnsafePlan(f"expected exactly one hosted zone for {apex}")
    return matches[0]
