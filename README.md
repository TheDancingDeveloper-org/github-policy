# Organization CI policy

This repository is the source of truth for organization-wide CI policy.

- GitHub Actions jobs use approved self-hosted runner labels by default.
- Public-hosted runners require an owner-approved, time-bounded exception in
  `runner-exceptions.json`.
- Workflow and policy changes require owner review through `CODEOWNERS`.
- `scripts/audit-workflows.sh` is the shared enforcement entry point.

The destination organization repository must be created with Actions disabled,
then protected rules and the self-hosted runner group must be configured before
enabling the audit workflow.
