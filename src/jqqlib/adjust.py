"""Dump-time cumulative vendor factor for Qlib prices, volume, and ``$factor``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import QLIB_ADJUSTMENT_NONE, QLIB_ADJUSTMENT_VENDOR_FACTOR

if TYPE_CHECKING:
    import pandas as pd

PRICE_FIELDS = ("open", "high", "low", "close")


def _exclusive_reverse_cumprod(values: pd.Series, symbols: pd.Series) -> pd.Series:
    """Per symbol, the product of the values on later rows (1.0 on the latest row).

    ``values`` must be ordered by symbol, then date; the result keeps its index.
    """
    reversed_values = values.iloc[::-1]
    reversed_symbols = symbols.iloc[::-1]
    products = reversed_values.groupby(reversed_symbols, sort=False).cumprod()
    return products.groupby(reversed_symbols, sort=False).shift(1, fill_value=1.0).iloc[::-1]


def _volume_factors(frame: pd.DataFrame, price_factors: pd.Series) -> pd.Series:
    import pandas as pd

    if "ex_rights_type" not in frame.columns:
        return price_factors
    numeric = pd.to_numeric(frame["ex_rights_type"], errors="coerce")
    as_text = frame["ex_rights_type"].astype(str).str.strip()
    rights = numeric.eq(3) | as_text.isin(["3", "3.0"])
    return price_factors.where(~rights, 1.0)


def apply_vendor_adjustment(
    df: pd.DataFrame, *, adjustment: str = QLIB_ADJUSTMENT_VENDOR_FACTOR
) -> pd.DataFrame:
    """Compute dump-time Qlib prices, volume, and ``factor`` from vendor rows.

    Per symbol, ``factor`` is the product of later ``adjustment_factor`` values
    (latest row is 1.0). Volume uses the same product except rights-issue rows
    (``ex_rights_type`` 3) contribute 1. Amount is unchanged. No rounding.
    ``adjustment="none"`` returns a copy and ignores ``adjustment_factor``.
    """
    import pandas as pd

    if adjustment == QLIB_ADJUSTMENT_NONE:
        return df.copy()
    out = df.copy()
    if "adjustment_factor" not in out.columns:
        raise ValueError("adjustment_factor is required when qlib.adjustment is vendor_factor.")
    numeric = pd.to_numeric(out["adjustment_factor"], errors="coerce")
    if int(numeric.isna().sum()):
        raise ValueError("Null adjustment_factor values are not allowed when qlib.adjustment is vendor_factor.")
    out["adjustment_factor"] = numeric
    # Vectorised over all symbols (a per-symbol Python loop dominated large
    # builds). Rows come out grouped by symbol in first-appearance order, each
    # symbol sorted by date; rows without a symbol are dropped, as groupby does.
    out = out[out["symbol"].notna()].reset_index(drop=True)
    if out.empty:
        out["factor"] = 1.0
        return out
    symbol_order = pd.Series(pd.factorize(out["symbol"])[0], index=out.index)
    ordered = out.iloc[
        pd.DataFrame({"order": symbol_order, "date": out["date"]})
        .sort_values(["order", "date"], kind="mergesort")
        .index
    ].reset_index(drop=True)
    price_cum = _exclusive_reverse_cumprod(ordered["adjustment_factor"], ordered["symbol"])
    volume_cum = _exclusive_reverse_cumprod(
        _volume_factors(ordered, ordered["adjustment_factor"]), ordered["symbol"]
    )
    for field in PRICE_FIELDS:
        ordered[field] = ordered[field] * price_cum
    ordered["volume"] = ordered["volume"] / volume_cum
    ordered["factor"] = price_cum
    return ordered
