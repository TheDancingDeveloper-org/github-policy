"""Tests for the runner-policy checker.

Run with `python3 -m pytest tests/` or plain `python3 tests/test_runner_policy.py`.
No third-party imports, so it runs on the self-hosted image as-is.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

CHECKER = Path(__file__).resolve().parent.parent / "scripts" / "runner_policy.py"
TODAY = "2026-08-17"


def audit(workflows: dict[str, str], exceptions: dict | None = None,
          repo: str = "example", today: str = TODAY) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".github" / "workflows").mkdir(parents=True)
        for name, body in workflows.items():
            (root / ".github" / "workflows" / name).write_text(body)
        exceptions_path = root / "runner-exceptions.json"
        exceptions_path.write_text(json.dumps(exceptions or {"schema_version": 1, "exceptions": []}))
        result = subprocess.run(
            [sys.executable, str(CHECKER), str(root), "--repo", repo,
             "--exceptions", str(exceptions_path), "--today", today],
            capture_output=True, text=True,
        )
        return result.returncode, result.stdout + result.stderr


COMPLIANT = """name: CI
jobs:
  build:
    runs-on: [self-hosted, node-b, linux, x64]
    steps:
      - run: true
"""

HOSTED = """name: CI
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: true
"""

TWO_JOBS = """name: CI
jobs:
  good:
    runs-on: [self-hosted, node-b, linux, x64]
    steps:
      - run: true
  bad:
    runs-on: ubuntu-latest
    steps:
      - run: true
"""


def test_compliant_passes():
    code, out = audit({"ci.yml": COMPLIANT})
    assert code == 0, out


def test_hosted_fails():
    code, out = audit({"ci.yml": HOSTED})
    assert code == 1
    assert "not explicitly self-hosted" in out


def test_dynamic_selection_fails():
    code, out = audit({"ci.yml": HOSTED.replace("ubuntu-latest", "${{ matrix.os }}")})
    assert code == 1
    assert "dynamic runner selection" in out


def test_block_sequence_form_is_understood():
    code, out = audit({"ci.yml": """name: CI
jobs:
  build:
    runs-on:
      - self-hosted
      - node-b
    steps:
      - run: true
"""})
    assert code == 0, out


def test_commented_out_runs_on_is_not_a_violation():
    # Several repos carry `# runs-on: ubuntu-latest` in a warning comment.
    code, out = audit({"ci.yml": COMPLIANT + "    # `runs-on: ubuntu-latest` is rejected here\n"})
    assert code == 0, out


def test_file_wide_exception_excuses():
    code, out = audit(
        {"ci.yml": HOSTED},
        {"schema_version": 1, "exceptions": [
            {"repo": "example", "workflow": "ci.yml", "reason": "r", "expires_on": "2026-12-01"}]},
    )
    assert code == 0, out
    assert "excused" in out


def test_job_scoped_exception_does_not_excuse_a_sibling():
    code, out = audit(
        {"ci.yml": TWO_JOBS.replace("""  good:
    runs-on: [self-hosted, node-b, linux, x64]""", """  good:
    runs-on: ubuntu-latest""")},
        {"schema_version": 1, "exceptions": [
            {"repo": "example", "workflow": "ci.yml", "jobs": ["bad"],
             "reason": "r", "expires_on": "2026-12-01"}]},
    )
    assert code == 1, out
    assert "job good" in out
    assert "job bad" not in out


def test_job_scoped_exception_excuses_its_own_job():
    code, out = audit(
        {"ci.yml": TWO_JOBS},
        {"schema_version": 1, "exceptions": [
            {"repo": "example", "workflow": "ci.yml", "jobs": ["bad"],
             "reason": "r", "expires_on": "2026-12-01"}]},
    )
    assert code == 0, out


def test_exception_for_another_repo_does_not_apply():
    code, out = audit(
        {"ci.yml": HOSTED},
        {"schema_version": 1, "exceptions": [
            {"repo": "somewhere-else", "workflow": "ci.yml", "reason": "r",
             "expires_on": "2026-12-01"}]},
    )
    assert code == 1, out


def test_expired_exception_fails_and_suppresses_nothing():
    code, out = audit(
        {"ci.yml": HOSTED},
        {"schema_version": 1, "exceptions": [
            {"repo": "example", "workflow": "ci.yml", "reason": "r", "expires_on": "2026-08-16"}]},
    )
    assert code == 1
    assert "expired runner exception" in out
    assert "not explicitly self-hosted" in out


def test_exception_expiring_today_is_still_live():
    code, out = audit(
        {"ci.yml": HOSTED},
        {"schema_version": 1, "exceptions": [
            {"repo": "example", "workflow": "ci.yml", "reason": "r", "expires_on": TODAY}]},
    )
    assert code == 0, out


def test_exception_without_expiry_is_rejected():
    code, out = audit(
        {"ci.yml": COMPLIANT},
        {"schema_version": 1, "exceptions": [{"repo": "example", "workflow": "ci.yml"}]},
    )
    assert code == 1
    assert "no valid expires_on" in out


def test_shipped_exceptions_file_is_valid_and_live():
    path = Path(__file__).resolve().parent.parent / "runner-exceptions.json"
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 1
    for entry in payload["exceptions"]:
        assert entry["repo"] and entry["workflow"]
        assert len(entry["reason"]) > 40, f"{entry['repo']}: record a real reason"
        assert entry["expires_on"] >= TODAY, f"{entry['repo']}: exception has expired"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as error:
            failures += 1
            print(f"FAIL {name}: {error}")
    sys.exit(1 if failures else 0)
