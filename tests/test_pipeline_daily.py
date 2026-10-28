from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import jsonschema
import pyarrow as pa
import requests
import yaml

from jqqlib.audit import GapAuditResult
from jqqlib.contracts import load_schema
from jqqlib.pipeline import (
    build_parser,
    catch_up_start_date,
    cli_error_exit_code,
    iter_recent_business_dates,
    main,
    parse_as_of_date,
    run_daily,
    validate_etl_config,
)
from jqqlib.provider_lock import provider_lock_path
from jqqlib.store import NoDataFetchedError
from jqqlib.trading_calendar import ExactCalendarRequired


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

    def test_default_daily_runner_refuses_replace_merge_mode(self) -> None:
        # replace would cut the Parquet down to the daily fetch window on every run.
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(yaml.safe_dump({
                "storage": {"parquet_dir": str(Path(tmp) / "parquet"), "qlib_dir": str(Path(tmp) / "qlib")},
                "etl": {"merge_mode": "replace"},
            }), encoding="utf-8")

            with (
                mock.patch("jqqlib.pipeline.run_all") as run_all,
                self.assertRaisesRegex(ValueError, "run-daily requires etl.merge_mode: upsert"),
            ):
                run_daily(config_path, dt.date(2026, 6, 5), dataset_end_reader=lambda _: dt.date(2026, 6, 4))

        run_all.assert_not_called()

    def test_default_daily_runner_runs_all_with_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(yaml.safe_dump({
                "storage": {"parquet_dir": str(Path(tmp) / "parquet"), "qlib_dir": str(Path(tmp) / "qlib")},
            }), encoding="utf-8")

            with mock.patch("jqqlib.pipeline.run_all") as run_all:
                target_date = run_daily(
                    config_path, dt.date(2026, 6, 5), dataset_end_reader=lambda _: dt.date(2026, 6, 2)
                )

        self.assertEqual(target_date, dt.date(2026, 6, 5))
        run_all.assert_called_once_with(config_path, dt.date(2026, 6, 3), dt.date(2026, 6, 5))

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
        utc_instant = dt.datetime(2026, 6, 4, 16, 0, tzinfo=dt.UTC)
        seen_tz: list[dt.tzinfo] = []

        def fake_now(tz: dt.tzinfo) -> dt.datetime:
            seen_tz.append(tz)
            return utc_instant.astimezone(tz)

        with mock.patch("jqqlib.pipeline._now", side_effect=fake_now):
            self.assertEqual(parse_as_of_date("today"), dt.date(2026, 6, 5))
        self.assertEqual([getattr(tz, "key", None) for tz in seen_tz], ["Asia/Tokyo"])


class AuditCommandTests(unittest.TestCase):
    def _config(self, tmp: str) -> Path:
        config_path = Path(tmp) / "settings.yaml"
        config_path.write_text(yaml.safe_dump({"storage": {
            "parquet_dir": str(Path(tmp) / "parquet"),
            "qlib_dir": str(Path(tmp) / "qlib"),
            "audit_dir": str(Path(tmp) / "audit"),
        }}))
        return config_path

    def test_unavailable_calendar_exits_2_not_as_a_data_gap(self):
        for command, patched in (("audit", "jqqlib.pipeline.run_audit"), ("run-daily", "jqqlib.pipeline.run_daily")):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                stderr = io.StringIO()
                with (
                    mock.patch("sys.argv", ["jqqlib", command, "--config", str(self._config(tmp))]),
                    mock.patch(patched, side_effect=ExactCalendarRequired("no XTKS")),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("XTKS trading calendar unavailable", stderr.getvalue())
                self.assertFalse((Path(tmp) / "audit" / "last_audit.json").exists())

    def test_run_daily_accepts_the_audit_window_flag_the_daily_wrapper_forwards(self):
        args = build_parser().parse_args(
            ["run-daily", "--config", "config.yaml", "--as-of", "2026-08-12", "--window-business-days", "5"]
        )
        self.assertEqual(args.command, "run-daily")
        self.assertEqual(args.window_business_days, 5)

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


class ResultJsonTests(unittest.TestCase):
    def _config(self, tmp: str) -> Path:
        config_path = Path(tmp) / "settings.yaml"
        config_path.write_text(yaml.safe_dump({"storage": {
            "parquet_dir": str(Path(tmp) / "parquet"),
            "qlib_dir": str(Path(tmp) / "qlib"),
            "audit_dir": str(Path(tmp) / "audit"),
        }}))
        return config_path

    def _run(self, argv: list[str], **patches: Any) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("sys.argv", ["jqqlib", *argv]))
            for target, kwargs in patches.items():
                stack.enter_context(mock.patch(f"jqqlib.pipeline.{target}", **kwargs))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            try:
                main()
            except SystemExit as exc:
                return int(exc.code or 0), stderr.getvalue()
        return 0, stderr.getvalue()

    def _result(self, path: Path) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.validate(document, load_schema("cli_result"))
        return document

    def test_completed_run_daily_and_audit_write_completed_with_exit_0(self) -> None:
        patches: dict[str, dict[str, Any]] = {
            "run-daily": {"run_daily": {"return_value": dt.date(2026, 8, 12)}},
            "audit": {"run_audit": {"return_value": GapAuditResult(window_business_days=5)}},
        }
        for command, patch in patches.items():
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                result_path = Path(tmp) / "result.json"
                code, _ = self._run(
                    [command, "--config", str(self._config(tmp)), "--as-of", "2026-08-12",
                     "--result-json", str(result_path)],
                    **patch,
                )
                self.assertEqual(code, 0)
                self.assertEqual(self._result(result_path), {
                    "schema_version": 1, "command": command, "outcome": "completed", "exit_code": 0, "error": None,
                })

    def test_audit_with_real_gaps_writes_gaps_with_exit_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "result.json"
            gaps = GapAuditResult(real_gaps=[dt.date(2026, 8, 10)], window_business_days=5)
            code, _ = self._run(
                ["audit", "--config", str(self._config(tmp)), "--as-of", "2026-08-12",
                 "--result-json", str(result_path)],
                run_audit={"return_value": gaps},
            )
            self.assertEqual(code, 1)
            self.assertEqual(self._result(result_path), {
                "schema_version": 1, "command": "audit", "outcome": "gaps", "exit_code": 1, "error": None,
            })
            self.assertTrue((Path(tmp) / "audit" / "last_audit.json").exists())

    def test_unavailable_calendar_writes_calendar_unavailable_with_exit_2(self) -> None:
        for command, target in (("audit", "run_audit"), ("run-daily", "run_daily")):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                result_path = Path(tmp) / "result.json"
                code, stderr = self._run(
                    [command, "--config", str(self._config(tmp)), "--result-json", str(result_path)],
                    **{target: {"side_effect": ExactCalendarRequired("no XTKS")}},
                )
                self.assertEqual(code, 2)
                self.assertIn("XTKS trading calendar unavailable: no XTKS", stderr)
                self.assertEqual(self._result(result_path), {
                    "schema_version": 1, "command": command, "outcome": "calendar_unavailable", "exit_code": 2,
                    "error": None,
                })

    def test_missing_config_writes_failed_with_exit_2_and_the_error_message(self) -> None:
        for command in ("run-daily", "audit"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                result_path = Path(tmp) / "result.json"
                code, stderr = self._run(
                    [command, "--config", str(Path(tmp) / "missing.yaml"), "--result-json", str(result_path)]
                )
                self.assertEqual(code, 2)
                document = self._result(result_path)
                self.assertEqual(stderr, f"error: {document['error']}\n")
                self.assertEqual(
                    {key: document[key] for key in ("schema_version", "command", "outcome", "exit_code")},
                    {"schema_version": 1, "command": command, "outcome": "failed", "exit_code": 2},
                )

    def test_runtime_failure_writes_failed_with_exit_1_and_the_error_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "result.json"
            code, stderr = self._run(
                ["run-daily", "--config", str(self._config(tmp)), "--result-json", str(result_path)],
                run_daily={"side_effect": NoDataFetchedError("No data fetched for any day")},
            )
            self.assertEqual(code, 1)
            self.assertEqual(stderr, "error: No data fetched for any day\n")
            self.assertEqual(self._result(result_path), {
                "schema_version": 1, "command": "run-daily", "outcome": "failed", "exit_code": 1,
                "error": "No data fetched for any day",
            })

    def test_an_unexpected_exit_status_still_writes_a_schema_valid_failed_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "result.json"
            code, _ = self._run(
                ["audit", "--config", str(self._config(tmp)), "--result-json", str(result_path)],
                run_audit={"side_effect": SystemExit(5)},
            )
            self.assertEqual(code, 5)
            self.assertEqual(self._result(result_path), {
                "schema_version": 1, "command": "audit", "outcome": "failed", "exit_code": 5,
                "error": "exited with status 5",
            })

    def test_without_the_flag_no_result_file_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, stderr = self._run(
                ["run-daily", "--config", str(self._config(tmp))],
                run_daily={"side_effect": NoDataFetchedError("No data fetched for any day")},
            )
            self.assertEqual(code, 1)
            self.assertEqual(stderr, "error: No data fetched for any day\n")
            lock_beside_the_provider = provider_lock_path(Path(tmp) / "qlib").name
            self.assertEqual(
                sorted(path.name for path in Path(tmp).iterdir()), sorted([lock_beside_the_provider, "settings.yaml"])
            )

    def test_unwritable_result_path_warns_and_keeps_the_original_exit_code(self) -> None:
        cases: dict[str, tuple[int, dict[str, Any]]] = {
            "completed": (0, {"return_value": dt.date(2026, 8, 12)}),
            "failed": (1, {"side_effect": NoDataFetchedError("No data fetched for any day")}),
            "calendar_unavailable": (2, {"side_effect": ExactCalendarRequired("no XTKS")}),
        }
        for outcome, (expected_code, patch) in cases.items():
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                not_a_directory = Path(tmp) / "not-a-directory"
                not_a_directory.write_text("existing file")
                result_path = not_a_directory / "result.json"
                code, stderr = self._run(
                    ["run-daily", "--config", str(self._config(tmp)), "--result-json", str(result_path)],
                    run_daily=patch,
                )
                self.assertEqual(code, expected_code)
                self.assertIn(f"Warning: could not write the result JSON {result_path}: ", stderr)
                if outcome == "failed":
                    self.assertTrue(stderr.startswith("error: No data fetched for any day\n"))


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


class CliExitCodeTests(unittest.TestCase):
    def test_user_errors_exit_2(self) -> None:
        for exc in (ValueError("etl.retries must be 0 or greater."), FileNotFoundError("config.yaml")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(cli_error_exit_code(exc), 2)

    def test_rejected_credentials_exit_2(self) -> None:
        # 401/403 are not retried and re-running cannot fix a wrong key: a config error.
        for status in (401, 403):
            response = requests.Response()
            response.status_code = status
            with self.subTest(status=status):
                self.assertEqual(cli_error_exit_code(requests.HTTPError(str(status), response=response)), 2)

    def test_run_failures_exit_1(self) -> None:
        response = requests.Response()
        response.status_code = 503
        failures = (
            RuntimeError("Parquet is not Qlib-ready"),
            NoDataFetchedError("No data fetched"),
            requests.HTTPError("503", response=response),
            requests.ConnectionError("Max retries exceeded"),
            pa.ArrowInvalid("Parquet magic bytes not found"),
        )
        for exc in failures:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(cli_error_exit_code(exc), 1)

    def test_run_daily_without_data_exits_1_not_as_a_config_error(self) -> None:
        stderr = io.StringIO()
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        config_path = Path(tmp) / "config.yaml"
        config_path.write_text(yaml.safe_dump({"storage": {
            "parquet_dir": str(Path(tmp) / "parquet"), "qlib_dir": str(Path(tmp) / "qlib"),
        }}))
        with (
            mock.patch("sys.argv", ["jqqlib", "run-daily", "--config", str(config_path), "--as-of", "2026-06-05"]),
            mock.patch("jqqlib.pipeline.run_daily", side_effect=NoDataFetchedError("No data fetched for any day")),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main()

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(stderr.getvalue(), "error: No data fetched for any day\n")


if __name__ == "__main__":
    unittest.main()
