"""Generate invented daily quotes for offline bootstrap and distribution checks."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import random
from collections.abc import Iterable
from pathlib import Path

from jqqlib.normalize import BOOTSTRAP_CSV_COLUMNS


def parse_split_event(value: str) -> tuple[str, str, float]:
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError("--split must be CODE:DATE:FACTOR")
    code, date, factor_text = parts
    dt.date.fromisoformat(date)
    try:
        factor = float(factor_text)
    except ValueError as exc:
        raise ValueError("--split FACTOR must be a positive finite number") from exc
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError("--split FACTOR must be a positive finite number")
    if not code.isdigit() or len(code) not in (4, 5):
        raise ValueError("symbols must contain four- or five-digit codes")
    return code, date, factor


def write_synthetic_daily_quotes_csv(
    path: Path,
    *,
    symbols: Iterable[str],
    start: str | dt.date,
    end: str | dt.date,
    events: dict[str, dict[str, float]] | None = None,
) -> Path:
    """Write deterministic weekday quotes, inclusive of both dates.

    Weekdays are illustrative and do not model exchange holidays. Prices are
    invented, with no adjustment events unless ``events`` is given. A private
    fixed seed leaves callers' random state unchanged. Default output has no
    AdjustmentFactor column so README and dist-smoke fixtures stay byte-identical.
    """
    start_date = dt.date.fromisoformat(start) if isinstance(start, str) else start
    end_date = dt.date.fromisoformat(end) if isinstance(end, str) else end
    codes = sorted(set(symbols))
    if not codes or any(not code.isdigit() or len(code) not in (4, 5) for code in codes):
        raise ValueError("symbols must contain four- or five-digit codes")
    if start_date > end_date:
        raise ValueError("start must be on or before end")
    dates = [
        start_date + dt.timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
        if (start_date + dt.timedelta(days=offset)).weekday() < 5
    ]
    if not dates:
        raise ValueError("date range must contain at least one weekday")
    rng = random.Random(0)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(BOOTSTRAP_CSV_COLUMNS)
    if events:
        fieldnames.append("AdjustmentFactor")
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for index, code in enumerate(codes):
            for day in dates:
                opening = 1000 + index * 100 + rng.randrange(20)
                closing = opening + rng.randrange(-5, 6)
                volume = rng.randrange(10, 100) * 100
                row = {
                    "Code": code, "Date": day.isoformat(),
                    "Open": opening, "High": max(opening, closing) + 5,
                    "Low": min(opening, closing) - 5, "Close": closing,
                    "Volume": volume, "TurnoverValue": closing * volume,
                }
                if events:
                    row["AdjustmentFactor"] = events.get(code, {}).get(day.isoformat(), 1.0)
                writer.writerow(row)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument("--symbols", nargs="+", default=["10010", "10020"],
                        help="Space- or comma-separated invented security codes")
    parser.add_argument("--start", default="2026-06-01")
    parser.add_argument("--end", default="2026-06-05")
    parser.add_argument(
        "--split",
        action="append",
        default=[],
        metavar="CODE:DATE:FACTOR",
        help="Invented split/reverse-split event; repeatable",
    )
    args = parser.parse_args()
    symbols = [code for group in args.symbols for code in group.split(",")]
    events: dict[str, dict[str, float]] = {}
    try:
        for item in args.split:
            code, date, factor = parse_split_event(item)
            events.setdefault(code, {})[date] = factor
        path = write_synthetic_daily_quotes_csv(
            args.out / "daily_quotes.csv",
            symbols=symbols,
            start=args.start,
            end=args.end,
            events=events or None,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(path)


if __name__ == "__main__":
    main()
