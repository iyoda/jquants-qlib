from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jqqlib.normalize import BOOTSTRAP_CSV_COLUMNS
from tests.support.synthetic import write_synthetic_daily_quotes_csv


class SyntheticQuotesTests(unittest.TestCase):
    def test_deterministic_bootstrap_columns_and_weekdays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = {"symbols": ["10020", "10010"], "start": "2026-06-05", "end": "2026-06-08"}
            first = write_synthetic_daily_quotes_csv(root / "first.csv", **args)
            second = write_synthetic_daily_quotes_csv(root / "nested" / "second.csv", **args)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with first.open(newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, BOOTSTRAP_CSV_COLUMNS)
                rows = list(reader)
            self.assertEqual(len(rows), 4)
            self.assertEqual({row["Date"] for row in rows}, {"2026-06-05", "2026-06-08"})
            self.assertEqual({row["Code"] for row in rows}, {"10010", "10020"})
            for row in rows:
                self.assertLessEqual(float(row["Low"]), min(float(row["Open"]), float(row["Close"])))
                self.assertGreaterEqual(float(row["High"]), max(float(row["Open"]), float(row["Close"])))
                self.assertGreater(float(row["Volume"]), 0)

    def test_invalid_inputs_do_not_create_a_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quotes.csv"
            for args in (
                {"symbols": [], "start": "2026-06-01", "end": "2026-06-05"},
                {"symbols": ["bad"], "start": "2026-06-01", "end": "2026-06-05"},
                {"symbols": ["10010"], "start": "2026-06-05", "end": "2026-06-01"},
                {"symbols": ["10010"], "start": "2026-06-06", "end": "2026-06-07"},
            ):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    write_synthetic_daily_quotes_csv(path, **args)
                self.assertFalse(path.exists())

    def test_module_cli_matches_function_output(self):
        # README runs `python -m tests.support.synthetic` from the repository root.
        repo_root = Path(__file__).resolve().parent.parent
        args = {"symbols": ["10010", "10020"], "start": "2026-06-01", "end": "2026-06-05"}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "sample"
            result = subprocess.run(
                [sys.executable, "-m", "tests.support.synthetic", "--out", str(out)],
                cwd=repo_root, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            cli_csv = out / "daily_quotes.csv"
            self.assertTrue(cli_csv.is_file())
            self.assertEqual(result.stdout.strip(), str(cli_csv))
            expected = write_synthetic_daily_quotes_csv(Path(tmp) / "expected.csv", **args)
            self.assertEqual(cli_csv.read_bytes(), expected.read_bytes())

    def test_split_events_write_adjustment_factor_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_synthetic_daily_quotes_csv(
                root / "quotes.csv",
                symbols=["10010"],
                start="2026-06-01",
                end="2026-06-05",
                events={"10010": {"2026-06-03": 0.5}},
            )
            with path.open(newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, [*BOOTSTRAP_CSV_COLUMNS, "AdjustmentFactor"])
                rows = list(reader)
            self.assertEqual({row["Date"]: row["AdjustmentFactor"] for row in rows}["2026-06-03"], "0.5")
            self.assertTrue(all(
                row["AdjustmentFactor"] in {"0.5", "1.0"} for row in rows
            ))

    def test_module_cli_split_flag(self):
        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "sample"
            result = subprocess.run(
                [
                    sys.executable, "-m", "tests.support.synthetic",
                    "--out", str(out), "--symbols", "10010", "--split", "10010:2026-06-03:0.5",
                ],
                cwd=repo_root, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            with (out / "daily_quotes.csv").open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertIn("AdjustmentFactor", rows[0])
            self.assertEqual(
                next(row["AdjustmentFactor"] for row in rows if row["Date"] == "2026-06-03"),
                "0.5",
            )

    def test_module_cli_rejects_invalid_split(self):
        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-m", "tests.support.synthetic", "--out", tmp, "--split", "bad"],
                cwd=repo_root, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("CODE:DATE:FACTOR", result.stderr)

    def test_module_cli_rejects_invalid_arguments(self):
        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-m", "tests.support.synthetic", "--out", tmp, "--symbols", "bad"],
                cwd=repo_root, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("four- or five-digit codes", result.stderr)
            self.assertFalse((Path(tmp) / "daily_quotes.csv").exists())
