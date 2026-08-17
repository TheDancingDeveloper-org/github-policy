#!/usr/bin/env bash
#
# Thin wrapper kept for the existing call sites. All logic is in
# scripts/runner_policy.py, which the org-wide gate runs directly.

set -euo pipefail

root="${1:-.}"
exec python3 "$(dirname "$0")/runner_policy.py" "$root" \
  ${POLICY_REPO:+--repo "$POLICY_REPO"} \
  --exceptions "${RUNNER_EXCEPTION_FILE:-$root/runner-exceptions.json}" \
  ${POLICY_DATE:+--today "$POLICY_DATE"}
