from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from jqqlib.config import load_config, publish_check_baseline, publish_check_ledger_path


class LoadConfigTests(unittest.TestCase):
    def load(self, document):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.yaml"
            path.write_text(yaml.safe_dump(document), encoding="utf-8")
            return load_config(path)

    REQUIRED = {"parquet_dir": "./parquet", "qlib_dir": "./qlib"}

    def test_default_audit_dir(self):
        config = self.load({"storage": dict(self.REQUIRED)})
        self.assertEqual(config["storage"]["audit_dir"], "./data/audit")

    def test_missing_required_storage_keys(self):
        for key in ("parquet_dir", "qlib_dir"):
            storage = {other: value for other, value in self.REQUIRED.items() if other != key}
            for document in ({"storage": storage}, {"storage": {**storage, "audit_dir": "./audit"}}):
                with self.subTest(key=key, document=document):
                    with self.assertRaisesRegex(ValueError, rf"^storage\.{key} is required\.$"):
                        self.load(document)

    def test_missing_storage_section_reports_first_required_key(self):
        for document in ({}, {"storage": {}}, {"jquants": {}}):
            with self.subTest(document=document):
                with self.assertRaisesRegex(ValueError, r"^storage\.parquet_dir is required\.$"):
                    self.load(document)

    def test_storage_paths_keep_working_directory_semantics(self):
        for value in ("./custom", "/tmp/custom"):
            storage = dict.fromkeys(("parquet_dir", "qlib_dir", "audit_dir"), value)
            with self.subTest(value=value):
                self.assertEqual(self.load({"storage": storage})["storage"], storage)

    def test_invalid_storage_section(self):
        for value in (None, [], "path", 12, False):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "storage configuration must be a mapping"):
                    self.load({"storage": value})

    def test_invalid_storage_paths(self):
        for key in ("parquet_dir", "qlib_dir", "audit_dir"):
            for value in (None, [], {}, 12, False, "", "  ", "bad\x00path"):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ValueError, rf"storage\.{key} must be a non-empty path string"):
                        self.load({"storage": {**self.REQUIRED, key: value}})

    def test_publish_check_git_ref_defaults_and_validates(self):
        config = self.load({"storage": self.REQUIRED})
        self.assertEqual(config["publish_check"]["git_ref"], "origin/main")
        config = self.load({"storage": self.REQUIRED, "publish_check": {"git_ref": "origin/master"}})
        self.assertEqual(config["publish_check"]["git_ref"], "origin/master")
        for bad in ("", "origin main", "--output=x", 3):
            with self.subTest(ref=bad), self.assertRaises(ValueError):
                self.load({"storage": self.REQUIRED, "publish_check": {"git_ref": bad}})

    def test_publish_check_baseline_defaults_to_git_and_accepts_only_git_or_file(self):
        config = self.load({"storage": self.REQUIRED})
        self.assertEqual(publish_check_baseline(config), "git")
        config = self.load({"storage": self.REQUIRED, "publish_check": {"baseline": None}})
        self.assertEqual(publish_check_baseline(config), "git")
        config = self.load(
            {"storage": self.REQUIRED, "publish_check": {"baseline": "file", "ledger_path": "./state/ledger.jsonl"}}
        )
        self.assertEqual(publish_check_baseline(config), "file")
        for bad in ("", "GIT", "ledger", 1, []):
            with self.subTest(baseline=bad), self.assertRaisesRegex(ValueError, "publish_check.baseline"):
                self.load({"storage": self.REQUIRED, "publish_check": {"baseline": bad}})

    def test_file_baseline_without_a_ledger_path_is_a_configuration_error(self):
        with self.assertRaisesRegex(ValueError, "publish_check.ledger_path is required"):
            self.load({"storage": self.REQUIRED, "publish_check": {"baseline": "file"}})

    def test_ledger_path_keeps_working_directory_semantics_and_rejects_non_paths(self):
        config = self.load({"storage": self.REQUIRED, "publish_check": {"ledger_path": "./state/ledger.jsonl"}})
        self.assertEqual(publish_check_ledger_path(config), Path("./state/ledger.jsonl"))
        for bad in ("", "  ", "bad\x00path", 3, []):
            with self.subTest(path=bad), self.assertRaisesRegex(ValueError, "publish_check.ledger_path must be"):
                self.load({"storage": self.REQUIRED, "publish_check": {"ledger_path": bad}})

    def test_ledger_path_lookup_without_a_configured_path_is_a_configuration_error(self):
        config = self.load({"storage": self.REQUIRED})
        with self.assertRaisesRegex(ValueError, "publish_check.ledger_path is required"):
            publish_check_ledger_path(config)

    def test_invalid_top_level(self):
        for value in (None, [], "path"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "configuration must be a mapping"):
                    self.load(value)


if __name__ == "__main__":
    unittest.main()
