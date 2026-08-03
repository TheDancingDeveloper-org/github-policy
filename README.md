# Organization CI policy

This repository is the source of truth for organization-wide CI policy.

- GitHub Actions jobs use approved self-hosted runner labels by default.
- Public-hosted runners require an owner-approved, time-bounded exception in
  `runner-exceptions.json`.
- Workflow and policy changes require owner review through `CODEOWNERS`.
- `scripts/audit-workflows.sh` is the shared checker used by both workflows
  below.

## How enforcement actually works

| File | Scope | Role |
|---|---|---|
| `.github/workflows/runner-policy-reusable.yml` | The **calling** repository | **The gate.** Reusable workflow; runs with the caller's context so `actions/checkout` fetches the calling repo. |
| `templates/runner-policy-stub.yml` | — | Copy into each repo as `.github/workflows/runner-policy.yml`. Three lines of real content. |
| `.github/workflows/audit.yml` | This repository only | Self-audit, plus the daily expiry check on `runner-exceptions.json`. |

`audit.yml` was previously named "Organization workflow policy audit", which
was misleading: it checks out only this repository, so it audited exactly one
repository — itself — and stayed green while three repositories in the
organization ran on `ubuntu-latest`. Nothing about the checker was wrong; it
was never pointed at the organization. Do not re-add organization-wide
ambitions to that file.

### Why not an organization ruleset

A ruleset with the `workflows` ("required workflows") rule would attach the
gate to every repository automatically with no per-repo file. **It does not
work on this organization.** That rule is a GitHub Enterprise feature and this
org is on the **Team** plan. The REST API accepts the ruleset and reports it
`active`, but it never executes — no check ever appears on a pull request — and
`/rulesets/rule-suites` returns 403 "Upgrade to GitHub Enterprise". This was
built, observed to do nothing, and deleted on 2026-08-03. Do not rebuild it
without an Enterprise upgrade; a gate that reports active and enforces nothing
is worse than no gate.

The reusable-workflow approach works on Team because this repository has
Actions access set to `organization`, which lets private-repo workflows be
called org-wide.

### Rollout, per repository

1. Copy `templates/runner-policy-stub.yml` to `.github/workflows/runner-policy.yml`.
2. Add `runner-policy` to the branch's required status checks. **Until this
   step the gate reports but does not block.**
3. Ensure the repo is in the `public-node-b` runner group, or the job has no
   runner and the check never completes.

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
