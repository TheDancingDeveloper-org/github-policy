"""Plain data shared by the checks, the collector and the renderers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

Severity = Literal["high", "medium", "low"]
Kind = Literal["static", "settings", "runtime"]
Status = Literal["pass", "fail", "exempt", "na", "unknown"]
STATUSES: tuple[Status, ...] = ("pass", "fail", "exempt", "na", "unknown")


@dataclass(frozen=True)
class RunnerClass:
    name: str
    forms: tuple[frozenset[str], ...]
    public_only: bool = False
    repos: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rule:
    id: str
    area: str
    title: str
    statement: str
    severity: Severity
    kind: Kind
    check: str
    machine: str
    fix: str
    enforce: Literal["report", "block"]
    retired: bool = False


@dataclass(frozen=True)
class Exemption:
    repo: str
    rule: str
    reason: str
    owner: str
    created: date
    expires: date
    workflow: str | None = None
    job: str | None = None

    def covers(self, finding: Finding) -> bool:
        if self.repo != finding.repo or self.rule != finding.rule:
            return False
        if self.workflow is not None and self.workflow != finding.workflow:
            return False
        return self.job is None or self.job == finding.job


@dataclass(frozen=True)
class Finding:
    repo: str
    rule: str
    message: str
    workflow: str | None = None
    job: str | None = None


@dataclass
class Result:
    repo: str
    rule: Rule
    status: Status
    findings: list[Finding] = field(default_factory=list)
    exempted: list[tuple[Finding, Exemption]] = field(default_factory=list)
    note: str = ""


@dataclass(frozen=True)
class Workflow:
    """One workflow file: its name under .github/workflows and parsed body."""

    file: str
    doc: dict[str, Any]
    text: str


@dataclass(frozen=True)
class RuntimeJob:
    workflow_path: str
    workflow_name: str
    job: str
    labels: tuple[str, ...]
    conclusion: str
    run_id: int
    run_conclusion: str
    event: str
    head_branch: str


@dataclass(frozen=True)
class RuntimeRun:
    workflow_path: str
    event: str
    head_branch: str
    conclusion: str
    created_at: str


@dataclass
class Snapshot:
    """Everything the checks may read about one repository.

    ``None`` means "not collected / not visible" and makes a dependent rule
    ``unknown`` rather than passing it.
    """

    repo: str
    full_name: str
    private: bool
    default_branch: str = "main"
    workflows: list[Workflow] = field(default_factory=list)
    parse_errors: dict[str, str] = field(default_factory=dict)
    tree: list[str] | None = None
    required_contexts: list[str] | None = None
    merge_queue: dict[str, Any] | None = None
    merge_queue_known: bool = False
    runtime_jobs: list[RuntimeJob] | None = None
    runtime_runs: list[RuntimeRun] | None = None
    actionlint: list[str] | None = None
    org_visibility: dict[str, bool] | None = None  # repo name -> private
    gaps: list[str] = field(default_factory=list)
