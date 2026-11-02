"""Read GitHub Actions workflow files: triggers, jobs, runs-on, producers."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

import yaml

from .model import RunnerClass, Workflow

EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
MATRIX_REF = re.compile(r"^\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}$")
PR_EVENTS = ("pull_request", "pull_request_target")


def parse(file: str, text: str) -> Workflow:
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError("not a mapping")
    return Workflow(file=file, doc=cast(dict[str, Any], loaded), text=text)


def triggers(wf: Workflow) -> dict[str, Any]:
    """`on:` normalised to {event: config-or-None}. PyYAML reads `on` as True."""
    doc = cast(dict[Any, Any], wf.doc)
    raw = doc.get("on", doc.get(True))
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {str(e): None for e in raw}
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    return {}


def str_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def push_config(wf: Workflow) -> dict[str, Any] | None:
    """The push trigger's config ({} for a bare `push:`), or None without push."""
    trig = triggers(wf)
    if "push" not in trig:
        return None
    cfg = trig["push"]
    return cast(dict[str, Any], cfg) if isinstance(cfg, dict) else {}


def pushes_branch(wf: Workflow, branch: str) -> bool:
    cfg = push_config(wf)
    if cfg is None:
        return False
    branches = str_list(cfg.get("branches"))
    if not branches and not cfg.get("branches-ignore"):
        # Only tags (or nothing at all, which means every branch).
        return not cfg.get("tags") and not cfg.get("tags-ignore")
    return not branches or any(b == branch or b in ("*", "**") for b in branches)


def is_ci(wf: Workflow) -> bool:
    trig = triggers(wf)
    return any(e in trig for e in (*PR_EVENTS, "merge_group"))


def path_filtered(wf: Workflow, events: tuple[str, ...]) -> list[str]:
    """Events among `events` whose trigger config carries paths/paths-ignore."""
    out = []
    for event, cfg in triggers(wf).items():
        if event in events and isinstance(cfg, dict) and (cfg.get("paths") or cfg.get("paths-ignore")):
            out.append(event)
    return out


@dataclass(frozen=True)
class Job:
    key: str
    body: dict[str, Any]
    workflow: Workflow

    @property
    def name(self) -> str:
        name = self.body.get("name")
        return str(name) if isinstance(name, str) and name else self.key

    @property
    def uses(self) -> str | None:
        uses = self.body.get("uses")
        return str(uses) if isinstance(uses, str) else None

    @property
    def condition(self) -> str:
        cond = self.body.get("if")
        return str(cond) if cond is not None else ""

    def steps(self) -> list[dict[str, Any]]:
        steps = self.body.get("steps")
        return (
            [cast(dict[str, Any], s) for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []
        )

    def runs(self) -> str:
        return "\n".join(str(s.get("run", "")) for s in self.steps())

    def env(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for scope in (self.workflow.doc.get("env"), self.body.get("env")):
            if isinstance(scope, dict):
                merged.update(scope)
        return merged


def jobs(wf: Workflow) -> Iterator[Job]:
    body = wf.doc.get("jobs")
    if not isinstance(body, dict):
        return
    for key, job in body.items():
        if isinstance(job, dict):
            yield Job(key=str(key), body=cast(dict[str, Any], job), workflow=wf)


def runs_on_sets(job: Job) -> list[frozenset[str]] | None:
    """The label sets a job can ask for. None if unresolvable (an expression
    that is not a literal-matrix lookup)."""
    raw = job.body.get("runs-on")
    if isinstance(raw, dict):  # {group: ..., labels: ...}
        raw = raw.get("labels")
        if raw is None:
            return None
    if isinstance(raw, str):
        match = MATRIX_REF.match(raw.strip())
        if match:
            return _matrix_values(job, match.group(1))
        if EXPR.search(raw):
            return None
        return [frozenset([raw])]
    if isinstance(raw, list):
        labels = [str(x) for x in raw]
        if any(EXPR.search(x) for x in labels):
            return None
        return [frozenset(labels)]
    return None


def _matrix_values(job: Job, key: str) -> list[frozenset[str]] | None:
    strategy = job.body.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return None
    values: list[Any] = []
    if isinstance(matrix.get(key), list):
        values += matrix[key]
    for extra in matrix.get("include") or []:
        if isinstance(extra, dict) and key in extra:
            values.append(extra[key])
    if not values:
        return None
    out = []
    for value in values:
        if isinstance(value, str) and not EXPR.search(value):
            out.append(frozenset([value]))
        elif isinstance(value, list) and all(isinstance(x, str) for x in value):
            out.append(frozenset(value))
        else:
            return None
    return out


def classify(labels: frozenset[str], classes: tuple[RunnerClass, ...]) -> RunnerClass | None:
    for cls in classes:
        if labels in cls.forms:
            return cls
    return None


def excludes_event(cond: str, event: str) -> bool:
    """Heuristic: does a job `if:` keep the job off `event`?

    True when the condition says `event_name != '<event>'`, pins event_name to
    other events only (`== 'push'`, `== 'merge_group' || == 'push'`), or pins
    the ref to main or a tag (a PR or queue run never has those refs).
    """
    c = cond.replace('"', "'")
    if not c:
        return False
    if re.search(rf"event_name\s*!=\s*'{event}'", c):
        return True
    positives = re.findall(r"event_name\s*==\s*'([a-z_]+)'", c)
    if positives and event not in positives:
        return True
    pinned_ref = (
        re.search(r"github\.ref\s*==\s*'refs/(heads/main|tags/)", c)
        or re.search(r"github\.ref_name\s*==\s*'main'", c)
        or "startsWith(github.ref, 'refs/tags/" in c
    )
    return bool(pinned_ref) and "||" not in c


def excludes_events(cond: str, events: tuple[str, ...]) -> bool:
    return all(excludes_event(cond, e) for e in events)


def can_run_on(job: Job, events: tuple[str, ...]) -> bool:
    trig = triggers(job.workflow)
    present = tuple(e for e in events if e in trig)
    return bool(present) and not excludes_events(job.condition, present)


def produces(job: Job, context: str) -> bool:
    """Does this job produce the check-run named `context`?"""
    name = job.name
    if context in (name, job.key):
        return True
    if context.startswith(f"{name} (") or context.startswith(f"{job.key} ("):
        return True  # matrix leg
    return job.uses is not None and (context.startswith(f"{name} / ") or context.startswith(f"{job.key} / "))
