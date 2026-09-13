from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jqqlib.pipeline import decide_publish, publish_pathspecs


class DecidePublishTests(unittest.TestCase):
    def test_identical_content_hash_is_no_op(self) -> None:
        self.assertFalse(decide_publish("abc123", "abc123"))

    def test_changed_content_hash_publishes(self) -> None:
        self.assertTrue(decide_publish("new456", "abc123"))

    def test_missing_committed_hash_is_first_publish(self) -> None:
        self.assertTrue(decide_publish("abc123", None))

    def test_missing_current_hash_is_no_op(self) -> None:
        self.assertFalse(decide_publish(None, "abc123"))


class PublishPathspecsTests(unittest.TestCase):
    def _pathspecs(self, extra: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "settings.yaml"
            config.write_text(
                f"storage:\n  parquet_dir: {root / 'p'}\n  qlib_dir: {root / 'q'}\n{extra}",
                encoding="utf-8",
            )
            return publish_pathspecs(config)

    def test_pathspecs_are_the_three_raw_artifacts(self) -> None:
        # Unknown leftover sections in a user's YAML are ignored, so an old
        # configuration cannot change which artifacts the daily wrapper stages.
        for extra in (
            "",
            "unknown_section:\n  enabled: true\n  revisions_dir: ./rev\n",
            "etl:\n  unknown_key: 5\n",
        ):
            with self.subTest(extra=extra):
                pathspecs = self._pathspecs(extra)
                self.assertEqual(
                    [Path(path).name for path in pathspecs],
                    ["daily_quotes.parquet", "dataset_manifest.json", "dataset_quality_report.json"],
                )
                self.assertEqual(
                    [Path(path).parent.name for path in pathspecs], ["p", "q", "q"]
                )


if __name__ == "__main__":
    unittest.main()
