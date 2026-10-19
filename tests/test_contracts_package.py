from __future__ import annotations

import unittest
from importlib.resources import files

import jsonschema

from jqqlib.contracts import load_schema


class ContractsPackageTests(unittest.TestCase):
    def test_all_schemas_are_packaged_and_loadable(self) -> None:
        for name in ("cli_result", "dataset_manifest", "dataset_quality_report"):
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



def _cli_result(**fields: object) -> dict[str, object]:
    return {"schema_version": 1, "command": "audit", "outcome": "completed", "exit_code": 0, "error": None, **fields}


class CliResultSchemaTests(unittest.TestCase):
    def test_each_documented_outcome_is_accepted(self) -> None:
        schema = load_schema("cli_result")
        for document in (
            _cli_result(command="run-daily"),
            _cli_result(outcome="gaps", exit_code=1),
            _cli_result(outcome="calendar_unavailable", exit_code=2),
            _cli_result(command="run-daily", outcome="failed", exit_code=2, error="bad config"),
        ):
            with self.subTest(document=document):
                jsonschema.validate(document, schema)

    def test_an_outcome_that_contradicts_command_exit_code_or_error_is_rejected(self) -> None:
        schema = load_schema("cli_result")
        for document in (
            _cli_result(command="run-daily", outcome="gaps", exit_code=1),
            _cli_result(outcome="gaps", exit_code=0),
            _cli_result(exit_code=1),
            _cli_result(error="x"),
            _cli_result(outcome="calendar_unavailable", exit_code=1),
            _cli_result(outcome="failed", exit_code=1, error=None),
            _cli_result(outcome="failed", exit_code=0, error="x"),
        ):
            with self.subTest(document=document), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(document, schema)

if __name__ == "__main__":
    unittest.main()
