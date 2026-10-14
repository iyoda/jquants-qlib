from __future__ import annotations

import unittest

import numpy as np
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

    def test_nullable_integer_factor_is_adjusted_like_float(self) -> None:
        raw = _series()
        as_int = raw.assign(adjustment_factor=pd.array([1, 2, 1, 1, 1], dtype="Int64"))
        as_float = raw.assign(adjustment_factor=[1.0, 2.0, 1.0, 1.0, 1.0])

        dumped = apply_vendor_adjustment(as_int)

        self.assertEqual(list(dumped["factor"]), [2.0, 1.0, 1.0, 1.0, 1.0])
        pd.testing.assert_frame_equal(
            dumped.drop(columns=["adjustment_factor"]),
            apply_vendor_adjustment(as_float).drop(columns=["adjustment_factor"]),
        )

    def test_rows_without_any_symbol_are_returned_with_factor_one(self) -> None:
        raw = _series().assign(symbol=None)

        dumped = apply_vendor_adjustment(raw)

        self.assertEqual(len(dumped), len(raw))
        self.assertEqual(list(dumped["factor"]), [1.0] * len(raw))

    def test_rows_without_a_symbol_are_dropped_like_the_reference(self) -> None:
        raw = _series()
        raw.loc[raw.index[1], "symbol"] = None

        dumped = apply_vendor_adjustment(raw)

        self.assertEqual(len(dumped), len(raw) - 1)
        pd.testing.assert_frame_equal(dumped, _reference_vendor_adjustment(raw))

    def test_vectorised_adjustment_matches_the_per_symbol_reference(self) -> None:
        # Shuffled multi-symbol input with splits and rights issues: the
        # vectorised transform must equal the per-symbol loop it replaced, row
        # order included (symbols in first-appearance order, dates ascending).
        rng = np.random.default_rng(7)
        dates = pd.bdate_range("2026-01-05", periods=40).strftime("%Y-%m-%d")
        frames = []
        for code in ("10030", "10010", "10020"):
            factors = rng.choice([1.0, 1.0, 1.0, 1.0, 0.5, 2.0, 0.8], size=len(dates))
            frames.append(pd.DataFrame({
                "symbol": code, "date": dates,
                "open": rng.uniform(90, 110, len(dates)), "high": rng.uniform(110, 120, len(dates)),
                "low": rng.uniform(80, 90, len(dates)), "close": rng.uniform(90, 110, len(dates)),
                "volume": rng.integers(100, 10_000, len(dates)).astype(float),
                "amount": rng.uniform(1e6, 1e7, len(dates)),
                "adjustment_factor": factors,
                "ex_rights_type": rng.choice(["0", "1", "3"], size=len(dates)),
            }))
        raw = pd.concat(frames, ignore_index=True).sample(frac=1.0, random_state=3)

        pd.testing.assert_frame_equal(apply_vendor_adjustment(raw), _reference_vendor_adjustment(raw))


def _reference_vendor_adjustment(df: pd.DataFrame) -> pd.DataFrame:
    """The per-symbol implementation the vectorised one replaced (0.1.0)."""

    def exclusive_reverse_cumprod(values: pd.Series) -> pd.Series:
        return values.iloc[::-1].cumprod().shift(1, fill_value=1.0).iloc[::-1]

    pieces = []
    for _, group in df.groupby("symbol", sort=False):
        group = group.sort_values("date")
        price_cum = exclusive_reverse_cumprod(group["adjustment_factor"])
        rights = group["ex_rights_type"].astype(str).str.strip().isin(["3", "3.0"])
        volume_cum = exclusive_reverse_cumprod(group["adjustment_factor"].where(~rights, 1.0))
        adjusted = group.copy()
        for field in ("open", "high", "low", "close"):
            adjusted[field] = adjusted[field] * price_cum
        adjusted["volume"] = adjusted["volume"] / volume_cum
        adjusted["factor"] = price_cum
        pieces.append(adjusted)
    return pd.concat(pieces, ignore_index=True)


if __name__ == "__main__":
    unittest.main()
