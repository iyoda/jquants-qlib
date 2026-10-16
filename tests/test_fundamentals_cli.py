from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from jqqlib.fundamentals_pipeline import main


class FundamentalsCliTests(unittest.TestCase):
    def invoke(self, *args: str) -> tuple[int | str | None, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("sys.argv", ["fundamentals_pipeline", *args]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                main()
            except SystemExit as exc:
                return exc.code, stdout.getvalue(), stderr.getvalue()
        return 0, stdout.getvalue(), stderr.getvalue()

    def test_missing_config_exits_2_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, stderr = self.invoke(
                "run-fundamentals-etl", "--config", str(Path(tmp) / "absent.yaml"),
            )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr.startswith("error: "), stderr)
        self.assertIn("absent.yaml", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_config_without_fundamentals_section_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.yaml"
            config.write_text(
                f"storage:\n  parquet_dir: {root / 'p'}\n  qlib_dir: {root / 'q'}\n",
                encoding="utf-8",
            )
            code, stdout, stderr = self.invoke("run-fundamentals-etl", "--config", str(config))
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr.startswith("error: "), stderr)
        self.assertNotIn("Traceback", stderr)

    def test_pit_audit_failure_exits_1_without_traceback(self) -> None:
        from jqqlib.normalize_fundamentals import PitAuditError

        with patch(
            "jqqlib.fundamentals_pipeline.run_fundamentals_etl",
            side_effect=PitAuditError("PIT AUDIT FAILED: 1 look-ahead violation(s) detected."),
        ):
            code, stdout, stderr = self.invoke("run-fundamentals-etl", "--config", "config.yaml")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "error: PIT AUDIT FAILED: 1 look-ahead violation(s) detected.\n")
        self.assertNotIn("Traceback", stderr)

    def test_no_fundamentals_data_exits_1_like_the_price_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.yaml"
            config.write_text(
                f"storage:\n  parquet_dir: {root / 'p'}\n  qlib_dir: {root / 'q'}\n"
                f"fundamentals:\n  parquet_dir: {root / 'f'}\n",
                encoding="utf-8",
            )
            with (
                patch("jqqlib.fundamentals_pipeline.create_jquants_client"),
                patch("jqqlib.fundamentals_pipeline.read_price_universe_codes", return_value=["10010"]),
                patch("jqqlib.fundamentals_pipeline.fetch_fundamentals", return_value=pd.DataFrame()),
            ):
                code, stdout, stderr = self.invoke("run-fundamentals-etl", "--config", str(config))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "error: No fundamentals data fetched from J-Quants API.\n")


if __name__ == "__main__":
    unittest.main()
