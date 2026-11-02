"""Load and validate ci-standard/rules.yaml and ci-standard/exemptions.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

import yaml

from .model import Exemption, Rule, RunnerClass

RULE_ID = re.compile(r"^CI-\d{2}$")
REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")
SEVERITIES = {"high", "medium", "low"}
KINDS = {"static", "settings", "runtime"}
RULE_FIELDS = {"id", "area", "title", "statement", "severity", "kind", "check", "machine", "fix", "enforce"}


class StandardError(ValueError):
    """rules.yaml or exemptions.yaml does not match its schema."""


@dataclass(frozen=True)
class Standard:
    rules: tuple[Rule, ...]
    classes: tuple[RunnerClass, ...]
    required_contexts: tuple[str, ...]
    max_exemption_days: int

    def rule(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)

    @property
    def active(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if not r.retired)


def default_standard_dir() -> Path:
    """ci-standard/ next to the package (repo checkout) or the image copy."""
    for candidate in (
        Path(__file__).resolve().parents[3] / "ci-standard",
        Path("/opt/ci-auditor/ci-standard"),
    ):
        if (candidate / "rules.yaml").exists():
            return candidate
    raise StandardError("ci-standard/rules.yaml not found; pass --standard")


def _mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StandardError(f"{where}: expected a mapping")
    return cast(dict[str, Any], value)


def parse_rules(text: str) -> Standard:
    doc = _mapping(yaml.safe_load(text), "rules.yaml")
    if doc.get("version") != 1:
        raise StandardError("rules.yaml: version must be 1")

    classes: list[RunnerClass] = []
    for name, raw in _mapping(doc.get("runner_classes"), "runner_classes").items():
        body = _mapping(raw, f"runner_classes.{name}")
        forms = body.get("forms")
        if not isinstance(forms, list) or not forms:
            raise StandardError(f"runner_classes.{name}: forms must be a non-empty list")
        parsed = []
        for form in forms:
            if not isinstance(form, list) or not form or not all(isinstance(x, str) for x in form):
                raise StandardError(f"runner_classes.{name}: each form is a list of labels")
            if form == ["self-hosted"]:
                raise StandardError(f"runner_classes.{name}: bare self-hosted is not a class")
            parsed.append(frozenset(form))
        classes.append(
            RunnerClass(
                name=str(name),
                forms=tuple(parsed),
                public_only=bool(body.get("public_only", False)),
                repos=tuple(str(r) for r in body.get("repos", [])),
            )
        )

    rules: list[Rule] = []
    seen: set[str] = set()
    for i, raw in enumerate(doc.get("rules") or []):
        body = _mapping(raw, f"rules[{i}]")
        missing = RULE_FIELDS - body.keys()
        if missing:
            raise StandardError(f"rules[{i}] ({body.get('id')}): missing {sorted(missing)}")
        rid = str(body["id"])
        if not RULE_ID.match(rid) or rid in seen:
            raise StandardError(f"rules[{i}]: bad or duplicate id {rid!r}")
        seen.add(rid)
        if body["severity"] not in SEVERITIES:
            raise StandardError(f"{rid}: severity must be one of {sorted(SEVERITIES)}")
        if body["kind"] not in KINDS:
            raise StandardError(f"{rid}: kind must be one of {sorted(KINDS)}")
        if body["enforce"] not in ("report", "block"):
            raise StandardError(f"{rid}: enforce must be report or block")
        if body["kind"] != "static" and body["enforce"] == "block":
            raise StandardError(f"{rid}: only static rules can block (the gate cannot see settings or runs)")
        rules.append(
            Rule(
                id=rid,
                area=str(body["area"]),
                title=str(body["title"]),
                statement=" ".join(str(body["statement"]).split()),
                severity=body["severity"],
                kind=body["kind"],
                check=str(body["check"]),
                machine=" ".join(str(body["machine"]).split()),
                fix=" ".join(str(body["fix"]).split()),
                enforce=body["enforce"],
                retired=bool(body.get("retired", False)),
            )
        )
    if not rules:
        raise StandardError("rules.yaml: no rules")
    return Standard(
        rules=tuple(rules),
        classes=tuple(classes),
        required_contexts=tuple(str(c) for c in doc.get("required_contexts", [])),
        max_exemption_days=int(doc.get("max_exemption_days", 180)),
    )


def _date(value: object, where: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise StandardError(f"{where}: not a YYYY-MM-DD date")


def parse_exemptions(text: str, standard: Standard) -> list[Exemption]:
    """Parse and validate. Raises on any malformed entry: a bad exemption file
    must be loud, never silently suppress or silently drop."""
    doc = _mapping(yaml.safe_load(text) or {}, "exemptions.yaml")
    if doc.get("version") != 1:
        raise StandardError("exemptions.yaml: version must be 1")
    known = {r.id for r in standard.rules}
    out: list[Exemption] = []
    allowed = {"repo", "rule", "reason", "owner", "created", "expires", "workflow", "job"}
    for i, raw in enumerate(doc.get("exemptions") or []):
        body = _mapping(raw, f"exemptions[{i}]")
        where = f"exemptions[{i}] ({body.get('repo')}/{body.get('rule')})"
        extra = body.keys() - allowed
        if extra:
            raise StandardError(f"{where}: unknown keys {sorted(extra)}")
        for key in ("repo", "rule", "reason", "owner", "created", "expires"):
            if not body.get(key):
                raise StandardError(f"{where}: {key} is required")
        repo, rule = str(body["repo"]), str(body["rule"])
        if not REPO_NAME.match(repo):
            raise StandardError(f"{where}: repo is a bare repository name")
        if rule not in known:
            raise StandardError(f"{where}: unknown rule {rule}")
        reason = " ".join(str(body["reason"]).split())
        if len(reason) < 40:
            raise StandardError(f"{where}: reason must say why and what ends it (>= 40 chars)")
        workflow = body.get("workflow")
        if workflow is not None and not WORKFLOW_FILE.match(str(workflow)):
            raise StandardError(f"{where}: workflow is a file name like ci.yml")
        job = body.get("job")
        if job is not None and workflow is None:
            raise StandardError(f"{where}: job needs workflow")
        created = _date(body["created"], f"{where}.created")
        expires = _date(body["expires"], f"{where}.expires")
        if expires <= created:
            raise StandardError(f"{where}: expires must be after created")
        if (expires - created).days > standard.max_exemption_days:
            raise StandardError(f"{where}: longer than {standard.max_exemption_days} days")
        out.append(
            Exemption(
                repo=repo,
                rule=rule,
                reason=reason,
                owner=str(body["owner"]),
                created=created,
                expires=expires,
                workflow=str(workflow) if workflow is not None else None,
                job=str(job) if job is not None else None,
            )
        )
    return out


def load(standard_dir: Path) -> tuple[Standard, list[Exemption]]:
    standard = parse_rules((standard_dir / "rules.yaml").read_text(encoding="utf-8"))
    exemptions_path = standard_dir / "exemptions.yaml"
    exemptions = (
        parse_exemptions(exemptions_path.read_text(encoding="utf-8"), standard)
        if exemptions_path.exists()
        else []
    )
    return standard, exemptions
