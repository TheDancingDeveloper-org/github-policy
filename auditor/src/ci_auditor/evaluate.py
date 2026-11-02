"""Evaluate every rule for one snapshot, minus unexpired exemptions."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from .checks import CHECKS
from .model import Exemption, Kind, Result, Snapshot
from .standard import Standard


def live(exemptions: Iterable[Exemption], today: date) -> list[Exemption]:
    """An exemption suppresses on its expiry day and not after."""
    return [e for e in exemptions if e.expires >= today]


def evaluate(
    snap: Snapshot,
    std: Standard,
    exemptions: Iterable[Exemption],
    today: date,
    kinds: tuple[Kind, ...] = ("static", "settings", "runtime"),
) -> list[Result]:
    active = live(exemptions, today)
    results: list[Result] = []
    for rule in std.active:
        if rule.kind not in kinds:
            continue
        outcome = CHECKS[rule.check](snap, std, rule.id)
        result = Result(repo=snap.repo, rule=rule, status=outcome.status, note=outcome.note)
        for finding in outcome.findings:
            cover = next((e for e in active if e.covers(finding)), None)
            if cover is None:
                result.findings.append(finding)
            else:
                result.exempted.append((finding, cover))
        if outcome.status == "fail" and not result.findings:
            result.status = "exempt"
        results.append(result)
    return results
