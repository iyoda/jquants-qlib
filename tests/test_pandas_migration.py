from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PandasMigrationTests(unittest.TestCase):
    def test_upsert_failures_determinism_and_pit_across_datetime_units(self):
        script = Path(__file__).resolve().parents[1] / "scripts/pandas_migration_probe.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "probe"
            subprocess.run([sys.executable, str(script), "--output-root", str(root)],
                           check=True, capture_output=True, text=True)
            report = json.loads((root / "content.json").read_text())
            columns = report["parquet"]["columns"]
            rows = report["parquet"]["data"]
            self.assertEqual(len(rows), 7)
            self.assertEqual({row[columns.index("symbol")] for row in rows}, {"0010", "123A"})
            self.assertEqual(rows[0][columns.index("close")], 107.0)
            self.assertTrue(all(row[columns.index("amount")] is None for row in rows))
            self.assertEqual(len(report["qlib"]), 16)
