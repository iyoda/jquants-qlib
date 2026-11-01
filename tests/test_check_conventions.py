from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_conventions.py"
_SPEC = importlib.util.spec_from_file_location("check_conventions_script", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
check_conventions = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_conventions)

MODULE_SCOPE_ALL = (
    "__all__ = []\n",
    "__all__: list[str] = []\n",
    "__all__ += []\n",
    "(__all__, other) = ([], 1)\n",
    "[first, *__all__] = [1, 2]\n",
    "if True:\n    __all__ = []\n",
    "try:\n    pass\nexcept Exception:\n    __all__ = []\n",
)
NOT_MODULE_SCOPE_ALL = (
    "def f():\n    __all__ = []\n",
    "class A:\n    __all__ = []\n",
    'x = "__all__ = []"\n',
    "names = __all__\n",
)


class AllDeclarationTests(unittest.TestCase):
    def test_module_scope_all_is_found_in_every_assignment_form(self) -> None:
        for source in MODULE_SCOPE_ALL:
            with self.subTest(source=source):
                self.assertTrue(check_conventions.declares_all(source))

    def test_names_inside_functions_classes_and_strings_are_not_module_all(self) -> None:
        for source in NOT_MODULE_SCOPE_ALL:
            with self.subTest(source=source):
                self.assertFalse(check_conventions.declares_all(source))


class RuleTests(unittest.TestCase):
    def test_the_repository_follows_the_rules(self) -> None:
        found = check_conventions.version_mismatch(REPO_ROOT) + check_conventions.misplaced_all_declarations(REPO_ROOT)
        self.assertEqual(found, [])

    def test_tag_pushes_are_rejected_and_branch_pushes_pass(self) -> None:
        lines = ["refs/heads/dev 1 refs/heads/dev 0\n", "refs/tags/v1 1 refs/tags/v1 0\n"]
        self.assertEqual([line.split(":")[0] for line in check_conventions.pushed_tags(lines)], ["refs/tags/v1"])

    def test_market_data_and_credentials_are_rejected(self) -> None:
        paths = ["data/x.parquet", "downloads/a.csv", "config.yaml", ".envrc", "config.example.yaml", "src/data/x.py"]
        found = check_conventions.unredistributable_paths(paths)
        self.assertEqual([line.split(":")[0] for line in found], paths[:4])


if __name__ == "__main__":
    unittest.main()
