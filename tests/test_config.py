from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from jqqlib.config import load_config


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

    def test_invalid_top_level(self):
        for value in (None, [], "path"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "configuration must be a mapping"):
                    self.load(value)


if __name__ == "__main__":
    unittest.main()
