from __future__ import annotations

import argparse
import ast
import io
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


def governed_python_files(paths: Iterable[Path], *, root: Path) -> list[Path]:
    governed_roots = [root / governed for governed in COMMENT_FREE_ROOTS]
    files: set[Path] = set()
    for path in paths:
        absolute = (path if path.is_absolute() else Path.cwd() / path).resolve()
        candidates = sorted(absolute.rglob("*.py")) if absolute.is_dir() else [absolute]
        for candidate in candidates:
            is_governed = any(candidate.is_relative_to(governed.resolve()) for governed in governed_roots)
            if candidate.suffix == ".py" and candidate.is_file() and is_governed:
                files.add(candidate)
    return sorted(files)


def violations(paths: Iterable[Path], *, root: Path = REPO_ROOT) -> list[str]:
    resolved_root = root.resolve()
    found = []
    for path in governed_python_files(paths, root=resolved_root):
        relative = path.relative_to(resolved_root)
        source = path.read_text(encoding="utf-8")
        for line, comment in explanatory_comments(source):
            found.append(f"{relative}:{line}: comment {comment!r}")
        for line, name in docstrings_outside_public_api(source, public_api_module=is_public_api_module(relative)):
            found.append(f"{relative}:{line}: docstring on {name}, which is not public API of the typed package")
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
