from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
UNREDISTRIBUTABLE_PREFIXES = ("data/", "downloads/")
UNREDISTRIBUTABLE_FILES = ("config.yaml", ".envrc")
ALL_DECLARATION_ALLOWED = Path("src/jqqlib/__init__.py")
ALL_DECLARATION_ROOTS = (Path("src"), Path("scripts"))
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


def declares_all(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in targets
        ):
            return True
    return False


def misplaced_all_declarations(root: Path) -> list[str]:
    found = []
    for base in ALL_DECLARATION_ROOTS:
        for path in sorted((root / base).rglob("*.py")):
            relative = path.relative_to(root)
            if relative != ALL_DECLARATION_ALLOWED and declares_all(path.read_text(encoding="utf-8")):
                found.append(f"{relative}: declare __all__ only in {ALL_DECLARATION_ALLOWED} (ruff D1 relies on it)")
    return found


def pushed_tags(push_lines: Iterable[str]) -> list[str]:
    found = []
    for line in push_lines:
        fields = line.split()
        if len(fields) == 4 and fields[2].startswith(TAG_REF_PREFIX):
            found.append(f"{fields[2]}: release tags are created by release.yml, never pushed by hand")
    return found


def staged_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [path for path in result.stdout.split("\0") if path]


def main(argv: Sequence[str] | None = None, *, root: Path = REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(description="Check the CONTRIBUTING.md ground rules that a machine can decide.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="also reject staged market data and credentials")
    mode.add_argument("--pre-push", action="store_true", help="read git pre-push stdin and reject tag pushes")
    args = parser.parse_args(argv)
    if args.pre_push:
        found = pushed_tags(sys.stdin)
    else:
        found = version_mismatch(root) + misplaced_all_declarations(root)
        if args.staged:
            found += unredistributable_paths(staged_paths(root))
    for violation in found:
        print(violation, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
