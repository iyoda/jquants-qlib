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

class RuleTests(unittest.TestCase):
    def test_the_repository_follows_the_rules(self) -> None:
        found = check_conventions.version_mismatch(REPO_ROOT)
        found += check_conventions.unredistributable_paths(check_conventions.tracked_paths(REPO_ROOT))
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
