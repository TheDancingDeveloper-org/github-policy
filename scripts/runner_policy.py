#!/usr/bin/env python3
"""Audit `runs-on:` selections in a repository's workflows.

The single source of truth for the rule. Both entry points use this file:

  * `.github/workflows/audit.yml` -> `scripts/audit-workflows.sh` (this repo)
  * `.github/workflows/runner-policy-reusable.yml` (every calling repository)

The reusable gate used to carry an inlined copy of this logic, because a
private policy repository cannot be checked out by a public caller. This
repository is public now, so the caller checks it out and runs this file
directly. Do not re-inline it -- two copies of a policy drift.

Deliberately dependency-free and line-based rather than YAML-parsing: it runs
on the self-hosted runner image, which ships no PyYAML.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

# A literal dollar-brace-brace anywhere in a workflow file would be parsed as a
# GitHub Actions expression before the job ever runs. This module is a separate
# file rather than a heredoc inside a workflow, so it is safe here -- but the
# constant stays for the benefit of anyone tempted to inline it again.
EXPR = "${{"


def indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def values(lines: list[str], index: int) -> list[str]:
    """The runner labels selected by the `runs-on:` at `index`.

    Handles both the inline form (`runs-on: [a, b]`, `runs-on: a`) and the
    block-sequence form spread over following lines.
    """
    line = lines[index]
    base = indent(line)
    value = line.split(":", 1)[1].split("#", 1)[0].strip()
    if value:
        return [p.strip().strip("\"'") for p in value.strip("[]").split(",") if p.strip()]
    result: list[str] = []
    for child in lines[index + 1:]:
        stripped = child.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if indent(child) <= base:
            break
        match = re.match(r"^-\s*([^#]+)", stripped)
        if match:
            result.append(match.group(1).strip().strip("\"'"))
    return result


def job_at(lines: list[str], index: int) -> str | None:
    """The job key enclosing the line at `index`.

    Exceptions are scoped per job, not per file: FarmEggs' ci.yml holds two
    compliant jobs and two excepted ones, and a file-wide exception there would
    also hide a future regression in the compliant pair.
    """
    jobs_indent = None
    for number in range(index, -1, -1):
        if re.match(r"^jobs\s*:", lines[number]):
            jobs_indent = indent(lines[number])
            break
    if jobs_indent is None:
        return None
    want = jobs_indent + 2
    for number in range(index, -1, -1):
        line = lines[number]
        if not line.strip() or line.strip().startswith("#"):
            continue
        if indent(line) != want:
            continue
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*:\s*(#.*)?$", line)
        if match:
            return match.group(1)
    return None


def load_exceptions(path: Path, today: str) -> tuple[list[dict], list[str]]:
    """Return (live exceptions, expiry errors).

    An expired entry is a hard failure in its own right -- that is what stops
    exceptions rotting silently -- and it never suppresses anything.
    """
    if not path.exists():
        return [], []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("exceptions"), list):
        return [], [f"{path}: not a schema_version 1 exceptions file"]

    live: list[dict] = []
    errors: list[str] = []
    for entry in payload["exceptions"]:
        expiry = str(entry.get("expires_on", ""))
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", expiry):
            errors.append(f"{path}: exception for {entry.get('repo')}/{entry.get('workflow')} "
                          f"has no valid expires_on")
            continue
        if expiry < today:
            errors.append(f"expired runner exception: {entry.get('repo')}/"
                          f"{entry.get('workflow')} expired {expiry}")
            continue
        live.append(entry)
    return live, errors


def excused(exceptions: list[dict], repo: str, workflow: str, job: str | None) -> dict | None:
    for entry in exceptions:
        if entry.get("repo") != repo or entry.get("workflow") != workflow:
            continue
        scope = entry.get("jobs")
        if scope is None or (job is not None and job in scope):
            return entry
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".",
                        help="repository checkout to audit")
    parser.add_argument("--repo", default="",
                        help="repository name the exceptions file is keyed by")
    parser.add_argument("--exceptions", default="",
                        help="path to runner-exceptions.json")
    parser.add_argument("--today", default=date.today().isoformat())
    args = parser.parse_args()

    root = Path(args.root)
    repo = args.repo or root.resolve().name
    exceptions_path = Path(args.exceptions) if args.exceptions else root / "runner-exceptions.json"
    exceptions, errors = load_exceptions(exceptions_path, args.today)
    for error in errors:
        print(error, file=sys.stderr)
    failed = bool(errors)

    workflows = sorted(
        p for pattern in (".github/workflows/*.yml", ".github/workflows/*.yaml")
        for p in root.glob(pattern)
    )
    if not workflows:
        print("no workflows found; nothing to audit")

    excused_count = 0
    for path in workflows:
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if not re.match(r"^\s*runs-on\s*:", line, re.I):
                continue
            selected = values(lines, number)
            hosted = not any(v.lower() == "self-hosted" for v in selected)
            dynamic = any(EXPR in v for v in selected)
            if not hosted and not dynamic:
                continue

            job = job_at(lines, number)
            entry = excused(exceptions, repo, path.name, job)
            if entry is not None:
                excused_count += 1
                print(f"{path}:{number + 1}: excused until {entry['expires_on']} "
                      f"({entry.get('reason', 'no reason recorded')})")
                continue

            where = f"{path}:{number + 1}"
            if job:
                where += f" (job {job})"
            if hosted:
                print(f"{where}: runner selection is not explicitly self-hosted "
                      f"-> {selected}", file=sys.stderr)
            if dynamic:
                print(f"{where}: dynamic runner selection requires explicit review",
                      file=sys.stderr)
            failed = True

    if failed:
        print("", file=sys.stderr)
        print("Organization policy: every job must select a self-hosted runner "
              "explicitly, e.g.", file=sys.stderr)
        print("  runs-on: [self-hosted, node-b, linux, x64]", file=sys.stderr)
        print("  runs-on: [self-hosted, node-b, linux, x64, docker, publish]  "
              "# needs Docker", file=sys.stderr)
        print("", file=sys.stderr)
        print("An owner-approved exception goes in github-policy/"
              "runner-exceptions.json.", file=sys.stderr)
        return 1

    suffix = f", {excused_count} excused" if excused_count else ""
    print(f"ok: {len(workflows)} workflow file(s) audited, all self-hosted{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
