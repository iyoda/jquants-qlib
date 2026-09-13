from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from jqqlib.audit import GapAuditResult
from jqqlib.pipeline import (
    catch_up_start_date,
    iter_recent_business_dates,
    main,
    parse_as_of_date,
    run_daily,
    validate_etl_config,
)
from jqqlib.store import NoDataFetchedError


class RunDailyTests(unittest.TestCase):
    def test_recent_business_dates_start_with_weekday_as_of(self) -> None:
        as_of = dt.date(2026, 6, 4)

        dates = list(iter_recent_business_dates(as_of, max_lookback_business_days=2))

        self.assertEqual(
            dates,
            [
                dt.date(2026, 6, 4),
                dt.date(2026, 6, 3),
                dt.date(2026, 6, 2),
            ],
        )

    def test_recent_business_dates_skip_exchange_holidays(self) -> None:
        # 2026-08-11 is Mountain Day: a weekday the TSE is closed, so it can
        # never have data and must not consume a lookback attempt.
        as_of = dt.date(2026, 8, 12)

        dates = list(iter_recent_business_dates(as_of, max_lookback_business_days=2))

        self.assertEqual(
            dates,
            [
                dt.date(2026, 8, 12),
                dt.date(2026, 8, 10),
                dt.date(2026, 8, 7),
            ],
        )

    def test_recent_business_dates_skip_weekend(self) -> None:
        as_of = dt.date(2026, 6, 7)

        dates = list(iter_recent_business_dates(as_of, max_lookback_business_days=1))

        self.assertEqual(
            dates,
            [
                dt.date(2026, 6, 5),
                dt.date(2026, 6, 4),
            ],
        )

    def test_run_daily_falls_back_on_no_data(self) -> None:
        calls: list[dt.date] = []

        def runner(config_path: Path, start_date: dt.date, end_date: dt.date) -> None:
            self.assertEqual(config_path, Path("config.yaml"))
            self.assertEqual(start_date, end_date)
            calls.append(start_date)
            if len(calls) == 1:
                raise NoDataFetchedError("No data fetched from J-Quants API.")

        target_date = run_daily(
            Path("config.yaml"),
            dt.date(2026, 6, 8),
            runner=runner,
            dataset_end_reader=lambda _: None,
        )

        self.assertEqual(target_date, dt.date(2026, 6, 5))
        self.assertEqual(calls, [dt.date(2026, 6, 8), dt.date(2026, 6, 5)])

    def test_run_daily_raises_non_no_data_errors_immediately(self) -> None:
        calls: list[dt.date] = []

        def runner(config_path: Path, start_date: dt.date, end_date: dt.date) -> None:
            calls.append(start_date)
            raise RuntimeError("authentication failed")

        with self.assertRaisesRegex(RuntimeError, "authentication failed"):
            run_daily(
                Path("config.yaml"),
                dt.date(2026, 6, 8),
                runner=runner,
                dataset_end_reader=lambda _: None,
            )

        self.assertEqual(calls, [dt.date(2026, 6, 8)])

    def test_run_daily_raises_no_data_after_all_candidates(self) -> None:
        calls: list[dt.date] = []

        def runner(config_path: Path, start_date: dt.date, end_date: dt.date) -> None:
            calls.append(start_date)
            raise NoDataFetchedError("No data fetched from J-Quants API.")

        with self.assertRaises(NoDataFetchedError):
            run_daily(
                Path("config.yaml"),
                dt.date(2026, 6, 7),
                max_lookback_business_days=1,
                runner=runner,
                dataset_end_reader=lambda _: None,
            )

        self.assertEqual(calls, [dt.date(2026, 6, 5), dt.date(2026, 6, 4)])

    def test_run_daily_catches_up_missing_range_in_single_fetch(self) -> None:
        # Catch-up fetches the full contiguous range in ONE call, so a
        # mid-run failure leaves no partially-advanced manifest, and there is no
        # wasteful double-fetch of the target day.
        calls: list[tuple[dt.date, dt.date]] = []

        def runner(config_path: Path, start_date: dt.date, end_date: dt.date) -> None:
            calls.append((start_date, end_date))

        with self.assertLogs("jqqlib.pipeline", level="INFO") as logged:
            target_date = run_daily(
                Path("config.yaml"),
                dt.date(2026, 6, 5),
                runner=runner,
                dataset_end_reader=lambda _: dt.date(2026, 6, 2),
            )

        self.assertEqual(target_date, dt.date(2026, 6, 5))
        self.assertEqual(calls, [(dt.date(2026, 6, 3), dt.date(2026, 6, 5))])
        self.assertIn(
            "INFO:jqqlib.pipeline:Catching up missing range 2026-06-03 to 2026-06-05.",
            logged.output,
        )

    def test_run_daily_propagates_catch_up_range_error_without_fallback(self) -> None:
        # A non-NoData failure during a catch-up range must propagate (no
        # silent advance, no fallback to a previous business day). Atomic
        # write_parquet means nothing is written, so the next run refills.
        calls: list[tuple[dt.date, dt.date]] = []

        def runner(config_path: Path, start_date: dt.date, end_date: dt.date) -> None:
            calls.append((start_date, end_date))
            raise RuntimeError("network exploded mid-range")

        with self.assertRaisesRegex(RuntimeError, "network exploded mid-range"):
            run_daily(
                Path("config.yaml"),
                dt.date(2026, 6, 5),
                runner=runner,
                dataset_end_reader=lambda _: dt.date(2026, 6, 2),
            )

        self.assertEqual(calls, [(dt.date(2026, 6, 3), dt.date(2026, 6, 5))])

    def test_catch_up_start_date_uses_target_when_dataset_is_current(self) -> None:
        self.assertEqual(
            catch_up_start_date(dt.date(2026, 6, 5), dt.date(2026, 6, 5)),
            dt.date(2026, 6, 5),
        )

    def test_parse_as_of_date_accepts_fixed_date(self) -> None:
        self.assertEqual(parse_as_of_date("2026-06-04"), dt.date(2026, 6, 4))

    def test_parse_as_of_date_today_is_asia_tokyo(self) -> None:
        # 2026-06-04 16:00 UTC is already 2026-06-05 01:00 in Asia/Tokyo; using
        # naive local time or UTC would still report 2026-06-04.
        utc_instant = dt.datetime(2026, 6, 4, 16, 0, tzinfo=dt.timezone.utc)
        seen_tz: list[dt.tzinfo] = []

        def fake_now(tz: dt.tzinfo) -> dt.datetime:
            seen_tz.append(tz)
            return utc_instant.astimezone(tz)

        with mock.patch("jqqlib.pipeline._now", side_effect=fake_now):
            self.assertEqual(parse_as_of_date("today"), dt.date(2026, 6, 5))
        self.assertEqual([getattr(tz, "key", None) for tz in seen_tz], ["Asia/Tokyo"])


class AuditCommandTests(unittest.TestCase):
    def test_audit_writes_to_configured_directory_and_preserves_exit_status(self):
        for gaps in ([], [dt.date(2026, 8, 10)]):
            with self.subTest(gaps=gaps), tempfile.TemporaryDirectory() as tmp:
                audit_dir = Path(tmp) / "monitoring" / "audit"
                config_path = Path(tmp) / "settings.yaml"
                config_path.write_text(yaml.safe_dump({"storage": {
                    "parquet_dir": str(Path(tmp) / "parquet"),
                    "qlib_dir": str(Path(tmp) / "qlib"),
                    "audit_dir": str(audit_dir),
                }}))
                result = GapAuditResult(real_gaps=gaps, window_business_days=5)
                with (
                    mock.patch("sys.argv", ["jqqlib", "audit", "--config", str(config_path), "--as-of", "2026-08-12"]),
                    mock.patch("jqqlib.pipeline.run_audit", return_value=result),
                    mock.patch.dict("os.environ", {"JQQLIB_RUN_STARTED_AT": "test-run"}),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    if gaps:
                        with self.assertRaises(SystemExit) as raised:
                            main()
                        self.assertEqual(raised.exception.code, 1)
                    else:
                        main()
                document = json.loads((audit_dir / "last_audit.json").read_text())
                self.assertEqual(document["as_of"], "2026-08-12")
                self.assertEqual(document["real_gaps"], [day.isoformat() for day in gaps])
                self.assertEqual(document["run_started_at"], "test-run")

    def test_sidecar_write_failure_warns_with_configured_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "not-a-directory"
            audit_dir.write_text("existing file")
            config_path = Path(tmp) / "settings.yaml"
            config_path.write_text(yaml.safe_dump({"storage": {
                "parquet_dir": str(Path(tmp) / "parquet"),
                "qlib_dir": str(Path(tmp) / "qlib"),
                "audit_dir": str(audit_dir),
            }}))
            stderr = io.StringIO()
            with (
                mock.patch("sys.argv", ["jqqlib", "audit", "--config", str(config_path), "--as-of", "2026-08-12"]),
                mock.patch("jqqlib.pipeline.run_audit", return_value=GapAuditResult()),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
            ):
                main()
            self.assertIn(
                f"Warning: could not write the audit sidecar {audit_dir / 'last_audit.json'}:", stderr.getvalue()
            )


class EtlConfigValidationTests(unittest.TestCase):
    def test_validates_positive_chunk_days(self) -> None:
        with self.assertRaisesRegex(ValueError, "etl.chunk_days"):
            validate_etl_config({"etl": {"chunk_days": 0}})

    def test_rejects_fractional_integer_config_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "etl.chunk_days"):
            validate_etl_config({"etl": {"chunk_days": 1.5}})

    def test_rejects_boolean_integer_config_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "etl.retries"):
            validate_etl_config({"etl": {"retries": True}})

    def test_rejects_boolean_float_config_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "etl.request_interval_sec"):
            validate_etl_config({"etl": {"request_interval_sec": True}})

    def test_validates_non_negative_request_interval(self) -> None:
        with self.assertRaisesRegex(ValueError, "etl.request_interval_sec"):
            validate_etl_config({"etl": {"request_interval_sec": -0.01}})

    def test_validates_retry_max_delay_not_below_base_delay(self) -> None:
        with self.assertRaisesRegex(ValueError, "retry_max_delay_sec"):
            validate_etl_config(
                {
                    "etl": {
                        "retry_base_delay_sec": 2.0,
                        "retry_max_delay_sec": 1.0,
                    }
                }
            )

    def test_rejects_unsupported_merge_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "etl.merge_mode"):
            validate_etl_config({"etl": {"merge_mode": "append"}})


if __name__ == "__main__":
    unittest.main()
