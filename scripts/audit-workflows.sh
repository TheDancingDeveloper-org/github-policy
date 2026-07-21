#!/usr/bin/env bash

set -euo pipefail

root="${1:-.}"
today="${POLICY_DATE:-$(date -u +%F)}"
exceptions="${RUNNER_EXCEPTION_FILE:-$root/runner-exceptions.json}"
failed=0

command -v jq >/dev/null
jq -e '.schema_version == 1 and (.exceptions | type == "array")' "$exceptions" >/dev/null

while IFS= read -r expiry; do
  [[ "$expiry" > "$today" || "$expiry" == "$today" ]] || {
    printf 'expired runner exception: %s\n' "$expiry" >&2
    failed=1
  }
done < <(jq -r '.exceptions[].expires_on' "$exceptions")

while IFS= read -r -d '' workflow; do
  if ! python3 - "$workflow" <<'PY'
import re
import sys

path = sys.argv[1]
lines = open(path, encoding='utf-8').read().splitlines()

def indent(line: str) -> int:
    return len(line) - len(line.lstrip(' '))

def values(index: int) -> list[str]:
    line = lines[index]
    base = indent(line)
    value = line.split(':', 1)[1].split('#', 1)[0].strip()
    if value:
        return [part.strip().strip('"\'') for part in value.strip('[]').split(',') if part.strip()]
    result = []
    for child in lines[index + 1:]:
        stripped = child.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if indent(child) <= base:
            break
        match = re.match(r'^-\s*([^#]+)', stripped)
        if match:
            result.append(match.group(1).strip().strip('"\''))
    return result

failed = False
for number, line in enumerate(lines):
    if not re.match(r'^\s*runs-on\s*:', line, re.I):
        continue
    selected = values(number)
    if not any(value.lower() == 'self-hosted' for value in selected):
        print(f'{path}:{number + 1}: runner selection is not explicitly self-hosted', file=sys.stderr)
        failed = True
    if any('${{' in value for value in selected):
        print(f'{path}:{number + 1}: dynamic runner selection requires explicit review', file=sys.stderr)
        failed = True
sys.exit(1 if failed else 0)
PY
  then
    failed=1
  fi
done < <(find "$root" -path '*/.git' -prune -o -path '*/.github/workflows/*.yml' -print0 -o -path '*/.github/workflows/*.yaml' -print0)

exit "$failed"
