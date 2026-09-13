from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jqqlib.pipeline import main, run_publish_check


@contextlib.contextmanager
def _chdir(path: Path):
    prev = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _write_manifest(
    repo: Path,
    parquet_sha256: str,
    *,
    requested_start: str,
    requested_end: str,
    quality_status: str = "pass",
    report_status: str | None = None,
    dataset_id: str | None = None,
    report_dataset_id: str | None = None,
    warning_detail: str | None = None,
) -> None:
    # Mimic the real manifest: a stable content hash in lineage, plus volatile
    # per-run fields (source window + generated_at) that MUST NOT affect the gate.
    dataset_id = dataset_id or f"jqqlib-v2-{requested_start}{requested_end}"
    report_status = quality_status if report_status is None else report_status
    report_dataset_id = dataset_id if report_dataset_id is None else report_dataset_id
    manifest = {
        "schema_version": "2",
        "generated_at": f"2026-06-05T{requested_end[-2:]}:00:00Z",
        "dataset_id": dataset_id,
        "quality_status": quality_status,
        "lineage": {"parquet_sha256": parquet_sha256},
        "source": {"requested_start": requested_start, "requested_end": requested_end},
    }
    qlib_dir = repo / "data" / "qlib_jp"
    qlib_dir.mkdir(parents=True, exist_ok=True)
    (qlib_dir / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    findings = []
    severity_counts = {"fail": 0, "warning": 0, "info": 0}
    if warning_detail:
        findings.append(
            {
                "check": "ohlcv_all_null_rows",
                "severity": "warning",
                "count": 1,
                "detail": warning_detail,
            }
        )
        severity_counts["warning"] = 1
    if report_status == "fail":
        findings.append({"check": "quality_failure", "severity": "fail", "count": 1, "detail": "quality failed"})
        severity_counts["fail"] = 1
    report = {
        "schema_version": 2,
        "dataset_id": report_dataset_id,
        "status": report_status,
        "severity_counts": severity_counts,
        "findings": findings,
    }
    (qlib_dir / "dataset_quality_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def _write_artifacts_at(qlib_dir: Path, parquet_sha256: str, *, dataset_id: str = "jqqlib-v2-new") -> None:
    qlib_dir.mkdir(parents=True, exist_ok=True)
    (qlib_dir / "dataset_manifest.json").write_text(
        json.dumps(
            {
                "dataset_id": dataset_id,
                "quality_status": "pass",
                "lineage": {"parquet_sha256": parquet_sha256},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (qlib_dir / "dataset_quality_report.json").write_text(
        json.dumps(
            {
                "dataset_id": dataset_id,
                "status": "pass",
                "severity_counts": {"fail": 0, "warning": 0, "info": 0},
                "findings": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _run_publish_check_cli() -> tuple[int, str, str]:
    import io

    out = io.StringIO()
    err = io.StringIO()
    argv = sys.argv[:]
    sys.argv = ["jqqlib", "publish-check", "--config", "config.yaml"]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main()
            except SystemExit as exc:
                return int(exc.code), out.getvalue(), err.getvalue()
    finally:
        sys.argv = argv
    raise AssertionError("publish-check did not exit")


@unittest.skipUnless(shutil.which("git"), "git is required")
class PublishCheckGitTests(unittest.TestCase):
    # Seed the origin-backed worktree once. Each test copies that template so
    # git init/commit/push is not repeated for every gate case.
    _template: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls._template = Path(tempfile.mkdtemp(prefix="pubcheck_template_"))
        cls.addClassCleanup(shutil.rmtree, cls._template, ignore_errors=True)
        repo = cls._template / "work"
        bare = cls._template / "remote.git"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        _git(repo, "init")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "checkout", "-b", "main")
        _git(repo, "remote", "add", "origin", "../remote.git")
        (repo / "config.yaml").write_text(
            'storage:\n  parquet_dir: "./data/parquet"\n  qlib_dir: "./data/qlib_jp"\n',
            encoding="utf-8",
        )
        _write_manifest(repo, "HASH_AAA", requested_start="2026-06-01", requested_end="2026-06-04")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "initial dataset")
        _git(repo, "push", "-u", "origin", "main")

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="pubcheck_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.repo = Path(self._tmp) / "work"
        self.bare = Path(self._tmp) / "remote.git"
        shutil.copytree(self._template / "work", self.repo, symlinks=True)
        shutil.copytree(self._template / "remote.git", self.bare, symlinks=True)

    def test_git_ref_from_config_selects_the_compared_branch(self) -> None:
        # The remote also gets a `master` branch with a different committed hash;
        # publish_check.git_ref decides which one the gate compares against.
        _git(self.repo, "checkout", "-b", "master")
        _write_manifest(self.repo, "HASH_BBB", requested_start="2026-06-01", requested_end="2026-06-04")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "master dataset")
        _git(self.repo, "push", "-u", "origin", "master")
        (self.repo / "config.yaml").write_text(
            'storage:\n  parquet_dir: "./data/parquet"\n  qlib_dir: "./data/qlib_jp"\n'
            'publish_check:\n  git_ref: "origin/master"\n',
            encoding="utf-8",
        )
        with _chdir(self.repo):
            self.assertEqual(run_publish_check(Path("config.yaml")), 3)  # matches origin/master
            _write_manifest(self.repo, "HASH_AAA", requested_start="2026-06-01", requested_end="2026-06-04")
            self.assertEqual(run_publish_check(Path("config.yaml")), 0)  # differs from origin/master

    def test_identical_content_different_window_is_no_op(self) -> None:
        # Same content hash, DIFFERENT fetch window + generated_at (the case that
        # broke the dataset_id gate). Must be treated as a no-op (exit 3).
        _write_manifest(self.repo, "HASH_AAA", requested_start="2026-06-02", requested_end="2026-06-05")
        with _chdir(self.repo):
            self.assertEqual(run_publish_check(Path("config.yaml")), 3)

    def test_no_op_reads_origin_manifest_through_committed_provider_symlink(self) -> None:
        # `publish` points the provider path at a build directory with a symlink.
        # When that symlink is what origin/main tracks, the committed hash must
        # still be found through it, or every run would look like a first publish.
        target = self.repo / "data" / "builds" / "build-a" / "qlib"
        target.mkdir(parents=True)
        for name in ("dataset_manifest.json", "dataset_quality_report.json"):
            shutil.copy2(self.repo / "data" / "qlib_jp" / name, target / name)
        shutil.rmtree(self.repo / "data" / "qlib_jp")
        (self.repo / "data" / "qlib_jp").symlink_to("builds/build-a/qlib", target_is_directory=True)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "publish provider symlink")
        _git(self.repo, "push")
        with _chdir(self.repo):
            self.assertEqual(run_publish_check(Path("config.yaml")), 3)

    def test_changed_content_requires_publish(self) -> None:
        _write_manifest(self.repo, "HASH_BBB", requested_start="2026-06-02", requested_end="2026-06-05")
        with _chdir(self.repo):
            self.assertEqual(run_publish_check(Path("config.yaml")), 0)

    def test_missing_local_manifest_fails_closed(self) -> None:
        # No working-tree manifest at all -> error (exit 1), never a silent no-op.
        (self.repo / "config.yaml").write_text(
            'storage:\n  parquet_dir: "./data/parquet"\n  qlib_dir: "./data/empty_qlib"\n',
            encoding="utf-8",
        )
        (self.repo / "data" / "empty_qlib").mkdir(parents=True)
        with _chdir(self.repo):
            self.assertEqual(run_publish_check(Path("config.yaml")), 1)

    def test_absent_on_origin_is_first_publish(self) -> None:
        # Remove the manifest from origin's view by pointing config at a fresh path.
        (self.repo / "config.yaml").write_text(
            'storage:\n  parquet_dir: "./data/parquet"\n  qlib_dir: "./data/qlib_jp2"\n',
            encoding="utf-8",
        )
        _write_artifacts_at(self.repo / "data" / "qlib_jp2", "HASH_NEW")
        with _chdir(self.repo):
            self.assertEqual(run_publish_check(Path("config.yaml")), 0)

    def test_quality_report_fail_blocks_publish(self) -> None:
        _write_manifest(
            self.repo,
            "HASH_BBB",
            requested_start="2026-06-02",
            requested_end="2026-06-05",
            quality_status="fail",
            report_status="fail",
        )
        with _chdir(self.repo):
            self.assertEqual(run_publish_check(Path("config.yaml")), 1)

    def test_manifest_report_status_mismatch_blocks_publish(self) -> None:
        _write_manifest(
            self.repo,
            "HASH_BBB",
            requested_start="2026-06-02",
            requested_end="2026-06-05",
            quality_status="pass",
            report_status="warning",
        )
        with _chdir(self.repo):
            self.assertEqual(run_publish_check(Path("config.yaml")), 1)

    def test_manifest_report_dataset_id_mismatch_blocks_publish(self) -> None:
        _write_manifest(
            self.repo,
            "HASH_BBB",
            requested_start="2026-06-02",
            requested_end="2026-06-05",
            report_dataset_id="jqqlib-v2-other",
        )
        with _chdir(self.repo):
            self.assertEqual(run_publish_check(Path("config.yaml")), 1)

    def test_cli_stdout_unchanged_for_changed_dataset_and_warning_goes_to_stderr(self) -> None:
        _write_manifest(
            self.repo,
            "HASH_BBB",
            requested_start="2026-06-02",
            requested_end="2026-06-05",
            quality_status="warning",
            warning_detail="Rows with all OHLCV fields null: 1",
        )
        with _chdir(self.repo):
            rc, stdout, stderr = _run_publish_check_cli()

        self.assertEqual(rc, 0)
        self.assertEqual(stdout, "Dataset content changed vs origin/main; publish required.\n")
        self.assertEqual(stderr, "quality warning: count=1; first=Rows with all OHLCV fields null: 1\n")

    def test_cli_stdout_unchanged_for_no_op_dataset(self) -> None:
        _write_manifest(self.repo, "HASH_AAA", requested_start="2026-06-02", requested_end="2026-06-05")
        with _chdir(self.repo):
            rc, stdout, stderr = _run_publish_check_cli()

        self.assertEqual(rc, 3)
        self.assertEqual(stdout, "No dataset content change vs origin/main; skipping publish.\n")
        self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
