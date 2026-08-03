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

## Exceptions

```json
{ "schema_version": 1,
  "exceptions": [
    { "repo": "example", "workflow": "ci.yml", "reason": "...",
      "expires_on": "2026-09-30" } ] }
```

The audit fails on any exception whose `expires_on` has passed, so exceptions
cannot rot silently.

## Related

- `~/Working/docs/CI-RUNNER-GATES.md` — the human-facing gate document:
  approved labels, new-repository checklist, and known gaps.

The destination organization repository must be created with Actions disabled,
then protected rules and the self-hosted runner group must be configured before
enabling the audit workflow.
