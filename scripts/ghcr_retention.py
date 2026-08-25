#!/usr/bin/env python3
"""Plan and (only with --apply) execute conservative GHCR package pruning."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / "ghcr-retention-policy.json").read_text())
EXCEPTIONS = json.loads((ROOT / "ghcr-retention-exceptions.json").read_text())
API = "https://api.github.com"


def request(method: str, path: str, token: str, body: bytes | None = None) -> Any:
    req = urllib.request.Request(API + path, data=body, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def registry_token(owner: str, package: str, token: str) -> str:
    scope = urllib.parse.quote(f"repository:{owner}/{package}:pull", safe=":/")
    req = urllib.request.Request(f"https://ghcr.io/token?service=ghcr.io&scope={scope}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)["token"]


def reachable_digests(owner: str, package: str, roots: set[str], token: str) -> set[str]:
    """Resolve OCI indexes and attestations so children are never orphaned."""
    bearer = registry_token(owner, package, token)
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        req = urllib.request.Request(f"https://ghcr.io/v2/{owner}/{package}/manifests/{current}")
        req.add_header("Authorization", f"Bearer {bearer}")
        req.add_header("Accept", "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                document = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            raise
        for child in document.get("manifests", []):
            if isinstance(child, dict) and child.get("digest"):
                pending.append(child["digest"])
        subject = document.get("subject")
        if isinstance(subject, dict) and subject.get("digest"):
            pending.append(subject["digest"])
    return seen


def versions(owner: str, package: str, token: str) -> list[dict[str, Any]]:
    encoded = urllib.parse.quote(package, safe="")
    result: list[dict[str, Any]] = []
    page = 1
    while True:
        rows = request("GET", f"/orgs/{owner}/packages/container/{encoded}/versions?per_page=100&page={page}", token)
        if not rows:
            return result
        result.extend(rows)
        page += 1


def tags(version: dict[str, Any]) -> list[str]:
    return list(version.get("metadata", {}).get("container", {}).get("tags", []))


def digest(version: dict[str, Any]) -> str | None:
    value = version.get("metadata", {}).get("container", {}).get("digest")
    return value if isinstance(value, str) else None


def exception_matches(owner: str, package: str, version: dict[str, Any]) -> bool:
    now = dt.date.today()
    for item in EXCEPTIONS.get("exceptions", []):
        if item.get("package") != package or item.get("owner", owner) != owner:
            continue
        expiry = dt.date.fromisoformat(item["expiry_date"])
        if expiry < now or (expiry - now).days > POLICY["exception_max_days"]:
            raise ValueError(f"invalid or overlong exception for {package}: {item}")
        pattern = item.get("tag_or_digest_pattern", "")
        if re.search(pattern, " ".join(tags(version)) + " " + (digest(version) or "")):
            return True
    return False


def plan_package(owner: str, package: str, token: str) -> dict[str, Any]:
    rows = versions(owner, package, token)
    keep: set[int] = set()
    stable = re.compile(POLICY["stable_tag_pattern"])
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=POLICY["untagged_grace_days"])
    for row in rows:
        row_tags = tags(row)
        if any(stable.fullmatch(tag) or tag in POLICY["stable_channels"] for tag in row_tags):
            keep.add(row["id"])
        if exception_matches(owner, package, row):
            keep.add(row["id"])

    # The newest ten revisions in every non-release channel are roots. A tag
    # is a root; child manifests are retained by the registry as dependencies
    # and are never selected for deletion by this tool.
    channels = sorted({tag for row in rows for tag in tags(row) if not stable.fullmatch(tag) and tag not in POLICY["stable_channels"]})
    for channel in channels:
        candidates = [r for r in rows if channel in tags(r)]
        candidates.sort(key=lambda r: r.get("updated_at", r.get("created_at", "")), reverse=True)
        keep.update(r["id"] for r in candidates[: POLICY["channel_revision_limit"]])

    # Keep one organization-wide rollback window as well. This covers commit
    # tags and deployment revisions whose tag is no longer the active channel.
    tagged_revisions = [r for r in rows if tags(r)]
    tagged_revisions.sort(key=lambda r: r.get("updated_at", r.get("created_at", "")), reverse=True)
    keep.update(r["id"] for r in tagged_revisions[: POLICY["rollback_revision_limit"]])
    deployed = {value.strip() for value in os.environ.get("GHCR_DEPLOYED_DIGESTS", "").split(",") if value.strip()}
    keep.update(r["id"] for r in rows if digest(r) in deployed)

    # Untagged versions are eligible only after the grace period. This is
    # intentionally conservative: package versions with no tag are never
    # deleted if their digest is referenced by a retained tagged version.
    retained_digests = {digest(r) for r in rows if r["id"] in keep} - {None}
    retained_digests = reachable_digests(owner, package, retained_digests, token)
    delete: list[dict[str, Any]] = []
    for row in rows:
        created = dt.datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        if row["id"] in keep or tags(row) or digest(row) in retained_digests or created >= cutoff:
            continue
        delete.append({"id": row["id"], "digest": digest(row), "tags": tags(row), "created_at": row["created_at"], "reason": "untagged and unreachable after grace period", "result": "planned"})
    reclaimed = sum(int(r.get("size_in_bytes", 0) or 0) for r in rows if r["id"] not in keep and r["id"] in {item["id"] for item in delete})
    return {"package": package, "before_versions": len(rows), "before_tagged": sum(bool(tags(r)) for r in rows), "retain_ids": sorted(keep), "delete": delete, "after_versions": len(rows) - len(delete), "estimated_storage_reclaimed_bytes": reclaimed or None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", default="thedancingdeveloper-org")
    parser.add_argument("--package", action="append")
    parser.add_argument("--apply", action="store_true", help="delete the generated plan; default is dry-run")
    parser.add_argument("--output", type=Path, default=Path("ghcr-retention-report.json"))
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GH_TOKEN or GITHUB_TOKEN is required", file=sys.stderr)
        return 2
    selected = args.package or POLICY["packages"]
    report = {"schema_version": 1, "mode": "apply" if args.apply else "dry-run", "owner": args.owner, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "packages": []}
    for package in selected:
        if package not in POLICY["packages"]:
            raise SystemExit(f"package is not in the central allowlist: {package}")
        item = plan_package(args.owner, package, token)
        report["packages"].append(item)
    by_name = {item["package"]: item for item in report["packages"]}
    # Paired images share deployment revisions. If the matching package still
    # retains a tag, preserve that revision here too; pruning must be atomic at
    # the revision level even when one package has extra untagged children.
    for left, right in POLICY["paired_packages"]:
        if left not in by_name or right not in by_name:
            continue
        left_keep_tags = {tag for row in versions(args.owner, left, token) if row["id"] in by_name[left]["retain_ids"] for tag in tags(row)}
        right_keep_tags = {tag for row in versions(args.owner, right, token) if row["id"] in by_name[right]["retain_ids"] for tag in tags(row)}
        for item, protected in ((by_name[left], right_keep_tags), (by_name[right], left_keep_tags)):
            item["delete"] = [candidate for candidate in item["delete"] if not protected.intersection(candidate["tags"])]
            item["after_versions"] = item["before_versions"] - len(item["delete"])
    if args.apply:
        for item in report["packages"]:
            for candidate in item["delete"]:
                request("DELETE", f"/orgs/{args.owner}/packages/container/{urllib.parse.quote(item['package'], safe='')}/versions/{candidate['id']}", token)
                candidate["result"] = "deleted"
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
