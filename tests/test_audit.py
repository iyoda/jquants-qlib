from __future__ import annotations

import builtins
import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jqqlib.audit import (
    GapAuditResult,
    audit_dataset_gaps,
    find_recent_business_day_gaps,
    read_dataset_dates,
    write_audit_sidecar,
)
from jqqlib.trading_calendar import ExactCalendarRequired


class FindRecentBusinessDayGapsTests(unittest.TestCase):
    # June 2026 has no Japanese public holidays, so every weekday below is an
    # XTKS session and these cases read exactly as they did pre-calendar.
    def test_complete_window_has_no_gaps(self) -> None:
        present = {dt.date(2026, 6, 1), dt.date(2026, 6, 2), dt.date(2026, 6, 3),
                   dt.date(2026, 6, 4), dt.date(2026, 6, 5)}
        result = find_recent_business_day_gaps(present, dt.date(2026, 6, 5), 4)
        self.assertEqual(result.real_gaps, [])
        self.assertEqual(result.suppressed_non_sessions, [])

    def test_interior_hole_is_flagged(self) -> None:
        present = {dt.date(2026, 6, 1), dt.date(2026, 6, 2), dt.date(2026, 6, 4),
                   dt.date(2026, 6, 5)}
        result = find_recent_business_day_gaps(present, dt.date(2026, 6, 5), 4)
        self.assertEqual(result.real_gaps, [dt.date(2026, 6, 3)])

    def test_leading_edge_before_dataset_start_not_flagged(self) -> None:
        # Dataset starts 6/3; 6/1 and 6/2 are sessions before the data begins.
        present = {dt.date(2026, 6, 3), dt.date(2026, 6, 4), dt.date(2026, 6, 5)}
        result = find_recent_business_day_gaps(present, dt.date(2026, 6, 5), 6)
        self.assertEqual(result.real_gaps, [])

    def test_days_after_dataset_end_not_flagged(self) -> None:
        # as_of is today (6/8 Mon) but the dataset only reaches 6/5; today has no
        # expected bar yet -> no false gap (no-expected-day carve-out).
        present = {dt.date(2026, 6, 3), dt.date(2026, 6, 4), dt.date(2026, 6, 5)}
        result = find_recent_business_day_gaps(present, dt.date(2026, 6, 8), 4)
        self.assertEqual(result.real_gaps, [])

    def test_weekend_dates_never_flagged(self) -> None:
        present = {dt.date(2026, 6, 4), dt.date(2026, 6, 5), dt.date(2026, 6, 8)}
        # 6/6 (Sat) and 6/7 (Sun) are within span but are weekends.
        result = find_recent_business_day_gaps(present, dt.date(2026, 6, 8), 4)
        self.assertEqual(result.real_gaps, [])
        self.assertEqual(result.suppressed_non_sessions, [],
                         "weekends are not suppressions; the old logic skipped them too")

    def test_empty_dataset_returns_no_gaps(self) -> None:
        self.assertEqual(
            find_recent_business_day_gaps(set(), dt.date(2026, 6, 5), 4).real_gaps, []
        )

    def test_negative_window_rejected(self) -> None:
        with self.assertRaises(ValueError):
            find_recent_business_day_gaps({dt.date(2026, 6, 5)}, dt.date(2026, 6, 5), -1)

    # -- XTKS holidays are suppressions, not gaps -----------------------------

    def test_exchange_holiday_is_suppressed_not_flagged(self) -> None:
        # 2026-08-11 is Mountain Day: a Tuesday the exchange is closed. The
        # retired weekday() < 5 heuristic reported it as a missing business day.
        present = {dt.date(2026, 8, 6), dt.date(2026, 8, 7), dt.date(2026, 8, 10),
                   dt.date(2026, 8, 12)}
        result = find_recent_business_day_gaps(present, dt.date(2026, 8, 12), 4)

        self.assertEqual(result.real_gaps, [])
        self.assertEqual(result.suppressed_non_sessions, [dt.date(2026, 8, 11)])

    def test_interior_session_hole_still_flagged_alongside_a_holiday(self) -> None:
        # Same window, but 8/10 (a real session) is genuinely missing.
        present = {dt.date(2026, 8, 6), dt.date(2026, 8, 7), dt.date(2026, 8, 12)}
        result = find_recent_business_day_gaps(present, dt.date(2026, 8, 12), 4)

        self.assertEqual(result.real_gaps, [dt.date(2026, 8, 10)])
        self.assertEqual(result.suppressed_non_sessions, [dt.date(2026, 8, 11)])

    def test_window_counts_sessions_so_a_holiday_does_not_shorten_it(self) -> None:
        # A 3-session window back from 8/12 reaches 8/6 because the 8/11 holiday
        # is not counted; the retired weekday window stopped at 8/7 and would
        # have missed this hole entirely.
        present = {dt.date(2026, 8, 7), dt.date(2026, 8, 10), dt.date(2026, 8, 12)}
        result = find_recent_business_day_gaps(
            present, dt.date(2026, 8, 12), 3,
            dataset_start=dt.date(2026, 8, 5), dataset_end=dt.date(2026, 8, 12),
        )

        self.assertEqual(result.real_gaps, [dt.date(2026, 8, 6)])
        self.assertEqual(result.window_business_days, 3)


class ExactCalendarRequiredTests(unittest.TestCase):
    def test_missing_exchange_calendars_fails_closed(self) -> None:
        # No weekday fallback: an audit that cannot tell a holiday from a hole
        # must fail rather than guess.
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "exchange_calendars":
                raise ModuleNotFoundError("No module named 'exchange_calendars'")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", blocked):
            with self.assertRaises(ExactCalendarRequired):
                find_recent_business_day_gaps(
                    {dt.date(2026, 8, 12)}, dt.date(2026, 8, 12), 4
                )

    def test_date_out_of_bounds_fails_closed_not_as_a_gap(self) -> None:
        from jqqlib.trading_calendar import is_session

        class DateOutOfBounds(Exception):
            pass

        class _Cal:
            def is_session(self, _iso: str) -> bool:
                raise DateOutOfBounds("out of range")

        with mock.patch("jqqlib.trading_calendar._xtks_calendar", return_value=_Cal()):
            with self.assertRaises(ExactCalendarRequired):
                is_session(dt.date(1990, 1, 1))


class AuditSidecarTests(unittest.TestCase):
    def test_sidecar_requires_an_explicit_path(self) -> None:
        from jqqlib.audit import GapAuditResult

        with self.assertRaisesRegex(TypeError, "path"):
            write_audit_sidecar(GapAuditResult(), dt.date(2026, 8, 12))

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="audit_sidecar_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_sidecar_records_the_run_and_both_date_lists(self) -> None:
        present = {dt.date(2026, 8, 6), dt.date(2026, 8, 7), dt.date(2026, 8, 12)}
        result = find_recent_business_day_gaps(present, dt.date(2026, 8, 12), 4)
        path = self._tmp / "nested" / "last_audit.json"

        write_audit_sidecar(result, dt.date(2026, 8, 12), path=path,
                            run_started_at="2026-08-12T10:30:00Z")

        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {
            "as_of": "2026-08-12",
            "window_business_days": 4,
            "real_gaps": ["2026-08-10"],
            "suppressed_non_sessions": ["2026-08-11"],
            "run_started_at": "2026-08-12T10:30:00Z",
        })

    def test_rewrite_is_atomic_and_leaves_no_temp_files(self) -> None:
        path = self._tmp / "last_audit.json"
        result = find_recent_business_day_gaps({dt.date(2026, 8, 12)}, dt.date(2026, 8, 12), 1)
        write_audit_sidecar(result, dt.date(2026, 8, 12), path=path, run_started_at="a")
        write_audit_sidecar(result, dt.date(2026, 8, 12), path=path, run_started_at="b")

        self.assertEqual([p.name for p in self._tmp.iterdir()], ["last_audit.json"])
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["run_started_at"], "b"
        )


class AuditDatasetAndFailureTests(unittest.TestCase):
    def test_missing_empty_and_populated_parquet(self):
        import pandas as pd

        as_of = dt.date(2026, 6, 3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quotes.parquet"
            self.assertEqual(read_dataset_dates(path), set())
            self.assertEqual(audit_dataset_gaps(path, as_of, 2), GapAuditResult(window_business_days=2))
            pd.DataFrame({"date": pd.Series([], dtype="string")}).to_parquet(path)
            self.assertEqual(read_dataset_dates(path), set())
            pd.DataFrame({"date": ["2026-06-01", "2026-06-01", "2026-06-03"]}).to_parquet(path)
            self.assertEqual(read_dataset_dates(path), {dt.date(2026, 6, 1), as_of})
            self.assertEqual(audit_dataset_gaps(path, as_of, 2).real_gaps, [dt.date(2026, 6, 2)])

    def test_failed_replace_preserves_existing_sidecar_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last_audit.json"
            path.write_text("previous")
            with mock.patch("jqqlib.audit.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    write_audit_sidecar(GapAuditResult(), dt.date(2026, 6, 3), path=path)
            self.assertEqual(path.read_text(), "previous")
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_cleanup_failure_does_not_mask_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last_audit.json"
            with mock.patch.object(Path, "write_text", side_effect=OSError("write failed")), \
                    mock.patch.object(Path, "unlink", side_effect=OSError("cleanup failed")):
                with self.assertRaisesRegex(OSError, "write failed"):
                    write_audit_sidecar(GapAuditResult(), dt.date(2026, 6, 3), path=path)


if __name__ == "__main__":
    unittest.main()
