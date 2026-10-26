from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from jqqlib.dump import parquet_to_qlib
from jqqlib.store import csv_to_parquet, write_parquet
from jqqlib.validate import qlib_readiness_errors
from tests.test_dump import _dataset, _seed_generated_tree


class SymbolPathTests(unittest.TestCase):
    def test_null_symbols_are_rejected_before_conversion_in_both_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = root / "qlib"
            marker = _seed_generated_tree(provider)
            for value in (None, float("nan"), pd.NA):
                frame = _dataset(["10010"])
                frame.loc[0, "symbol"] = value
                self.assertIn("Null symbol", qlib_readiness_errors(frame))
                parquet = root / "input.parquet"
                frame.to_parquet(parquet)
                for mode in ("none", "vendor_factor"):
                    with self.subTest(value=value, mode=mode):
                        with self.assertRaisesRegex(ValueError, "Null symbol"):
                            parquet_to_qlib(parquet, provider, adjustment=mode)
                        self.assertEqual(marker.read_bytes(), b"stale")

    def test_null_vendor_codes_do_not_become_literal_symbol_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "daily_quotes.parquet"
            output.write_bytes(b"original")
            for value in (None, float("nan"), pd.NA):
                with self.subTest(value=value):
                    frame = _dataset(["10010"]).rename(columns={"symbol": "Code"})
                    frame.loc[0, "Code"] = value
                    with self.assertRaisesRegex(RuntimeError, "Null symbol"):
                        write_parquet(frame, root)
                    self.assertEqual(output.read_bytes(), b"original")

    def test_unsafe_symbols_leave_provider_and_external_csv_unchanged_in_both_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            victim = root / "victim.csv"
            victim.write_bytes(b"original")
            provider = root / "qlib"
            marker = _seed_generated_tree(provider)
            unsafe = [str(victim.with_suffix("")), "../victim", "a/b", "a\\b", ".", "..",
                      "C:\\victim", "bad\nname", "bad\tname", " bad", "bad ", "bad\x00name", "銘柄"]
            for mode in ("none", "vendor_factor"):
                for symbol in unsafe:
                    with self.subTest(mode=mode, symbol=symbol):
                        parquet = root / "input.parquet"
                        _dataset([symbol]).to_parquet(parquet)
                        with self.assertRaisesRegex(ValueError, "Unsafe symbol"):
                            parquet_to_qlib(parquet, provider, adjustment=mode)
                        self.assertEqual(victim.read_bytes(), b"original")
                        self.assertEqual(marker.read_bytes(), b"stale")

    def test_colliding_names_are_rejected_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parquet = root / "input.parquet"
            _dataset(["AbC", "abc"]).to_parquet(parquet)
            with self.assertRaisesRegex(ValueError, "Colliding symbols"):
                parquet_to_qlib(parquet, root / "provider", adjustment="none")
            self.assertFalse((root / "provider").exists())

    def test_write_parquet_rejects_bad_code_without_replacing_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "daily_quotes.parquet"
            output.write_bytes(b"original")
            for symbol in ("../victim", "/absolute", "a\\b"):
                with self.subTest(symbol=symbol):
                    with self.assertRaisesRegex(RuntimeError, "Unsafe symbol"):
                        write_parquet(_dataset([symbol]), root)
                    self.assertEqual(output.read_bytes(), b"original")

    def test_numeric_alphanumeric_and_safe_punctuation_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            symbols = ["10010", "130A0", "TEST_1", "TEST.2", "TEST-3"]
            parquet = root / "input.parquet"
            _dataset(symbols).to_parquet(parquet)
            for mode in ("none", "vendor_factor"):
                provider = root / mode
                parquet_to_qlib(parquet, provider, adjustment=mode)
                for symbol in symbols:
                    self.assertTrue((provider / "features" / symbol.lower() / "close.day.bin").is_file())

    def test_bootstrap_csv_cannot_replace_existing_parquet_with_an_unsafe_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv = root / "bootstrap.csv"
            _dataset(["../victim"]).to_csv(csv, index=False)
            parquet = root / "daily_quotes.parquet"
            parquet.write_bytes(b"original")
            with self.assertRaisesRegex(RuntimeError, "Unsafe symbol"):
                csv_to_parquet(csv, root)
            self.assertEqual(parquet.read_bytes(), b"original")
