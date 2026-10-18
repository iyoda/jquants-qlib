from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jqqlib
from jqqlib import ledger, pipeline
from jqqlib.ledger import (
    LEDGER_SCHEMA_VERSION,
    LedgerError,
    append_ledger_entry,
    init_ledger,
    jqqlib_source_commit,
    ledger_entry_from_manifest,
    read_ledger,
)
from jqqlib.pipeline import evaluate_publish_check, main
from tests.git_isolation import scrub_inherited_git_environment

scrub_inherited_git_environment()

POLICY = {"mode": "vendor_factor", "price_fields": ["open", "close"]}


def _manifest(parquet_sha256: str = "a" * 64, *, policy: dict | None = None) -> dict:
    return {
        "build_id": "20261015T103541Z-9d275b6ec5c5fd1e",
        "dataset_id": "jqqlib-v2-9d275b6ec5c5fd1e",
        "generated_at": "2026-10-15T10:35:41Z",
        "quality_status": "warning",
        "dataset": {"date_start": "2021-03-01", "date_end": "2026-10-15"},
        "lineage": {"parquet_sha256": parquet_sha256},
        "adjustment_policy": POLICY if policy is None else policy,
    }


def _entry(parquet_sha256: str = "a" * 64, *, policy: dict | None = None) -> dict:
    return ledger_entry_from_manifest(
        _manifest(parquet_sha256, policy=policy),
        jqqlib_version="0.1.0",
        jqqlib_source_commit="c" * 40,
    )


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="ledger_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.path = self._tmp / "state" / "publish_ledger.jsonl"


class ReadLedgerTests(LedgerTestCase):
    def test_missing_ledger_is_an_error_not_an_empty_baseline(self) -> None:
        with self.assertRaisesRegex(LedgerError, "publish ledger missing"):
            read_ledger(self.path)

    def test_empty_ledger_reads_as_no_entries(self) -> None:
        init_ledger(self.path)
        self.assertEqual(read_ledger(self.path), [])

    def test_ledger_without_a_trailing_newline_is_a_truncated_write(self) -> None:
        init_ledger(self.path, _entry())
        self.path.write_bytes(self.path.read_bytes().rstrip(b"\n"))
        with self.assertRaisesRegex(LedgerError, "does not end with a newline"):
            read_ledger(self.path)

    def test_unparsable_or_blank_line_anywhere_is_an_error(self) -> None:
        good = json.dumps(_entry())
        for body in (f"{good[:-5]}\n{good}\n", f"{good}\n\n{good}\n", "[]\n"):
            with self.subTest(body=body[:20]):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(body, encoding="utf-8")
                with self.assertRaisesRegex(LedgerError, "line [0-9]+"):
                    read_ledger(self.path)

    def test_unknown_schema_version_or_kind_is_an_error(self) -> None:
        for override in ({"schema_version": LEDGER_SCHEMA_VERSION + 1}, {"schema_version": "1"}, {"kind": "backup"}):
            with self.subTest(override=override):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({**_entry(), **override}) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(LedgerError, "line 1"):
                    read_ledger(self.path)

    def test_entry_without_parquet_sha256_or_with_a_non_object_policy_is_an_error(self) -> None:
        for override in ({"parquet_sha256": None}, {"parquet_sha256": ""}, {"adjustment_policy": "vendor_factor"}):
            with self.subTest(override=override):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({**_entry(), **override}) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(LedgerError, "line 1"):
                    read_ledger(self.path)

    def test_unreadable_ledger_is_an_error(self) -> None:
        self.path.mkdir(parents=True)
        with self.assertRaisesRegex(LedgerError, "publish ledger unreadable"):
            read_ledger(self.path)

    def test_entry_missing_any_field_or_with_a_wrong_type_is_an_error(self) -> None:
        entry = _entry()
        broken = [{key: value for key, value in entry.items() if key != missing} for missing in entry]
        broken += [
            {**entry, "adjustment_policy": None},
            {**entry, "recorded_at": 20261016},
            {**entry, "jqqlib_version": 1},
            {**entry, "date_end": ["2026-10-15"]},
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for row in broken:
            with self.subTest(keys=sorted(row)[:3], row=str(row)[:60]):
                self.path.write_text(json.dumps(row) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(LedgerError, "line 1"):
                    read_ledger(self.path)

    def test_symlinked_ledger_is_refused_so_reads_writes_and_the_lock_cannot_diverge(self) -> None:
        real = self._tmp / "real.jsonl"
        init_ledger(real, _entry())
        self.path.parent.mkdir(parents=True)
        self.path.symlink_to(real)
        before = real.read_bytes()
        with self.assertRaisesRegex(LedgerError, "symlink"):
            read_ledger(self.path)
        with self.assertRaisesRegex(LedgerError, "symlink"):
            append_ledger_entry(self.path, _entry("b" * 64))
        with self.assertRaises((LedgerError, FileExistsError)):
            init_ledger(self.path)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(real.read_bytes(), before)

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unreadable_ledger_location_is_a_ledger_error_not_a_user_error(self) -> None:
        init_ledger(self.path)
        self.path.parent.chmod(0)
        self.addCleanup(self.path.parent.chmod, 0o700)
        with self.assertRaisesRegex(LedgerError, "publish ledger unreadable"):
            read_ledger(self.path)

    def test_failed_ledger_write_is_a_ledger_error_and_keeps_the_previous_ledger(self) -> None:
        init_ledger(self.path, _entry("a" * 64))
        before = self.path.read_bytes()
        with mock.patch.object(ledger.os, "replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaisesRegex(LedgerError, "cannot write the publish ledger"):
                append_ledger_entry(self.path, _entry("b" * 64))
        self.assertEqual(self.path.read_bytes(), before)

    def test_init_write_failure_even_a_temporary_file_collision_is_a_ledger_error(self) -> None:
        for failure in (FileExistsError(17, "File exists"), OSError(28, "No space left on device")):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(ledger, "_replace_atomically", side_effect=failure):
                    with self.assertRaisesRegex(LedgerError, "cannot write the publish ledger"):
                        init_ledger(self.path)
                self.assertFalse(self.path.exists())

    def test_directory_fsync_failure_after_the_rename_keeps_the_new_ledger_and_a_rerun_is_a_no_op(self) -> None:
        init_ledger(self.path, _entry("a" * 64))
        real_fsync = ledger.os.fsync

        def fail_on_the_directory(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(5, "Input/output error")
            real_fsync(fd)

        with mock.patch.object(ledger.os, "fsync", side_effect=fail_on_the_directory):
            with self.assertRaisesRegex(LedgerError, "cannot write the publish ledger"):
                append_ledger_entry(self.path, _entry("b" * 64))
        self.assertEqual([row["parquet_sha256"][0] for row in read_ledger(self.path)], ["a", "b"])
        self.assertFalse(append_ledger_entry(self.path, _entry("b" * 64)))
        self.assertEqual(len(read_ledger(self.path)), 2)


class LedgerEntryTests(unittest.TestCase):
    def test_entry_carries_the_manifest_identity_and_the_producer_versions(self) -> None:
        entry = ledger_entry_from_manifest(
            _manifest(), jqqlib_version="0.1.0", jqqlib_source_commit=None, recorded_at="2026-10-16T10:00:00Z"
        )
        self.assertEqual(
            entry,
            {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "kind": "publish",
                "recorded_at": "2026-10-16T10:00:00Z",
                "build_id": "20261015T103541Z-9d275b6ec5c5fd1e",
                "dataset_id": "jqqlib-v2-9d275b6ec5c5fd1e",
                "generated_at": "2026-10-15T10:35:41Z",
                "date_end": "2026-10-15",
                "quality_status": "warning",
                "parquet_sha256": "a" * 64,
                "adjustment_policy": POLICY,
                "jqqlib_version": "0.1.0",
                "jqqlib_source_commit": None,
            },
        )

    def test_manifest_without_parquet_sha256_cannot_become_an_entry(self) -> None:
        manifest = _manifest()
        manifest["lineage"] = {}
        with self.assertRaisesRegex(LedgerError, "lineage.parquet_sha256"):
            ledger_entry_from_manifest(manifest, jqqlib_version=None, jqqlib_source_commit=None)


class InitLedgerTests(LedgerTestCase):
    def test_init_creates_the_parent_directory_and_refuses_an_existing_ledger(self) -> None:
        init_ledger(self.path)
        self.assertEqual(self.path.read_bytes(), b"")
        with self.assertRaises(FileExistsError):
            init_ledger(self.path, _entry())
        self.assertEqual(self.path.read_bytes(), b"")

    def test_init_with_an_entry_seeds_exactly_one_line(self) -> None:
        entry = _entry()
        init_ledger(self.path, entry)
        self.assertEqual(read_ledger(self.path), [entry])
        self.assertEqual(self.path.read_text(encoding="utf-8").count("\n"), 1)


class AppendLedgerEntryTests(LedgerTestCase):
    def test_append_adds_one_line_per_changed_dataset(self) -> None:
        init_ledger(self.path, _entry("a" * 64))
        self.assertTrue(append_ledger_entry(self.path, _entry("b" * 64)))
        self.assertTrue(append_ledger_entry(self.path, _entry("b" * 64, policy={"mode": "none"})))
        self.assertEqual(
            [(row["parquet_sha256"][0], row["adjustment_policy"]) for row in read_ledger(self.path)],
            [("a", POLICY), ("b", POLICY), ("b", {"mode": "none"})],
        )

    def test_rerun_after_a_completed_append_does_not_duplicate_the_last_entry(self) -> None:
        init_ledger(self.path)
        self.assertTrue(append_ledger_entry(self.path, _entry()))
        before = self.path.read_bytes()
        self.assertFalse(append_ledger_entry(self.path, _entry()))
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_different_producer_version_alone_is_the_same_dataset(self) -> None:
        init_ledger(self.path, _entry())
        rebuilt_by_other_jqqlib = {**_entry(), "jqqlib_version": "9.9.9", "jqqlib_source_commit": None}
        self.assertFalse(append_ledger_entry(self.path, rebuilt_by_other_jqqlib))
        self.assertEqual(len(read_ledger(self.path)), 1)

    def test_append_reads_and_rewrites_while_holding_the_exclusive_lock(self) -> None:
        init_ledger(self.path, _entry("a" * 64))
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        probes = []
        real_read = ledger.read_ledger

        def read_while_probing_the_lock(path: Path) -> list:
            with lock_path.open("a") as other:
                try:
                    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probes.append("held")
                else:
                    fcntl.flock(other.fileno(), fcntl.LOCK_UN)
                    probes.append("free")
            return real_read(path)

        with mock.patch.object(ledger, "read_ledger", side_effect=read_while_probing_the_lock):
            self.assertTrue(append_ledger_entry(self.path, _entry("b" * 64)))
        self.assertEqual(probes, ["held"])

    def test_append_replaces_the_ledger_while_holding_the_exclusive_lock(self) -> None:
        init_ledger(self.path, _entry("a" * 64))
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        probes = []
        real_replace = ledger.os.replace

        def replace_while_probing_the_lock(source: str, destination: str) -> None:
            with lock_path.open("a") as other:
                try:
                    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probes.append("held")
                else:
                    fcntl.flock(other.fileno(), fcntl.LOCK_UN)
                    probes.append("free")
            real_replace(source, destination)

        with mock.patch.object(ledger.os, "replace", side_effect=replace_while_probing_the_lock):
            self.assertTrue(append_ledger_entry(self.path, _entry("b" * 64)))
        self.assertEqual(probes, ["held"])

    def test_interrupted_append_leaves_the_previous_ledger_and_no_temporary_file(self) -> None:
        init_ledger(self.path, _entry("a" * 64))
        before = self.path.read_bytes()
        with mock.patch.object(ledger.os, "replace", side_effect=KeyboardInterrupt), self.assertRaises(
            KeyboardInterrupt
        ):
            append_ledger_entry(self.path, _entry("b" * 64))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            sorted(p.name for p in self.path.parent.iterdir()), sorted([self.path.name, f"{self.path.name}.lock"])
        )
        self.assertTrue(append_ledger_entry(self.path, _entry("b" * 64)))
        self.assertEqual(len(read_ledger(self.path)), 2)

    def test_append_to_a_missing_or_corrupt_ledger_is_refused(self) -> None:
        with self.assertRaisesRegex(LedgerError, "publish ledger missing"):
            append_ledger_entry(self.path, _entry())
        self.assertFalse(self.path.exists())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json\n", encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "line 1"):
            append_ledger_entry(self.path, _entry())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json\n")


@unittest.skipUnless(shutil.which("git"), "git is required")
class SourceCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="ledger_git_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _repo_with(self, relative: str) -> tuple[Path, str]:
        repo = self._tmp / "repo"
        package = repo / relative
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "Test"],
            ["add", "-A"],
            ["commit", "-q", "-m", "init"],
        ):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        return package, head

    def test_commit_is_recorded_for_a_jquants_qlib_source_tree(self) -> None:
        package, head = self._repo_with("src/jqqlib")
        self.assertEqual(jqqlib_source_commit(package), head)

    def test_installed_package_inside_another_repository_records_no_commit(self) -> None:
        package, _ = self._repo_with(".venv/lib/python3.12/site-packages/jqqlib")
        self.assertIsNone(jqqlib_source_commit(package))

    def test_package_outside_any_repository_records_no_commit(self) -> None:
        package = self._tmp / "site-packages" / "jqqlib"
        package.mkdir(parents=True)
        self.assertIsNone(jqqlib_source_commit(package))

    def test_missing_git_binary_records_no_commit(self) -> None:
        package, _ = self._repo_with("src/jqqlib")
        with mock.patch.object(ledger.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(jqqlib_source_commit(package))

    def test_inherited_git_repository_variables_do_not_change_the_recorded_commit(self) -> None:
        package, head = self._repo_with("src/jqqlib")
        other = self._tmp / "other"
        other.mkdir()
        subprocess.run(["git", "-C", str(other), "init", "-q"], check=True, capture_output=True)
        inherited = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other), "GIT_INDEX_FILE": "/nonexistent"}
        with mock.patch.dict(os.environ, inherited):
            self.assertEqual(jqqlib_source_commit(package), head)

    def test_git_configuration_inherited_from_the_environment_still_applies(self) -> None:
        package, head = self._repo_with("src/jqqlib")
        foreign_owner = {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
        probe = subprocess.run(
            ["git", "-C", str(package), "rev-parse", "HEAD"],
            capture_output=True,
            env={**os.environ, **foreign_owner},
        )
        if probe.returncode == 0:
            self.skipTest("this git does not honour GIT_TEST_ASSUME_DIFFERENT_OWNER")
        trusted_by_environment = {
            **foreign_owner,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        }
        with mock.patch.dict(os.environ, foreign_owner):
            self.assertIsNone(jqqlib_source_commit(package))
        with mock.patch.dict(os.environ, trusted_by_environment):
            self.assertEqual(jqqlib_source_commit(package), head)


@contextlib.contextmanager
def _chdir(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _run_cli(*args: str) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    argv = sys.argv[:]
    sys.argv = ["jqqlib", *args]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main()
            except SystemExit as exc:
                return int(exc.code or 0), out.getvalue(), err.getvalue()
    finally:
        sys.argv = argv
    return 0, out.getvalue(), err.getvalue()


class FileBaselineTestCase(unittest.TestCase):
    LEDGER = "./data/state/publish_ledger.jsonl"

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ledger_baseline_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._cwd = _chdir(self.root)
        self._cwd.__enter__()
        self.addCleanup(self._cwd.__exit__, None, None, None)
        self.config = self.root / "config.yaml"
        self.config.write_text(
            'storage:\n  parquet_dir: "./data/parquet"\n  qlib_dir: "./data/qlib_jp"\n'
            f'publish_check:\n  baseline: file\n  ledger_path: "{self.LEDGER}"\n',
            encoding="utf-8",
        )
        self.ledger = self.root / self.LEDGER
        self.write_dataset("a" * 64)

    def write_dataset(
        self, parquet_sha256: str, *, policy: dict | None = None, status: str = "pass", report_status: str | None = None
    ) -> dict:
        manifest = {**_manifest(parquet_sha256, policy=policy), "quality_status": status}
        qlib_dir = self.root / "data" / "qlib_jp"
        qlib_dir.mkdir(parents=True, exist_ok=True)
        (qlib_dir / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        report = {
            "dataset_id": manifest["dataset_id"],
            "status": status if report_status is None else report_status,
            "severity_counts": {"fail": 0, "warning": 0, "info": 0},
            "findings": [],
        }
        (qlib_dir / "dataset_quality_report.json").write_text(json.dumps(report), encoding="utf-8")
        return manifest

    def seed(self, parquet_sha256: str | None = "a" * 64, *, policy: dict | None = None) -> None:
        init_ledger(self.ledger, None if parquet_sha256 is None else _entry(parquet_sha256, policy=policy))


class PublishCheckFileBaselineTests(FileBaselineTestCase):
    def test_unchanged_dataset_is_a_no_op_without_any_git_work_tree(self) -> None:
        self.seed("a" * 64)
        with mock.patch.object(pipeline, "_git", side_effect=AssertionError("file baseline must not call git")):
            decision = evaluate_publish_check(self.config)
        self.assertEqual((decision.returncode, decision.reason), (3, "unchanged"))
        self.assertEqual(decision.committed_hash, "a" * 64)

    def test_changed_parquet_or_adjustment_policy_requires_publish(self) -> None:
        self.seed("a" * 64)
        self.write_dataset("b" * 64)
        self.assertEqual(evaluate_publish_check(self.config).returncode, 0)
        self.write_dataset("a" * 64, policy={"mode": "none"})
        self.assertEqual(evaluate_publish_check(self.config).returncode, 0)

    def test_baseline_is_the_last_entry_not_any_earlier_one(self) -> None:
        self.seed("b" * 64)
        append_ledger_entry(self.ledger, _entry("a" * 64))
        self.assertEqual(evaluate_publish_check(self.config).returncode, 3)
        self.write_dataset("b" * 64)
        self.assertEqual(evaluate_publish_check(self.config).returncode, 0)

    def test_initialised_empty_ledger_is_a_first_publish(self) -> None:
        self.seed(None)
        decision = evaluate_publish_check(self.config)
        self.assertEqual((decision.returncode, decision.committed_hash), (0, None))

    def test_missing_ledger_fails_closed_instead_of_a_first_publish(self) -> None:
        decision = evaluate_publish_check(self.config)
        self.assertEqual(decision.returncode, 1)
        self.assertIn("publish ledger missing", decision.reason)

    def test_error_decision_names_the_configured_baseline(self) -> None:
        decision = evaluate_publish_check(self.config)
        self.assertEqual((decision.returncode, decision.baseline), (1, "file"))
        self.assertEqual(decision.ledger_path, str(Path(self.LEDGER)))
        self.write_dataset("a" * 64, status="fail")
        decision = evaluate_publish_check(self.config)
        self.assertEqual((decision.returncode, decision.baseline), (1, "file"))

    def test_git_baseline_never_falls_back_to_a_matching_ledger(self) -> None:
        self.seed("a" * 64)
        self.config.write_text(
            'storage:\n  parquet_dir: "./data/parquet"\n  qlib_dir: "./data/qlib_jp"\n'
            f'publish_check:\n  baseline: git\n  ledger_path: "{self.LEDGER}"\n',
            encoding="utf-8",
        )
        decision = evaluate_publish_check(self.config)
        self.assertEqual((decision.returncode, decision.baseline), (1, "git"))
        self.assertIn("cannot read the committed manifest", decision.reason)

    def test_damaged_ledger_fails_closed(self) -> None:
        self.seed("a" * 64)
        self.ledger.write_bytes(self.ledger.read_bytes()[:-10])
        decision = evaluate_publish_check(self.config)
        self.assertEqual(decision.returncode, 1)
        self.assertIn("publish ledger", decision.reason)

    def test_cli_names_the_ledger_it_compared_against(self) -> None:
        self.seed("a" * 64)
        rc, out, _ = _run_cli("publish-check", "--config", "config.yaml")
        self.assertEqual(rc, 3)
        self.assertEqual(out, f"No dataset content change vs ledger {Path(self.LEDGER)}; skipping publish.\n")
        self.write_dataset("b" * 64)
        rc, out, _ = _run_cli("publish-check", "--config", "config.yaml")
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"Dataset content changed vs ledger {Path(self.LEDGER)}; publish required.\n")


class LedgerCliTests(FileBaselineTestCase):
    def test_init_creates_an_empty_ledger_once(self) -> None:
        rc, out, _ = _run_cli("ledger", "init", "--config", "config.yaml")
        self.assertEqual((rc, out), (0, f"Publish ledger initialised (empty): {Path(self.LEDGER)}\n"))
        self.assertEqual(read_ledger(self.ledger), [])
        rc, _, err = _run_cli("ledger", "init", "--config", "config.yaml")
        self.assertEqual(rc, 2)
        self.assertIn("already exists", err)

    def test_init_from_a_manifest_seeds_the_baseline_without_producer_versions(self) -> None:
        seed = self.root / "origin_manifest.json"
        seed.write_text(json.dumps(_manifest("a" * 64)), encoding="utf-8")
        self.write_dataset("b" * 64)
        rc, out, _ = _run_cli("ledger", "init", "--config", "config.yaml", "--from-manifest", str(seed))
        self.assertEqual(rc, 0)
        self.assertIn(f"seeded from {seed}", out)
        [entry] = read_ledger(self.ledger)
        self.assertEqual(
            (entry["parquet_sha256"], entry["jqqlib_version"], entry["jqqlib_source_commit"]), ("a" * 64, None, None)
        )
        self.assertEqual(evaluate_publish_check(self.config).returncode, 0)

    def test_init_from_an_unusable_manifest_is_a_user_error_and_creates_no_ledger(self) -> None:
        missing = self.root / "missing.json"
        not_json = self.root / "not_json.json"
        not_json.write_text("{", encoding="utf-8")
        not_object = self.root / "not_object.json"
        not_object.write_text("[]", encoding="utf-8")
        no_hash = self.root / "no_hash.json"
        no_hash.write_text(json.dumps({"lineage": {}}), encoding="utf-8")
        for path in (missing, not_json, not_object, no_hash):
            with self.subTest(path=path.name):
                rc, _, err = _run_cli("ledger", "init", "--config", "config.yaml", "--from-manifest", str(path))
                self.assertEqual(rc, 2)
                self.assertTrue(err.startswith("error: "))
                self.assertFalse(self.ledger.exists())

    def test_append_records_the_current_dataset_and_the_running_jqqlib(self) -> None:
        self.seed(None)
        rc, out, _ = _run_cli("ledger", "append", "--config", "config.yaml")
        self.assertEqual((rc, out), (0, f"Publish ledger entry appended: {Path(self.LEDGER)}\n"))
        [entry] = read_ledger(self.ledger)
        self.assertEqual(entry["parquet_sha256"], "a" * 64)
        self.assertEqual(entry["quality_status"], "pass")
        self.assertEqual(entry["jqqlib_version"], jqqlib.__version__)
        self.assertEqual(entry["jqqlib_source_commit"], jqqlib_source_commit())
        self.assertEqual(evaluate_publish_check(self.config).returncode, 3)

    def test_append_rerun_after_success_appends_nothing_and_succeeds(self) -> None:
        self.seed(None)
        self.assertEqual(_run_cli("ledger", "append", "--config", "config.yaml")[0], 0)
        rc, out, _ = _run_cli("ledger", "append", "--config", "config.yaml")
        self.assertEqual((rc, out), (0, f"Publish ledger already records this dataset: {Path(self.LEDGER)}\n"))
        self.assertEqual(len(read_ledger(self.ledger)), 1)

    def test_append_refuses_a_dataset_that_publish_check_would_refuse(self) -> None:
        self.seed(None)
        for status, report_status in (("fail", None), ("pass", "warning")):
            with self.subTest(status=status, report_status=report_status):
                self.write_dataset("b" * 64, status=status, report_status=report_status)
                rc, _, err = _run_cli("ledger", "append", "--config", "config.yaml")
                self.assertEqual(rc, 1)
                self.assertTrue(err.startswith("error: "))
                self.assertEqual(read_ledger(self.ledger), [])

    def test_append_without_an_initialised_ledger_fails(self) -> None:
        rc, _, err = _run_cli("ledger", "append", "--config", "config.yaml")
        self.assertEqual(rc, 1)
        self.assertIn("publish ledger missing", err)
        self.assertFalse(self.ledger.exists())

    def test_ledger_commands_without_a_ledger_path_are_configuration_errors(self) -> None:
        self.config.write_text(
            'storage:\n  parquet_dir: "./data/parquet"\n  qlib_dir: "./data/qlib_jp"\n', encoding="utf-8"
        )
        for action in ("init", "append"):
            with self.subTest(action=action):
                rc, _, err = _run_cli("ledger", action, "--config", "config.yaml")
                self.assertEqual(rc, 2)
                self.assertIn("publish_check.ledger_path is required", err)


if __name__ == "__main__":
    unittest.main()
