"""rules.yaml / exemptions.yaml validation, rendering, identity and the lint CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from ci_auditor import render
from ci_auditor.__main__ import main
from ci_auditor.evaluate import evaluate
from ci_auditor.identity import check as identity_check
from ci_auditor.model import Exemption, Snapshot
from ci_auditor.standard import StandardError, load, parse_exemptions, parse_rules

ROOT = Path(__file__).resolve().parents[2]
STD_DIR = ROOT / "ci-standard"


def test_shipped_standard_is_valid() -> None:
    std, exemptions = load(STD_DIR)
    assert len(std.rules) >= 24
    assert {"ci-small", "rust-heavy", "docker", "publish", "windows", "hosted"} <= {
        c.name for c in std.classes
    }
    assert all(e.rule in {r.id for r in std.rules} for e in exemptions)
    assert main(["validate", "--standard", str(STD_DIR), "--today", "2026-11-02"]) == 0


def test_only_static_rules_block() -> None:
    std, _ = load(STD_DIR)
    assert all(r.kind == "static" for r in std.rules if r.enforce == "block")


def test_bare_self_hosted_class_rejected() -> None:
    text = (STD_DIR / "rules.yaml").read_text().replace("forms: [[ci-small]]", "forms: [[self-hosted]]")
    with pytest.raises(StandardError, match="bare self-hosted"):
        parse_rules(text)


GOOD_EX = """
version: 1
exemptions:
  - {repo: demo, rule: CI-05, reason: "a reason that is long enough to say why and what ends it",
     owner: oversight, created: 2026-11-01, expires: 2026-12-01}
"""


@pytest.mark.parametrize(
    "change,match",
    [
        (("CI-05", "CI-99"), "unknown rule"),
        (("CI-05", "CI-*"), "unknown rule"),
        (("expires: 2026-12-01", "expires: 2027-12-01"), "longer than"),
        (("expires: 2026-12-01", "expires: 2026-10-01"), "after created"),
        (("owner: oversight, ", ""), "owner is required"),
        (('reason: "a reason that is long enough to say why and what ends it"', "reason: short"), ">= 40"),
        (("owner: oversight,", "owner: oversight, job: lint,"), "job needs workflow"),
        (("repo: demo", "repo: org/demo"), "bare repository"),
    ],
)
def test_bad_exemptions_are_loud(change: tuple[str, str], match: str) -> None:
    std, _ = load(STD_DIR)
    assert len(parse_exemptions(GOOD_EX, std)) == 1
    with pytest.raises(StandardError, match=match):
        parse_exemptions(GOOD_EX.replace(*change), std)


def test_metrics_render() -> None:
    std, _ = load(STD_DIR)
    snap = Snapshot(repo="demo", full_name="o/demo", private=True)
    results = evaluate(snap, std, [], date(2026, 11, 2), kinds=("static",))
    ex = Exemption("demo", "CI-05", "r" * 40, "oversight", date(2026, 11, 1), date(2026, 11, 2))
    now = datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC).timestamp()
    text = render.metrics(results, [snap], [ex], now, now, now, 1.5, {("repos", 200): 3}, 0)
    assert 'ci_conformance{repo="demo",rule="CI-01"' in text
    assert text.count('rule="CI-01",title=') == 5  # one-hot over five statuses
    assert (
        'ci_exemption_expiry_seconds{repo="demo",rule="CI-05",workflow="",job="",owner="oversight"} 86399'
        in text
    )
    assert "ci_audit_last_run_timestamp" in text and 'visibility="private"' in text
    assert (
        render.main_checks({"demo": {"ci": "failure"}}).splitlines()[-1]
        == 'ci_main_required_check{repo="demo",context="ci",conclusion="failure"} 1'
    )


def test_finding_lines_are_json() -> None:
    std, _ = load(STD_DIR)
    from ci_auditor import workflow as wfm

    snap = Snapshot(repo="demo", full_name="o/demo", private=True)
    snap.workflows.append(
        wfm.parse(
            "w.yml", "on: pull_request\njobs:\n  a:\n    runs-on: [self-hosted]\n    steps: [{run: 'true'}]\n"
        )
    )
    lines = render.finding_lines(
        evaluate(snap, std, [], date(2026, 11, 2), kinds=("static",)), datetime(2026, 11, 2, tzinfo=UTC)
    )
    parsed = [json.loads(line) for line in lines]
    assert {"CI-05", "CI-11", "CI-12"} <= {p["rule"] for p in parsed}
    assert all(p["msg"] == "ci_finding" and p["repo"] == "demo" for p in parsed)


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "worker")
    _git(tmp_path, "config", "user.email", "worker@example.com")
    (tmp_path / "a.txt").write_text("hello\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_identity_ignores_commit_bodies(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "b.txt").write_text("more\n")
    _git(root, "add", ".")
    _git(
        root, "commit", "-q", "-m", "change\n\nClaimed-Author: Forbidden Person <forbiddenperson@example.com>"
    )
    assert identity_check(root, "HEAD~1", "HEAD", ["forbiddenperson"]) == []


def test_identity_flags_author_path_and_content(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "forbiddenperson.md").write_text("ok\n")
    (root / "c.txt").write_text("contact forbiddenperson here\n")
    _git(root, "add", ".")
    _git(root, "-c", "user.email=forbiddenperson@example.com", "commit", "-q", "-m", "x")
    found = {f.message for f in identity_check(root, "HEAD~1", "HEAD", ["forbiddenperson"])}
    assert found == {
        "forbidden identifier in author email",
        "forbidden identifier in committer email",
        "forbidden identifier in a tracked path",
        "forbidden identifier in tracked content",
    }
    no_content = {
        f.message for f in identity_check(root, "HEAD~1", "HEAD", ["forbiddenperson"], content=False)
    }
    assert "forbidden identifier in tracked content" not in no_content


def test_lint_cli_report_vs_enforce(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wdir = tmp_path / ".github" / "workflows"
    wdir.mkdir(parents=True)
    (wdir / "ci.yml").write_text(
        "on: pull_request\njobs:\n  a:\n    runs-on: [self-hosted]\n    steps: [{run: 'true'}]\n"
    )
    base = [
        "lint",
        str(tmp_path),
        "--repo",
        "demo",
        "--standard",
        str(STD_DIR),
        "--today",
        "2026-11-02",
        "--format",
        "github",
    ]
    assert main(base) == 0
    out = capsys.readouterr().out
    assert "::warning file=.github/workflows/ci.yml,title=CI-05" in out
    assert main([*base, "--enforce"]) == 1
    assert "::error file=.github/workflows/ci.yml,title=CI-05" in capsys.readouterr().out


def test_actionlint_config_lists_every_class_label() -> None:
    import yaml

    std, _ = load(STD_DIR)
    custom = {label for c in std.classes if not c.public_only for form in c.forms for label in form} - {
        "self-hosted"
    }
    config = yaml.safe_load((STD_DIR / "actionlint.yaml").read_text())
    assert set(config["self-hosted-runner"]["labels"]) == custom


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not installed")
def test_run_actionlint_with_relative_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from ci_auditor import workflow as wfm
    from ci_auditor.github import run_actionlint

    monkeypatch.chdir(ROOT)
    snap = Snapshot(repo="demo", full_name="o/demo", private=True)
    snap.workflows.append(
        wfm.parse(
            "w.yml",
            "on: push\njobs:\n  a:\n    runs-on: ci-small\n    steps:\n"
            "      - run: echo ${{ github.nope }}\n",
        )
    )
    run_actionlint(snap, None, Path("ci-standard/actionlint.yaml"))
    assert snap.gaps == []
    assert snap.actionlint and "nope" in snap.actionlint[0]
