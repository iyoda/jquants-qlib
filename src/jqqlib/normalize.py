"""Map J-Quants quote columns onto Qlib Parquet fields."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


QLIB_FIELDS = ["open", "high", "low", "close", "volume", "amount"]
QLIB_FACTOR_FIELD = "factor"
QLIB_FIELDS_WITH_FACTOR = [*QLIB_FIELDS, QLIB_FACTOR_FIELD]
PARQUET_BASE_COLUMNS = ["symbol", "date", *QLIB_FIELDS, "adjustment_factor"]
BOOTSTRAP_CSV_COLUMNS = ["Code", "Date", "Open", "High", "Low", "Close", "Volume", "TurnoverValue"]


def _ex_rights_as_str(value: object) -> object:
    import pandas as pd

    if value is None or value is pd.NA:
        return pd.NA
    if isinstance(value, float) and math.isnan(value):
        return pd.NA
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            return str(value)
        return str(int(value))
    text = str(value).strip()
    if text in {"nan", "<NA>", "None", "NaT"}:
        return pd.NA
    if text.endswith(".0") and text[:-2].lstrip("-").isdigit():
        return text[:-2]
    return text


def normalize_for_qlib(df: pd.DataFrame) -> pd.DataFrame:
    import pandas as pd

    column_map = {
        "Code": "symbol",
        "Date": "date",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
        "TurnoverValue": "amount",
    }

    existing = {k: v for k, v in column_map.items() if k in df.columns}
    normalized = df.rename(columns=existing)

    required = ["symbol", "date", "open", "high", "low", "close", "volume"]
    for col in required:
        if col not in normalized.columns:
            raise ValueError(f"Missing required column for Qlib conversion: {col}")

    if "amount" not in normalized.columns:
        normalized["amount"] = pd.NA

    if "AdjFactor" in normalized.columns:
        factor_source = normalized["AdjFactor"]
    elif "AdjustmentFactor" in normalized.columns:
        factor_source = normalized["AdjustmentFactor"]
    else:
        factor_source = None
    if factor_source is None:
        normalized["adjustment_factor"] = 1.0
    else:
        normalized["adjustment_factor"] = pd.to_numeric(factor_source, errors="coerce").fillna(1.0)

    if "ExRT" in normalized.columns:
        normalized["ex_rights_type"] = normalized["ExRT"].map(_ex_rights_as_str)

    normalized["symbol"] = normalized["symbol"].astype(str)
    normalized["date"] = pd.to_datetime(normalized["date"]).dt.strftime("%Y-%m-%d")

    columns = list(PARQUET_BASE_COLUMNS)
    if "ex_rights_type" in normalized.columns:
        columns.append("ex_rights_type")
    return normalized[columns]


def normalize_daily_quote_columns(df: pd.DataFrame) -> pd.DataFrame:
    column_map = {
        "O": "Open",
        "H": "High",
        "L": "Low",
        "C": "Close",
        "Vo": "Volume",
        "Va": "TurnoverValue",
    }
    return df.rename(columns={k: v for k, v in column_map.items() if k in df.columns})
