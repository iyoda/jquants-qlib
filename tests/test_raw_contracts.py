from __future__ import annotations

import unittest

import pandas as pd

from jqqlib.config import QLIB_ADJUSTMENT_NONE
from jqqlib.manifest import _adjustment_policy, _field_schema
from jqqlib.normalize import QLIB_FIELDS, QLIB_FIELDS_WITH_FACTOR, normalize_for_qlib
from jqqlib.pipeline import validate_etl_config


class RawCompatibilityContractTests(unittest.TestCase):
    def test_normalization_preserves_exact_raw_contract(self) -> None:
        source = pd.DataFrame(
            [{
                "Code": 10010,
                "Date": "2026-06-04",
                "Open": 1.0,
                "High": 2.0,
                "Low": 0.5,
                "Close": 1.5,
                "Volume": 100,
                "TurnoverValue": 150.0,
                "AdjFactor": 0.5,
                "AdjC": 0.75,
            }]
        )

        normalized = normalize_for_qlib(source)

        self.assertEqual(
            list(normalized.columns),
            ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "adjustment_factor"],
        )
        self.assertEqual(normalized.iloc[0].to_dict(), {
            "symbol": "10010",
            "date": "2026-06-04",
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 100,
            "amount": 150.0,
            "adjustment_factor": 0.5,
        })

    def test_raw_dump_fields_remain_six_unadjusted_fields(self) -> None:
        self.assertEqual(QLIB_FIELDS, ["open", "high", "low", "close", "volume", "amount"])
        schema = _field_schema(QLIB_ADJUSTMENT_NONE)
        self.assertEqual([name for name in schema if name not in {"symbol", "date"}], QLIB_FIELDS)
        self.assertTrue(all(schema[field]["adjustment"] == "none" for field in QLIB_FIELDS))

    def test_vendor_factor_schema_adds_factor(self) -> None:
        schema = _field_schema()
        self.assertEqual(
            [name for name in schema if name not in {"symbol", "date"}],
            QLIB_FIELDS_WITH_FACTOR,
        )
        self.assertEqual(schema["factor"]["adjustment"], "cumulative_vendor_factor")
        self.assertEqual(schema["factor"]["dtype"], "float32")
        self.assertEqual(schema["open"]["adjustment"], "split_reverse_split_rights_issue")
        self.assertEqual(schema["volume"]["adjustment"], "split_reverse_split")
        self.assertEqual(schema["amount"]["adjustment"], "none")

    def test_raw_adjustment_policy_remains_no_adjustment(self) -> None:
        policy = _adjustment_policy(QLIB_ADJUSTMENT_NONE)
        self.assertEqual(policy["price_adjustment"], "none")
        self.assertEqual(policy["volume_adjustment"], "none")
        self.assertEqual(policy["amount_adjustment"], "none")
        self.assertNotIn("source", policy)
        self.assertEqual(
            policy["known_limitations"],
            ["qlib.adjustment is none: OHLC/volume are raw; no $factor series is emitted."],
        )

    def test_vendor_factor_adjustment_policy(self) -> None:
        policy = _adjustment_policy()
        self.assertEqual(policy["price_adjustment"], "vendor_cumulative_factor")
        self.assertEqual(policy["volume_adjustment"], "vendor_cumulative_factor_excluding_rights_issue")
        self.assertEqual(policy["amount_adjustment"], "none")
        self.assertEqual(policy["source"], "J-Quants AdjFactor")

    def test_etl_defaults_are_the_full_validated_key_set(self) -> None:
        self.assertEqual(validate_etl_config({}), {
            "chunk_days": 30,
            "retries": 3,
            "retry_base_delay_sec": 1.0,
            "retry_max_delay_sec": 8.0,
            "retry_jitter_sec": 0.3,
            "merge_mode": "upsert",
            "request_interval_sec": 0.0,
        })


if __name__ == "__main__":
    unittest.main()
