from __future__ import annotations

import unittest

import pandas as pd

from jqqlib.normalize_fundamentals import assert_pit_audit_passes, audit_pit, normalize_fundamentals


def _calendar() -> pd.DatetimeIndex:
    return pd.DatetimeIndex(
        pd.to_datetime(
            [
                "2026-06-01",
                "2026-06-02",
                "2026-06-03",
                "2026-06-04",
                "2026-06-05",
                "2026-06-08",
                "2026-06-09",
                "2026-06-10",
            ]
        )
    )


class NormalizeFundamentalsTests(unittest.TestCase):
    def test_effective_date_is_next_trading_day_strictly_after_disclosure(self) -> None:
        raw = pd.DataFrame(
            [{"Code": "10010", "DiscDate": "2026-06-02", "DiscTime": "15:00:00", "Eq": 100.0}]
        )

        panel = normalize_fundamentals(raw, _calendar())
        by_code = panel.xs("10010", level="instrument")

        self.assertEqual(by_code.index[0], pd.Timestamp("2026-06-03"))
        self.assertEqual(by_code.iloc[0]["Eq"], 100.0)
        self.assertEqual(by_code.iloc[0]["_src_DiscDate"], pd.Timestamp("2026-06-02"))

    def test_same_day_disclosure_is_never_effective_same_day(self) -> None:
        # Disclosure lands exactly on a trading day in the calendar; the
        # effective date must still be the NEXT trading day, never that day.
        raw = pd.DataFrame([{"Code": "10010", "DiscDate": "2026-06-04", "Eq": 100.0}])

        panel = normalize_fundamentals(raw, _calendar())
        by_code = panel.xs("10010", level="instrument")

        self.assertNotIn(pd.Timestamp("2026-06-04"), by_code.index)
        self.assertEqual(by_code.index[0], pd.Timestamp("2026-06-05"))

    def test_value_is_forward_filled_until_next_disclosure(self) -> None:
        raw = pd.DataFrame(
            [
                {"Code": "10010", "DiscDate": "2026-06-02", "Eq": 100.0},
                {"Code": "10010", "DiscDate": "2026-06-08", "Eq": 200.0},
            ]
        )

        panel = normalize_fundamentals(raw, _calendar())
        by_code = panel.xs("10010", level="instrument")

        self.assertEqual(by_code.loc["2026-06-03", "Eq"], 100.0)
        self.assertEqual(by_code.loc["2026-06-08", "Eq"], 100.0)
        self.assertEqual(by_code.loc["2026-06-09", "Eq"], 200.0)
        self.assertEqual(by_code.loc["2026-06-10", "Eq"], 200.0)

    def test_rows_before_first_disclosure_are_dropped(self) -> None:
        raw = pd.DataFrame([{"Code": "10010", "DiscDate": "2026-06-08", "Eq": 100.0}])

        panel = normalize_fundamentals(raw, _calendar())
        by_code = panel.xs("10010", level="instrument")

        self.assertNotIn(pd.Timestamp("2026-06-01"), by_code.index)
        self.assertEqual(by_code.index.min(), pd.Timestamp("2026-06-09"))

    def test_index_is_two_level_datetime_instrument(self) -> None:
        raw = pd.DataFrame(
            [
                {"Code": "10010", "DiscDate": "2026-06-02", "Eq": 100.0},
                {"Code": "10020", "DiscDate": "2026-06-04", "Eq": 50.0},
            ]
        )

        panel = normalize_fundamentals(raw, _calendar())

        self.assertEqual(panel.index.names, ["datetime", "instrument"])
        self.assertEqual(panel.index.nlevels, 2)
        self.assertEqual(sorted(panel.index.get_level_values("instrument").unique()), ["10010", "10020"])

    def test_duplicate_disclosures_on_same_effective_date_keep_latest(self) -> None:
        # Two disclosures for the same code both land on the same effective date
        # (e.g. same trading day gap); the later DiscDate should win.
        raw = pd.DataFrame(
            [
                {"Code": "10010", "DiscDate": "2026-06-01", "Eq": 10.0},
                {"Code": "10010", "DiscDate": "2026-06-02", "Eq": 20.0},
            ]
        )

        panel = normalize_fundamentals(raw, _calendar())
        by_code = panel.xs("10010", level="instrument")

        # Both disclosures are effective 2026-06-03 (next trading day after each);
        # the second (later DiscDate) must be the one that survives.
        self.assertEqual(by_code.loc["2026-06-03", "Eq"], 20.0)

    def test_clean_panel_has_zero_pit_violations_and_passes_gate(self) -> None:
        raw = pd.DataFrame(
            [
                {"Code": "10010", "DiscDate": "2026-06-02", "Eq": 100.0},
                {"Code": "10020", "DiscDate": "2026-06-04", "Eq": 50.0},
            ]
        )

        panel = normalize_fundamentals(raw, _calendar())
        audit = audit_pit(panel)

        self.assertEqual(audit["pit_violations"], 0)
        self.assertTrue(audit["pit_audit_pass"])
        assert_pit_audit_passes(audit)  # must not raise

    def test_empty_raw_frame_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_fundamentals(pd.DataFrame(), _calendar())


if __name__ == "__main__":
    unittest.main()
