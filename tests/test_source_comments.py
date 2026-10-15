from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("check_comments_script", REPO_ROOT / "scripts" / "check_comments.py")
assert _SPEC is not None and _SPEC.loader is not None
check_comments = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_comments)

ALLOWED_SOURCES = (
    "#!/usr/bin/env python\nx = 1\n",
    "x = 1  # pragma: no cover\n",
    "x = f()  # type: ignore[no-untyped-call]  # vendored module has no stubs\n",
    "x = f()  # type: ignore[attr-defined, misc]  # pandas-stubs lags the runtime API\n",
    "x = 1  # noqa: E501\n",
    "x = 1  # noqa: F401, E402\n",
    'x = "# not a comment"\ny = f"{x}#also not"\n',
)
REJECTED_SOURCES = {
    "x = 1  # pragma: no cover - only when the package is not installed\n": 1,
    "x = 1  # type: ignore\n": 1,
    "x = 1  # type: ignore[attr-defined]\n": 1,
    "x = 1  # type: ignore[attr-defined]  mypy rejects a reason without its own hash\n": 1,
    "x = 1  # type: ignore[  vendored module has no stubs\n": 1,
    "x = 1  # noqa\n": 1,
    "x = 1  # noqa: E501 the URL cannot be split\n": 1,
    "x = 1\n#!/usr/bin/env python\n": 2,
    "x = 1\ny = 2  # explains why\n": 2,
}
DOC = '"""Doc."""'
DOCSTRING_CASES = (
    (f"def public():\n    {DOC}\n", True, []),
    (f"def _private():\n    {DOC}\n", True, ["_private"]),
    (f"def public():\n    def nested():\n        {DOC}\n", True, ["public.nested"]),
    (f"class Public:\n    {DOC}\n    def method(self):\n        {DOC}\n", True, []),
    (f"class Public:\n    def __init__(self):\n        {DOC}\n", True, []),
    (f"class Public:\n    class Inner:\n        {DOC}\n", True, []),
    (f"class Public:\n    def _helper(self):\n        {DOC}\n", True, ["Public._helper"]),
    (f"class _Private:\n    def method(self):\n        {DOC}\n", True, ["_Private.method"]),
    (f"{DOC}\ndef public():\n    {DOC}\n", False, ["<module>", "public"]),
)


class RepositoryPolicyTests(unittest.TestCase):
    def test_production_code_states_intent_through_names_and_tests_not_comments(self) -> None:
        self.assertEqual(check_comments.violations([REPO_ROOT / "src/jqqlib", REPO_ROOT / "scripts"]), [])


class RuleTests(unittest.TestCase):
    def test_shebang_strings_and_well_formed_tool_directives_are_allowed(self) -> None:
        for source in ALLOWED_SOURCES:
            with self.subTest(source=source):
                self.assertEqual(check_comments.explanatory_comments(source), [])

    def test_directives_cannot_smuggle_or_omit_an_explanation_and_comments_are_reported_by_line(self) -> None:
        for source, line in REJECTED_SOURCES.items():
            with self.subTest(source=source):
                self.assertEqual([found_line for found_line, _ in check_comments.explanatory_comments(source)], [line])

    def test_only_public_api_of_the_typed_package_may_carry_docstrings(self) -> None:
        for source, public_api_module, expected in DOCSTRING_CASES:
            with self.subTest(source=source, public_api_module=public_api_module):
                found = check_comments.docstrings_outside_public_api(source, public_api_module=public_api_module)
                self.assertEqual([name for _, name in found], expected)

    def test_public_api_modules_are_the_non_private_modules_of_the_typed_package(self) -> None:
        self.assertTrue(check_comments.is_public_api_module(Path("src/jqqlib/fetch.py")))
        self.assertTrue(check_comments.is_public_api_module(Path("src/jqqlib/__init__.py")))
        self.assertFalse(check_comments.is_public_api_module(Path("src/jqqlib/_internal.py")))
        self.assertFalse(check_comments.is_public_api_module(Path("scripts/dist_smoke.py")))


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)

    def _write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _run(self, *paths: Path) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = check_comments.main([str(path) for path in paths], root=self.root)
        return code, stderr.getvalue()

    def test_reports_each_violation_with_its_location_and_fails(self) -> None:
        module = self._write("src/jqqlib/bad.py", 'def _helper():\n    """Explains."""\n    return 1  # because\n')

        code, stderr = self._run(module)

        self.assertEqual(code, 1)
        self.assertIn("src/jqqlib/bad.py:3: comment '# because'", stderr)
        self.assertIn("src/jqqlib/bad.py:2: docstring on _helper", stderr)

    def test_a_file_that_cannot_be_parsed_is_reported_instead_of_crashing(self) -> None:
        self._write("src/jqqlib/broken.py", "def broken(:\n")
        self._write("src/jqqlib/null_byte.py", "x = 1\x00\n")
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "latin1.py").write_bytes(b"x = '\xe9'\n")
        loop = self.root / "scripts" / "loop.py"
        os.symlink(loop, loop)

        code, stderr = self._run()

        self.assertEqual(code, 1)
        self.assertIn("src/jqqlib/broken.py: cannot be checked:", stderr)
        self.assertIn("src/jqqlib/null_byte.py: cannot be checked:", stderr)
        self.assertIn("scripts/latin1.py: cannot be checked: UnicodeDecodeError", stderr)
        self.assertIn("scripts/loop.py: cannot be checked:", stderr)

    @unittest.skipIf(os.geteuid() == 0, "root can read a file without read permission")
    def test_an_unreadable_file_is_reported_instead_of_crashing(self) -> None:
        unreadable = self._write("scripts/secret.py", "x = 1\n")
        unreadable.chmod(0)
        self.addCleanup(unreadable.chmod, 0o644)

        code, stderr = self._run(unreadable)

        self.assertEqual(code, 1)
        self.assertIn("scripts/secret.py: cannot be checked: PermissionError", stderr)

    def test_a_python_named_symlink_is_checked_whatever_its_target_is_called(self) -> None:
        self._write("src/jqqlib/notes.txt", "x = 1  # hidden behind a .txt target\n")
        os.symlink(self.root / "src/jqqlib/notes.txt", self.root / "src/jqqlib/alias.py")

        code, stderr = self._run()

        self.assertEqual(code, 1)
        self.assertIn("src/jqqlib/alias.py:1: comment", stderr)

    def test_public_api_is_judged_by_the_path_the_module_is_imported_from(self) -> None:
        self._write("src/jqqlib/_impl.py", 'def helper():\n    """Doc."""\n')
        public = self.root / "src/jqqlib/public.py"
        os.symlink(self.root / "src/jqqlib/_impl.py", public)

        self.assertEqual(self._run(public), (0, ""))
        self.assertIn("src/jqqlib/_impl.py:2: docstring on helper", self._run()[1])

    def test_a_package_directory_symlinked_out_of_the_repository_is_still_checked_as_the_package(self) -> None:
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        Path(elsewhere.name, "api.py").write_text('def api():\n    """Doc."""\n    return 1  # why\n', encoding="utf-8")
        (self.root / "src").mkdir()
        os.symlink(elsewhere.name, self.root / "src/jqqlib")

        code, stderr = self._run()

        self.assertEqual(code, 1)
        self.assertEqual(stderr.splitlines(), ["src/jqqlib/api.py:3: comment '# why'"])

    def test_paths_outside_the_governed_trees_are_ignored(self) -> None:
        outside = self._write("tests/test_anything.py", "x = 1  # a test may explain itself\n")
        readme = self._write("README.md", "# heading\n")

        self.assertEqual(self._run(outside, readme, self.root / "missing.py"), (0, ""))

    def test_a_symlink_to_a_file_outside_the_governed_trees_is_ignored_however_it_is_reached(self) -> None:
        target = self._write("tests/helper.py", "x = 1  # outside the policy\n")
        link = self.root / "src" / "jqqlib" / "linked.py"
        link.parent.mkdir(parents=True)
        os.symlink(target, link)

        self.assertEqual(self._run(), (0, ""))
        self.assertEqual(self._run(link), (0, ""))

    def test_without_paths_the_whole_governed_trees_are_checked(self) -> None:
        self._write("scripts/tool.py", '"""Usage text."""\n')

        code, stderr = self._run()

        self.assertEqual(code, 1)
        self.assertIn("scripts/tool.py:1: docstring on <module>", stderr)


if __name__ == "__main__":
    unittest.main()
