from __future__ import annotations

import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

import yaml

from jqqlib.config import load_config
from jqqlib.pipeline import packaged_config_example

REPO_ROOT = Path(__file__).resolve().parents[1]


class ConfigExampleTests(unittest.TestCase):
    def test_root_example_is_byte_identical_to_packaged_copy(self) -> None:
        root_copy = REPO_ROOT / "config.example.yaml"
        self.assertTrue(root_copy.is_file(), root_copy)
        packaged = files("jqqlib.data").joinpath("config.example.yaml")
        self.assertTrue(packaged.is_file())
        self.assertEqual(root_copy.read_bytes(), packaged.read_bytes())
        self.assertEqual(packaged_config_example(), packaged.read_bytes())

    def test_packaged_example_parses_and_loads(self) -> None:
        document = yaml.safe_load(packaged_config_example().decode("utf-8"))
        self.assertIsInstance(document, dict)
        self.assertIn("storage", document)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_bytes(packaged_config_example())
            config = load_config(path)
        self.assertEqual(config["storage"]["parquet_dir"], "./data/parquet")
        self.assertEqual(config["storage"]["qlib_dir"], "./data/qlib_jp")
        self.assertEqual(config["storage"]["audit_dir"], "./data/audit")


if __name__ == "__main__":
    unittest.main()
