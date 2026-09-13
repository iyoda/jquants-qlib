from __future__ import annotations

import unittest

import pandas as pd

from jqqlib.adjust import apply_vendor_adjustment
from jqqlib.config import QLIB_ADJUSTMENT_NONE


def _series() -> pd.DataFrame:
    # Three events: 1:2 split, 1:10 reverse split, rights issue (ExRT 3).
    return pd.DataFrame(
        [
            {"symbol": "10010", "date": "2026-06-01", "open": 100.0, "high": 110.0, "low": 90.0,
             "close": 100.0, "volume": 1000.0, "amount": 100000.0, "adjustment_factor": 1.0,
             "ex_rights_type": "0"},
            {"symbol": "10010", "date": "2026-06-02", "open": 50.0, "high": 55.0, "low": 45.0,
             "close": 50.0, "volume": 2000.0, "amount": 100000.0, "adjustment_factor": 0.5,
             "ex_rights_type": "1"},
            {"symbol": "10010", "date": "2026-06-03", "open": 500.0, "high": 550.0, "low": 450.0,
             "close": 500.0, "volume": 200.0, "amount": 100000.0, "adjustment_factor": 10.0,
             "ex_rights_type": "2"},
            {"symbol": "10010", "date": "2026-06-04", "open": 400.0, "high": 440.0, "low": 360.0,
             "close": 400.0, "volume": 250.0, "amount": 100000.0, "adjustment_factor": 0.8,
             "ex_rights_type": "3"},
            {"symbol": "10010", "date": "2026-06-05", "open": 400.0, "high": 440.0, "low": 360.0,
             "close": 400.0, "volume": 250.0, "amount": 100000.0, "adjustment_factor": 1.0,
             "ex_rights_type": "0"},
        ]
    )


class VendorAdjustmentTests(unittest.TestCase):
    def test_three_event_series_matches_vendor_cumadj_rules(self) -> None:
        raw = _series()
        dumped = apply_vendor_adjustment(raw)
        dumped = dumped.sort_values("date").reset_index(drop=True)
        # Later-only product: D5=1, D4=1, D3=0.8, D2=8, D1=4.
        self.assertEqual(list(dumped["factor"]), [4.0, 8.0, 0.8, 1.0, 1.0])
        self.assertEqual(float(dumped.iloc[-1]["factor"]), 1.0)
        for field in ("open", "high", "low", "close"):
            restored = dumped[field] / dumped["factor"]
            pd.testing.assert_series_equal(restored.reset_index(drop=True), raw[field], check_names=False)
        # Rights issue on D4 contributes 1 to volume_cum: D5=1, D4=1, D3=1, D2=10, D1=5.
        self.assertEqual(list(dumped["volume"]), [200.0, 200.0, 200.0, 250.0, 250.0])
        self.assertEqual(list(dumped["amount"]), [100000.0] * 5)

    def test_missing_adjustment_factor_raises(self) -> None:
        raw = _series().drop(columns=["adjustment_factor", "ex_rights_type"])
        with self.assertRaisesRegex(ValueError, "adjustment_factor is required"):
            apply_vendor_adjustment(raw)

    def test_null_adjustment_factor_raises(self) -> None:
        raw = _series()
        raw.loc[raw.index[0], "adjustment_factor"] = float("nan")
        with self.assertRaisesRegex(ValueError, "Null adjustment_factor"):
            apply_vendor_adjustment(raw)

    def test_none_mode_ignores_missing_adjustment_factor(self) -> None:
        raw = _series().drop(columns=["adjustment_factor", "ex_rights_type"])
        dumped = apply_vendor_adjustment(raw, adjustment=QLIB_ADJUSTMENT_NONE)
        self.assertNotIn("factor", dumped.columns)
        pd.testing.assert_frame_equal(dumped.reset_index(drop=True), raw.reset_index(drop=True))


if __name__ == "__main__":
    unittest.main()
