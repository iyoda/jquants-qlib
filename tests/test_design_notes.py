from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DESIGN_NOTES = REPO_ROOT / "docs" / "design-notes.md"
CODE_TREES = ("src/jqqlib", "scripts", "tests")
BACKTICKED = re.compile(r"`([^`\n]+)`")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
REPO_PATH = re.compile(r"(?:src|scripts|tests|docs)/[\w./-]+\.(?:py|md|sh)")
BARE_FILENAME = re.compile(r"[\w-]+\.(?:py|md|sh)")


def _looks_like_a_code_name(token: str) -> bool:
    has_mixed_case = token != token.lower() and token != token.upper()
    return IDENTIFIER.fullmatch(token) is not None and ("_" in token or has_mixed_case)


def _code_files() -> list[Path]:
    this_test = Path(__file__).resolve()
    files = (path.resolve() for tree in CODE_TREES for path in sorted((REPO_ROOT / tree).rglob("*.py")))
    return [path for path in files if path != this_test]


class DesignNotesReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tokens = set(BACKTICKED.findall(DESIGN_NOTES.read_text(encoding="utf-8")))

    def test_every_cited_name_exists_in_the_code_or_tests_other_than_this_check(self) -> None:
        code = "\n".join(path.read_text(encoding="utf-8") for path in _code_files())
        names = sorted(token for token in self.tokens if _looks_like_a_code_name(token))
        self.assertGreater(len(names), 30)
        self.assertEqual([name for name in names if re.search(rf"\b{re.escape(name)}\b", code) is None], [])

    def test_every_cited_test_is_defined(self) -> None:
        tests_text = "\n".join(path.read_text(encoding="utf-8") for path in (REPO_ROOT / "tests").rglob("*.py"))
        cited = sorted(token for token in self.tokens if token.startswith("test_") and IDENTIFIER.fullmatch(token))
        self.assertGreater(len(cited), 15)
        self.assertEqual([name for name in cited if re.search(rf"\bdef {re.escape(name)}\(", tests_text) is None], [])

    def test_every_cited_repository_path_and_file_name_exists(self) -> None:
        paths = sorted(token for token in self.tokens if REPO_PATH.fullmatch(token))
        file_names = sorted(token for token in self.tokens if BARE_FILENAME.fullmatch(token))
        existing_names = {path.name for tree in (*CODE_TREES, "docs") for path in (REPO_ROOT / tree).rglob("*")}
        self.assertGreater(len(paths), 5)
        self.assertGreater(len(file_names), 5)
        self.assertEqual([path for path in paths if not (REPO_ROOT / path).exists()], [])
        self.assertEqual([name for name in file_names if name not in existing_names], [])

    def test_code_name_detection_skips_plain_words_and_environment_names(self) -> None:
        self.assertTrue(_looks_like_a_code_name("snake_case_name"))
        self.assertTrue(_looks_like_a_code_name("CamelCaseName"))
        self.assertFalse(_looks_like_a_code_name("PYTHONPATH"))
        self.assertFalse(_looks_like_a_code_name("factor"))
        self.assertFalse(_looks_like_a_code_name("$factor"))


if __name__ == "__main__":
    unittest.main()
