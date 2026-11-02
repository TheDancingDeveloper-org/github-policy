"""One function per rule `check:` in ci-standard/rules.yaml.

Each check reads a Snapshot and returns an Outcome. It never raises for
missing data: data it needs but did not get makes it `unknown`; a repository
the rule does not apply to is `na`.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from . import workflow as wfm
from .model import Finding, Snapshot
from .standard import Standard
from .workflow import Job

OutcomeStatus = Literal["pass", "fail", "na", "unknown"]
POLICY_WORKFLOW = "TheDancingDeveloper-org/github-policy/.github/workflows/policy.yml@"
SEMVER_TAG = re.compile(r"^v\d+(\.\d+){0,2}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
HEAVY_STEP = re.compile(
    r"docker\s+(buildx\s+)?build|docker\s+compose\s+.*\bup\b|docker-compose\s+.*\bup\b|"
    r"playwright\s+test"
)
BODY_GREP = re.compile(r"(%B|%b|--format=%B|--pretty=%B|format:%B)")
PRIVATE_FETCH = re.compile(
    r"(git\s+clone\s+\S*|cargo\s+install\s+.*--git\s+\S+|pip\s+install\s+\S*git\+\S+|uv\s+pip\s+install\s+\S*git\+\S+|"
    r"npm\s+(i|install)\s+\S*github:\S+)"
)
GITHUB_REPO_URL = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(\.git)?(?=[\s/@#'\"]|$)")
FORGEJO_URL = re.compile(r"repo\.indexarr\.net[/:]")
ORG = "TheDancingDeveloper-org"
OPEN_OWNERS = {"actions", "github", ORG}


@dataclass
class Outcome:
    status: OutcomeStatus
    findings: list[Finding] = field(default_factory=list)
    note: str = ""


def _done(
    snap: Snapshot,
    rule: str,
    found: Sequence[tuple[str, str | None, str | None]],
    applicable: bool = True,
    note: str = "",
) -> Outcome:
    if not applicable:
        return Outcome("na", note=note)
    unique = dict.fromkeys(found)  # one finding per (message, workflow, job)
    findings = [Finding(repo=snap.repo, rule=rule, message=m, workflow=w, job=j) for m, w, j in unique]
    return Outcome("fail" if findings else "pass", findings, note)


def _all_jobs(snap: Snapshot) -> list[Job]:
    return [j for wf in snap.workflows for j in wfm.jobs(wf)]


def _class_of(job: Job, std: Standard) -> list[str]:
    sets = wfm.runs_on_sets(job) or []
    return [c.name for s in sets if (c := wfm.classify(s, std.classes)) is not None]


# --- triggers ----------------------------------------------------------------------------------------------


def trigger_push_scope(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    ci = [wf for wf in snap.workflows if wfm.is_ci(wf)]
    for wf in ci:
        cfg = wfm.push_config(wf)
        if cfg is None:
            continue
        branches = wfm.str_list(cfg.get("branches"))
        if not branches and not cfg.get("tags") and not cfg.get("branches-ignore"):
            found.append(("unfiltered `push:` runs CI on every branch", wf.file, None))
            continue
        if cfg.get("branches-ignore"):
            found.append(("`push: branches-ignore` runs CI on every other branch", wf.file, None))
        others = [b for b in branches if b != snap.default_branch]
        if others:
            found.append((f"push runs CI on branches {others}", wf.file, None))
    return _done(snap, rule, found, applicable=bool(ci))


def trigger_no_main_retest(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    both = [wf for wf in snap.workflows if wfm.is_ci(wf) and wfm.pushes_branch(wf, snap.default_branch)]
    for wf in both:
        retest = [j.key for j in wfm.jobs(wf) if not _push_only(j)]
        if retest:
            found.append((f"re-runs {retest} on push to {snap.default_branch}", wf.file, None))
    return _done(snap, rule, found, applicable=bool(both))


def _push_only(job: Job) -> bool:
    c = job.condition.replace('"', "'")
    return bool(
        re.search(r"event_name\s*==\s*'push'", c)
        or re.search(r"github\.ref\s*==\s*'refs/heads/", c)
        or re.search(r"github\.ref_name\s*==\s*'", c)
    )


def trigger_merge_group_matches_queue(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    if not snap.merge_queue_known:
        return Outcome("unknown", note="branch rules not visible")
    found: list[tuple[str, str | None, str | None]] = []
    with_mg = [wf for wf in snap.workflows if "merge_group" in wfm.triggers(wf)]
    if snap.merge_queue is None:
        for wf in with_mg:
            found.append(("merge_group trigger but the default branch has no merge queue", wf.file, None))
        return _done(snap, rule, found)
    if snap.required_contexts is None:
        return Outcome("unknown", note="required contexts not visible")
    for ctx in snap.required_contexts:
        for job in _producers(snap, ctx):
            if "merge_group" not in wfm.triggers(job.workflow):
                found.append(
                    (
                        f"required `{ctx}` is produced by a workflow without merge_group",
                        job.workflow.file,
                        job.key,
                    )
                )
    return _done(snap, rule, found)


def trigger_heavy_off_pr(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    pr_jobs = [j for j in _all_jobs(snap) if wfm.can_run_on(j, wfm.PR_EVENTS)]
    for job in pr_jobs:
        heavy = {"docker", "bigmem", "publish"} & set(_class_of(job, std))
        step_hit = HEAVY_STEP.search(job.runs()) or any(
            "docker/build-push-action" in str(s.get("uses", "")) for s in job.steps()
        )
        if heavy or step_hit:
            why = f"class {sorted(heavy)}" if heavy else "heavy step (image build / compose e2e / playwright)"
            found.append((f"runs on pull_request: {why}", job.workflow.file, job.key))
    return _done(snap, rule, found, applicable=bool(snap.workflows))


# --- runners -----------------------------------------------------------------------------------------------


def runner_class_allowed(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    jobs = [j for j in _all_jobs(snap) if j.uses is None]
    for job in jobs:
        sets = wfm.runs_on_sets(job)
        if sets is None:
            found.append(
                (
                    f"runs-on is not resolvable to a class: {job.body.get('runs-on')!r}",
                    job.workflow.file,
                    job.key,
                )
            )
            continue
        for labels in sets:
            cls = wfm.classify(labels, std.classes)
            shown = sorted(labels)
            if cls is None:
                hint = " (bare self-hosted is not a class)" if labels == frozenset(["self-hosted"]) else ""
                found.append((f"runs-on {shown} is not a runner class{hint}", job.workflow.file, job.key))
            elif cls.public_only and snap.private:
                found.append((f"hosted runner {shown} in a private repository", job.workflow.file, job.key))
            elif cls.repos and snap.repo not in cls.repos:
                found.append(
                    (f"class {cls.name} is reserved for {list(cls.repos)}", job.workflow.file, job.key)
                )
    return _done(snap, rule, found, applicable=bool(jobs))


def runner_publish_scope(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    publish = [j for j in _all_jobs(snap) if "publish" in _class_of(j, std)]
    for job in publish:
        events = (*wfm.PR_EVENTS, "merge_group")
        if wfm.can_run_on(job, events):
            on = [e for e in events if e in wfm.triggers(job.workflow)]
            found.append((f"publish-class job can run on {on}", job.workflow.file, job.key))
    return _done(snap, rule, found, applicable=bool(publish))


def runtime_runner_labels(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    if snap.runtime_jobs is None:
        return Outcome("unknown", note="runs not collected")
    bad: Counter[tuple[str, str, tuple[str, ...]]] = Counter()
    for job in snap.runtime_jobs:
        if not job.labels:
            continue
        cls = wfm.classify(frozenset(job.labels), std.classes)
        if cls is None or (cls.public_only and snap.private):
            bad[(job.workflow_path.rsplit("/", 1)[-1], job.job, tuple(sorted(job.labels)))] += 1
    found: list[tuple[str, str | None, str | None]] = [
        (f"{n} recent job(s) ran with labels {list(labels)}", wf, name)
        for (wf, name, labels), n in sorted(bad.items())
    ]
    return _done(snap, rule, found, applicable=bool(snap.runtime_jobs))


# --- gate --------------------------------------------------------------------------------------------------


def _producers(snap: Snapshot, context: str) -> list[Job]:
    return [j for j in _all_jobs(snap) if wfm.produces(j, context)]


def gate_shape(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    if snap.required_contexts is None:
        return Outcome("unknown", note="required contexts not visible")
    found: list[tuple[str, str | None, str | None]] = []
    required = snap.required_contexts
    if not required:
        found.append(("the default branch requires no status checks", None, None))
        return _done(snap, rule, found, applicable=bool(snap.workflows))
    if "ci" not in required:
        found.append((f"required contexts {sorted(required)} do not include the `ci` gate", None, None))
    extra = sorted(set(required) - set(std.required_contexts))
    if extra:
        found.append(
            (f"required contexts {extra} are not gate contexts {list(std.required_contexts)}", None, None)
        )
    for job in _producers(snap, "ci"):
        if not job.body.get("needs"):
            found.append(("`ci` gate job has no needs", job.workflow.file, job.key))
        if "always()" not in job.condition:
            found.append(
                (
                    "`ci` gate job lacks `if: always()` (a failed need would skip it, not fail it)",
                    job.workflow.file,
                    job.key,
                )
            )
    return _done(snap, rule, found)


def gate_producers_unfiltered(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    if snap.required_contexts is None:
        return Outcome("unknown", note="required contexts not visible")
    found: list[tuple[str, str | None, str | None]] = []
    for ctx in snap.required_contexts:
        producers = _producers(snap, ctx)
        if not producers:
            found.append((f"required `{ctx}` has no producing job on {snap.default_branch}", None, None))
            continue
        for job in producers:
            filtered = wfm.path_filtered(job.workflow, (*wfm.PR_EVENTS, "merge_group"))
            if filtered:
                found.append(
                    (
                        f"required `{ctx}` comes from a workflow path-filtered on {filtered}",
                        job.workflow.file,
                        job.key,
                    )
                )
    return _done(snap, rule, found, applicable=bool(snap.required_contexts))


def gate_merge_queue(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    if not snap.merge_queue_known:
        return Outcome("unknown", note="branch rules not visible")
    found: list[tuple[str, str | None, str | None]] = []
    mq = snap.merge_queue
    if mq is None:
        if not snap.private and snap.required_contexts:
            found.append(("public repository with required checks has no merge queue", None, None))
        return _done(snap, rule, found, applicable=not snap.private and bool(snap.required_contexts))
    if mq.get("merge_method") != "REBASE":
        found.append((f"merge queue merge_method is {mq.get('merge_method')}, not REBASE", None, None))
    if mq.get("grouping_strategy") != "ALLGREEN":
        found.append((f"merge queue grouping is {mq.get('grouping_strategy')}, not ALLGREEN", None, None))
    return _done(snap, rule, found)


# --- jobs --------------------------------------------------------------------------------------------------


def job_timeouts(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    jobs = [j for j in _all_jobs(snap) if j.uses is None]
    for job in jobs:
        value = job.body.get("timeout-minutes")
        if value is None:
            found.append(("no timeout-minutes", job.workflow.file, job.key))
        elif isinstance(value, int | float) and value > 180:
            found.append((f"timeout-minutes {value} > 180", job.workflow.file, job.key))
    return _done(snap, rule, found, applicable=bool(jobs))


def workflow_concurrency(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    scoped = [
        wf
        for wf in snap.workflows
        if any(e in wfm.triggers(wf) for e in (*wfm.PR_EVENTS, "push", "merge_group"))
    ]
    for wf in scoped:
        top = wf.doc.get("concurrency")
        per_job = [j for j in wfm.jobs(wf)]
        if top is None and not (per_job and all(j.body.get("concurrency") is not None for j in per_job)):
            found.append(("no concurrency group", wf.file, None))
            continue
        if (
            "merge_group" in wfm.triggers(wf)
            and isinstance(top, dict)
            and top.get("cancel-in-progress") is True
        ):
            found.append(("cancel-in-progress: true also cancels merge-queue runs", wf.file, None))
    return _done(snap, rule, found, applicable=bool(scoped))


def job_no_continue_on_error(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    jobs = _all_jobs(snap)
    for job in jobs:
        if "continue-on-error" in job.body and job.body["continue-on-error"] is not False:
            found.append(
                (
                    f"job-level continue-on-error: {job.body['continue-on-error']!r}",
                    job.workflow.file,
                    job.key,
                )
            )
    return _done(snap, rule, found, applicable=bool(jobs))


def runtime_masked_failures(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    if snap.runtime_jobs is None or snap.required_contexts is None:
        return Outcome("unknown", note="runs or required contexts not collected")
    producer_files = {j.workflow.file for ctx in snap.required_contexts for j in _producers(snap, ctx)}
    masked: Counter[tuple[str, str]] = Counter()
    for job in snap.runtime_jobs:
        file = job.workflow_path.rsplit("/", 1)[-1]
        if file in producer_files and job.conclusion == "failure" and job.run_conclusion == "success":
            masked[(file, job.job)] += 1
    found: list[tuple[str, str | None, str | None]] = [
        (f"failed in {n} run(s) that still concluded success", wf, name)
        for (wf, name), n in sorted(masked.items())
    ]
    return _done(snap, rule, found, applicable=bool(producer_files))


# --- policy ------------------------------------------------------------------------------------------------


def policy_gate_adopted(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    calls = [j for j in _all_jobs(snap) if j.uses and j.uses.startswith(POLICY_WORKFLOW)]
    if not calls:
        found.append(("does not call github-policy policy.yml", None, None))
    for job in calls:
        ref = (job.uses or "").split("@", 1)[1]
        if not (SEMVER_TAG.match(ref) or SHA.match(ref)):
            found.append((f"policy.yml pinned to `{ref}`, not a tag or SHA", job.workflow.file, job.key))
        with_ = job.body.get("with")
        given = with_.get("ref") if isinstance(with_, dict) else None
        if given != ref:
            found.append(
                (
                    f"policy.yml `with: ref` is {given!r}, must repeat the uses ref `{ref}`",
                    job.workflow.file,
                    job.key,
                )
            )
    return _done(snap, rule, found, applicable=bool(snap.workflows))


def policy_identity_scope(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    if snap.tree is not None:
        for path in snap.tree:
            if "migration-policy" in path.rsplit("/", 1)[-1]:
                found.append((f"local identity-policy copy `{path}` (use the shared policy.yml)", None, None))
    for job in _all_jobs(snap):
        text = job.runs()
        if "git" in text and BODY_GREP.search(text) and "grep" in text:
            found.append(("greps commit message bodies", job.workflow.file, job.key))
    return _done(snap, rule, found, applicable=bool(snap.workflows) or bool(snap.tree))


def workflows_actionlint(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    if snap.actionlint is None:
        return Outcome("unknown", note="actionlint not run here")
    found: list[tuple[str, str | None, str | None]] = []
    for line in snap.actionlint:
        file = line.split(":", 1)[0].rsplit("/", 1)[-1] if ":" in line else None
        found.append((line, file, None))
    for file, err in snap.parse_errors.items():
        found.append((f"YAML does not parse: {err}", file, None))
    return _done(snap, rule, found, applicable=bool(snap.workflows) or bool(snap.parse_errors))


def actions_pinned(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    for job in _all_jobs(snap):
        uses = [str(s["uses"]) for s in job.steps() if isinstance(s.get("uses"), str)]
        if job.uses:
            uses.append(job.uses)
        for ref in uses:
            if ref.startswith(("./", "docker://")) or "@" not in ref:
                continue
            owner = ref.split("/", 1)[0]
            if owner in OPEN_OWNERS:
                continue
            if not SHA.match(ref.rsplit("@", 1)[1]):
                found.append((f"`{ref}` is not pinned to a commit SHA", job.workflow.file, job.key))
    return _done(snap, rule, found, applicable=bool(snap.workflows))


# --- cache -------------------------------------------------------------------------------------------------


def _main_only(value: object) -> bool:
    if value is False or value == "false":
        return True
    v = str(value).replace('"', "'")
    return bool(re.search(r"refs/heads/|ref_name\s*==\s*'main'|event_name\s*==\s*'(push|schedule)'", v))


def cache_rust_save_main_only(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    seen = False
    for job in _all_jobs(snap):
        for step in job.steps():
            uses = str(step.get("uses", ""))
            with_: dict[str, Any] = step.get("with") if isinstance(step.get("with"), dict) else {}  # type: ignore[assignment]
            if uses.startswith("Swatinem/rust-cache"):
                seen = True
                if not _main_only(with_.get("save-if", "")):
                    found.append(("rust-cache saves outside main (set save-if)", job.workflow.file, job.key))
            elif uses.startswith("actions/cache@"):
                paths = str(with_.get("path", ""))
                if re.search(r"(^|\s|/)target\b|\.cargo", paths):
                    seen = True
                    found.append(
                        (
                            "actions/cache saves cargo dirs on every ref (use rust-cache, or "
                            "actions/cache/restore)",
                            job.workflow.file,
                            job.key,
                        )
                    )
    return _done(snap, rule, found, applicable=seen)


def cache_sccache_optional(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    seen = False
    for wf in snap.workflows:
        if "sccache" not in wf.text:
            continue
        seen = True
        for job in wfm.jobs(wf):
            if "sccache" not in str(job.body) and "sccache" not in str(wf.doc.get("env", "")):
                continue
            flag = str(job.env().get("SCCACHE_IGNORE_SERVER_IO_ERROR", "")).lower()
            if flag not in ("1", "true"):
                found.append(("uses sccache without SCCACHE_IGNORE_SERVER_IO_ERROR=1", wf.file, job.key))
    return _done(snap, rule, found, applicable=seen)


# --- release -----------------------------------------------------------------------------------------------


def _releases(snap: Snapshot) -> bool:
    for wf in snap.workflows:
        trig = wfm.triggers(wf)
        cfg = wfm.push_config(wf)
        if "release" in trig or (cfg is not None and (cfg.get("tags") or cfg.get("tags-ignore"))):
            return True
    return False


def _dry_runs(snap: Snapshot) -> list[str]:
    out = []
    for wf in snap.workflows:
        if set(wfm.triggers(wf)) <= {"workflow_call"}:
            continue  # a reusable building block, not a repository's dry run
        label = f"{wf.file} {wf.doc.get('name', '')}".lower().replace("_", "-")
        if "dry-run" in label or "dry run" in label or "dryrun" in label:
            out.append(wf.file)
    return out


def release_dry_run_present(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    if not _releases(snap):
        return Outcome("na")
    found: list[tuple[str, str | None, str | None]] = []
    dry = [wf for wf in snap.workflows if wf.file in _dry_runs(snap)]
    if not dry:
        found.append(("releases on tags but has no release dry-run workflow", None, None))
    elif not any("schedule" in wfm.triggers(wf) for wf in dry):
        found.append(("release dry-run has no nightly schedule", dry[0].file, None))
    return _done(snap, rule, found)


def runtime_dry_run_green(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    files = set(_dry_runs(snap))
    if not files:
        return Outcome("na")
    if snap.runtime_runs is None:
        return Outcome("unknown", note="runs not collected")
    nightly = sorted(
        (
            r
            for r in snap.runtime_runs
            if r.workflow_path.rsplit("/", 1)[-1] in files
            and r.event == "schedule"
            and r.head_branch == snap.default_branch
            and r.conclusion
        ),
        key=lambda r: r.created_at,
    )
    if not nightly:
        return Outcome("unknown", note="no scheduled dry-run in the window")
    found: list[tuple[str, str | None, str | None]] = []
    last = nightly[-1]
    if last.conclusion != "success":
        found.append(
            (
                f"latest nightly dry-run ({last.created_at}) concluded {last.conclusion}",
                last.workflow_path.rsplit("/", 1)[-1],
                None,
            )
        )
    streak = best = 0
    for run in nightly:
        streak = streak + 1 if run.conclusion != "success" else 0
        best = max(best, streak)
    if best >= 3:
        found.append((f"{best} nightly dry-runs failed in a row", None, None))
    return _done(snap, rule, found)


# --- deps --------------------------------------------------------------------------------------------------


def deps_no_private_fetch(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    for job in _all_jobs(snap):
        texts = [job.runs()] + [str(s.get("with", "")) for s in job.steps()]
        for text in texts:
            for match in PRIVATE_FETCH.finditer(text):
                fetch = match.group(0)
                if FORGEJO_URL.search(fetch):
                    found.append(
                        (f"fetches from Forgejo at CI time: `{fetch[:120]}`", job.workflow.file, job.key)
                    )
                    continue
                for owner, name, _ in GITHUB_REPO_URL.findall(fetch):
                    if (
                        owner.lower() == ORG.lower()
                        and snap.org_visibility is not None
                        and snap.org_visibility.get(name, True)
                    ):
                        found.append((f"fetches private repo {name} at CI time", job.workflow.file, job.key))
        for step in job.steps():
            with_ = step.get("with")
            if isinstance(with_, dict) and "secrets." in str(with_.get("build-args", "")):
                found.append(
                    ("secret passed as a Docker build-arg (use `secrets:`)", job.workflow.file, job.key)
                )
    return _done(snap, rule, found, applicable=bool(snap.workflows))


def codeql_release_only(snap: Snapshot, std: Standard, rule: str) -> Outcome:
    found: list[tuple[str, str | None, str | None]] = []
    codeql = [wf for wf in snap.workflows if "github/codeql-action" in wf.text]
    for wf in codeql:
        bad = []
        for event, cfg in wfm.triggers(wf).items():
            if event in ("release", "workflow_dispatch"):
                continue
            if event == "push" and isinstance(cfg, dict) and cfg.get("tags") and not cfg.get("branches"):
                continue
            bad.append(event)
        if bad:
            found.append((f"CodeQL runs on {bad}", wf.file, None))
    if snap.required_contexts:
        for ctx in snap.required_contexts:
            if "codeql" in ctx.lower() or ctx.lower().startswith("analyze"):
                found.append((f"`{ctx}` (CodeQL) is a required check", None, None))
    return _done(snap, rule, found, applicable=bool(codeql) or bool(snap.required_contexts))


Check = Callable[[Snapshot, Standard, str], Outcome]
CHECKS: dict[str, Check] = {
    fn.__name__: fn
    for fn in (
        trigger_push_scope,
        trigger_no_main_retest,
        trigger_merge_group_matches_queue,
        trigger_heavy_off_pr,
        runner_class_allowed,
        runner_publish_scope,
        runtime_runner_labels,
        gate_shape,
        gate_producers_unfiltered,
        gate_merge_queue,
        job_timeouts,
        workflow_concurrency,
        job_no_continue_on_error,
        runtime_masked_failures,
        policy_gate_adopted,
        policy_identity_scope,
        workflows_actionlint,
        actions_pinned,
        cache_rust_save_main_only,
        cache_sccache_optional,
        release_dry_run_present,
        runtime_dry_run_green,
        deps_no_private_fetch,
        codeql_release_only,
    )
}
