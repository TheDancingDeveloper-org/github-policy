"""Read-only GitHub REST collection for the auditor.

What a read-only token can see, and what it cannot (README "Visibility"):

* workflows (git tree + blobs), required checks (the branch's protection
  summary plus /rules/branches, which includes org rulesets that apply),
  the merge queue rule, recent runs and their jobs - all readable.
* org rulesets as objects, runner groups and their repo/workflow
  restrictions, and the runner inventory need admin:org; they are not read.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from . import workflow as wfm
from .model import RuntimeJob, RuntimeRun, Snapshot

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


@dataclass
class GitHub:
    token: str
    rate_reserve: int = 500
    api_requests: dict[tuple[str, int], int] = field(default_factory=dict)
    remaining: int | None = None
    reset: int | None = None
    _etags: dict[str, tuple[str, Any]] = field(default_factory=dict)
    _immutable: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _request(self, url: str, kind: str, accept: str) -> tuple[int, Any]:
        if (
            self.remaining is not None
            and self.remaining < self.rate_reserve
            and time.time() < (self.reset or 0)
        ):
            raise GitHubError(f"rate reserve reached ({self.remaining} left)")
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": accept,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ci-auditor",
            },
        )
        cached = self._etags.get(url)
        if cached:
            req.add_header("If-None-Match", cached[0])
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, headers, raw = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as err:
            status, headers, raw = err.code, err.headers, b""
        with self._lock:
            self.api_requests[(kind, status)] = self.api_requests.get((kind, status), 0) + 1
            if headers.get("x-ratelimit-remaining") is not None:
                self.remaining = int(headers["x-ratelimit-remaining"])
                self.reset = int(headers.get("x-ratelimit-reset", "0"))
        if status == 304 and cached:
            return 200, cached[1]
        if status >= 400:
            return status, None
        body: Any = raw.decode("utf-8", "replace") if accept.endswith(".raw") else json.loads(raw or b"null")
        if headers.get("ETag"):
            self._etags[url] = (headers["ETag"], body)
        return status, body

    def get(
        self, path: str, kind: str, *, optional: bool = False, accept: str = "application/vnd.github+json"
    ) -> Any:
        """GET a path. `optional` turns 403/404 into None (a visibility gap)."""
        status, body = self._request(path if path.startswith("http") else API + path, kind, accept)
        if status >= 400:
            if optional and status in (403, 404):
                return None
            raise GitHubError(f"GET {path}: HTTP {status}")
        return body

    def immutable(self, key: str, path: str, kind: str) -> Any:
        """GET once and keep forever (blobs by sha, jobs of completed runs)."""
        if key not in self._immutable:
            self._immutable[key] = self.get(path, kind)
        return self._immutable[key]

    def paged(self, path: str, kind: str, limit: int = 1000) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        sep = "&" if "?" in path else "?"
        while len(out) < limit:
            batch = self.get(f"{path}{sep}per_page=100&page={page}", kind)
            if not isinstance(batch, list) or not batch:
                break
            out += batch
            if len(batch) < 100:
                break
            page += 1
        return out


@dataclass(frozen=True)
class RepoRef:
    repo: str
    full_name: str
    private: bool
    archived: bool
    default_branch: str
    pushed_at: str = ""


def list_repos(gh: GitHub, org: str, extra: list[str]) -> list[RepoRef]:
    raw = gh.paged(f"/orgs/{org}/repos?type=all", "repos")
    for full in extra:
        body = gh.get(f"/repos/{full}", "repo", optional=True)
        if isinstance(body, dict):
            raw.append(body)
    return [
        RepoRef(
            repo=str(r["name"]),
            full_name=str(r["full_name"]),
            private=bool(r["private"]),
            archived=bool(r["archived"]),
            default_branch=str(r.get("default_branch") or "main"),
            pushed_at=str(r.get("pushed_at") or ""),
        )
        for r in raw
    ]


def _workflows(gh: GitHub, ref: RepoRef, snap: Snapshot) -> None:
    tree = gh.get(f"/repos/{ref.full_name}/git/trees/{ref.default_branch}?recursive=1", "tree", optional=True)
    if not isinstance(tree, dict):
        snap.gaps.append("tree")
        return
    entries = [cast(dict[str, Any], e) for e in tree.get("tree", []) if isinstance(e, dict)]
    snap.tree = [str(e["path"]) for e in entries if e.get("type") == "blob"]
    if tree.get("truncated"):
        snap.gaps.append("tree_truncated")
    for entry in entries:
        path = str(entry.get("path", ""))
        parent, _, name = path.rpartition("/")
        if parent != ".github/workflows" or not name.endswith((".yml", ".yaml")):
            continue
        blob = gh.immutable(
            f"blob:{entry['sha']}", f"/repos/{ref.full_name}/git/blobs/{entry['sha']}", "blob"
        )
        text = base64.b64decode(blob["content"]).decode("utf-8", "replace")
        try:
            snap.workflows.append(wfm.parse(name, text))
        except Exception as err:  # noqa: BLE001 - any parse failure is a finding, not a crash
            snap.parse_errors[name] = f"{type(err).__name__}: {str(err).splitlines()[0][:160]}"


def _branch_rules(gh: GitHub, ref: RepoRef, snap: Snapshot) -> None:
    contexts: set[str] = set()
    branch = gh.get(f"/repos/{ref.full_name}/branches/{ref.default_branch}", "branch", optional=True)
    rules = gh.get(f"/repos/{ref.full_name}/rules/branches/{ref.default_branch}", "rules", optional=True)
    if not isinstance(branch, dict) or rules is None:
        snap.gaps.append("branch_rules")
        return
    protection = branch.get("protection") or {}
    rsc = protection.get("required_status_checks") or {}
    contexts.update(str(c) for c in rsc.get("contexts") or [])
    snap.merge_queue_known = True
    for rule in rules if isinstance(rules, list) else []:
        params = rule.get("parameters") or {}
        if rule.get("type") == "required_status_checks":
            contexts.update(str(c["context"]) for c in params.get("required_status_checks") or [])
        elif rule.get("type") == "merge_queue":
            snap.merge_queue = cast(dict[str, Any], params)
    snap.required_contexts = sorted(contexts)


def _runtime(gh: GitHub, ref: RepoRef, snap: Snapshot, days: int, max_runs: int) -> None:
    if ref.pushed_at and datetime.fromisoformat(ref.pushed_at.replace("Z", "+00:00")).timestamp() < (
        time.time() - days * 86400
    ):
        snap.runtime_runs, snap.runtime_jobs = [], []  # no push in the window: nothing ran worth reading
        return
    since = datetime.fromtimestamp(time.time() - days * 86400, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = gh.get(
        f"/repos/{ref.full_name}/actions/runs?per_page=100&created=%3E%3D{since}", "runs", optional=True
    )
    if not isinstance(body, dict):
        snap.gaps.append("runs")
        return
    runs = [cast(dict[str, Any], r) for r in body.get("workflow_runs", [])]
    snap.runtime_runs = [
        RuntimeRun(
            workflow_path=str(r.get("path", "")),
            event=str(r.get("event", "")),
            head_branch=str(r.get("head_branch") or ""),
            conclusion=str(r.get("conclusion") or ""),
            created_at=str(r["created_at"]),
        )
        for r in runs
    ]
    jobs: list[RuntimeJob] = []
    completed = [r for r in runs if r.get("status") == "completed"][:max_runs]
    for run in completed:
        jd = gh.immutable(
            f"jobs:{run['id']}",
            f"/repos/{ref.full_name}/actions/runs/{run['id']}/jobs?per_page=100&filter=latest",
            "jobs",
        )
        for job in jd.get("jobs", []):
            jobs.append(
                RuntimeJob(
                    workflow_path=str(run.get("path", "")),
                    workflow_name=str(run.get("name") or ""),
                    job=str(job.get("name", "")),
                    labels=tuple(str(x) for x in job.get("labels") or []),
                    conclusion=str(job.get("conclusion") or ""),
                    run_id=int(run["id"]),
                    run_conclusion=str(run.get("conclusion") or ""),
                    event=str(run.get("event", "")),
                    head_branch=str(run.get("head_branch") or ""),
                )
            )
    snap.runtime_jobs = jobs


def run_actionlint(snap: Snapshot, binary: str | None, config: Path | None = None) -> None:
    exe = binary or shutil.which("actionlint")
    if not exe or not snap.workflows:
        return
    with tempfile.TemporaryDirectory() as tmp:
        wdir = Path(tmp) / ".github" / "workflows"
        wdir.mkdir(parents=True)
        for wf in snap.workflows:
            (wdir / wf.file).write_text(wf.text, encoding="utf-8")
        subprocess.run(["git", "init", "-q", tmp], check=False, capture_output=True)
        extra = ["-config-file", str(config.resolve())] if config is not None and config.exists() else []
        proc = subprocess.run(
            [
                exe,
                "-shellcheck=",
                "-pyflakes=",
                "-no-color",
                "-oneline",
                "-ignore",
                'label ".+" is unknown',
                *extra,
            ],
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    if proc.returncode not in (0, 1):
        snap.gaps.append("actionlint")
        return
    # Runner labels are CI-05's job; actionlint's own label check is ignored above.
    snap.actionlint = [line.strip()[:300] for line in proc.stdout.splitlines() if line.strip()]


def collect(
    gh: GitHub,
    ref: RepoRef,
    org_visibility: dict[str, bool],
    *,
    run_days: int = 7,
    max_runs: int = 20,
    actionlint: str | None = None,
    actionlint_config: Path | None = None,
) -> Snapshot:
    snap = Snapshot(
        repo=ref.repo,
        full_name=ref.full_name,
        private=ref.private,
        default_branch=ref.default_branch,
        org_visibility=org_visibility,
    )
    steps: list[Callable[[], None]] = [
        lambda: _workflows(gh, ref, snap),
        lambda: _branch_rules(gh, ref, snap),
        lambda: _runtime(gh, ref, snap, run_days, max_runs),
    ]
    for step in steps:
        try:
            step()
        except GitHubError as err:
            snap.gaps.append("api")
            print(json.dumps({"msg": "ci_audit_error", "repo": ref.repo, "error": str(err)}), file=sys.stderr)
    run_actionlint(snap, actionlint, actionlint_config)
    return snap


def main_status(gh: GitHub, ref: RepoRef, contexts: list[str]) -> dict[str, str]:
    """Conclusion of each required context on the default-branch head."""
    out = dict.fromkeys(contexts, "missing")
    if not contexts:
        return out
    runs = gh.get(
        f"/repos/{ref.full_name}/commits/{ref.default_branch}/check-runs?per_page=100&filter=latest",
        "check_runs",
        optional=True,
    )
    for run in (runs or {}).get("check_runs", []):
        name = str(run.get("name", ""))
        if name in out:
            out[name] = str(run.get("conclusion") or run.get("status") or "pending")
    statuses = gh.get(f"/repos/{ref.full_name}/commits/{ref.default_branch}/status", "status", optional=True)
    for st in (statuses or {}).get("statuses", []):
        if st.get("context") in out and out[st["context"]] == "missing":
            out[str(st["context"])] = str(st.get("state"))
    return out


def fetch_text(gh: GitHub, full_name: str, path: str, ref: str = "main") -> str | None:
    body = gh.get(
        f"/repos/{full_name}/contents/{path}?ref={ref}",
        "contents",
        optional=True,
        accept="application/vnd.github.raw",
    )
    return body if isinstance(body, str) else None


def token_from_env() -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")
    return token
