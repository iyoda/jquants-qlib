from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

import jqqlib
from jqqlib.contracts import load_schema
from jqqlib.pipeline import main, packaged_config_example
from tests.support.synthetic import write_synthetic_daily_quotes_csv


def console_script() -> str:
    """Locate the installed ``jqqlib`` entry point next to the running interpreter."""
    candidate = Path(sys.executable).parent / "jqqlib"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("jqqlib")
    if found is None:
        message = "jqqlib console script is not installed for this interpreter"
        if os.environ.get("CI"):
            # On CI the package is always installed; a missing entry point is a
            # packaging regression, not a reason to skip the end-to-end tests.
            raise RuntimeError(message)
        raise unittest.SkipTest(message)
    return found


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [console_script(), *args], capture_output=True, text=True, check=False, cwd=cwd,
    )


class _EmptyDumpDataAll:
    """Stand-in for a Qlib dump that silently produces no provider subtree."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def dump(self) -> None:
        pass


class CliTests(unittest.TestCase):
    def invoke(self, *args, dump_data_all=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("sys.argv", ["jqqlib", *args]))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            if dump_data_all is not None:
                stack.enter_context(patch("jqqlib.qlib_dump.DumpDataAll", dump_data_all))
            raised = stack.enter_context(self.assertRaises(SystemExit))
            main()
        return raised.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_all_subcommand_help(self):
        commands = ("init-config", "run-etl", "convert", "validate", "bootstrap-csv", "download-csv",
                    "run-all", "run-daily", "audit", "publish-check", "publish", "publish-paths")
        for command in ("", *commands):
            with self.subTest(command=command):
                code, stdout, _ = self.invoke(*([command] if command else []), "--help")
                self.assertEqual(code, 0)
                self.assertIn("usage:", stdout)

    def test_missing_config_is_actionable(self):
        for command in ("convert", "validate", "run-daily", "audit", "publish-check", "publish-paths"):
            with self.subTest(command=command):
                code, _, stderr = self.invoke(command)
                self.assertNotEqual(code, 0)
                self.assertIn("--config", stderr)
                self.assertIn("required", stderr)

    def test_version_matches_package_metadata(self):
        code, stdout, _ = self.invoke("--version")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, f"jqqlib {jqqlib.__version__}\n")

    def test_console_script_version(self):
        result = run_cli("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"jqqlib {jqqlib.__version__}\n")

    def test_python_dash_m_jqqlib_is_the_same_cli(self):
        result = subprocess.run(
            [sys.executable, "-m", "jqqlib", "--help"], capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("usage: jqqlib "))
        self.assertIn("init-config", result.stdout)

    def test_config_errors_exit_2_without_traceback(self):
        cases = {
            "invalid_storage": ("storage: invalid\n", "storage configuration must be a mapping."),
            "missing_key": ("storage:\n  parquet_dir: ./p\n", "storage.qlib_dir is required."),
        }
        for name, (document, message) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "settings.yaml"
                path.write_text(document, encoding="utf-8")
                result = run_cli("convert", "--config", str(path))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, f"error: {message}\n")
        with self.subTest(case="missing_file"), tempfile.TemporaryDirectory() as tmp:
            result = run_cli("convert", "--config", str(Path(tmp) / "absent.yaml"))
            self.assertEqual(result.returncode, 2)
            self.assertTrue(result.stderr.startswith("error: "), result.stderr)
            self.assertIn("absent.yaml", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
        with self.subTest(case="invalid_yaml"), tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.yaml"
            path.write_text("storage: [unclosed\n", encoding="utf-8")
            result = run_cli("convert", "--config", str(path))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertTrue(result.stderr.startswith(f"error: invalid YAML in {path}: "), result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_dataset_state_errors_exit_1_without_traceback(self):
        # A Qlib dump that produces no provider subtree is a dataset-state
        # failure, reported like a failed check rather than as a crash.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "settings.yaml"
            path.write_text(
                f"storage:\n  parquet_dir: {root / 'p'}\n  qlib_dir: {root / 'q'}\n",
                encoding="utf-8",
            )
            write_synthetic_daily_quotes_csv(
                root / "daily_quotes.csv", symbols=["10010"], start="2026-06-01", end="2026-06-02",
            )
            code, stdout, stderr = self.invoke(
                "bootstrap-csv", "--config", str(path), "--csv", str(root / "daily_quotes.csv"),
                dump_data_all=_EmptyDumpDataAll,
            )
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertTrue(stderr.startswith("error: "), stderr)
            self.assertIn("did not create generated subtree", stderr)
            self.assertNotIn("Traceback", stderr)

    def test_convert_missing_adjustment_factor_exits_1_without_dump(self):
        import pandas as pd

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parquet_dir = root / "p"
            qlib_dir = root / "q"
            parquet_dir.mkdir()
            pd.DataFrame(
                {
                    "symbol": ["10010"],
                    "date": ["2026-06-01"],
                    "open": [1.0],
                    "high": [2.0],
                    "low": [0.5],
                    "close": [1.5],
                    "volume": [100.0],
                    "amount": [150.0],
                }
            ).to_parquet(parquet_dir / "daily_quotes.parquet", index=False)
            stale = qlib_dir / "features" / "99990" / "open.day.bin"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale")
            path = root / "settings.yaml"
            path.write_text(
                f"storage:\n  parquet_dir: {parquet_dir}\n  qlib_dir: {qlib_dir}\n",
                encoding="utf-8",
            )
            result = run_cli("convert", "--config", str(path))
            # A negative return code is a signal (e.g. -6 = SIGABRT); surface the
            # captured stderr so a native crash at interpreter exit is diagnosable.
            self.assertEqual(result.returncode, 1, msg=f"stderr:\n{result.stderr}")
            self.assertEqual(result.stdout, "")
            self.assertTrue(result.stderr.startswith("error: Parquet is not Qlib-ready:"), result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertTrue(stale.exists())
            self.assertFalse((qlib_dir / "features" / "10010").exists())

    def test_missing_config_in_process_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, stderr = self.invoke("publish-paths", "--config", str(Path(tmp) / "absent.yaml"))
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr.startswith("error: "), stderr)


class InitConfigTests(unittest.TestCase):
    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("sys.argv", ["jqqlib", "init-config", *args]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                main()
            except SystemExit as exc:
                return exc.code, stdout.getvalue(), stderr.getvalue()
        return 0, stdout.getvalue(), stderr.getvalue()

    def test_creates_file_identical_to_packaged_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "config.yaml"
            code, stdout, stderr = self.invoke(str(target))
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(stdout, f"Configuration written: {target}\n")
            self.assertEqual(target.read_bytes(), packaged_config_example())

    def test_default_path_is_config_yaml_in_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli("init-config", cwd=Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(tmp) / "config.yaml").read_bytes(), packaged_config_example())

    def test_refuses_to_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.yaml"
            target.write_text("storage: {}\n", encoding="utf-8")
            code, stdout, stderr = self.invoke(str(target))
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, f"error: {target} already exists; pass --force to overwrite it.\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "storage: {}\n")

    def test_force_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.yaml"
            target.write_text("storage: {}\n", encoding="utf-8")
            code, _, stderr = self.invoke(str(target), "--force")
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(target.read_bytes(), packaged_config_example())

    def test_console_script_refusal_has_no_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.yaml"
            target.write_text("x: 1\n", encoding="utf-8")
            result = run_cli("init-config", str(target))
            self.assertEqual(result.returncode, 2)
            self.assertTrue(result.stderr.startswith("error: "), result.stderr)
            self.assertNotIn("Traceback", result.stderr)


class ReadmeSyntheticQuickstartTests(unittest.TestCase):
    """Run the README's API-key-free example end to end through the real CLI process."""

    def run_cli(self, *args):
        return run_cli(*args)

    def test_bootstrap_csv_then_convert_produces_a_valid_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "sample"
            csv_path = write_synthetic_daily_quotes_csv(
                sample / "daily_quotes.csv", symbols=["10010", "10020"], start="2026-06-01", end="2026-06-05",
            )
            config_path = sample / "config.yaml"
            config_path.write_text(
                f"storage:\n  parquet_dir: {sample / 'parquet'}\n  qlib_dir: {sample / 'qlib'}\n",
                encoding="utf-8",
            )

            bootstrap = self.run_cli("bootstrap-csv", "--config", str(config_path), "--csv", str(csv_path))
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
            convert = self.run_cli("convert", "--config", str(config_path))
            self.assertEqual(convert.returncode, 0, convert.stderr)

            qlib_dir = sample / "qlib"
            self.assertTrue((sample / "parquet" / "daily_quotes.parquet").is_file())
            for relative in (
                "calendars/day.txt", "instruments/all.txt", "dataset_manifest.json", "dataset_quality_report.json",
            ):
                with self.subTest(file=relative):
                    self.assertTrue((qlib_dir / relative).is_file())
            self.assertEqual(len((qlib_dir / "calendars" / "day.txt").read_text().split()), 5)
            instruments = (qlib_dir / "instruments" / "all.txt").read_text().splitlines()
            self.assertEqual(sorted(line.split("\t")[0] for line in instruments), ["10010", "10020"])
            for symbol in ("10010", "10020"):
                with self.subTest(factor_file=symbol):
                    self.assertTrue((qlib_dir / "features" / symbol / "factor.day.bin").is_file())
            manifest = json.loads((qlib_dir / "dataset_manifest.json").read_text(encoding="utf-8"))
            jsonschema.validate(manifest, load_schema("dataset_manifest"))
            report = json.loads((qlib_dir / "dataset_quality_report.json").read_text(encoding="utf-8"))
            jsonschema.validate(report, load_schema("dataset_quality_report"))


class PipelineCliLoggingTests(unittest.TestCase):
    def test_run_daily_progress_goes_to_stdout_and_import_attaches_no_handler(self):
        from jqqlib.pipeline import run_daily as real_run_daily

        self.assertEqual(logging.getLogger("jqqlib").handlers, [])
        self.assertEqual(logging.getLogger("jqqlib.pipeline").handlers, [])

        def wrapped_run_daily(config_path, as_of, max_lookback_business_days=10, **_kwargs):
            return real_run_daily(
                config_path,
                as_of,
                max_lookback_business_days=max_lookback_business_days,
                runner=lambda *_a, **_k: None,
                dataset_end_reader=lambda _: dt.date(2026, 6, 2),
            )

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("sys.argv", ["jqqlib", "run-daily", "--config", "config.yaml", "--as-of", "2026-06-05"]),
            patch("jqqlib.pipeline.run_daily", side_effect=wrapped_run_daily),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            main()

        self.assertIn("Catching up missing range 2026-06-03 to 2026-06-05.\n", stdout.getvalue())
        self.assertIn("Daily pipeline completed for 2026-06-05.\n", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(logging.getLogger("jqqlib").handlers, [])
        self.assertEqual(logging.getLogger("jqqlib.pipeline").handlers, [])

    def test_cli_logging_sends_warnings_to_stderr(self):
        from jqqlib.pipeline import _configure_cli_logging, _teardown_cli_logging

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            _configure_cli_logging()
            try:
                logging.getLogger("jqqlib.pipeline").info(
                    "Catching up missing range 2026-06-03 to 2026-06-05."
                )
                logging.getLogger("jqqlib.pipeline").warning("Warning: could not write the audit sidecar")
            finally:
                _teardown_cli_logging()
        self.assertEqual(stdout.getvalue(), "Catching up missing range 2026-06-03 to 2026-06-05.\n")
        self.assertEqual(stderr.getvalue(), "Warning: could not write the audit sidecar\n")
        self.assertEqual(logging.getLogger("jqqlib").handlers, [])

    def test_as_of_help_mentions_asia_tokyo(self):
        for command in ("run-daily", "audit"):
            with self.subTest(command=command):
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    patch("sys.argv", ["jqqlib", command, "--help"]),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    main()
                self.assertEqual(raised.exception.code, 0)
                self.assertIn("Asia/Tokyo", stdout.getvalue())
