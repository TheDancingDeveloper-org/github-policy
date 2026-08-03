# Organization CI policy

This repository is the source of truth for organization-wide CI policy.

- GitHub Actions jobs use approved self-hosted runner labels by default.
- Public-hosted runners require an owner-approved, time-bounded exception in
  `runner-exceptions.json`.
- Workflow and policy changes require owner review through `CODEOWNERS`.
- `scripts/audit-workflows.sh` is the shared checker used by both workflows
  below.

## How enforcement actually works

There are two workflows, and the distinction matters:

| Workflow | Scope | Role |
|---|---|---|
| `enforce-runner-policy.yml` | The repository it runs in | **The gate.** Attached to every repository by an organization ruleset, so it runs in each target repository's context and must pass before a pull request merges. |
| `audit.yml` | This repository only | Self-audit, plus the daily expiry check on `runner-exceptions.json`. |

`audit.yml` was previously named "Organization workflow policy audit", which
was misleading: it checks out only this repository, so it audited exactly one
repository — itself — and stayed green while three repositories in the
organization ran on `ubuntu-latest`. Nothing about the checker was wrong; it
was never pointed at the organization. Do not re-add organization-wide
ambitions to that file. Per-repository enforcement is the correct mechanism
because it needs no cross-repository token.

### Fail-closed behaviour

`enforce-runner-policy.yml` requests self-hosted labels. A repository that has
not been added to the `public-node-b` runner group has no runner able to accept
the job, so the check stays queued and the pull request cannot merge. This is
intentional: it surfaces missing runner-group membership instead of letting a
repository quietly fall back to GitHub-hosted runners, which is exactly how the
2026-08-03 violations arose.

### Why the gate inlines its checker

This repository is **private**. A target repository cannot fetch
`scripts/audit-workflows.sh` from it — `raw.githubusercontent.com` returns 404
unauthenticated, and the target repo's `GITHUB_TOKEN` has no read access here.
The first version of the gate tried exactly that and failed with `curl: (22)
404` on its own pull request.

So `enforce-runner-policy.yml` carries the checker inline. The ruleset already
delivers that file from this repository, so there is still one source of truth
— it is the workflow. `scripts/audit-workflows.sh` remains the local checker
used by `audit.yml`. **Keep the two in sync**; they implement the same two
rules (explicit `self-hosted`, no dynamic `runs-on`).

## Exceptions

`runner-exceptions.json` is consumed by `audit.yml` only, which fails on any
entry whose `expires_on` has passed so exceptions cannot rot silently:

```json
{ "schema_version": 1,
  "exceptions": [
    { "repo": "example", "workflow": "ci.yml", "reason": "...",
      "expires_on": "2026-09-30" } ] }
```

The org-wide gate cannot read that file (see above), so it is strict. A
genuine, owner-approved exception is expressed in the ruleset itself — exclude
the repository in the ruleset conditions, or add a bypass actor. Both are
visible in the ruleset UI and the organization audit log, which is a stronger
record than a JSON entry. Record the reason and expiry in
`runner-exceptions.json` as well so the daily expiry check still surfaces it.

## Related

- `~/Working/docs/CI-RUNNER-GATES.md` — the human-facing gate document:
  approved labels, new-repository checklist, and known gaps.

The destination organization repository must be created with Actions disabled,
then protected rules and the self-hosted runner group must be configured before
enabling the audit workflow.
