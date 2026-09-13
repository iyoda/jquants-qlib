from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from jqqlib.manifest import _sha256_file
from jqqlib.store import DEFAULT_PARQUET_FILENAME, write_parquet


def _rows():
    return [
        {
            "Code": "10010", "Date": "2026-06-04", "Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5,
            "Volume": 100, "TurnoverValue": 150.0,
        },
        {
            "Code": "10010", "Date": "2026-06-05", "Open": 1.5, "High": 2.5, "Low": 1.0, "Close": 2.0,
            "Volume": 110, "TurnoverValue": 220.0,
        },
        {
            "Code": "10020", "Date": "2026-06-04", "Open": 3.0, "High": 4.0, "Low": 2.5, "Close": 3.5,
            "Volume": 200, "TurnoverValue": 700.0,
        },
        {
            "Code": "10020", "Date": "2026-06-05", "Open": 3.5, "High": 4.5, "Low": 3.0, "Close": 4.0,
            "Volume": 210, "TurnoverValue": 840.0,
        },
    ]


class WriteParquetDeterminismTests(unittest.TestCase):
    def _write_and_hash(self, frame: pd.DataFrame) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            out = write_parquet(frame, Path(tmp), merge_mode="replace")
            return _sha256_file(out)

    def test_identical_content_different_input_order_yields_identical_bytes(self) -> None:
        rows = _rows()
        hash_a = self._write_and_hash(pd.DataFrame(rows))
        hash_b = self._write_and_hash(pd.DataFrame(list(reversed(rows))))
        shuffled = [rows[2], rows[0], rows[3], rows[1]]
        hash_c = self._write_and_hash(pd.DataFrame(shuffled))
        self.assertEqual(hash_a, hash_b)
        self.assertEqual(hash_a, hash_c)

    def test_upsert_no_op_matches_full_replace_bytes(self) -> None:
        # An incremental upsert of already-present rows must converge to the same
        # bytes as a single full replace of the same logical content.
        rows = _rows()
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            write_parquet(pd.DataFrame(rows[:2]), d, merge_mode="replace")
            # re-fetch the same first two rows (different order) + add the rest
            write_parquet(pd.DataFrame([rows[1], rows[0]]), d, merge_mode="upsert")
            out = write_parquet(pd.DataFrame([rows[3], rows[2]]), d, merge_mode="upsert")
            incremental = _sha256_file(out)

        full = self._write_and_hash(pd.DataFrame(rows))
        self.assertEqual(incremental, full)
        self.assertEqual(out.name, DEFAULT_PARQUET_FILENAME)


if __name__ == "__main__":
    unittest.main()
