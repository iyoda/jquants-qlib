from __future__ import annotations

import copy
import logging
import os
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from jqqlib.config import QLIB_ADJUSTMENT_NONE, load_config
from jqqlib.dump import parquet_to_qlib
from jqqlib.normalize import normalize_for_qlib
from jqqlib.store import csv_to_parquet, write_bootstrap_csv, write_parquet
from jqqlib.validate import validate_qlib_readiness
from tests.support.synthetic import write_synthetic_daily_quotes_csv


def _qlib_restore():
    from qlib.config import C
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

    def restore() -> None:
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
    return restore


class NormalizeVendorFactorTests(unittest.TestCase):
    def test_maps_adjfactor_and_exrt_and_defaults_missing_to_one(self) -> None:
        mapped = normalize_for_qlib(pd.DataFrame([{
            "Code": 10010, "Date": "2026-06-04", "Open": 1.0, "High": 2.0, "Low": 0.5,
            "Close": 1.5, "Volume": 100, "TurnoverValue": 150.0, "AdjFactor": 0.5, "ExRT": 3,
        }]))
        self.assertEqual(
            list(mapped.columns),
            ["symbol", "date", "open", "high", "low", "close", "volume", "amount",
             "adjustment_factor", "ex_rights_type"],
        )
        self.assertEqual(mapped.iloc[0]["adjustment_factor"], 0.5)
        self.assertEqual(mapped.iloc[0]["ex_rights_type"], "3")

        from_bulk = normalize_for_qlib(pd.DataFrame([{
            "Code": 10010, "Date": "2026-06-04", "Open": 1.0, "High": 2.0, "Low": 0.5,
            "Close": 1.5, "Volume": 100, "TurnoverValue": 150.0, "AdjustmentFactor": 0.5,
        }]))
        self.assertEqual(list(from_bulk.columns), [
            "symbol", "date", "open", "high", "low", "close", "volume", "amount", "adjustment_factor",
        ])
        self.assertEqual(from_bulk.iloc[0]["adjustment_factor"], 0.5)

        missing = normalize_for_qlib(pd.DataFrame([{
            "Code": 10010, "Date": "2026-06-04", "Open": 1.0, "High": 2.0, "Low": 0.5,
            "Close": 1.5, "Volume": 100, "TurnoverValue": 150.0,
        }]))
        self.assertEqual(missing.iloc[0]["adjustment_factor"], 1.0)
        self.assertNotIn("ex_rights_type", missing.columns)


class ValidateVendorFactorTests(unittest.TestCase):
    def test_requires_finite_positive_adjustment_factor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_quotes.parquet"
            rows = {
                "symbol": ["10010"], "date": ["2026-06-01"],
                "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
                "volume": [100.0], "amount": [150.0],
            }
            pd.DataFrame(rows).to_parquet(path, index=False)
            errors = validate_qlib_readiness(path)
            self.assertTrue(any("adjustment_factor" in item for item in errors))

            pd.DataFrame({**rows, "adjustment_factor": [0.0]}).to_parquet(path, index=False)
            self.assertTrue(any("Non-positive" in item for item in validate_qlib_readiness(path)))

            pd.DataFrame({**rows, "adjustment_factor": [float("nan")]}).to_parquet(path, index=False)
            self.assertTrue(any("Null adjustment_factor" in item for item in validate_qlib_readiness(path)))

            pd.DataFrame({**rows, "adjustment_factor": [1.0]}).to_parquet(path, index=False)
            self.assertEqual(validate_qlib_readiness(path), [])


class DumpVendorFactorTests(unittest.TestCase):
    def test_none_mode_omits_factor_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parquet_path = root / "daily_quotes.parquet"
            pd.DataFrame({
                "symbol": ["10010"], "date": ["2026-06-01"],
                "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
                "volume": [100.0], "amount": [150.0], "adjustment_factor": [1.0],
            }).to_parquet(parquet_path, index=False)
            qlib_dir = root / "qlib"
            parquet_to_qlib(parquet_path, qlib_dir, adjustment=QLIB_ADJUSTMENT_NONE)
            names = sorted(path.name for path in (qlib_dir / "features" / "10010").iterdir())
            self.assertEqual(
                names,
                ["amount.day.bin", "close.day.bin", "high.day.bin",
                 "low.day.bin", "open.day.bin", "volume.day.bin"],
            )
            self.assertFalse((qlib_dir / "features" / "10010" / "factor.day.bin").exists())

    def test_none_mode_ignores_missing_adjustment_factor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parquet_path = root / "daily_quotes.parquet"
            pd.DataFrame({
                "symbol": ["10010"], "date": ["2026-06-01"],
                "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
                "volume": [100.0], "amount": [150.0],
            }).to_parquet(parquet_path, index=False)
            qlib_dir = root / "qlib"
            parquet_to_qlib(parquet_path, qlib_dir, adjustment=QLIB_ADJUSTMENT_NONE)
            names = sorted(path.name for path in (qlib_dir / "features" / "10010").iterdir())
            self.assertEqual(
                names,
                ["amount.day.bin", "close.day.bin", "high.day.bin",
                 "low.day.bin", "open.day.bin", "volume.day.bin"],
            )

    def test_upsert_ex_date_row_changes_earlier_factor_on_next_dump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = pd.DataFrame([
                {"Code": "10010", "Date": "2026-06-01", "Open": 100, "High": 110, "Low": 90,
                 "Close": 100, "Volume": 1000, "TurnoverValue": 100000, "AdjFactor": 1.0},
                {"Code": "10010", "Date": "2026-06-02", "Open": 100, "High": 110, "Low": 90,
                 "Close": 100, "Volume": 1000, "TurnoverValue": 100000, "AdjFactor": 1.0},
            ])
            parquet_path = write_parquet(first, root / "parquet", merge_mode="replace")
            qlib_dir = root / "qlib"
            parquet_to_qlib(parquet_path, qlib_dir)
            self.addCleanup(_qlib_restore())
            import qlib
            from qlib.data import D

            qlib.init(
                provider_uri=str(qlib_dir),
                expression_cache=None,
                dataset_cache=None,
                logging_level=logging.WARNING,
            )
            before = D.features(["10010"], ["$factor"], start_time="2026-06-01", end_time="2026-06-02")
            self.assertAlmostEqual(float(before.iloc[0]["$factor"]), 1.0, places=6)

            later = pd.DataFrame([{
                "Code": "10010", "Date": "2026-06-02", "Open": 50, "High": 55, "Low": 45,
                "Close": 50, "Volume": 2000, "TurnoverValue": 100000, "AdjFactor": 0.5,
            }])
            write_parquet(later, root / "parquet", merge_mode="upsert")
            parquet_to_qlib(parquet_path, qlib_dir)
            qlib.init(
                provider_uri=str(qlib_dir),
                expression_cache=None,
                dataset_cache=None,
                logging_level=logging.WARNING,
            )
            after = D.features(["10010"], ["$close", "$factor"], start_time="2026-06-01", end_time="2026-06-02")
            self.assertAlmostEqual(float(after.iloc[0]["$factor"]), 0.5, places=6)
            self.assertAlmostEqual(float(after.iloc[-1]["$factor"]), 1.0, places=6)
            self.assertAlmostEqual(float(after.iloc[0]["$close"]) / float(after.iloc[0]["$factor"]), 100.0, places=6)

    def test_exrt_survives_bulk_csv_bootstrap_and_rights_issue_volume(self) -> None:
        self.addCleanup(_qlib_restore())
        import qlib
        from qlib.data import D

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bulk = pd.DataFrame([
                {"Code": "10010", "Date": "2026-06-01", "Open": 100, "High": 110, "Low": 90,
                 "Close": 100, "Volume": 1000, "TurnoverValue": 100000,
                 "AdjustmentFactor": 1.0, "ExRT": 0},
                {"Code": "10010", "Date": "2026-06-02", "Open": 80, "High": 88, "Low": 72,
                 "Close": 80, "Volume": 250, "TurnoverValue": 20000,
                 "AdjustmentFactor": 0.8, "ExRT": 3},
            ])
            csv_path = write_bootstrap_csv(bulk, root / "daily_quotes.csv")
            csv_df = pd.read_csv(csv_path)
            self.assertIn("ExRT", csv_df.columns)
            parquet_path = csv_to_parquet(csv_path, root / "parquet")
            stored = pd.read_parquet(parquet_path)
            self.assertIn("ex_rights_type", stored.columns)
            self.assertEqual(list(stored.sort_values("date")["ex_rights_type"]), ["0", "3"])
            qlib_dir = root / "qlib"
            parquet_to_qlib(parquet_path, qlib_dir)
            qlib.init(
                provider_uri=str(qlib_dir),
                expression_cache=None,
                dataset_cache=None,
                logging_level=logging.WARNING,
            )
            values = D.features(
                ["10010"], ["$volume", "$factor"], start_time="2026-06-01", end_time="2026-06-02",
            )
            self.assertAlmostEqual(float(values.iloc[0]["$volume"]), 1000.0, places=6)
            self.assertAlmostEqual(float(values.iloc[1]["$volume"]), 250.0, places=6)
            self.assertAlmostEqual(float(values.iloc[1]["$factor"]), 1.0, places=6)


class VendorFactorE2ETests(unittest.TestCase):
    def test_bootstrap_split_fixture_round_trips_through_qlib(self) -> None:
        self.addCleanup(_qlib_restore())
        import qlib
        from qlib.data import D

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = write_synthetic_daily_quotes_csv(
                root / "daily_quotes.csv",
                symbols=["10010"],
                start="2026-06-01",
                end="2026-06-05",
                events={"10010": {"2026-06-03": 0.5}},
            )
            parquet_path = write_parquet(pd.read_csv(csv_path), root / "parquet")
            qlib_dir = root / "qlib"
            parquet_to_qlib(parquet_path, qlib_dir)
            qlib.init(
                provider_uri=str(qlib_dir),
                expression_cache=None,
                dataset_cache=None,
                logging_level=logging.WARNING,
            )
            values = D.features(
                ["10010"], ["$close", "$factor"], start_time="2026-06-01", end_time="2026-06-05",
            )
            self.assertAlmostEqual(float(values.iloc[-1]["$factor"]), 1.0, places=6)
            raw = pd.read_parquet(parquet_path).sort_values("date")
            restored = values["$close"].to_numpy() / values["$factor"].to_numpy()
            self.assertEqual(len(restored), len(raw))
            for got, expected in zip(restored, raw["close"].to_numpy(), strict=True):
                self.assertAlmostEqual(float(got), float(expected), places=5)


class CheckQlibDefaultFieldsTests(unittest.TestCase):
    def test_default_fields_include_factor(self) -> None:
        source = Path(__file__).resolve().parents[1] / "scripts" / "check_qlib.py"
        text = source.read_text(encoding="utf-8")
        self.assertIn('"$factor"', text)
        self.assertIn("DEFAULT_FIELDS", text)

    def test_drops_factor_when_bin_is_missing(self) -> None:
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "scripts" / "check_qlib.py"
        spec = importlib.util.spec_from_file_location("check_qlib_script", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feature_dir = root / "features" / "10010"
            feature_dir.mkdir(parents=True)
            (feature_dir / "close.day.bin").write_bytes(b"x")
            self.assertEqual(
                module.fields_for_provider(root, ["10010"]),
                ["$open", "$high", "$low", "$close", "$volume", "$amount"],
            )
            (feature_dir / "factor.day.bin").write_bytes(b"x")
            self.assertEqual(module.fields_for_provider(root, ["10010"]), module.DEFAULT_FIELDS)


class ConfigQlibAdjustmentTests(unittest.TestCase):
    def test_default_and_invalid_adjustment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("storage:\n  parquet_dir: ./p\n  qlib_dir: ./q\n", encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config["qlib"]["adjustment"], "vendor_factor")
            path.write_text(
                "storage:\n  parquet_dir: ./p\n  qlib_dir: ./q\nqlib:\n  adjustment: mystery\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "qlib.adjustment must be one of"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
