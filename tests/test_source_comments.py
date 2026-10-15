from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("check_comments_script", REPO_ROOT / "scripts" / "check_comments.py")
assert _SPEC is not None and _SPEC.loader is not None
check_comments = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_comments)


class RepositoryPolicyTests(unittest.TestCase):
    def test_production_code_states_intent_through_names_and_tests_not_comments(self) -> None:
        self.assertEqual(check_comments.violations([REPO_ROOT / "src/jqqlib", REPO_ROOT / "scripts"]), [])


class CommentRuleTests(unittest.TestCase):
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
                self.assertEqual(check_comments.explanatory_comments(source), [])

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
                self.assertEqual(check_comments.explanatory_comments(f"x = 1  {comment}\n"), [(1, comment)])

    def test_a_shebang_after_the_first_line_is_a_comment(self) -> None:
        self.assertEqual(
            check_comments.explanatory_comments("x = 1\n#!/usr/bin/env python\n"), [(2, "#!/usr/bin/env python")]
        )

    def test_an_explanatory_comment_is_reported_with_its_line(self) -> None:
        self.assertEqual(
            check_comments.explanatory_comments("x = 1\ny = 2  # explains why\n"), [(2, "# explains why")]
        )

    def test_a_hash_inside_a_string_is_not_a_comment(self) -> None:
        self.assertEqual(check_comments.explanatory_comments('x = "# not a comment"\ny = f"{x}#also not"\n'), [])


PUBLIC_AND_PRIVATE_DEFINITIONS = '''"""Module."""


def public():
    """Public."""

    def nested():
        """Nested."""


def _private():
    """Private."""


class Public:
    """Public class."""

    def method(self):
        """Public method."""

    def __init__(self):
        """Dunder."""

    def _helper(self):
        """Private method."""


class _Private:
    def method(self):
        """Method of a private class."""
'''


class DocstringRuleTests(unittest.TestCase):
    def test_only_public_api_of_the_typed_package_may_carry_docstrings(self) -> None:
        found = check_comments.docstrings_outside_public_api(PUBLIC_AND_PRIVATE_DEFINITIONS, public_api_module=True)
        self.assertEqual(
            [name for _, name in found], ["public.nested", "_private", "Public._helper", "_Private.method"]
        )

    def test_modules_outside_the_typed_package_carry_no_docstrings_at_all(self) -> None:
        found = check_comments.docstrings_outside_public_api(PUBLIC_AND_PRIVATE_DEFINITIONS, public_api_module=False)
        self.assertEqual(
            [name for _, name in found],
            ["<module>", "public", "public.nested", "_private", "Public", "Public.method", "Public.__init__",
             "Public._helper", "_Private.method"],
        )

    def test_public_api_modules_are_the_non_private_modules_of_the_typed_package(self) -> None:
        self.assertTrue(check_comments.is_public_api_module(Path("src/jqqlib/fetch.py")))
        self.assertTrue(check_comments.is_public_api_module(Path("src/jqqlib/__init__.py")))
        self.assertFalse(check_comments.is_public_api_module(Path("src/jqqlib/_internal.py")))
        self.assertFalse(check_comments.is_public_api_module(Path("scripts/dist_smoke.py")))


class CommandLineTests(unittest.TestCase):
    def _run(self, root: Path, *paths: Path) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = check_comments.main([str(path) for path in paths], root=root)
        return code, stderr.getvalue()

    def test_reports_each_violation_with_its_location_and_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "src" / "jqqlib" / "bad.py"
            module.parent.mkdir(parents=True)
            module.write_text('def _helper():\n    """Explains."""\n    return 1  # because\n', encoding="utf-8")

            code, stderr = self._run(root, module)

        self.assertEqual(code, 1)
        self.assertIn("src/jqqlib/bad.py:3: comment '# because'", stderr)
        self.assertIn("src/jqqlib/bad.py:2: docstring on _helper", stderr)

    def test_paths_outside_the_governed_trees_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "tests" / "test_anything.py"
            outside.parent.mkdir(parents=True)
            outside.write_text("x = 1  # a test may explain itself\n", encoding="utf-8")
            readme = root / "README.md"
            readme.write_text("# heading\n", encoding="utf-8")

            self.assertEqual(self._run(root, outside, readme, root / "missing.py"), (0, ""))

    def test_without_paths_the_whole_governed_trees_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "scripts" / "tool.py"
            script.parent.mkdir(parents=True)
            script.write_text('"""Usage text."""\n', encoding="utf-8")

            code, stderr = self._run(root)

        self.assertEqual(code, 1)
        self.assertIn("scripts/tool.py:1: docstring on <module>", stderr)


if __name__ == "__main__":
    unittest.main()
