from __future__ import annotations

import io
import re
import tokenize
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMENT_FREE_TREES = ("src/jqqlib", "scripts")
TOOL_DIRECTIVE_PATTERNS = (
    re.compile(r"# pragma: no cover"),
    re.compile(r"# type: ignore\[[a-z0-9-]+(?:, ?[a-z0-9-]+)*\]\s+#\s*\S.*"),
    re.compile(r"# noqa: [A-Z]+[0-9]+(?:, ?[A-Z]+[0-9]+)*"),
)


def _is_tool_directive(comment: str) -> bool:
    return any(pattern.fullmatch(comment.rstrip()) for pattern in TOOL_DIRECTIVE_PATTERNS)


def _explanatory_comments(source: str) -> list[tuple[int, str]]:
    found = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        is_shebang = token.start == (1, 0) and token.string.startswith("#!")
        if is_shebang or _is_tool_directive(token.string):
            continue
        found.append((token.start[0], token.string))
    return found


class SourceCommentPolicyTests(unittest.TestCase):
    def test_production_code_states_intent_through_names_and_tests_not_comments(self) -> None:
        offenders = []
        for tree in COMMENT_FREE_TREES:
            for path in sorted((REPO_ROOT / tree).rglob("*.py")):
                for line, comment in _explanatory_comments(path.read_text(encoding="utf-8")):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}: {comment}")
        self.assertEqual(offenders, [])

    def test_shebang_and_well_formed_tool_directives_are_allowed(self) -> None:
        for source in (
            "#!/usr/bin/env python\nx = 1\n",
            "x = 1  # pragma: no cover\n",
            "x = f()  # type: ignore[no-untyped-call]  # vendored module has no stubs\n",
            "x = f()  # type: ignore[attr-defined, misc]  # pandas-stubs lags the runtime API\n",
            "x = 1  # noqa: E501\n",
            "x = 1  # noqa: F401, E402\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(_explanatory_comments(source), [])

    def test_directives_cannot_smuggle_or_omit_an_explanation(self) -> None:
        for comment in (
            "# pragma: no cover - only when the package is not installed",
            "# type: ignore",
            "# type: ignore[attr-defined]",
            "# type: ignore[attr-defined]  mypy rejects a reason without its own hash",
            "# type: ignore[  vendored module has no stubs",
            "# noqa",
            "# noqa: E501 the URL cannot be split",
        ):
            with self.subTest(comment=comment):
                self.assertEqual(_explanatory_comments(f"x = 1  {comment}\n"), [(1, comment)])

    def test_a_shebang_after_the_first_line_is_a_comment(self) -> None:
        self.assertEqual(_explanatory_comments("x = 1\n#!/usr/bin/env python\n"), [(2, "#!/usr/bin/env python")])

    def test_an_explanatory_comment_is_reported_with_its_line(self) -> None:
        self.assertEqual(_explanatory_comments("x = 1\ny = 2  # explains why\n"), [(2, "# explains why")])

    def test_a_hash_inside_a_string_is_not_a_comment(self) -> None:
        self.assertEqual(_explanatory_comments('x = "# not a comment"\ny = f"{x}#also not"\n'), [])


if __name__ == "__main__":
    unittest.main()
