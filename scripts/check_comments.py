from __future__ import annotations

import argparse
import ast
import io
import os
import re
import sys
import tokenize
from collections.abc import Iterable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TYPED_PACKAGE_ROOT = Path("src/jqqlib")
COMMENT_FREE_ROOTS = (TYPED_PACKAGE_ROOT, Path("scripts"))
TOOL_DIRECTIVE_PATTERNS = (
    re.compile(r"# pragma: no cover"),
    re.compile(r"# type: ignore\[[a-z0-9-]+(?:, ?[a-z0-9-]+)*\]\s+#\s*\S.*"),
    re.compile(r"# noqa: [A-Z]+[0-9]+(?:, ?[A-Z]+[0-9]+)*"),
)
DEFINITION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def is_tool_directive(comment: str) -> bool:
    return any(pattern.fullmatch(comment.rstrip()) for pattern in TOOL_DIRECTIVE_PATTERNS)


def explanatory_comments(source: str) -> list[tuple[int, str]]:
    found = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        is_shebang = token.start == (1, 0) and token.string.startswith("#!")
        if is_shebang or is_tool_directive(token.string):
            continue
        found.append((token.start[0], token.string))
    return found


def is_private_name(name: str) -> bool:
    is_dunder = name.startswith("__") and name.endswith("__")
    return name.startswith("_") and not is_dunder


def is_public_api_module(relative_path: Path) -> bool:
    module_parts = [*relative_path.parent.parts, relative_path.stem]
    return relative_path.is_relative_to(TYPED_PACKAGE_ROOT) and not any(map(is_private_name, module_parts))


def docstrings_outside_public_api(source: str, *, public_api_module: bool) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    if not public_api_module and ast.get_docstring(tree) is not None:
        found.append((tree.body[0].lineno, "<module>"))

    def visit(node: ast.AST, qualified_name: str, inside_function: bool, enclosing_is_public: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, DEFINITION_NODES):
                visit(child, qualified_name, inside_function, enclosing_is_public)
                continue
            name = f"{qualified_name}.{child.name}" if qualified_name else child.name
            is_public = enclosing_is_public and not inside_function and not is_private_name(child.name)
            if ast.get_docstring(child) is not None and not is_public:
                found.append((child.body[0].lineno, name))
            visit(child, name, inside_function or not isinstance(child, ast.ClassDef), is_public)

    visit(tree, "", False, public_api_module)
    return found


def is_governed(relative_path: Path) -> bool:
    return any(relative_path.is_relative_to(governed) for governed in COMMENT_FREE_ROOTS)


def governed_relative_path(candidate: Path, *, logical_root: Path, root: Path) -> Path | None:
    if candidate.suffix != ".py":
        return None
    logical_relative = Path(os.path.relpath(candidate, logical_root))
    try:
        real = candidate.resolve()
        if not real.is_file():
            return None
    except (OSError, RuntimeError):
        return logical_relative if is_governed(logical_relative) else None
    for governed in COMMENT_FREE_ROOTS:
        real_governed = (root / governed).resolve()
        if real.is_relative_to(real_governed):
            return logical_relative if is_governed(logical_relative) else governed / real.relative_to(real_governed)
    return None


def python_files_under(directory: Path, unreadable_directories: list[OSError]) -> list[Path]:
    return sorted(
        Path(parent, name)
        for parent, _, names in os.walk(directory, onerror=unreadable_directories.append)
        for name in names
        if name.endswith(".py")
    )


def governed_python_files(paths: Iterable[Path], *, root: Path) -> list[tuple[Path, Path | OSError]]:
    logical_root = Path(os.path.abspath(root))
    files: dict[Path, Path | OSError] = {}
    for path in paths:
        logical = Path(os.path.abspath(path))
        unreadable_directories: list[OSError] = []
        try:
            candidates = python_files_under(logical, unreadable_directories) if logical.is_dir() else [logical]
        except OSError:
            candidates = [logical]
        for error in unreadable_directories:
            relative_directory = Path(os.path.relpath(str(error.filename), logical_root))
            if is_governed(relative_directory):
                files.setdefault(relative_directory, error)
        for candidate in candidates:
            relative = governed_relative_path(candidate, logical_root=logical_root, root=root)
            if relative is not None:
                files.setdefault(relative, candidate)
    return sorted(files.items())


def violations(paths: Iterable[Path], *, root: Path = REPO_ROOT) -> list[str]:
    found = []
    for relative, path in governed_python_files(paths, root=root):
        if isinstance(path, OSError):
            found.append(f"{relative}: cannot be checked: {type(path).__name__}: {path}")
            continue
        try:
            source = path.read_text(encoding="utf-8")
            comments = explanatory_comments(source)
            docstrings = docstrings_outside_public_api(source, public_api_module=is_public_api_module(relative))
        except (OSError, ValueError, SyntaxError, tokenize.TokenError) as exc:
            found.append(f"{relative}: cannot be checked: {type(exc).__name__}: {exc}")
            continue
        found.extend(f"{relative}:{line}: comment {comment!r}" for line, comment in comments)
        found.extend(
            f"{relative}:{line}: docstring on {name}, which is not public API of the typed package"
            for line, name in docstrings
        )
    return found


def main(argv: Sequence[str] | None = None, *, root: Path = REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail on explanatory comments in the Python files under src/jqqlib and scripts, and on docstrings "
            "outside the public API of the typed package. Other paths are ignored, so staged-file lists can be "
            "passed as is."
        )
    )
    parser.add_argument("paths", nargs="*", type=Path, help="files or directories (default: src/jqqlib scripts)")
    args = parser.parse_args(argv)
    paths = args.paths or [root / governed for governed in COMMENT_FREE_ROOTS]
    found = violations(paths, root=root)
    for violation in found:
        print(violation, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
