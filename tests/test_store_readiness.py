from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
import yaml

from jqqlib.manifest import _sha256_file
from jqqlib.pipeline import read_existing_dataset_end, run_daily
from jqqlib.store import DEFAULT_PARQUET_FILENAME, write_parquet


def _row(code: str, date: str, adj_factor: float = 1.0) -> dict:
    return {
        "Code": code, "Date": date, "O": 1.0, "H": 2.0, "L": 0.5, "C": 1.5,
        "Vo": 100, "Va": 150.0, "AdjFactor": adj_factor,
    }


class WriteParquetReadinessTests(unittest.TestCase):
    def test_upsert_with_invalid_rows_leaves_existing_parquet_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parquet_dir = Path(tmp)
            out = write_parquet(pd.DataFrame([_row("10010", "2026-06-04")]), parquet_dir, merge_mode="upsert")
            before = _sha256_file(out)

            with self.assertRaisesRegex(RuntimeError, "Non-positive adjustment_factor rows: 1"):
                write_parquet(
                    pd.DataFrame([_row("10010", "2026-06-05", adj_factor=0.0)]), parquet_dir, merge_mode="upsert"
                )

            self.assertEqual(_sha256_file(out), before)
            self.assertEqual(sorted(p.name for p in parquet_dir.iterdir()), [DEFAULT_PARQUET_FILENAME])

    def test_replace_with_duplicate_rows_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parquet_dir = Path(tmp)
            rows = [_row("10010", "2026-06-04"), _row("10010", "2026-06-04")]

            with self.assertRaisesRegex(RuntimeError, r"Duplicate \(symbol, date\) rows: 1"):
                write_parquet(pd.DataFrame(rows), parquet_dir, merge_mode="replace")

            self.assertEqual(list(parquet_dir.iterdir()), [])

    def test_upsert_can_overwrite_previously_invalid_rows(self) -> None:
        # A re-fetch of the bad range replaces the offending rows, so the
        # merged frame validates and the write goes through.
        with tempfile.TemporaryDirectory() as tmp:
            parquet_dir = Path(tmp)
            pd.DataFrame({
                "symbol": ["10010"], "date": ["2026-06-04"], "open": [1.0], "high": [2.0], "low": [0.5],
                "close": [1.5], "volume": [100], "amount": [150.0], "adjustment_factor": [0.0],
            }).to_parquet(parquet_dir / DEFAULT_PARQUET_FILENAME, index=False)

            out = write_parquet(pd.DataFrame([_row("10010", "2026-06-04")]), parquet_dir, merge_mode="upsert")

            self.assertEqual(pd.read_parquet(out)["adjustment_factor"].tolist(), [1.0])


class RunDailyInvalidFetchTests(unittest.TestCase):
    def test_rejected_fetch_is_refetched_by_the_next_run(self) -> None:
        # A fetch that fails the readiness checks must not advance the Parquet:
        # the next run-daily resumes from the same start date instead of
        # skipping the rejected session forever.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yaml"
            config_path.write_text(yaml.safe_dump({
                "storage": {"parquet_dir": str(root / "parquet"), "qlib_dir": str(root / "qlib")},
                "qlib": {"adjustment": "vendor_factor"},
            }), encoding="utf-8")
            write_parquet(pd.DataFrame([_row("10010", "2026-06-03")]), root / "parquet", merge_mode="upsert")

            fetched = [
                pd.DataFrame([_row("10010", "2026-06-04", adj_factor=0.0)]),
                pd.DataFrame([_row("10010", "2026-06-04"), _row("10010", "2026-06-05")]),
            ]
            with (
                mock.patch("jqqlib.pipeline.create_jquants_client"),
                mock.patch("jqqlib.pipeline.fetch_daily_quotes", side_effect=fetched) as fetch,
            ):
                with self.assertRaisesRegex(RuntimeError, "not Qlib-ready"):
                    run_daily(config_path, dt.date(2026, 6, 4))
                self.assertEqual(read_existing_dataset_end(config_path), dt.date(2026, 6, 3))

                self.assertEqual(run_daily(config_path, dt.date(2026, 6, 5)), dt.date(2026, 6, 5))

            ranges = [(call.args[1], call.args[2]) for call in fetch.call_args_list]
            self.assertEqual(
                ranges,
                [(dt.date(2026, 6, 4), dt.date(2026, 6, 4)), (dt.date(2026, 6, 4), dt.date(2026, 6, 5))],
            )
            self.assertEqual(read_existing_dataset_end(config_path), dt.date(2026, 6, 5))
            self.assertTrue((root / "qlib" / "calendars" / "day.txt").exists())


if __name__ == "__main__":
    unittest.main()
