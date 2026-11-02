from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
UNREDISTRIBUTABLE_PREFIXES = ("data/", "downloads/")
UNREDISTRIBUTABLE_FILES = ("config.yaml", ".envrc")
CITATION_VERSION = re.compile(r"^version:\s*['\"]?([^'\"\s]+)['\"]?\s*$", re.MULTILINE)
TAG_REF_PREFIX = "refs/tags/"


def unredistributable_paths(paths: Iterable[str]) -> list[str]:
    return [
        f"{path}: market data and credentials are never committed (J-Quants terms forbid redistribution)"
        for path in paths
        if path.startswith(UNREDISTRIBUTABLE_PREFIXES) or path in UNREDISTRIBUTABLE_FILES
    ]


def version_mismatch(root: Path) -> list[str]:
    project_version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    match = CITATION_VERSION.search((root / "CITATION.cff").read_text(encoding="utf-8"))
    citation_version = match.group(1) if match else None
    if citation_version == project_version:
        return []
    return [f"CITATION.cff version {citation_version} != pyproject.toml [project].version {project_version}"]


def pushed_tags(push_lines: Iterable[str]) -> list[str]:
    found = []
    for line in push_lines:
        fields = line.split()
        if len(fields) == 4 and fields[2].startswith(TAG_REF_PREFIX):
            found.append(f"{fields[2]}: release tags are created by release.yml, never pushed by hand")
    return found


def tracked_paths(root: Path) -> list[str]:
    return git_paths(root, "ls-files", "-z")


def staged_paths(root: Path) -> list[str]:
    return git_paths(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")


def git_paths(root: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [path for path in result.stdout.split("\0") if path]


def main(argv: Sequence[str] | None = None, *, root: Path = REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(description="Check the CONTRIBUTING.md ground rules that a machine can decide.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="check staged paths instead of tracked ones")
    mode.add_argument("--pre-push", action="store_true", help="read git pre-push stdin and reject tag pushes")
    args = parser.parse_args(argv)
    if args.pre_push:
        found = pushed_tags(sys.stdin)
    else:
        paths = staged_paths(root) if args.staged else tracked_paths(root)
        found = version_mismatch(root) + unredistributable_paths(paths)
    for violation in found:
        print(violation, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
