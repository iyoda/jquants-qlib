from __future__ import annotations

import unittest
from importlib.resources import files

from jqqlib.contracts import load_schema


class ContractsPackageTests(unittest.TestCase):
    def test_both_schemas_are_packaged_and_loadable(self) -> None:
        for name in ("dataset_manifest", "dataset_quality_report"):
            with self.subTest(name=name):
                resource = files("jqqlib.contracts").joinpath(f"{name}.schema.json")
                self.assertTrue(resource.is_file())
                schema = load_schema(name)
                self.assertIsInstance(schema, dict)
                self.assertIn("$schema", schema)
                self.assertIn("properties", schema)

    def test_unknown_schema_names_are_rejected(self) -> None:
        for name in ("unknown", "../dataset_manifest", "dataset_manifest.schema.json", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                load_schema(name)


if __name__ == "__main__":
    unittest.main()
