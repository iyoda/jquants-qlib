from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

import jsonschema

from jqqlib.contracts import load_schema
from jqqlib.pipeline import _PROVIDER_WRITTEN_BY_COMMAND, build_parser, run_bootstrap_csv
from jqqlib.provider_lock import ProviderLockedError, provider_lock_path, provider_read_lock, provider_write_lock
from tests.support.synthetic import write_synthetic_daily_quotes_csv

PROVIDER_LOCKED_EXIT_CODE = 75

_LOCK_HOLDER = """
import sys
from pathlib import Path
from jqqlib.provider_lock import provider_read_lock, provider_write_lock
lock = {"write": provider_write_lock, "read": provider_read_lock}[sys.argv[1]]
with lock(Path(sys.argv[2])):
    print("held", flush=True)
    sys.stdin.read()
"""


@contextlib.contextmanager
def lock_held_by_another_process(kind: str, provider: Path) -> Iterator[None]:
    with subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, kind, str(provider)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    ) as holder:
        assert holder.stdin is not None and holder.stdout is not None
        try:
            if holder.stdout.readline() != "held\n":
                raise AssertionError(f"lock holder did not take the {kind} lock of {provider}")
            yield
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)


def run_jqqlib(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "jqqlib", *args], capture_output=True, text=True, check=False, timeout=120,
    )


def tree_bytes(*roots: Path) -> dict[str, bytes]:
    return {
        str(path): path.read_bytes()
        for root in roots
        for path in sorted([root, *root.rglob("*")] if root.is_dir() else [root])
        if path.is_file()
    }


class ProviderLockCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.qlib_dir = root / "qlib"
        self.parquet_dir = root / "parquet"
        self.ledger_path = root / "state" / "publish_ledger.jsonl"
        self.csv_path = write_synthetic_daily_quotes_csv(
            root / "daily_quotes.csv", symbols=["10010", "10020"], start="2026-06-01", end="2026-06-05",
        )
        self.config_path = root / "config.yaml"
        self.config_path.write_text(
            "storage:\n"
            f"  parquet_dir: {self.parquet_dir}\n"
            f"  qlib_dir: {self.qlib_dir}\n"
            f"  audit_dir: {root / 'audit'}\n"
            "publish_check:\n"
            "  baseline: file\n"
            f"  ledger_path: {self.ledger_path}\n",
            encoding="utf-8",
        )
        run_bootstrap_csv(self.config_path, self.csv_path)
        self.config = str(self.config_path)

    def provider_bytes(self) -> dict[str, bytes]:
        return tree_bytes(self.qlib_dir, self.parquet_dir)

    def assert_refused_as_locked(self, result: subprocess.CompletedProcess[str], provider: Path) -> None:
        self.assertEqual(result.returncode, PROVIDER_LOCKED_EXIT_CODE, result.stderr)
        self.assertIn("error:", result.stderr)
        self.assertIn(str(provider_lock_path(provider)), result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_writer_commands_fail_fast_and_leave_the_provider_unchanged_while_another_process_holds_the_lock(self):
        daily_result = Path(self._tmp.name) / "run-daily.result.json"
        writer_commands = {
            "convert": ("convert", "--config", self.config),
            "bootstrap-csv": ("bootstrap-csv", "--config", self.config, "--csv", str(self.csv_path)),
            "run-etl": ("run-etl", "--config", self.config, "--start", "2026-06-08", "--end", "2026-06-08"),
            "run-all": ("run-all", "--config", self.config, "--start", "2026-06-08", "--end", "2026-06-08"),
            "run-daily": (
                "run-daily", "--config", self.config, "--as-of", "2026-06-08", "--result-json", str(daily_result),
            ),
        }
        before = self.provider_bytes()
        with lock_held_by_another_process("write", self.qlib_dir):
            for name, command in writer_commands.items():
                with self.subTest(command=name):
                    self.assert_refused_as_locked(run_jqqlib(*command), self.qlib_dir)
                    self.assertEqual(self.provider_bytes(), before)
        result = json.loads(daily_result.read_text(encoding="utf-8"))
        jsonschema.validate(result, load_schema("cli_result"))
        self.assertEqual((result["outcome"], result["exit_code"]), ("failed", PROVIDER_LOCKED_EXIT_CODE))

    def test_writer_command_runs_once_the_other_process_releases_the_lock(self):
        with lock_held_by_another_process("write", self.qlib_dir):
            self.assertEqual(run_jqqlib("convert", "--config", self.config).returncode, PROVIDER_LOCKED_EXIT_CODE)
        convert = run_jqqlib("convert", "--config", self.config)
        self.assertEqual(convert.returncode, 0, convert.stderr)

    def test_reader_lock_keeps_writer_commands_out_of_the_provider(self):
        before = self.provider_bytes()
        with lock_held_by_another_process("read", self.qlib_dir):
            self.assert_refused_as_locked(run_jqqlib("convert", "--config", self.config), self.qlib_dir)
            self.assertEqual(self.provider_bytes(), before)
            with self.assertRaises(ProviderLockedError), provider_write_lock(self.qlib_dir):
                self.fail("a writer took the lock while a reader held it")

    def test_readers_share_the_lock_and_together_still_keep_a_writer_out(self):
        before = self.provider_bytes()
        with lock_held_by_another_process("read", self.qlib_dir), provider_read_lock(self.qlib_dir):
            self.assert_refused_as_locked(run_jqqlib("convert", "--config", self.config), self.qlib_dir)
        self.assertEqual(self.provider_bytes(), before)

    def test_reader_lock_fails_fast_while_a_writer_holds_the_lock(self):
        with lock_held_by_another_process("write", self.qlib_dir):
            with self.assertRaises(ProviderLockedError), provider_read_lock(self.qlib_dir):
                self.fail("a reader took the lock while a writer held it")

    def test_read_only_commands_run_while_a_writer_holds_the_lock(self):
        ledger = run_jqqlib("ledger", "init", "--config", self.config)
        self.assertEqual(ledger.returncode, 0, ledger.stderr)
        with lock_held_by_another_process("write", self.qlib_dir):
            validate = run_jqqlib("validate", "--config", self.config)
            self.assertEqual(validate.returncode, 0, validate.stderr)
            publish_check = run_jqqlib("publish-check", "--config", self.config)
            self.assertEqual(publish_check.returncode, 0, publish_check.stderr)
            publish_paths = run_jqqlib("publish-paths", "--config", self.config)
            self.assertEqual(publish_paths.returncode, 0, publish_paths.stderr)
            self.assertIn(str(self.qlib_dir / "dataset_manifest.json"), publish_paths.stdout.splitlines())

    def test_publish_fails_fast_while_the_provider_symlink_is_locked(self):
        root = Path(self._tmp.name)
        first_build, second_build = root / "builds" / "first", root / "builds" / "second"
        first_build.mkdir(parents=True)
        second_build.mkdir()
        provider = root / "current"
        provider.symlink_to(first_build, target_is_directory=True)
        publish = ("publish", "--build-provider-uri", str(second_build), "--provider-uri", str(provider))
        with lock_held_by_another_process("write", provider):
            self.assert_refused_as_locked(run_jqqlib(*publish), provider)
            self.assertEqual(provider.resolve(), first_build.resolve())
        published = run_jqqlib(*publish)
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertEqual(provider.resolve(), second_build.resolve())

    def test_lock_file_sits_beside_the_provider_so_a_copy_of_the_tree_never_contains_it(self):
        convert = run_jqqlib("convert", "--config", self.config)
        self.assertEqual(convert.returncode, 0, convert.stderr)
        self.assertEqual(provider_lock_path(self.qlib_dir), self.qlib_dir.parent.resolve() / ".qlib.write.lock")
        self.assertTrue(provider_lock_path(self.qlib_dir).is_file())
        self.assertEqual([path for path in self.qlib_dir.rglob("*") if "lock" in path.name], [])


class ProviderWriterRegistryTests(unittest.TestCase):
    def test_every_subcommand_declares_whether_it_writes_a_provider(self):
        subcommands = next(
            action for action in build_parser()._actions if isinstance(action, argparse._SubParsersAction)
        )
        self.assertEqual(set(_PROVIDER_WRITTEN_BY_COMMAND), set(subcommands.choices))


if __name__ == "__main__":
    unittest.main()
