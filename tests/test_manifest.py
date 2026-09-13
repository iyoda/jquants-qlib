from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pandas as pd

from jqqlib.config import QLIB_ADJUSTMENT_NONE
from jqqlib.contracts import load_schema
from jqqlib.manifest import (
    _producer_git_commit,
    build_dataset_manifest,
    build_dataset_quality_report,
    read_dataset_manifest,
    read_dataset_quality_report,
    write_dataset_artifacts,
    write_dataset_manifest,
)


class ProducerGitCommitTests(unittest.TestCase):
    def test_git_commit_uses_producers_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            producer_dir = Path(tmpdir)
            with patch("jqqlib.manifest.Path.cwd", return_value=producer_dir), patch(
                "jqqlib.manifest.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="abc123\n"),
            ) as run:
                self.assertEqual(_producer_git_commit(), "abc123")
            run.assert_called_once_with(
                ["git", "rev-parse", "HEAD"], cwd=producer_dir,
                check=True, capture_output=True, text=True,
            )

    def test_git_failures_do_not_prevent_manifest_creation(self) -> None:
        for error in (FileNotFoundError("git"), subprocess.CalledProcessError(128, "git")):
            with self.subTest(error=type(error).__name__), patch(
                "jqqlib.manifest.subprocess.run", side_effect=error,
            ):
                self.assertIsNone(_producer_git_commit())


class DatasetManifestTests(unittest.TestCase):
    def test_build_dataset_manifest_records_dataset_and_qlib_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"

            pd.DataFrame(
                {
                    "symbol": ["10010", "10010", "10030"],
                    "date": ["2024-01-04", "2024-01-05", "2024-01-04"],
                    "open": [1.0, 2.0, 3.0],
                    "high": [1.1, 2.1, 3.1],
                    "low": [0.9, 1.9, 2.9],
                    "close": [1.0, 2.0, 3.0],
                    "volume": [100, 200, 300],
                    "amount": [1000, 2000, 3000],
                }
            ).to_parquet(parquet_path, index=False)
            (qlib_dir / "calendars").mkdir(parents=True)
            (qlib_dir / "calendars" / "day.txt").write_text("2024-01-04\n2024-01-05\n", encoding="utf-8")
            (qlib_dir / "instruments").mkdir()
            (qlib_dir / "instruments" / "all.txt").write_text(
                "10010\t2024-01-04\t2024-01-05\n13320\t2024-01-04\t2024-01-04\n",
                encoding="utf-8",
            )

            manifest = build_dataset_manifest(
                parquet_path=parquet_path,
                qlib_dir=qlib_dir,
                source={"kind": "test_source"},
            )

            self.assertEqual(manifest["schema_version"], 2)
            self.assertTrue(manifest["dataset_id"].startswith("jqqlib-v2-"))
            self.assertIn("build_id", manifest)
            self.assertEqual(manifest["quality_status"], "pass")
            self.assertEqual(manifest["dataset"]["date_start"], "2024-01-04")
            self.assertEqual(manifest["dataset"]["date_end"], "2024-01-05")
            self.assertEqual(manifest["dataset"]["rows"], 3)
            self.assertEqual(manifest["dataset"]["instruments"], 2)
            self.assertEqual(manifest["qlib"]["calendar_days"], 2)
            self.assertEqual(manifest["qlib"]["instrument_count"], 2)
            self.assertEqual(manifest["field_schema"]["open"]["adjustment"], "split_reverse_split_rights_issue")
            self.assertEqual(manifest["field_schema"]["factor"]["adjustment"], "cumulative_vendor_factor")
            self.assertEqual(manifest["field_schema"]["factor"]["dtype"], "float32")
            self.assertEqual(manifest["adjustment_policy"]["price_adjustment"], "vendor_cumulative_factor")
            self.assertEqual(manifest["adjustment_policy"]["source"], "J-Quants AdjFactor")
            self.assertIn("factor", manifest["dataset"]["fields"])
            self.assertEqual(len(manifest["lineage"]["parquet_sha256"]), 64)
            self.assertEqual(len(manifest["lineage"]["manifest_sha256"]), 64)
            self.assertEqual(manifest["lineage"]["manifest_sha256_scope"], "payload_without_manifest_sha256")
            self.assertEqual(manifest["source"]["kind"], "test_source")
            jsonschema.validate(manifest, load_schema("dataset_manifest"))

            report = build_dataset_quality_report(
                parquet_path=parquet_path, qlib_dir=qlib_dir, manifest=manifest
            )
            self.assertEqual(report["dataset_id"], manifest["dataset_id"])
            jsonschema.validate(report, load_schema("dataset_quality_report"))

    def test_none_adjustment_omits_factor_from_field_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"
            pd.DataFrame(
                {
                    "symbol": ["10010"],
                    "date": ["2024-01-04"],
                    "open": [1.0],
                    "high": [1.1],
                    "low": [0.9],
                    "close": [1.0],
                    "volume": [100],
                    "amount": [1000],
                    "adjustment_factor": [1.0],
                }
            ).to_parquet(parquet_path, index=False)
            (qlib_dir / "calendars").mkdir(parents=True)
            (qlib_dir / "calendars" / "day.txt").write_text("2024-01-04\n", encoding="utf-8")
            (qlib_dir / "instruments").mkdir()
            (qlib_dir / "instruments" / "all.txt").write_text("10010\t2024-01-04\t2024-01-04\n", encoding="utf-8")
            manifest = build_dataset_manifest(
                parquet_path=parquet_path,
                qlib_dir=qlib_dir,
                source={"kind": "test_source"},
                adjustment=QLIB_ADJUSTMENT_NONE,
            )
            self.assertEqual(manifest["field_schema"]["open"]["adjustment"], "none")
            self.assertNotIn("factor", manifest["field_schema"])
            self.assertEqual(manifest["adjustment_policy"]["price_adjustment"], "none")
            self.assertEqual(
                manifest["adjustment_policy"]["known_limitations"],
                ["qlib.adjustment is none: OHLC/volume are raw; no $factor series is emitted."],
            )
            self.assertNotIn("source", manifest["adjustment_policy"])
            self.assertEqual(
                manifest["dataset"]["fields"],
                ["open", "high", "low", "close", "volume", "amount"],
            )
            jsonschema.validate(manifest, load_schema("dataset_manifest"))

    def test_write_and_read_dataset_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"

            pd.DataFrame(
                {
                    "symbol": ["10010"],
                    "date": ["2024-01-04"],
                    "open": [1.0],
                    "high": [1.1],
                    "low": [0.9],
                    "close": [1.0],
                    "volume": [100],
                    "amount": [1000],
                }
            ).to_parquet(parquet_path, index=False)

            manifest_path = write_dataset_manifest(
                parquet_path=parquet_path,
                qlib_dir=qlib_dir,
                source={"kind": "test_source"},
            )
            loaded = read_dataset_manifest(qlib_dir)

            self.assertEqual(manifest_path, qlib_dir / "dataset_manifest.json")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["provider_uri"], str(qlib_dir))

    def test_build_dataset_quality_report_records_validation_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"

            pd.DataFrame(
                {
                    "symbol": ["10010", "10010", "10030"],
                    "date": ["2024-01-04", "2024-01-04", "2024-01-05"],
                    "open": [1.0, 2.0, 3.0],
                    "high": [1.1, 2.1, 3.1],
                    "low": [0.9, 1.9, 2.9],
                    "close": [1.0, 2.0, 3.0],
                    "volume": [100, 200, 300],
                    "amount": [1000, 2000, 3000],
                }
            ).to_parquet(parquet_path, index=False)

            report = build_dataset_quality_report(parquet_path=parquet_path, qlib_dir=qlib_dir)

            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["status"], "fail")
            self.assertIn("Duplicate (symbol, date) rows: 1", report["validation_errors"])
            self.assertEqual(report["highest_severity"], "fail")
            self.assertGreaterEqual(report["severity_counts"]["fail"], 1)
            self.assertEqual(report["checks"]["duplicate_symbol_date_rows"], 1)

    def test_quality_report_classifies_all_ohlcv_null_rows_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"

            pd.DataFrame(
                {
                    "symbol": ["10010", "10010"],
                    "date": ["2024-01-04", "2024-01-05"],
                    "open": [1.0, None],
                    "high": [1.1, None],
                    "low": [0.9, None],
                    "close": [1.0, None],
                    "volume": [100, None],
                    "amount": [1000, None],
                }
            ).to_parquet(parquet_path, index=False)
            (qlib_dir / "calendars").mkdir(parents=True)
            (qlib_dir / "calendars" / "day.txt").write_text("2024-01-04\n2024-01-05\n", encoding="utf-8")
            (qlib_dir / "instruments").mkdir()
            (qlib_dir / "instruments" / "all.txt").write_text("10010\t2024-01-04\t2024-01-05\n", encoding="utf-8")

            report = build_dataset_quality_report(parquet_path=parquet_path, qlib_dir=qlib_dir)

            self.assertEqual(report["status"], "warning")
            self.assertEqual(report["validation_errors"], [])
            self.assertEqual(report["checks"]["ohlcv_null_classification"]["all_ohlcv_null_rows"], 1)
            classification = report["checks"]["ohlcv_null_classification"]
            self.assertEqual(classification["affected_symbol_count"], 1)
            self.assertEqual(classification["row_ratio"], 0.5)
            self.assertEqual(classification["date_concentration"], [{"date": "2024-01-05", "rows": 1}])
            warning = next(finding for finding in report["findings"] if finding["check"] == "ohlcv_all_null_rows")
            self.assertEqual(warning["affected_symbol_count"], 1)
            self.assertEqual(warning["row_ratio"], 0.5)
            self.assertEqual(warning["date_concentration"], [{"date": "2024-01-05", "rows": 1}])

    def test_quality_report_classifies_partial_ohlcv_null_rows_as_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"

            pd.DataFrame(
                {
                    "symbol": ["10010"],
                    "date": ["2024-01-04"],
                    "open": [None],
                    "high": [1.1],
                    "low": [0.9],
                    "close": [1.0],
                    "volume": [100],
                    "amount": [1000],
                }
            ).to_parquet(parquet_path, index=False)
            (qlib_dir / "calendars").mkdir(parents=True)
            (qlib_dir / "calendars" / "day.txt").write_text("2024-01-04\n", encoding="utf-8")
            (qlib_dir / "instruments").mkdir()
            (qlib_dir / "instruments" / "all.txt").write_text("10010\t2024-01-04\t2024-01-04\n", encoding="utf-8")

            report = build_dataset_quality_report(parquet_path=parquet_path, qlib_dir=qlib_dir)

            self.assertEqual(report["status"], "fail")
            self.assertIn("Rows with partial OHLCV null fields: 1", report["validation_errors"])
            self.assertEqual(report["checks"]["ohlcv_null_classification"]["partial_ohlcv_null_rows"], 1)

    def test_quality_report_dumped_factor_fails_on_non_positive_factor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"

            pd.DataFrame(
                {
                    "symbol": ["10010", "10010"],
                    "date": ["2024-01-04", "2024-01-05"],
                    "open": [1.0, 1.0],
                    "high": [1.1, 1.1],
                    "low": [0.9, 0.9],
                    "close": [1.0, 1.0],
                    "volume": [100, 100],
                    "amount": [1000, 1000],
                    "adjustment_factor": [1.0, 0.0],
                }
            ).to_parquet(parquet_path, index=False)
            (qlib_dir / "calendars").mkdir(parents=True)
            (qlib_dir / "calendars" / "day.txt").write_text("2024-01-04\n2024-01-05\n", encoding="utf-8")
            (qlib_dir / "instruments").mkdir()
            (qlib_dir / "instruments" / "all.txt").write_text("10010\t2024-01-04\t2024-01-05\n", encoding="utf-8")

            report = build_dataset_quality_report(parquet_path=parquet_path, qlib_dir=qlib_dir)

            self.assertEqual(report["status"], "fail")
            dumped_factor = next(finding for finding in report["findings"] if finding["check"] == "dumped_factor")
            self.assertEqual(dumped_factor["severity"], "fail")
            self.assertGreater(report["checks"]["dumped_factor_invalid_rows"], 0)

    def test_write_dataset_artifacts_writes_manifest_and_quality_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"

            pd.DataFrame(
                {
                    "symbol": ["10010"],
                    "date": ["2024-01-04"],
                    "open": [1.0],
                    "high": [1.1],
                    "low": [0.9],
                    "close": [1.0],
                    "volume": [100],
                    "amount": [1000],
                }
            ).to_parquet(parquet_path, index=False)

            manifest_path, quality_path = write_dataset_artifacts(
                parquet_path=parquet_path,
                qlib_dir=qlib_dir,
                source={"kind": "test_source"},
            )

            self.assertEqual(manifest_path, qlib_dir / "dataset_manifest.json")
            self.assertEqual(quality_path, qlib_dir / "dataset_quality_report.json")
            self.assertIsNotNone(read_dataset_manifest(qlib_dir))
            self.assertIsNotNone(read_dataset_quality_report(qlib_dir))


if __name__ == "__main__":
    unittest.main()
