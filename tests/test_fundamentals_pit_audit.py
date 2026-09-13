from __future__ import annotations

import unittest

import pandas as pd

from jqqlib.normalize_fundamentals import PitAuditError, assert_pit_audit_passes, audit_pit


class PitAuditFailClosedTests(unittest.TestCase):
    def test_injected_lookahead_violation_raises_pit_audit_error(self) -> None:
        # Artificially inject a record whose source disclosure date is on/after
        # the as-of trading day it is attached to -- a look-ahead leak that the
        # fail-closed gate must catch before any downstream factor sees it.
        panel = pd.DataFrame(
            {
                "Eq": [100.0],
                "_src_DiscDate": [pd.Timestamp("2026-06-05")],
            },
            index=pd.MultiIndex.from_tuples(
                [(pd.Timestamp("2026-06-03"), "10010")], names=["datetime", "instrument"]
            ),
        )

        audit = audit_pit(panel)

        self.assertGreater(audit["pit_violations"], 0)
        self.assertFalse(audit["pit_audit_pass"])
        with self.assertRaises(PitAuditError):
            assert_pit_audit_passes(audit)

    def test_mixed_panel_counts_only_the_violating_rows(self) -> None:
        panel = pd.DataFrame(
            {
                "Eq": [100.0, 200.0, 300.0],
                "_src_DiscDate": [
                    pd.Timestamp("2026-06-01"),  # clean: disclosed strictly before
                    pd.Timestamp("2026-06-05"),  # violation: disclosed on the as-of day
                    pd.Timestamp("2026-06-10"),  # violation: disclosed after the as-of day
                ],
            },
            index=pd.MultiIndex.from_tuples(
                [
                    (pd.Timestamp("2026-06-03"), "10010"),
                    (pd.Timestamp("2026-06-05"), "10020"),
                    (pd.Timestamp("2026-06-04"), "10030"),
                ],
                names=["datetime", "instrument"],
            ),
        )

        audit = audit_pit(panel)

        self.assertEqual(audit["pit_violations"], 2)
        with self.assertRaises(PitAuditError):
            assert_pit_audit_passes(audit)

    def test_zero_violations_does_not_raise(self) -> None:
        panel = pd.DataFrame(
            {
                "Eq": [100.0],
                "_src_DiscDate": [pd.Timestamp("2026-06-01")],
            },
            index=pd.MultiIndex.from_tuples(
                [(pd.Timestamp("2026-06-03"), "10010")], names=["datetime", "instrument"]
            ),
        )

        audit = audit_pit(panel)

        self.assertEqual(audit["pit_violations"], 0)
        assert_pit_audit_passes(audit)  # must not raise


if __name__ == "__main__":
    unittest.main()
