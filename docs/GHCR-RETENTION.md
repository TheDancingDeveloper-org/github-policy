# GHCR retention policy

`ghcr-retention-policy.json` is the single retention policy for the
organization's container packages. Repository-specific files may name their
packages, but must not redefine retention windows or deletion rules.

The scheduled workflow runs a dry run every Sunday and uploads a JSON report
containing before/after counts, deletion reasons, digests, and results. Apply
is manual and explicitly gated from the workflow dispatch input; review the
dry-run artifact before enabling it. The tool also resolves OCI indexes and
referrers before considering an untagged version unreachable, and protects
paired package revisions together.

For a local, read-only plan (with a token that can read packages):

```console
GH_TOKEN=... python3 scripts/ghcr_retention.py --package rustnzbd
```

Only the central allowlist and the centrally reviewed exception file may alter
the scope. Exceptions require an owner, reason, and expiry no more than 90
days away. `estimated_storage_reclaimed_bytes` is populated when the GitHub
package API supplies size data; otherwise it is explicitly `null` rather than
invented.
