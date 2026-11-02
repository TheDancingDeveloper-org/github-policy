"""Each rule's check against a passing and a failing workflow."""

from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path

import pytest

from ci_auditor import workflow as wfm
from ci_auditor.checks import CHECKS
from ci_auditor.evaluate import evaluate
from ci_auditor.model import Exemption, RuntimeJob, RuntimeRun, Snapshot
from ci_auditor.standard import Standard, load

STD_DIR = Path(__file__).resolve().parents[2] / "ci-standard"


@pytest.fixture(scope="module")
def std() -> Standard:
    return load(STD_DIR)[0]


GOOD = """
name: CI
on:
  pull_request:
  merge_group:
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name != 'merge_group' }}
jobs:
  lint:
    runs-on: ci-small
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v5
      - uses: Swatinem/rust-cache@9d47c6ad4b02e050fd481d890b2ea34778fd09d6
        with:
          save-if: ${{ github.ref == 'refs/heads/main' }}
      - run: cargo clippy
  e2e:
    if: github.event_name == 'merge_group'
    runs-on: [self-hosted, docker]
    timeout-minutes: 40
    steps:
      - run: docker build .
  ci:
    if: always()
    needs: [lint, e2e]
    runs-on: ci-small
    timeout-minutes: 5
    steps:
      - run: echo gate
  policy:
    uses: TheDancingDeveloper-org/github-policy/.github/workflows/policy.yml@v1.0.0
    with: {ref: v1.0.0}
"""


def snap(*files: tuple[str, str], private: bool = True, **kw: object) -> Snapshot:
    s = Snapshot(repo="demo", full_name="TheDancingDeveloper-org/demo", private=private)
    for name, text in files:
        s.workflows.append(wfm.parse(name, textwrap.dedent(text)))
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def run(std: Standard, rule: str, s: Snapshot) -> tuple[str, list[str]]:
    r = std.rule(rule)
    out = CHECKS[r.check](s, std, r.id)
    return out.status, [f.message for f in out.findings]


def test_every_rule_has_a_check_and_back(std: Standard) -> None:
    assert {r.check for r in std.rules} == set(CHECKS)
    assert [r.id for r in std.rules] == [f"CI-{i:02d}" for i in range(1, len(std.rules) + 1)]


def test_good_workflow_passes_every_static_rule(std: Standard) -> None:
    s = snap(("ci.yml", GOOD), tree=[".github/workflows/ci.yml"])
    s.actionlint = []
    results = {r.rule.id: r for r in evaluate(s, std, [], date(2026, 11, 2), kinds=("static",))}
    failing = {k: [f.message for f in r.findings] for k, r in results.items() if r.status == "fail"}
    assert failing == {}


def test_push_everywhere_fails(std: Standard) -> None:
    bad = GOOD.replace("on:\n  pull_request:", "on:\n  push:\n    branches: ['**']\n  pull_request:")
    status, msgs = run(std, "CI-01", snap(("ci.yml", bad)))
    assert status == "fail" and "['**']" in msgs[0]
    bare = GOOD.replace("on:\n  pull_request:", "on:\n  push:\n  pull_request:")
    assert run(std, "CI-01", snap(("ci.yml", bare)))[0] == "fail"


def test_main_retest(std: Standard) -> None:
    bad = GOOD.replace("on:\n  pull_request:", "on:\n  push:\n    branches: [main]\n  pull_request:")
    status, msgs = run(std, "CI-02", snap(("ci.yml", bad)))
    assert status == "fail" and "lint" in msgs[0]
    publish_only = """
    on: {push: {branches: [main]}, pull_request: {}}
    jobs:
      image:
        if: github.event_name == 'push'
        runs-on: [self-hosted, publish]
        timeout-minutes: 30
        steps: [{run: docker push x}]
    """
    assert run(std, "CI-02", snap(("build.yml", publish_only)))[0] == "pass"


def test_merge_group_vs_queue(std: Standard) -> None:
    s = snap(("ci.yml", GOOD), merge_queue_known=True, merge_queue=None, required_contexts=["ci"])
    assert run(std, "CI-03", s)[0] == "fail"
    s.merge_queue = {"merge_method": "REBASE"}
    assert run(std, "CI-03", s)[0] == "pass"
    assert run(std, "CI-03", snap(("ci.yml", GOOD)))[0] == "unknown"


def test_heavy_on_pr(std: Standard) -> None:
    bad = GOOD.replace("    if: github.event_name == 'merge_group'\n", "")
    status, msgs = run(std, "CI-04", snap(("ci.yml", bad)))
    assert status == "fail" and "docker" in msgs[0]


@pytest.mark.parametrize(
    "runs_on,private,ok",
    [
        ("ci-small", True, True),
        ("[self-hosted]", True, False),
        ("[self-hosted, linux, x64]", True, False),
        ("[self-hosted, docker, publish]", True, True),
        ("ubuntu-24.04", False, True),
        ("ubuntu-24.04", True, False),
        ("ubuntu-latest", False, False),
        ("[self-hosted, graft-dev]", True, False),
        ("${{ inputs.runner }}", True, False),
    ],
)
def test_runner_classes(std: Standard, runs_on: str, private: bool, ok: bool) -> None:
    wf = f"""
    on: workflow_dispatch
    jobs:
      j:
        runs-on: {runs_on}
        timeout-minutes: 5
        steps: [{{run: 'true'}}]
    """
    status, _ = run(std, "CI-05", snap(("w.yml", wf), private=private))
    assert (status == "pass") is ok


def test_runner_matrix_resolves(std: Standard) -> None:
    wf = """
    on: pull_request
    jobs:
      t:
        strategy: {matrix: {os: [ubuntu-24.04, windows-2025]}}
        runs-on: ${{ matrix.os }}
        timeout-minutes: 5
        steps: [{run: 'true'}]
    """
    assert run(std, "CI-05", snap(("w.yml", wf), private=False))[0] == "pass"
    assert run(std, "CI-05", snap(("w.yml", wf), private=True))[0] == "fail"


def test_publish_scope(std: Standard) -> None:
    wf = """
    on: [pull_request, push]
    jobs:
      e2e:
        runs-on: [self-hosted, docker, publish]
        timeout-minutes: 5
        steps: [{run: 'true'}]
    """
    status, msgs = run(std, "CI-06", snap(("e2e.yml", wf)))
    assert status == "fail" and "pull_request" in msgs[0]


def test_runtime_labels(std: Standard) -> None:
    jobs = [
        RuntimeJob(
            ".github/workflows/ci.yml",
            "CI",
            "lint",
            ("self-hosted",),
            "success",
            1,
            "success",
            "pull_request",
            "graft/batch-1",
        ),
        RuntimeJob(
            ".github/workflows/ci.yml",
            "CI",
            "fmt",
            ("ci-small",),
            "success",
            1,
            "success",
            "pull_request",
            "graft/batch-1",
        ),
    ]
    status, msgs = run(std, "CI-07", snap(("ci.yml", GOOD), runtime_jobs=jobs))
    assert status == "fail" and msgs == ["1 recent job(s) ran with labels ['self-hosted']"]
    assert run(std, "CI-07", snap(("ci.yml", GOOD)))[0] == "unknown"


def test_gate_shape(std: Standard) -> None:
    assert run(std, "CI-08", snap(("ci.yml", GOOD), required_contexts=["ci", "policy"]))[0] == "pass"
    status, msgs = run(std, "CI-08", snap(("ci.yml", GOOD), required_contexts=["rust", "policy"]))
    assert status == "fail" and any("`ci`" in m for m in msgs) and any("rust" in m for m in msgs)
    no_always = GOOD.replace("    if: always()\n", "")
    status, msgs = run(std, "CI-08", snap(("ci.yml", no_always), required_contexts=["ci"]))
    assert status == "fail" and "always()" in msgs[0]


def test_gate_producers(std: Standard) -> None:
    filtered = GOOD.replace("  pull_request:\n", "  pull_request:\n    paths: ['src/**']\n")
    status, msgs = run(std, "CI-09", snap(("ci.yml", filtered), required_contexts=["ci"]))
    assert status == "fail" and "path-filtered" in msgs[0]
    status, msgs = run(std, "CI-09", snap(("ci.yml", GOOD), required_contexts=["build"]))
    assert status == "fail" and "no producing job" in msgs[0]
    reusable = snap(("ci.yml", GOOD), required_contexts=["policy / policy"])
    assert run(std, "CI-09", reusable)[0] == "pass"


def test_merge_queue(std: Standard) -> None:
    s = snap(
        ("ci.yml", GOOD),
        merge_queue_known=True,
        required_contexts=["ci"],
        merge_queue={"merge_method": "SQUASH", "grouping_strategy": "ALLGREEN"},
    )
    status, msgs = run(std, "CI-10", s)
    assert status == "fail" and "SQUASH" in msgs[0]
    public = snap(
        ("ci.yml", GOOD), private=False, merge_queue_known=True, merge_queue=None, required_contexts=["ci"]
    )
    assert run(std, "CI-10", public)[0] == "fail"
    private = snap(
        ("ci.yml", GOOD), private=True, merge_queue_known=True, merge_queue=None, required_contexts=["ci"]
    )
    assert run(std, "CI-10", private)[0] == "na"


def test_timeouts_concurrency_continue_on_error(std: Standard) -> None:
    bad = """
    on: {pull_request: {}, merge_group: {}}
    concurrency: {group: x, cancel-in-progress: true}
    jobs:
      a:
        runs-on: ci-small
        continue-on-error: true
        steps: [{run: 'true'}]
    """
    s = snap(("ci.yml", bad))
    assert run(std, "CI-11", s) == ("fail", ["no timeout-minutes"])
    assert run(std, "CI-12", s)[1] == ["cancel-in-progress: true also cancels merge-queue runs"]
    assert run(std, "CI-13", s)[0] == "fail"


def test_masked_failures(std: Standard) -> None:
    jobs = [
        RuntimeJob(
            ".github/workflows/ci.yml",
            "CI",
            "lint",
            ("ci-small",),
            "failure",
            7,
            "success",
            "merge_group",
            "gh-readonly-queue/main/x",
        )
    ]
    s = snap(("ci.yml", GOOD), runtime_jobs=jobs, required_contexts=["ci"])
    assert run(std, "CI-14", s)[0] == "fail"


def test_policy_gate(std: Standard) -> None:
    assert run(std, "CI-15", snap(("ci.yml", GOOD)))[0] == "pass"
    at_main = GOOD.replace("policy.yml@v1.0.0", "policy.yml@main")
    assert "not a tag or SHA" in run(std, "CI-15", snap(("ci.yml", at_main)))[1][0]


def test_identity_scope(std: Standard) -> None:
    s = snap(("ci.yml", GOOD), tree=["ci/migration-policy.sh"])
    assert run(std, "CI-16", s)[0] == "fail"
    grep_body = """
    on: pull_request
    jobs:
      p:
        runs-on: ci-small
        timeout-minutes: 5
        steps: [{run: "git show -s --format=%B HEAD | grep -i name"}]
    """
    assert run(std, "CI-16", snap(("p.yml", grep_body), tree=[]))[0] == "fail"


def test_actionlint(std: Standard) -> None:
    s = snap(("ci.yml", GOOD), actionlint=[".github/workflows/ci.yml:3:1: unexpected key [syntax-check]"])
    status, _ = run(std, "CI-17", s)
    assert status == "fail"
    assert run(std, "CI-17", snap(("ci.yml", GOOD)))[0] == "unknown"


def test_pinned_actions(std: Standard) -> None:
    unpinned = GOOD.replace(
        "Swatinem/rust-cache@9d47c6ad4b02e050fd481d890b2ea34778fd09d6", "Swatinem/rust-cache@v2"
    )
    status, msgs = run(std, "CI-18", snap(("ci.yml", unpinned)))
    assert status == "fail" and "rust-cache@v2" in msgs[0]


def test_rust_cache_and_sccache(std: Standard) -> None:
    always = GOOD.replace("save-if: ${{ github.ref == 'refs/heads/main' }}", "shared-key: x")
    assert run(std, "CI-19", snap(("ci.yml", always)))[0] == "fail"
    scc = """
    on: pull_request
    env: {RUSTC_WRAPPER: sccache}
    jobs:
      b:
        runs-on: [self-hosted, rust-heavy]
        timeout-minutes: 30
        steps: [{run: cargo build}]
    """
    assert run(std, "CI-20", snap(("b.yml", scc)))[0] == "fail"
    ok = scc.replace("RUSTC_WRAPPER: sccache", "RUSTC_WRAPPER: sccache, SCCACHE_IGNORE_SERVER_IO_ERROR: '1'")
    assert run(std, "CI-20", snap(("b.yml", ok)))[0] == "pass"
    assert run(std, "CI-20", snap(("ci.yml", GOOD)))[0] == "na"


RELEASE = """
on: {push: {tags: ['v*']}}
jobs:
  build: {runs-on: [self-hosted, publish], timeout-minutes: 60, steps: [{run: make}]}
"""
DRY = """
name: Release dry run
on: {schedule: [{cron: '0 3 * * *'}], pull_request: {paths: ['release/**']}}
jobs:
  dry: {runs-on: [self-hosted, docker], timeout-minutes: 60, steps: [{run: make}]}
"""


def test_release_dry_run(std: Standard) -> None:
    assert run(std, "CI-21", snap(("ci.yml", GOOD)))[0] == "na"
    assert run(std, "CI-21", snap(("release.yml", RELEASE)))[0] == "fail"
    assert run(std, "CI-21", snap(("release.yml", RELEASE), ("release-dry-run.yml", DRY)))[0] == "pass"


def test_dry_run_green(std: Standard) -> None:
    runs = [
        RuntimeRun(".github/workflows/release-dry-run.yml", "schedule", "main", c, f"2026-11-0{i}T03:00:00Z")
        for i, c in enumerate(["failure", "failure", "failure", "success"], start=1)
    ]
    s = snap(("release.yml", RELEASE), ("release-dry-run.yml", DRY), runtime_runs=runs)
    status, msgs = run(std, "CI-22", s)
    assert status == "fail" and "3 nightly" in msgs[0]
    s.runtime_runs = runs[-1:]
    assert run(std, "CI-22", s)[0] == "pass"


def test_private_deps(std: Standard) -> None:
    wf = """
    on: pull_request
    jobs:
      b:
        runs-on: [self-hosted, docker]
        timeout-minutes: 30
        steps:
          - run: git clone https://github.com/TheDancingDeveloper-org/msp-klaudia.git
          - run: cargo install --git https://repo.indexarr.net/indexarr/tool
          - uses: docker/build-push-action@263435318d21b8e681c14492fe198d362a7d2c83
            with: {build-args: "TOKEN=${{ secrets.T }}"}
    """
    s = snap(("b.yml", wf), org_visibility={"msp-klaudia": True})
    status, msgs = run(std, "CI-23", s)
    assert status == "fail" and len(msgs) == 3
    s.org_visibility = {"msp-klaudia": False}
    assert len(run(std, "CI-23", s)[1]) == 2


def test_codeql(std: Standard) -> None:
    wf = """
    on: {pull_request: {}, push: {tags: ['v*']}}
    jobs:
      analyze: {runs-on: [self-hosted, bigmem], timeout-minutes: 60, steps: [{uses: github/codeql-action/init@v3}]}
    """
    assert run(std, "CI-24", snap(("codeql.yml", wf)))[1] == ["CodeQL runs on ['pull_request']"]
    ok = wf.replace("pull_request: {}, ", "workflow_dispatch: {}, ")
    assert run(std, "CI-24", snap(("codeql.yml", ok)))[0] == "pass"


def test_exemption_suppresses_until_expiry(std: Standard) -> None:
    bad = GOOD.replace(
        "runs-on: ci-small\n    timeout-minutes: 10", "runs-on: [self-hosted]\n    timeout-minutes: 10"
    )
    s = snap(("ci.yml", bad))
    ex = Exemption(
        repo="demo",
        rule="CI-05",
        reason="x" * 40,
        owner="oversight",
        created=date(2026, 11, 1),
        expires=date(2026, 11, 10),
        workflow="ci.yml",
        job="lint",
    )
    on_day = {r.rule.id: r for r in evaluate(s, std, [ex], date(2026, 11, 10), kinds=("static",))}
    assert on_day["CI-05"].status == "exempt" and len(on_day["CI-05"].exempted) == 1
    after = {r.rule.id: r for r in evaluate(s, std, [ex], date(2026, 11, 11), kinds=("static",))}
    assert after["CI-05"].status == "fail"
    other_job = Exemption(**{**ex.__dict__, "job": "e2e"})
    scoped = {r.rule.id: r for r in evaluate(s, std, [other_job], date(2026, 11, 5), kinds=("static",))}
    assert scoped["CI-05"].status == "fail"


def test_policy_ref_must_repeat_uses(std: Standard) -> None:
    drift = GOOD.replace("with: {ref: v1.0.0}", "with: {ref: v0.9.0}")
    status, msgs = run(std, "CI-15", snap(("ci.yml", drift)))
    assert status == "fail" and "must repeat" in msgs[0]


def test_reusable_runs_on_input_default(std: Standard) -> None:
    wf = """
    on:
      workflow_call:
        inputs:
          runs-on: {type: string, default: '["self-hosted", "rust-heavy"]'}
    jobs:
      r:
        runs-on: ${{ fromJSON(inputs.runs-on) }}
        timeout-minutes: 30
        steps: [{run: cargo test}]
    """
    assert run(std, "CI-05", snap(("rust-ci.yml", wf)))[0] == "pass"
    bare = wf.replace('["self-hosted", "rust-heavy"]', '["self-hosted"]')
    assert run(std, "CI-05", snap(("rust-ci.yml", bare)))[0] == "fail"
