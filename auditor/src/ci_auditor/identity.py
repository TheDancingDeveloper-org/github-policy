"""Identity policy: commit author and committer, tracked paths and content.

Never the commit message body. Bodies carry graft `Claimed-Author:` and
`Co-Authored-By:` trailers, transcripts and paths that workers do not
control; grepping them closed 8 batches in two weeks (CI review §3.7, §3.10).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


def default_forbidden() -> list[str]:
    # Built from parts so this file never matches itself.
    return ["".join(("sp", "roo", "ty"))]


@dataclass(frozen=True)
class IdentityFinding:
    where: str
    message: str


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def commits(root: Path, base: str | None, head: str) -> list[str]:
    if base and set(base) != {"0"}:
        try:
            return _git(root, "rev-list", "--reverse", f"{base}..{head}").split()
        except subprocess.CalledProcessError:
            pass
    return _git(root, "rev-list", "--max-count=1", head).split()


def check(
    root: Path, base: str | None, head: str, forbidden: list[str], content: bool = True
) -> list[IdentityFinding]:
    needles = [f.lower() for f in forbidden if f]
    found: list[IdentityFinding] = []
    for sha in commits(root, base, head):
        # %an %ae %cn %ce only - deliberately not %B.
        meta = _git(root, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce", sha).strip().split("\x00")
        for field, value in zip(
            ("author name", "author email", "committer name", "committer email"), meta, strict=False
        ):
            if any(n in value.lower() for n in needles):
                found.append(IdentityFinding(sha[:12], f"forbidden identifier in {field}"))
    paths = _git(root, "ls-files", "-z").split("\x00")
    for path in paths:
        if path and any(n in path.lower() for n in needles):
            found.append(IdentityFinding(path, "forbidden identifier in a tracked path"))
    if content:
        for needle in needles:
            proc = subprocess.run(
                ["git", "-C", str(root), "grep", "-Iil", "--", needle],
                capture_output=True,
                text=True,
                check=False,
            )
            for path in proc.stdout.split():
                found.append(IdentityFinding(path, "forbidden identifier in tracked content"))
    return found
