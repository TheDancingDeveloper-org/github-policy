"""ci-auditor command line: lint | serve | once | validate | identity."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from . import __version__
from . import workflow as wfm
from .evaluate import evaluate
from .github import run_actionlint
from .identity import check as identity_check
from .identity import default_forbidden
from .model import Result, Snapshot
from .standard import StandardError, default_standard_dir, load, parse_exemptions


def snapshot_from_checkout(root: Path, repo: str, private: bool, default_branch: str) -> Snapshot:
    snap = Snapshot(repo=repo, full_name=repo, private=private, default_branch=default_branch)
    wdir = root / ".github" / "workflows"
    for path in sorted([*wdir.glob("*.yml"), *wdir.glob("*.yaml")]):
        try:
            snap.workflows.append(wfm.parse(path.name, path.read_text(encoding="utf-8")))
        except Exception as err:  # noqa: BLE001
            snap.parse_errors[path.name] = f"{type(err).__name__}: {str(err).splitlines()[0][:160]}"
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=True)
        snap.tree = out.stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        snap.tree = [
            str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git" not in p.parts
        ]
    return snap


def _annotation(level: str, r: Result, message: str, workflow: str | None) -> str:
    file = f" file=.github/workflows/{workflow}," if workflow else " "
    title = f"{r.rule.id} {r.rule.title}".replace(",", " ")
    return f"::{level}{file}title={title}::{message} - fix: {r.rule.fix}".replace("\n", " ")


def cmd_lint(args: argparse.Namespace) -> int:
    std_dir = Path(args.standard) if args.standard else default_standard_dir()
    std, exemptions = load(std_dir)
    if args.exemptions:
        exemptions = parse_exemptions(Path(args.exemptions).read_text(encoding="utf-8"), std)
    root = Path(args.root)
    snap = snapshot_from_checkout(root, args.repo or root.resolve().name, args.private, args.default_branch)
    if args.actionlint:
        run_actionlint(snap, args.actionlint)
    results = evaluate(snap, std, exemptions, date.fromisoformat(args.today), kinds=("static",))
    blocking = 0
    summary = [
        f"### CI guardrails ({'enforce' if args.enforce else 'report only'})",
        "",
        "| rule | status | findings |",
        "|---|---|---|",
    ]
    for r in results:
        if r.status in ("na", "unknown") and not r.findings:
            continue
        summary.append(f"| {r.rule.id} {r.rule.title} | {r.status} | {len(r.findings)} |")
        level = "error" if args.enforce and r.rule.enforce == "block" else "warning"
        for f in r.findings:
            if args.format == "github":
                print(_annotation(level, r, f.message, f.workflow))
            else:
                where = "/".join(x for x in (f.workflow, f.job) if x)
                print(f"{r.rule.id} [{r.status}] {where}: {f.message}")
            if level == "error":
                blocking += 1
        for f, e in r.exempted:
            print(f"{r.rule.id} [exempt until {e.expires}, {e.owner}] {f.workflow or ''}: {f.message}")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(summary) + "\n")
    fails = sum(len(r.findings) for r in results)
    print(f"ci-guardrails: {len(snap.workflows)} workflow(s), {fails} finding(s), {blocking} blocking")
    return 1 if blocking else 0


def cmd_identity(args: argparse.Namespace) -> int:
    forbidden = default_forbidden() + [x for x in args.forbidden.split(",") if x]
    found = identity_check(Path(args.root), args.base or None, args.head, forbidden, content=args.content)
    level = "error" if args.enforce else "warning"
    for f in found:
        print(f"::{level} title=identity policy::{f.where}: {f.message}")
    print(
        f"identity policy: {len(found)} finding(s) (author/committer, paths{', content' if args.content else ''};"
        " commit bodies are not read)"
    )
    return 1 if found and args.enforce else 0


def cmd_validate(args: argparse.Namespace) -> int:
    from .checks import CHECKS

    std_dir = Path(args.standard) if args.standard else default_standard_dir()
    try:
        std, exemptions = load(std_dir)
    except StandardError as err:
        print(f"invalid: {err}", file=sys.stderr)
        return 1
    missing = sorted({r.check for r in std.rules} - CHECKS.keys())
    unused = sorted(CHECKS.keys() - {r.check for r in std.rules})
    if missing or unused:
        print(f"rules/checks mismatch: missing={missing} unused={unused}", file=sys.stderr)
        return 1
    today = date.fromisoformat(args.today)
    expired = [e for e in exemptions if e.expires < today]
    for e in expired:
        print(f"expired exemption: {e.repo} {e.rule} ({e.owner}) expired {e.expires}", file=sys.stderr)
    print(f"ok: {len(std.rules)} rules, {len(std.classes)} runner classes, {len(exemptions)} exemptions")
    return 1 if expired and args.fail_expired else 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import Auditor, serve

    auditor = Auditor.from_env()
    if args.once:
        auditor.audit()
        auditor.refresh_main()
        sys.stdout.write(auditor.render())
        return 0
    serve(auditor)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ci-auditor", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    today = datetime.now(UTC).date().isoformat()

    lint = sub.add_parser("lint", help="static rules over one checkout (the policy gate)")
    lint.add_argument("root", nargs="?", default=".")
    lint.add_argument("--repo", default="")
    lint.add_argument("--private", action=argparse.BooleanOptionalAction, default=True)
    lint.add_argument("--default-branch", default="main")
    lint.add_argument("--standard", default="")
    lint.add_argument("--exemptions", default="", help="exemptions.yaml to use instead of the bundled one")
    lint.add_argument("--actionlint", default="", help="actionlint binary (CI-17)")
    lint.add_argument("--enforce", action="store_true", help="fail on findings of `enforce: block` rules")
    lint.add_argument("--format", choices=("github", "text"), default="text")
    lint.add_argument("--today", default=today)
    lint.set_defaults(fn=cmd_lint)

    ident = sub.add_parser("identity", help="author/committer, path and content identity check")
    ident.add_argument("root", nargs="?", default=".")
    ident.add_argument("--base", default="")
    ident.add_argument("--head", default="HEAD")
    ident.add_argument("--forbidden", default="", help="extra comma-separated identifiers")
    ident.add_argument("--content", action=argparse.BooleanOptionalAction, default=True)
    ident.add_argument("--enforce", action="store_true")
    ident.set_defaults(fn=cmd_identity)

    val = sub.add_parser("validate", help="validate rules.yaml and exemptions.yaml")
    val.add_argument("--standard", default="")
    val.add_argument("--today", default=today)
    val.add_argument("--fail-expired", action="store_true")
    val.set_defaults(fn=cmd_validate)

    srv = sub.add_parser("serve", help="org-wide auditor and Prometheus exporter")
    srv.add_argument("--once", action="store_true", help="one audit, print metrics, exit")
    srv.set_defaults(fn=cmd_serve)

    args = parser.parse_args(argv)
    try:
        return int(args.fn(args))
    except StandardError as err:
        print(json.dumps({"msg": "ci_standard_invalid", "error": str(err)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
