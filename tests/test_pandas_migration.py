from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from jqqlib.adjust import apply_vendor_adjustment


def run_probe(root: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/pandas_migration_probe.py"
    subprocess.run([sys.executable, str(script), "--output-root", str(root)],
                   check=True, capture_output=True, text=True)


class PandasMigrationTests(unittest.TestCase):
    def test_upsert_failures_determinism_and_pit_across_datetime_units(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "probe"
            run_probe(root)
            report = json.loads((root / "content.json").read_text())
            columns = report["parquet"]["columns"]
            rows = report["parquet"]["data"]
            self.assertEqual(len(rows), 7)
            self.assertEqual({row[columns.index("symbol")] for row in rows}, {"0010", "123A"})
            self.assertEqual(rows[0][columns.index("close")], 107.0)
            self.assertTrue(all(row[columns.index("amount")] is None for row in rows))
            self.assertEqual(len(report["qlib"]), 16)

    def test_bins_equal_direct_float32_of_adjusted_float64(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "probe"
            run_probe(root)
            adjusted = apply_vendor_adjustment(pd.read_parquet(root / "parquet/daily_quotes.parquet"))
            calendar = pd.to_datetime(pd.read_csv(root / "qlib/calendars/day.txt", header=None)[0])
            bins = sorted((root / "qlib/features").glob("*/*.day.bin"))
            self.assertEqual(len(bins), 14)
            for path in bins:
                frame = adjusted[adjusted["symbol"].str.lower() == path.parent.name]
                series = frame.set_index(pd.to_datetime(frame["date"]))[path.name.split(".")[0]]
                raw = np.fromfile(path, dtype="<f")
                dates = calendar.iloc[int(raw[0]):int(raw[0]) + len(raw) - 1]
                expected = series.reindex(dates.values).to_numpy(dtype="float64").astype("<f")
                with self.subTest(path=f"{path.parent.name}/{path.name}"):
                    np.testing.assert_array_equal(raw[1:], expected)
