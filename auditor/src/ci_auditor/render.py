"""Prometheus text and JSON-lines output."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, time

from .model import STATUSES, Exemption, Result, Snapshot


def _esc(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _lbl(**labels: object) -> str:
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items()) + "}"


def expiry_epoch(day: date) -> float:
    """End of the expiry day, UTC: the exemption still holds on that day."""
    return datetime.combine(day, time(23, 59, 59), tzinfo=UTC).timestamp()


def metrics(
    results: Iterable[Result],
    snapshots: Iterable[Snapshot],
    exemptions: Iterable[Exemption],
    now: float,
    last_run: float,
    last_success: float,
    duration: float,
    api_requests: dict[tuple[str, int], int],
    errors: int,
) -> str:
    out: list[str] = []
    add = out.append
    results = list(results)
    add("# HELP ci_conformance 1 for the current status of each repo x rule (one-hot over status).")
    add("# TYPE ci_conformance gauge")
    for r in results:
        for status in STATUSES:
            add(
                f"ci_conformance{_lbl(repo=r.repo, rule=r.rule.id, title=r.rule.title, area=r.rule.area, severity=r.rule.severity, enforce=r.rule.enforce, status=status)} {int(r.status == status)}"
            )
    add("# HELP ci_conformance_findings Non-exempt findings per repo x rule.")
    add("# TYPE ci_conformance_findings gauge")
    for r in results:
        add(
            f"ci_conformance_findings{_lbl(repo=r.repo, rule=r.rule.id, severity=r.rule.severity)} {len(r.findings)}"
        )
    add("# HELP ci_conformance_exempted_findings Findings suppressed by a live exemption.")
    add("# TYPE ci_conformance_exempted_findings gauge")
    for r in results:
        if r.exempted:
            add(f"ci_conformance_exempted_findings{_lbl(repo=r.repo, rule=r.rule.id)} {len(r.exempted)}")
    add("# HELP ci_exemption_expiry_seconds Seconds until the exemption expires (negative: expired).")
    add("# TYPE ci_exemption_expiry_seconds gauge")
    for e in exemptions:
        add(
            f"ci_exemption_expiry_seconds{_lbl(repo=e.repo, rule=e.rule, workflow=e.workflow or '', job=e.job or '', owner=e.owner)} {expiry_epoch(e.expires) - now:.0f}"
        )
    add("# HELP ci_repo_info Repositories audited.")
    add("# TYPE ci_repo_info gauge")
    for s in snapshots:
        add(
            f"ci_repo_info{_lbl(repo=s.repo, full_name=s.full_name, visibility='private' if s.private else 'public', merge_queue=('unknown' if not s.merge_queue_known else ('yes' if s.merge_queue else 'no')), workflows=len(s.workflows))} 1"
        )
        for gap in s.gaps:
            add(f"ci_audit_gap{_lbl(repo=s.repo, source=gap)} 1")
    add("# TYPE ci_audit_last_run_timestamp gauge")
    add(f"ci_audit_last_run_timestamp {last_run:.0f}")
    add("# TYPE ci_audit_last_success_timestamp gauge")
    add(f"ci_audit_last_success_timestamp {last_success:.0f}")
    add("# TYPE ci_audit_duration_seconds gauge")
    add(f"ci_audit_duration_seconds {duration:.1f}")
    add("# TYPE ci_audit_errors_total counter")
    add(f"ci_audit_errors_total {errors}")
    add("# TYPE ci_audit_api_requests_total counter")
    for (kind, code), n in sorted(api_requests.items()):
        add(f"ci_audit_api_requests_total{_lbl(endpoint=kind, status=code)} {n}")
    return "\n".join(out) + "\n"


def main_checks(statuses: dict[str, dict[str, str]]) -> str:
    """ci_main_required_check{repo,context,conclusion} for the default-branch head."""
    out = [
        "# HELP ci_main_required_check 1 for the conclusion of each required context on the default-branch head.",
        "# TYPE ci_main_required_check gauge",
    ]
    for repo, contexts in sorted(statuses.items()):
        for ctx, conclusion in sorted(contexts.items()):
            out.append(f"ci_main_required_check{_lbl(repo=repo, context=ctx, conclusion=conclusion)} 1")
    return "\n".join(out) + "\n"


def finding_lines(results: Iterable[Result], ts: datetime) -> list[str]:
    """One JSON object per finding (and per exempted finding), for Loki."""
    lines = []
    stamp = ts.isoformat(timespec="seconds")
    for r in results:
        base = {
            "ts": stamp,
            "msg": "ci_finding",
            "repo": r.repo,
            "rule": r.rule.id,
            "title": r.rule.title,
            "severity": r.rule.severity,
            "enforce": r.rule.enforce,
        }
        for f in r.findings:
            lines.append(
                json.dumps(
                    {
                        **base,
                        "status": "fail",
                        "workflow": f.workflow,
                        "job": f.job,
                        "message": f.message,
                        "fix": r.rule.fix,
                    },
                    sort_keys=True,
                )
            )
        for f, e in r.exempted:
            lines.append(
                json.dumps(
                    {
                        **base,
                        "status": "exempt",
                        "workflow": f.workflow,
                        "job": f.job,
                        "message": f.message,
                        "exemption_owner": e.owner,
                        "exemption_expires": e.expires.isoformat(),
                    },
                    sort_keys=True,
                )
            )
    return lines
