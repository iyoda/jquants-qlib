from __future__ import annotations

import copy
import errno
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import jqqlib.qlib_dump as qlib_dump
from jqqlib.dump import parquet_to_qlib


def _dataset(symbols: list[str]) -> pd.DataFrame:
    rows = []
    for symbol in symbols:
        rows.append(
            {
                "symbol": symbol,
                "date": "2026-06-01",
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 100.0,
                "amount": 150.0,
                "adjustment_factor": 1.0,
            }
        )
    return pd.DataFrame(rows)


def _seed_generated_tree(qlib_dir: Path) -> Path:
    stale_feature = qlib_dir / "features" / "99990" / "open.day.bin"
    stale_feature.parent.mkdir(parents=True)
    stale_feature.write_bytes(b"stale")
    (qlib_dir / "calendars").mkdir()
    (qlib_dir / "calendars" / "day.txt").write_text("2020-01-01\n", encoding="utf-8")
    (qlib_dir / "instruments").mkdir()
    (qlib_dir / "instruments" / "all.txt").write_text("99990\t2020-01-01\t2020-01-01\n", encoding="utf-8")
    return stale_feature


class ParquetToQlibPruneTests(unittest.TestCase):
    def test_default_two_argument_call_dumps_the_vendor_factor_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"
            extra = _dataset(["10010"])
            extra["unrelated"] = 0.5
            extra.to_parquet(parquet_path, index=False)

            parquet_to_qlib(parquet_path, qlib_dir)

            feature_dir = qlib_dir / "features" / "10010"
            self.assertEqual(
                sorted(path.name for path in feature_dir.iterdir()),
                ["amount.day.bin", "close.day.bin", "factor.day.bin", "high.day.bin",
                 "low.day.bin", "open.day.bin", "volume.day.bin"],
            )

    def test_unknown_and_missing_requested_fields_fail_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"
            stale_feature = _seed_generated_tree(qlib_dir)
            _dataset(["10010"]).drop(columns=["amount"]).to_parquet(parquet_path, index=False)

            with self.assertRaisesRegex(ValueError, "Unknown Qlib fields"):
                parquet_to_qlib(parquet_path, qlib_dir, ["open", "mystery"])
            with self.assertRaisesRegex(ValueError, "Missing columns.*amount"):
                parquet_to_qlib(parquet_path, qlib_dir, ["open", "amount"])

            self.assertTrue(stale_feature.exists())

    def test_real_qlib_reads_back_the_generated_raw_provider(self) -> None:
        import qlib
        from qlib.config import C
        from qlib.data import D
        from qlib.log import get_module_logger

        root_logger = logging.getLogger()
        qlib_logger = logging.getLogger("qlib")
        mlflow_logger = logging.getLogger("mlflow")
        snapshot = {
            "root_level": root_logger.level,
            "root_handlers": list(root_logger.handlers),
            "qlib_level": qlib_logger.level,
            "qlib_handlers": list(qlib_logger.handlers),
            "qlib_propagate": qlib_logger.propagate,
            "mlflow_level": mlflow_logger.level,
            "c_config": copy.deepcopy(C.__dict__["_config"]),
            "mlflow_hint": os.environ.get("MLFLOW_DISABLE_AGENT_HINT"),
        }

        def restore_qlib_side_effects() -> None:
            root_logger.setLevel(snapshot["root_level"])
            root_logger.handlers[:] = snapshot["root_handlers"]
            qlib_logger.setLevel(snapshot["qlib_level"])
            qlib_logger.handlers[:] = snapshot["qlib_handlers"]
            qlib_logger.propagate = snapshot["qlib_propagate"]
            mlflow_logger.setLevel(snapshot["mlflow_level"])
            C.__dict__["_config"] = copy.deepcopy(snapshot["c_config"])
            get_module_logger.setLevel(snapshot["qlib_level"])
            if snapshot["mlflow_hint"] is None:
                os.environ.pop("MLFLOW_DISABLE_AGENT_HINT", None)
            else:
                os.environ["MLFLOW_DISABLE_AGENT_HINT"] = snapshot["mlflow_hint"]

        os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"
        mlflow_logger.setLevel(logging.WARNING)
        self.addCleanup(restore_qlib_side_effects)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"
            _dataset(["10010"]).to_parquet(parquet_path, index=False)
            parquet_to_qlib(parquet_path, qlib_dir)

            qlib.init(
                provider_uri=str(qlib_dir),
                expression_cache=None,
                dataset_cache=None,
                logging_level=logging.WARNING,
            )
            values = D.features(
                ["10010"], ["$close", "$volume", "$factor"], start_time="2026-06-01", end_time="2026-06-01"
            )
            self.assertEqual(len(values), 1)
            self.assertAlmostEqual(float(values.iloc[0]["$close"]), 1.5, places=6)
            self.assertAlmostEqual(float(values.iloc[0]["$volume"]), 100.0, places=6)
            self.assertAlmostEqual(float(values.iloc[0]["$factor"]), 1.0, places=6)

    def test_missing_parquet_does_not_delete_existing_provider_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            qlib_dir = root / "qlib_jp"
            stale_feature = _seed_generated_tree(qlib_dir)

            with self.assertRaises(FileNotFoundError):
                parquet_to_qlib(root / "missing.parquet", qlib_dir)

            self.assertTrue(stale_feature.exists())

    def test_empty_parquet_does_not_delete_existing_provider_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"
            stale_feature = _seed_generated_tree(qlib_dir)
            _dataset(["10010"]).iloc[0:0].to_parquet(parquet_path, index=False)

            with self.assertRaisesRegex(ValueError, "empty"):
                parquet_to_qlib(parquet_path, qlib_dir)

            self.assertTrue(stale_feature.exists())

    def test_missing_required_column_does_not_delete_existing_provider_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"
            stale_feature = _seed_generated_tree(qlib_dir)
            _dataset(["10010"]).drop(columns=["amount"]).to_parquet(parquet_path, index=False)

            with self.assertRaisesRegex(ValueError, "Missing columns"):
                parquet_to_qlib(parquet_path, qlib_dir)

            self.assertTrue(stale_feature.exists())

    def test_successful_convert_prunes_obsolete_generated_subtrees_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"
            stale_feature = _seed_generated_tree(qlib_dir)
            marker = qlib_dir / "README.txt"
            marker.write_text("preserve me\n", encoding="utf-8")
            _dataset(["10010"]).to_parquet(parquet_path, index=False)

            parquet_to_qlib(parquet_path, qlib_dir)

            self.assertFalse(stale_feature.exists())
            self.assertTrue((qlib_dir / "features").exists())
            self.assertTrue((qlib_dir / "calendars" / "day.txt").exists())
            self.assertTrue((qlib_dir / "instruments" / "all.txt").exists())
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me\n")

    def test_dump_failure_does_not_delete_existing_provider_subtree(self) -> None:
        class FailingDumpDataAll:
            def __init__(self, *args, qlib_dir: str, **kwargs) -> None:
                self.qlib_dir = Path(qlib_dir)

            def dump(self) -> None:
                (self.qlib_dir / "features" / "10010").mkdir(parents=True)
                raise RuntimeError("dump failed")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"
            stale_feature = _seed_generated_tree(qlib_dir)
            _dataset(["10010"]).to_parquet(parquet_path, index=False)

            original = qlib_dump.DumpDataAll
            qlib_dump.DumpDataAll = FailingDumpDataAll
            try:
                with self.assertRaisesRegex(RuntimeError, "dump failed"):
                    parquet_to_qlib(parquet_path, qlib_dir)
            finally:
                qlib_dump.DumpDataAll = original

            self.assertTrue(stale_feature.exists())
            self.assertEqual((qlib_dir / "calendars" / "day.txt").read_text(encoding="utf-8"), "2020-01-01\n")

    def test_interrupt_during_subtree_swap_restores_the_previous_provider(self) -> None:
        # KeyboardInterrupt is not an Exception: the restore must still run, and
        # the backups must not be deleted before they are moved back.
        original_rename = Path.rename

        def interrupting_rename(self: Path, target):
            if self.name == "calendars" and ".qlib_build_" in str(self):
                raise KeyboardInterrupt
            return original_rename(self, target)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"
            stale_feature = _seed_generated_tree(qlib_dir)
            _dataset(["10010"]).to_parquet(parquet_path, index=False)

            with mock.patch.object(Path, "rename", interrupting_rename), self.assertRaises(KeyboardInterrupt):
                parquet_to_qlib(parquet_path, qlib_dir)

            self.assertTrue(stale_feature.exists())
            self.assertFalse((qlib_dir / "features" / "10010").exists())
            self.assertEqual((qlib_dir / "calendars" / "day.txt").read_text(encoding="utf-8"), "2020-01-01\n")
            self.assertTrue((qlib_dir / "instruments" / "all.txt").exists())
            self.assertEqual(sorted(p.name for p in qlib_dir.iterdir()), ["calendars", "features", "instruments"])

    def test_failed_restore_after_an_interrupt_keeps_the_backup_recoverable(self) -> None:
        original_rename = Path.rename

        def interrupt_install_then_fail_restore(self: Path, target):
            if self.name == "calendars" and ".qlib_build_" in str(self):
                raise KeyboardInterrupt
            if self.name == "features" and ".qlib_generated_backup_" in str(self):
                raise OSError(errno.EIO, "restore failed")
            return original_rename(self, target)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"
            _seed_generated_tree(qlib_dir)
            _dataset(["10010"]).to_parquet(parquet_path, index=False)

            with mock.patch.object(Path, "rename", interrupt_install_then_fail_restore), self.assertRaises(OSError):
                parquet_to_qlib(parquet_path, qlib_dir)

            backup_roots = list(qlib_dir.glob(".qlib_generated_backup_*"))
            self.assertEqual(len(backup_roots), 1)
            self.assertEqual((backup_roots[0] / "features" / "99990" / "open.day.bin").read_bytes(), b"stale")

    def test_symlinked_qlib_dir_on_another_filesystem_is_staged_beside_its_target(self) -> None:
        # A published provider is a symlink that may point at another filesystem.
        # Simulate that boundary: renames between the two roots fail with EXDEV.
        original_rename = Path.rename

        with tempfile.TemporaryDirectory() as link_tmp, tempfile.TemporaryDirectory() as build_tmp:
            link_root = Path(link_tmp).resolve()
            build_root = Path(build_tmp).resolve()

            def device_of(path: Path) -> Path:
                return build_root if path.resolve().is_relative_to(build_root) else link_root

            def cross_device_rename(self: Path, target):
                if device_of(self) != device_of(Path(target).parent):
                    raise OSError(errno.EXDEV, "Invalid cross-device link")
                return original_rename(self, target)

            real_qlib = build_root / "builds" / "b1" / "qlib"
            stale_feature = _seed_generated_tree(real_qlib)
            qlib_dir = link_root / "qlib_jp"
            qlib_dir.symlink_to(real_qlib, target_is_directory=True)
            parquet_path = link_root / "daily_quotes.parquet"
            _dataset(["10010"]).to_parquet(parquet_path, index=False)

            with mock.patch.object(Path, "rename", cross_device_rename):
                parquet_to_qlib(parquet_path, qlib_dir)

            self.assertFalse(stale_feature.exists())
            self.assertTrue((real_qlib / "features" / "10010").is_dir())
            self.assertTrue(qlib_dir.is_symlink())

    def test_missing_adjustment_factor_does_not_write_a_dump(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"
            stale_feature = _seed_generated_tree(qlib_dir)
            _dataset(["10010"]).drop(columns=["adjustment_factor"]).to_parquet(parquet_path, index=False)

            with self.assertRaisesRegex(RuntimeError, "Parquet is not Qlib-ready:"):
                parquet_to_qlib(parquet_path, qlib_dir)

            self.assertTrue(stale_feature.exists())
            self.assertFalse((qlib_dir / "features" / "10010").exists())

    def test_zero_adjustment_factor_on_a_later_row_does_not_write_a_dump(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parquet_path = root / "daily_quotes.parquet"
            qlib_dir = root / "qlib_jp"
            stale_feature = _seed_generated_tree(qlib_dir)
            frame = pd.concat(
                [
                    _dataset(["10010"]).assign(date="2026-06-01", adjustment_factor=1.0),
                    _dataset(["10010"]).assign(date="2026-06-02", adjustment_factor=0.0),
                ],
                ignore_index=True,
            )
            frame.to_parquet(parquet_path, index=False)

            with self.assertRaisesRegex(RuntimeError, "Parquet is not Qlib-ready:.*Non-positive"):
                parquet_to_qlib(parquet_path, qlib_dir)

            self.assertTrue(stale_feature.exists())
            self.assertFalse((qlib_dir / "features" / "10010").exists())


if __name__ == "__main__":
    unittest.main()
