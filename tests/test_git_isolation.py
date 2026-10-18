from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SPAWNS_GIT = re.compile(r'\[\s*"git"')


def _git(repo: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(repo), *args], env=env, check=True, capture_output=True, text=True,
    ).stdout


@unittest.skipUnless(shutil.which("git"), "git required")
class GitIsolationTests(unittest.TestCase):
    def test_every_test_module_that_spawns_git_scrubs_the_inherited_git_environment(self) -> None:
        spawning = [
            path.name for path in sorted(TESTS_DIR.glob("test_*.py"))
            if SPAWNS_GIT.search(path.read_text(encoding="utf-8"))
        ]
        self.assertTrue(spawning)
        for name in spawning:
            with self.subTest(module=name):
                self.assertIn(
                    "scrub_inherited_git_environment()", (TESTS_DIR / name).read_text(encoding="utf-8"),
                )

    def test_a_hook_exported_git_dir_does_not_redirect_a_test_repository_to_the_outer_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp) / "outer"
            outer.mkdir()
            (outer / "tracked.txt").write_text("outer", encoding="utf-8")
            for args in (
                ["init", "-q", "-b", "dev"],
                ["-c", "user.name=Outer", "-c", "user.email=o@example.com", "add", "-A"],
                ["-c", "user.name=Outer", "-c", "user.email=o@example.com", "commit", "-q", "-m", "outer"],
                ["worktree", "add", "-q", "-b", "feature", str(Path(tmp) / "linked")],
            ):
                _git(outer, *args)
            linked_git_dir = outer / ".git" / "worktrees" / "linked"
            protected = [outer / ".git" / "config", outer / ".git" / "index", linked_git_dir / "index"]
            bytes_before = {path: path.read_bytes() for path in protected}
            refs_before = _git(outer, "for-each-ref")
            scratch = Path(tmp) / "scratch"
            scratch.mkdir()
            (scratch / "f.txt").write_text("x", encoding="utf-8")
            child = textwrap.dedent(f"""\
                import subprocess, sys
                sys.path.insert(0, {str(TESTS_DIR.parent)!r})
                from tests.git_isolation import scrub_inherited_git_environment
                scrub_inherited_git_environment()
                for args in (["init", "-q"], ["config", "user.name", "Test"], ["config", "user.email", "t@example.com"],
                             ["add", "-A"], ["commit", "-q", "-m", "init"]):
                    subprocess.run(["git", "-C", {str(scratch)!r}, *args], check=True, capture_output=True)
                """)
            env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
            env.update(
                {
                    "GIT_DIR": str(linked_git_dir),
                    "GIT_INDEX_FILE": str(linked_git_dir / "index"),
                    "GIT_PREFIX": "",
                }
            )
            subprocess.run([sys.executable, "-c", child], env=env, check=True, capture_output=True)

            for path, before in bytes_before.items():
                with self.subTest(file=str(path.relative_to(outer))):
                    self.assertEqual(path.read_bytes(), before)
            self.assertEqual(_git(outer, "for-each-ref"), refs_before)
            self.assertEqual(_git(scratch, "log", "--format=%s"), "init\n")
