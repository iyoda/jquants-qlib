from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jsonschema
import pandas as pd
from git_isolation import scrub_inherited_git_environment

from jqqlib.config import DEFAULT_QLIB_ADJUSTMENT, QLIB_ADJUSTMENT_NONE
from jqqlib.contracts import load_schema
from jqqlib.manifest import (
    _build_quality_inspection,
    _producer_git_commit,
    read_dataset_manifest,
    read_dataset_quality_report,
    write_dataset_artifacts,
)

scrub_inherited_git_environment()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _artifacts(
    parquet_path: Path,
    qlib_dir: Path,
    *,
    source: dict[str, Any] | None = None,
    adjustment: str = DEFAULT_QLIB_ADJUSTMENT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path, quality_path = write_dataset_artifacts(
        parquet_path=parquet_path, qlib_dir=qlib_dir, source=source or {"kind": "test"}, adjustment=adjustment
    )
    return _read_json(manifest_path), _read_json(quality_path)


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
    def test_dataset_manifest_records_dataset_and_qlib_coverage(self) -> None:
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
                    "adjustment_factor": [1.0, 1.0, 1.0],
                }
            ).to_parquet(parquet_path, index=False)
            (qlib_dir / "calendars").mkdir(parents=True)
            (qlib_dir / "calendars" / "day.txt").write_text("2024-01-04\n2024-01-05\n", encoding="utf-8")
            (qlib_dir / "instruments").mkdir()
            (qlib_dir / "instruments" / "all.txt").write_text(
                "10010\t2024-01-04\t2024-01-05\n13320\t2024-01-04\t2024-01-04\n",
                encoding="utf-8",
            )

            manifest, report = _artifacts(parquet_path, qlib_dir, source={"kind": "test_source"})

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
            manifest, _ = _artifacts(
                parquet_path, qlib_dir, source={"kind": "test_source"}, adjustment=QLIB_ADJUSTMENT_NONE
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

            manifest_path, _ = write_dataset_artifacts(
                parquet_path=parquet_path,
                qlib_dir=qlib_dir,
                source={"kind": "test_source"},
            )
            loaded = read_dataset_manifest(qlib_dir)

            self.assertEqual(manifest_path, qlib_dir / "dataset_manifest.json")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["provider_uri"], str(qlib_dir))
            self.assertIsNone(read_dataset_manifest(root / "missing"))
            self.assertIsNone(read_dataset_quality_report(root / "missing"))

    def test_quality_report_records_validation_summary(self) -> None:
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

            _, report = _artifacts(parquet_path, qlib_dir)

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
                    "adjustment_factor": [1.0, 1.0],
                }
            ).to_parquet(parquet_path, index=False)
            (qlib_dir / "calendars").mkdir(parents=True)
            (qlib_dir / "calendars" / "day.txt").write_text("2024-01-04\n2024-01-05\n", encoding="utf-8")
            (qlib_dir / "instruments").mkdir()
            (qlib_dir / "instruments" / "all.txt").write_text("10010\t2024-01-04\t2024-01-05\n", encoding="utf-8")

            _, report = _artifacts(parquet_path, qlib_dir)

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

            _, report = _artifacts(parquet_path, qlib_dir)

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

            _, report = _artifacts(parquet_path, qlib_dir)

            self.assertEqual(report["status"], "fail")
            dumped_factor = next(finding for finding in report["findings"] if finding["check"] == "dumped_factor")
            self.assertEqual(dumped_factor["severity"], "fail")
            self.assertGreater(report["checks"]["dumped_factor_invalid_rows"], 0)

    def _seed_vendor_dataset(self, root: Path, *, adjustment_factor: list | None) -> tuple[Path, Path]:
        parquet_path = root / "daily_quotes.parquet"
        qlib_dir = root / "qlib_jp"
        frame = pd.DataFrame(
            {
                "symbol": ["10010", "10010"],
                "date": ["2024-01-04", "2024-01-05"],
                "open": [1.0, 1.0],
                "high": [1.1, 1.1],
                "low": [0.9, 0.9],
                "close": [1.0, 1.0],
                "volume": [100, 100],
                "amount": [1000, 1000],
            }
        )
        if adjustment_factor is not None:
            frame["adjustment_factor"] = adjustment_factor
        frame.to_parquet(parquet_path, index=False)
        (qlib_dir / "calendars").mkdir(parents=True)
        (qlib_dir / "calendars" / "day.txt").write_text("2024-01-04\n2024-01-05\n", encoding="utf-8")
        (qlib_dir / "instruments").mkdir()
        (qlib_dir / "instruments" / "all.txt").write_text("10010\t2024-01-04\t2024-01-05\n", encoding="utf-8")
        return parquet_path, qlib_dir

    def test_quality_report_fails_when_the_vendor_factor_cannot_be_computed(self) -> None:
        # vendor_factor promises $factor: a Parquet without a usable factor used
        # to skip the check and report "pass".
        for label, factor in (("missing column", None), ("null factor", [1.0, None])):
            with self.subTest(label), tempfile.TemporaryDirectory() as tmpdir:
                parquet_path, qlib_dir = self._seed_vendor_dataset(Path(tmpdir), adjustment_factor=factor)

                _, report = _artifacts(parquet_path, qlib_dir)

                self.assertEqual(report["status"], "fail")
                finding = next(item for item in report["findings"] if item["check"] == "dumped_factor")
                self.assertTrue(finding["detail"].startswith("Dumped factor cannot be computed: "), finding)
                jsonschema.validate(report, load_schema("dataset_quality_report"))

                _, none_report = _artifacts(parquet_path, qlib_dir, adjustment=QLIB_ADJUSTMENT_NONE)
                self.assertEqual(none_report["status"], "pass")

    def test_quality_report_accepts_a_nullable_integer_factor_read_back_from_parquet(self) -> None:
        # Parquet round trip of an Int64 adjustment_factor: the vectorised factor
        # transform must not crash the report (it only catches ValueError).
        with tempfile.TemporaryDirectory() as tmpdir:
            parquet_path, qlib_dir = self._seed_vendor_dataset(
                Path(tmpdir), adjustment_factor=pd.array([1, 1], dtype="Int64")
            )
            self.assertEqual(str(pd.read_parquet(parquet_path)["adjustment_factor"].dtype), "Int64")

            _, report = _artifacts(parquet_path, qlib_dir)

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["checks"]["dumped_factor_invalid_rows"], 0)

    def test_dataset_id_depends_on_the_adjustment_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parquet_path, qlib_dir = self._seed_vendor_dataset(Path(tmpdir), adjustment_factor=[1.0, 1.0])
            ids = {
                adjustment: _artifacts(
                    parquet_path, qlib_dir, source={"kind": "test"}, adjustment=adjustment
                )[0]["dataset_id"]
                for adjustment in ("vendor_factor", QLIB_ADJUSTMENT_NONE)
            }

        self.assertNotEqual(ids["vendor_factor"], ids[QLIB_ADJUSTMENT_NONE])

    def test_manifest_lineage_binds_the_final_quality_report_bytes(self) -> None:
        import hashlib
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            parquet_path, qlib_dir = self._seed_vendor_dataset(Path(tmpdir), adjustment_factor=[1.0, 1.0])
            manifest_path, quality_path = write_dataset_artifacts(
                parquet_path=parquet_path, qlib_dir=qlib_dir, source={"kind": "test"}
            )
            report_sha256 = hashlib.sha256(quality_path.read_bytes()).hexdigest()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        lineage = manifest["lineage"]
        self.assertEqual(lineage["quality_report_sha256"], report_sha256)
        recorded_manifest_sha256 = lineage.pop("manifest_sha256")
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        self.assertEqual(recorded_manifest_sha256, hashlib.sha256(canonical.encode("utf-8")).hexdigest())

    def test_write_dataset_artifacts_inspects_the_dataset_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parquet_path, qlib_dir = self._seed_vendor_dataset(Path(tmpdir), adjustment_factor=[1.0, 1.0])
            with patch(
                "jqqlib.manifest._build_quality_inspection", wraps=_build_quality_inspection
            ) as inspection:
                manifest_path, quality_path = write_dataset_artifacts(
                    parquet_path=parquet_path, qlib_dir=qlib_dir, source={"kind": "test"}
                )

            self.assertEqual(inspection.call_count, 1)
            manifest = _read_json(manifest_path)
            report = _read_json(quality_path)
        self.assertEqual(report["dataset_id"], manifest["dataset_id"])
        self.assertEqual(report["status"], manifest["quality_status"])
        jsonschema.validate(manifest, load_schema("dataset_manifest"))
        jsonschema.validate(report, load_schema("dataset_quality_report"))

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
