import re
from collections.abc import Iterable
from typing import Any, cast

_SAFE_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*", re.ASCII)
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}


def symbol_errors(symbols: Iterable[object]) -> list[str]:
    import pandas as pd

    errors = []
    seen: dict[str, str] = {}
    for value in symbols:
        if pd.isna(cast(Any, value)):
            errors.append("Null symbol")
            continue
        symbol = str(value)
        if not _SAFE_SYMBOL.fullmatch(symbol):
            errors.append(f"Unsafe symbol: {symbol!r}")
            continue
        key = symbol.lower()
        if key in _RESERVED:
            key = "_qlib_" + key
        if key in seen and seen[key] != symbol:
            errors.append(f"Colliding symbols: {seen[key]!r}, {symbol!r}")
        seen[key] = symbol
    return errors
