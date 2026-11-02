"""The long-running auditor: re-audits hourly, serves /metrics, /healthz, /findings.

Why a long-running exporter and not a CronJob plus pushgateway:
* the estate has no pushgateway, and pushed series never go stale, so a dead
  CronJob would keep showing the last conformance forever;
* scrape `up` and ci_audit_last_success_timestamp give a real liveness signal;
* the in-process ETag and blob caches make each hourly pass cheap (blobs and
  completed-run jobs are fetched once), which a fresh CronJob pod would lose;
* it is the same shape as gha-exporter and github-org-monitor next to it.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import render
from .evaluate import evaluate
from .github import GitHub, RepoRef, collect, fetch_text, list_repos, main_status, token_from_env
from .model import Exemption, Result, Snapshot
from .standard import Standard, StandardError, default_standard_dir, load, parse_exemptions


def log(**fields: object) -> None:
    fields.setdefault("ts", datetime.now(UTC).isoformat(timespec="seconds"))
    print(json.dumps(fields, sort_keys=True, default=str), flush=True)


@dataclass
class Auditor:
    gh: GitHub
    std: Standard
    exemptions: list[Exemption]
    org: str
    extra_repos: list[str]
    skip: set[str]
    exemptions_repo: str
    exemptions_ref: str
    run_days: int = 7
    max_runs: int = 20
    actionlint: str | None = None
    actionlint_config: Path | None = None
    include_archived: bool = False
    repos: list[RepoRef] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    results: list[Result] = field(default_factory=list)
    main: dict[str, dict[str, str]] = field(default_factory=dict)
    exemptions_valid: bool = True
    last_run: float = 0.0
    last_success: float = 0.0
    duration: float = 0.0
    errors: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_env(cls) -> Auditor:
        std_dir = (
            Path(os.environ["CI_STANDARD_DIR"])
            if os.environ.get("CI_STANDARD_DIR")
            else default_standard_dir()
        )
        std, bundled = load(std_dir)
        env = os.environ.get
        return cls(
            gh=GitHub(token=token_from_env(), rate_reserve=int(env("RATE_RESERVE", "500"))),
            std=std,
            exemptions=bundled,
            org=env("GITHUB_ORG", "TheDancingDeveloper-org"),
            extra_repos=[x for x in env("EXTRA_REPOS", "thedancingdeveloper/fluent-gpui").split(",") if x],
            skip={x for x in env("SKIP_REPOS", "").split(",") if x},
            exemptions_repo=env("EXEMPTIONS_REPO", "TheDancingDeveloper-org/github-policy"),
            exemptions_ref=env("EXEMPTIONS_REF", "main"),
            run_days=int(env("RUN_DAYS", "7")),
            max_runs=int(env("MAX_RUNS_PER_REPO", "20")),
            actionlint=env("ACTIONLINT") or None,
            actionlint_config=std_dir / "actionlint.yaml",
            include_archived=env("INCLUDE_ARCHIVED") == "1",
        )

    def refresh_exemptions(self) -> None:
        """Live exemptions from github-policy main; keep the last good set on error."""
        text = fetch_text(self.gh, self.exemptions_repo, "ci-standard/exemptions.yaml", self.exemptions_ref)
        if text is None:
            log(msg="ci_exemptions_unavailable", repo=self.exemptions_repo)
            return
        try:
            self.exemptions = parse_exemptions(text, self.std)
            self.exemptions_valid = True
        except StandardError as err:
            self.exemptions_valid = False
            log(msg="ci_exemptions_invalid", error=str(err))

    def audit(self) -> None:
        start = time.time()
        self.last_run = start
        try:
            self.refresh_exemptions()
            repos = [
                r
                for r in list_repos(self.gh, self.org, self.extra_repos)
                if (self.include_archived or not r.archived) and r.repo not in self.skip
            ]
            visibility = {r.repo: r.private for r in repos}
            snapshots, results = [], []
            today = date.today()
            for ref in repos:
                snap = collect(
                    self.gh,
                    ref,
                    visibility,
                    run_days=self.run_days,
                    max_runs=self.max_runs,
                    actionlint=self.actionlint,
                    actionlint_config=self.actionlint_config,
                )
                snapshots.append(snap)
                results += evaluate(snap, self.std, self.exemptions, today)
            stamp = datetime.now(UTC)
            for line in render.finding_lines(results, stamp):
                print(line, flush=True)
            with self.lock:
                self.repos, self.snapshots, self.results = repos, snapshots, results
                self.last_success = time.time()
            fails = sum(1 for r in results if r.status == "fail")
            log(
                msg="ci_audit_done",
                repos=len(repos),
                results=len(results),
                failing=fails,
                seconds=round(time.time() - start, 1),
            )
        except Exception as err:  # noqa: BLE001 - keep serving the last good audit
            self.errors += 1
            log(msg="ci_audit_error", error=f"{type(err).__name__}: {err}")
        finally:
            self.duration = time.time() - start

    def refresh_main(self) -> None:
        """Required contexts on each default-branch head (cheap; every 10 min)."""
        with self.lock:
            pairs = [
                (ref, snap.required_contexts or [])
                for ref, snap in zip(self.repos, self.snapshots, strict=False)
            ]
        out: dict[str, dict[str, str]] = {}
        for ref, contexts in pairs:
            if not contexts:
                continue
            try:
                out[ref.repo] = main_status(self.gh, ref, contexts)
            except Exception as err:  # noqa: BLE001
                self.errors += 1
                log(msg="ci_main_status_error", repo=ref.repo, error=str(err))
        with self.lock:
            self.main = out

    def render(self) -> str:
        with self.lock:
            body = render.metrics(
                self.results,
                self.snapshots,
                self.exemptions,
                time.time(),
                self.last_run,
                self.last_success,
                self.duration,
                dict(self.gh.api_requests),
                self.errors,
            )
            body += render.main_checks(self.main)
            body += f"# TYPE ci_exemptions_valid gauge\nci_exemptions_valid {int(self.exemptions_valid)}\n"
            if self.gh.remaining is not None:
                body += f"# TYPE ci_audit_ratelimit_remaining gauge\nci_audit_ratelimit_remaining {self.gh.remaining}\n"
        return body

    def findings_json(self) -> str:
        with self.lock:
            return json.dumps(
                [
                    {
                        "repo": r.repo,
                        "rule": r.rule.id,
                        "status": r.status,
                        "note": r.note,
                        "findings": [f.__dict__ for f in r.findings],
                    }
                    for r in self.results
                    if r.status != "pass"
                ],
                indent=1,
            )


def serve(auditor: Auditor) -> None:
    port = int(os.environ.get("METRICS_PORT", "9803"))
    audit_every = int(os.environ.get("AUDIT_INTERVAL_SECONDS", "3600"))
    main_every = int(os.environ.get("MAIN_INTERVAL_SECONDS", "600"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/metrics":
                body, ctype, code = auditor.render().encode(), "text/plain; version=0.0.4", 200
            elif path == "/findings":
                body, ctype, code = auditor.findings_json().encode(), "application/json", 200
            elif path == "/healthz":
                ok = auditor.last_success == 0 or time.time() - auditor.last_success < 3 * audit_every
                body, ctype, code = (b"ok\n" if ok else b"stale\n"), "text/plain", 200 if ok else 503
            else:
                body, ctype, code = b"not found\n", "text/plain", 404
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(msg="ci_auditor_serving", port=port, org=auditor.org, extra=auditor.extra_repos)
    next_audit = next_main = 0.0
    while True:
        now = time.time()
        if now >= next_audit:
            auditor.audit()
            next_audit = time.time() + audit_every
            next_main = 0.0
        if now >= next_main:
            auditor.refresh_main()
            next_main = time.time() + main_every
        time.sleep(15)


if __name__ == "__main__":
    sys.exit("use: python -m ci_auditor serve")
