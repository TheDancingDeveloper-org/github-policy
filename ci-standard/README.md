# CI guardrails

The CI standard for every repository in TheDancingDeveloper-org, plus
`thedancingdeveloper/fluent-gpui`, as code (WI-3087). The source is the CI review
of 2026-11-02 (`~/Working/Active/ci-review-2026-11.md` §3–§5) and the delivery
process (`.claude/rules/delivery-process.md`, `graft-only.md`).

Operator decisions (2026-11-02):

- Every repo follows the guardrails. A repo outside them needs a **specific
  exemption**: one rule, a reason, an owner and an expiry.
- Conformance is **audited continuously**, with a Grafana dashboard and alerts.
- **Public repos may use GitHub-hosted runners** for light jobs. Private repos
  stay self-hosted.
- graft's merge queue moves from squash to rebase. Oversight changes that
  ruleset; until then a dated exemption covers graft CI-10.

## What is here

| Path | What |
|---|---|
| `rules.yaml` | The rules: `CI-01`…`CI-24`, each with a statement, severity, kind, machine check and fix. It also holds the runner classes and the allowed required contexts. |
| `exemptions.yaml` | The exemptions in force (schema `exemptions.schema.json`). The auditor and the gate read it from `main`. |
| `actionlint.yaml` | The class labels, so actionlint accepts them. |
| `../auditor/` | `ci-auditor`, the one checker (Python, typed, tested). `lint` is the in-repo gate; `serve` is the org-wide exporter. |
| `../.github/workflows/policy.yml` | The reusable policy gate: actionlint, `ci-auditor lint` and the identity check. |
| `../.github/workflows/rust-ci.yml` | The reusable Rust job: fmt, clippy, nextest and cargo-deny, using rust-cache saved from main only. |
| `../.github/workflows/image.yml` | The reusable image build: BuildKit with a registry cache written from main only, and push by digest. |
| `../.github/workflows/release-dry-run.yml` | The reusable release preflight. It goes with the repo's own build, called with `publish: false`. |
| `../templates/` | Caller shapes: `ci-gate.yml` (the standard CI workflow), `policy-stub.yml` and `release-dry-run-stub.yml`. |

Every reusable workflow is versioned by tag (`v1.0.0`, …). Callers pin a tag or
a SHA, never `@main` (CI-15), and Dependabot (`github-actions` ecosystem) bumps
the pin. `policy.yml` takes `with: ref:`, which must repeat the `uses:` ref; a
reusable workflow cannot see its own ref. CI-15 flags a mismatch.

## The rules

| ID | Area | Rule | Sev | Kind | Gate |
|---|---|---|---|---|---|
| CI-01 | triggers | No branch-wide push CI (`push: ['**']`, bare `push:`, `dev`) | medium | static | block |
| CI-02 | triggers | No re-test on push to main; push publishes and warms caches only | medium | static | block |
| CI-03 | triggers | `merge_group` trigger if and only if there is a merge queue | low | settings | report |
| CI-04 | triggers | Expensive suites (docker, e2e, Playwright) not on pull_request | medium | static | report |
| CI-05 | runners | `runs-on` names exactly one class. No bare `[self-hosted]`. Hosted only in public repos. | high | static | block |
| CI-06 | runners | `publish` class only on main, tags or dispatch | high | static | block |
| CI-07 | runners | Jobs that actually ran asked for a class | medium | runtime | report |
| CI-08 | gate | One required gate `ci` (`if: always()`, `needs`) | high | settings | report |
| CI-09 | gate | Every required context has a producer that is not path-filtered | high | settings | report |
| CI-10 | gate | Merge queue REBASE + ALLGREEN; public repos have a queue | medium | settings | report |
| CI-11 | jobs | `timeout-minutes` on every job, at most 180 | medium | static | block |
| CI-12 | jobs | `concurrency` on every workflow; never cancel merge-queue runs | medium | static | block |
| CI-13 | jobs | No job-level `continue-on-error` | high | static | block |
| CI-14 | jobs | No masked failures (a job failed, its run succeeded) | medium | runtime | report |
| CI-15 | policy | Calls `policy.yml`, pinned to a tag or SHA | medium | static | report |
| CI-16 | policy | Identity checks author and committer only; no local copy that reads bodies | medium | static | block |
| CI-17 | policy | Workflows pass actionlint | medium | static | block |
| CI-18 | policy | Third-party actions pinned by SHA | low | static | report |
| CI-19 | cache | `Swatinem/rust-cache` saved from main only | low | static | report |
| CI-20 | cache | sccache optional (`SCCACHE_IGNORE_SERVER_IO_ERROR=1`) | low | static | block |
| CI-21 | release | A repo that tags has a release dry-run (nightly and PR path) | medium | static | report |
| CI-22 | release | Nightly dry-run green; never 3 red in a row | medium | runtime | report |
| CI-23 | deps | No private source fetched at CI time; no secret build-args | high | static | block |
| CI-24 | deps | CodeQL on releases only, never required | medium | static | block |

**Gate** is what `policy.yml` does with a finding once the repo sets
`enforce: true`:
- `block` fails the job.
- `report` only warns.

Settings and runtime rules can't block, because the gate can't see branch
protection or past runs. They show in the dashboard and alerts instead.

### Runner classes (CI-05)

| Class | `runs-on` | Use for |
|---|---|---|
| `ci-small` | `ci-small` (ARC scale set, WI-3080) | policy, fmt, lint, type-check, unit tests, gate jobs. No Docker. |
| `rust-heavy` | `[self-hosted, rust-heavy]` | large cargo builds, clippy, nextest |
| `docker` | `[self-hosted, docker]` | compose e2e, smoke tests, image builds without push |
| `bigmem` | `[self-hosted, bigmem]` | CodeQL on tags, parity recordings |
| `publish` | `[self-hosted, publish]` or `[self-hosted, docker, publish]` | pushing, deploying, signing (main, tags, dispatch) |
| `tailnet` | `[self-hosted, tailnet]` | jobs that reach estate hosts |
| `windows` | `[self-hosted, windows]` | Windows builds and signing |
| `graft-dev` | `[self-hosted, graft-dev]` | the graft repo only |
| `hosted` | `ubuntu-24.04`, `ubuntu-24.04-arm`, `windows-2025`, `macos-15` | public repos, light jobs, no secrets. Pinned images, never `-latest`. |

## Exemptions

```yaml
- repo: graft            # bare repository name
  rule: CI-10            # exactly one rule; no wildcards
  workflow: ci.yml       # optional: narrow to a file
  job: e2e               # optional: narrow to a job (needs workflow)
  reason: >-             # >= 40 chars: why, and what ends it
    ...
  owner: oversight       # GitHub handle or `oversight`; removes it
  created: 2026-11-02
  expires: 2026-11-16    # at most 180 days after created
```

- An expired exemption suppresses nothing.
- The dashboard lists every exemption with its time left, and an alert fires 7
  days before expiry.
- The auditor rejects a malformed file loudly (`ci_exemptions_valid 0`, plus a
  log line) and keeps the last good set.
- This repository's own CI runs `ci-auditor validate --fail-expired`.
- To add an exemption, open a PR here; CODEOWNERS reviews it. It takes effect
  at the next audit (within an hour), with no redeploy.

The four `runner-exceptions.json` entries (cadastre, FarmEggs) are carried over
as CI-05 exemptions. `runner-exceptions.json` stays for the legacy
`runner-policy-reusable.yml` gate until every caller has moved to `policy.yml`.

## The auditor (`ci-auditor serve`)

- **Language: Python 3.13, typed (`mypy --strict`), tested (pytest).** The
  estate's exporters (gha-exporter, github-org-monitor) and policy scripts are
  Python. The work is YAML parsing and REST calls, not CPU. It has a single
  dependency, PyYAML. The same package is the in-repo gate (`lint`), so the
  dashboard and the gate can't disagree.
- **Shape: a long-running exporter, not a CronJob plus pushgateway.** It
  re-audits every hour and refreshes the main-branch check status every 10
  minutes.
  - The estate has no pushgateway, and pushed series never go stale, so a dead
    job would keep showing last week's conformance.
  - A long-running pod has a real liveness signal (`up`,
    `ci_audit_last_success_timestamp`).
  - It keeps its ETag, blob and completed-run caches warm, so hourly passes cost
    a fraction of the first.
- **Per repository, it reads:**
  - the default branch's tree and workflow blobs;
  - the branch summary (`protection.required_status_checks`);
  - `/rules/branches/{branch}`, which includes org rulesets that apply, and
    gives the required contexts and merge-queue parameters;
  - the last 7 days of runs, with jobs for the latest 20 completed runs (job
    `labels`, conclusions);
  - the head check-runs on the default branch.

  It runs actionlint on the workflow text, then evaluates every rule minus the
  live exemptions.
- **Metrics:**
  - `ci_conformance{repo,rule,title,area,severity,enforce,status}`: one-hot over
    pass, fail, exempt, na and unknown;
  - `ci_conformance_findings`, `ci_exemption_expiry_seconds{repo,rule,workflow,job,owner}`;
  - `ci_main_required_check{repo,context,conclusion}`;
  - `ci_repo_info`, `ci_audit_gap{repo,source}`;
  - `ci_audit_last_run_timestamp`, `ci_audit_last_success_timestamp`;
  - `ci_audit_duration_seconds`, `ci_audit_api_requests_total`, `ci_exemptions_valid`.
- **Logs:** one JSON line per finding on stdout (`"msg": "ci_finding"`, with
  repo, rule, severity, status, workflow, job, message and fix), for Loki.
- **Endpoints:** `/metrics`, `/healthz`, and `/findings` (JSON of every result
  that isn't a pass).
- **Image:** `ghcr.io/thedancingdeveloper-org/ci-auditor:<tag>`, built by
  `.github/workflows/ci-auditor.yml` on tag push. It bundles the rules of that
  tag; exemptions come from `main` at run time.
- **Deployment:** ops `apps/ci-auditor/` (ArgoCD). Its read-only GitHub token
  comes in through ESO.

Run it locally:

```bash
cd auditor && uv sync
uv run ci-auditor validate
uv run ci-auditor lint ~/Working/Active/egressy --repo egressy --no-private --actionlint "$(command -v actionlint)"
GITHUB_TOKEN=... uv run ci-auditor serve --once   # one audit; metrics and finding lines on stdout
```

### Visibility: what a read-only token can and can't see

The auditor sees:
- workflows;
- required contexts (branch protection summary plus applied rulesets);
- merge-queue parameters;
- runs and jobs, with their labels;
- check-runs.

A **fine-grained token** needs Contents, Metadata, Actions and Administration,
all read-only. Administration is optional: without it the branch summary still
carries the required contexts. A fine-grained token is scoped to **one owner**,
so `thedancingdeveloper/fluent-gpui` (a personal-account repo) needs a classic
token or a second token. Until then it is read as a public repo, where rulesets
and runs are still visible.

It can't see the following, because they need `admin:org`, which no estate
token has (CI review §6):
- the org rulesets as objects (only their effect on each branch);
- the runner inventory and runner labels (taken from job records instead);
- runner groups, and which repos and workflows they admit. So the standard's
  §3.3 rule 2 ("special runners must not accept generic work") is **not
  audited**. CI-07 sees the effect, a generic job landing on a special runner,
  but not the group configuration.

## Rollout: report first, then enforce

Baseline from the first live audit (2026-11-02 21:55Z, 80 repos, 391 s):
- 608 failing repo × rule results;
- the most common failures are CI-15 (69 repos), CI-08 (67), CI-11 (66),
  CI-02 and CI-12 (65 each), and CI-05 (60).

Most of the 80 repos are small and inactive. The graft repos below carry the
load.

**Phase 0 (this change):**
- the standard, the auditor and the dashboards;
- nothing blocks;
- the old `runner-policy-reusable.yml` stays as it is.

**Phase 1, report only (weeks 1–2).** Each repo adopts `policy.yml` in report
mode (`templates/ci-gate.yml` or `policy-stub.yml`). Findings show as PR
annotations and in Grafana. Repos that have none yet get exemptions only where
a fix is genuinely blocked.

**Phase 2, enforce per repo.** When a repo has no `block`-rule findings for 7
days:
1. set `enforce: true` in its policy call;
2. make `ci` its only required context (with `policy` folded into the gate).

From then on, drift without an exemption fails the PR. The dashboard's
"enforcing" column counts repos with `enforce: true`.

**Phase 3, org default.** New repos start from `templates/ci-gate.yml` with
`enforce: true`. The legacy runner-policy gate and `runner-exceptions.json` are
deleted when no repo calls them.

### Migration order (S < 1 day, M 1–3 days, L > 3 days)

The review's 16 quick wins (QW) are folded in.

| # | Repo / area | Change | Rules | QW | Effort |
|---|---|---|---|---|---|
| 0 | github-policy | Tag `v1.0.0`; publish the ci-auditor image; deploy the auditor and dashboards (ops) | all | 1, 11, 12 | S (this change) |
| 1 | runners (ops) | Separate runner groups for publish, bigmem, graft-dev and windows; drop the generic labels; fix `github-runner-job-completed.sh` `home_dir`; assert `/tmp` 1777 in the job-started hook; bake PyYAML and actionlint into the ci-small image | CI-05, CI-07 | 2, 3 | M |
| 2 | email-rs | Drop `push: ['**']`; concurrency and timeouts; one `ci` gate that folds in `release-dry-run`; merge queue (public); adopt policy.yml | CI-01, 02, 08, 10, 11, 12, 15 | 4 | S |
| 3 | rustnzb | Fix `desktop/src-tauri/Cargo.lock`; quarantine 3 e2e specs; e2e, desktop, smoke and dry-run move from `[docker, publish]` to `[docker]`; repair the nightly dry-run; adopt policy.yml | CI-06, 13, 22, 05, 15, 16 | 5, 6 | M |
| 4 | vogt | Drop `push: main` from ci.yml; e2e `cache-from` the build.yml registry cache, with e2e in merge_group only; mypy over touched tests in worker gates; adopt policy.yml (vogt's stricter checks stay as inputs); `ci` as the required gate | CI-02, 04, 08, 19, 15 | 7, 13, 14 | M |
| 5 | rustTorrent | Drop push CI on main and dev; delete `dev`; adopt policy.yml (fixes the Windows and trailer collisions); Windows lane on the shared release build | CI-01, 02, 16, 22, 24 | 7, 11 | M |
| 6 | myaiagent | `ruff format` in the land gate; class labels for docker queue relief; keep direct merge; postgres on tmpfs | CI-02, 04, 05, 08 | 14 | S |
| 7 | graft | Merge queue SQUASH → REBASE (oversight; delete the CI-10 exemption); remove or relabel `bench-tests` and the macOS jobs; quarantine `restart-timing` and 4 flaky tests; remove `CARGO_BUILD_JOBS: 4`; sccache optional | CI-10, 05, 13, 20, 03 | 8, 9, 15 | M |
| 8 | egressy | Required `build` comes from a gate job (not path-filtered); GHCR secret guard; adopt policy.yml; merge queue | CI-08, 09, 06, 10, 16 | 10 | S |
| 9 | fluent-gpui | Merge queue on, or remove the dead `merge_group`; `ubuntu-24.04` instead of `-latest`; adopt rust-ci.yml | CI-03, 05, 10, 15 | — | S |
| 10 | the rest (about 70 small repos) | One batch PR each from `templates/ci-gate.yml`; or, for repos with no CI, CI-08 exemptions with an owner and a 90-day expiry | CI-08, 11, 12, 15 | — | M in total |
| 11 | all | rust-ci, cache-warm and image adoption; in-cluster BuildKit (`remote-buildkit` input) | CI-19, 04 | — | L |
| — | process | Oversight cuts batches at 6 lands and never lets a backlog pass ~12 | — | 16 | S |
