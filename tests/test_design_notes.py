from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DESIGN_NOTES = REPO_ROOT / "docs" / "design-notes.md"
CODE_TREES = ("src/jqqlib", "scripts", "tests")
BACKTICKED = re.compile(r"`([^`\n]+)`")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
REPO_PATH = re.compile(r"(?:src|scripts|tests|docs)/[\w./-]+\.(?:py|md)")


def _looks_like_a_code_name(token: str) -> bool:
    has_mixed_case = token != token.lower() and token != token.upper()
    return IDENTIFIER.fullmatch(token) is not None and ("_" in token or has_mixed_case)


def _code_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for tree in CODE_TREES for path in sorted((REPO_ROOT / tree).rglob("*.py"))
    )


class DesignNotesReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tokens = BACKTICKED.findall(DESIGN_NOTES.read_text(encoding="utf-8"))

    def test_every_cited_name_exists_in_the_code_or_tests(self) -> None:
        code = _code_text()
        names = sorted({token for token in self.tokens if _looks_like_a_code_name(token)})
        missing = [name for name in names if re.search(rf"\b{re.escape(name)}\b", code) is None]
        self.assertGreater(len(names), 50)
        self.assertEqual(missing, [])

    def test_every_cited_test_is_defined(self) -> None:
        tests_text = "\n".join(path.read_text(encoding="utf-8") for path in (REPO_ROOT / "tests").rglob("*.py"))
        cited = sorted({token for token in self.tokens if token.startswith("test_") and IDENTIFIER.fullmatch(token)})
        undefined = [name for name in cited if re.search(rf"\bdef {re.escape(name)}\(", tests_text) is None]
        self.assertGreater(len(cited), 20)
        self.assertEqual(undefined, [])

    def test_every_cited_repository_path_exists(self) -> None:
        paths = sorted({token for token in self.tokens if REPO_PATH.fullmatch(token)})
        self.assertEqual([path for path in paths if not (REPO_ROOT / path).exists()], [])

    def test_code_name_detection_skips_plain_words_and_environment_names(self) -> None:
        self.assertTrue(_looks_like_a_code_name("_is_transient"))
        self.assertTrue(_looks_like_a_code_name("ExactCalendarRequired"))
        self.assertFalse(_looks_like_a_code_name("PYTHONPATH"))
        self.assertFalse(_looks_like_a_code_name("factor"))
        self.assertFalse(_looks_like_a_code_name("$factor"))


if __name__ == "__main__":
    unittest.main()
