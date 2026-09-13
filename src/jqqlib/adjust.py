"""Dump-time cumulative vendor factor for Qlib prices, volume, and ``$factor``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import QLIB_ADJUSTMENT_NONE, QLIB_ADJUSTMENT_VENDOR_FACTOR

if TYPE_CHECKING:
    import pandas as pd

PRICE_FIELDS = ("open", "high", "low", "close")


def _exclusive_reverse_cumprod(values: pd.Series) -> pd.Series:
    reversed_values = values.iloc[::-1]
    return reversed_values.cumprod().shift(1, fill_value=1.0).iloc[::-1]


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
    pieces: list[pd.DataFrame] = []
    for _, group in out.groupby("symbol", sort=False):
        group = group.sort_values("date")
        price_cum = _exclusive_reverse_cumprod(group["adjustment_factor"])
        volume_cum = _exclusive_reverse_cumprod(_volume_factors(group, group["adjustment_factor"]))
        adjusted = group.copy()
        for field in PRICE_FIELDS:
            adjusted[field] = adjusted[field] * price_cum
        adjusted["volume"] = adjusted["volume"] / volume_cum
        adjusted["factor"] = price_cum
        pieces.append(adjusted)
    if not pieces:
        out["factor"] = 1.0
        return out
    return pd.concat(pieces, ignore_index=True)
