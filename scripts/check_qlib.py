from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import qlib
from qlib.constant import REG_CN  # qlib has no JP region constant; REG_CN is the non-US default
from qlib.data import D

DEFAULT_FIELDS = ["$open", "$high", "$low", "$close", "$volume", "$amount", "$factor"]


def fields_for_provider(
    provider_uri: Path,
    instruments: list[str],
    fields: list[str] | None = None,
) -> list[str]:
    """Drop ``$factor`` when the sampled instruments have no factor binary.

    Providers built with ``qlib.adjustment: none`` omit ``factor.day.bin``.
    """
    selected = list(DEFAULT_FIELDS if fields is None else fields)
    if "$factor" not in selected or not instruments:
        return selected
    features = provider_uri / "features"
    if all((features / str(instrument).lower() / "factor.day.bin").is_file() for instrument in instruments):
        return selected
    return [field for field in selected if field != "$factor"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Check that the local Qlib dataset can be read.")
    parser.add_argument("--provider-uri", default="data/qlib_jp")
    parser.add_argument(
        "--date",
        default=None,
        help="Trading day to sample, YYYY-MM-DD (default: the last day in the dataset calendar)",
    )
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    provider_uri = Path(args.provider_uri).expanduser().resolve()
    qlib.init(provider_uri=str(provider_uri), region=REG_CN, dataset_cache=None, expression_cache=None)

    date = args.date
    if date is None:
        calendar = D.calendar(freq="day")
        if len(calendar) == 0:
            raise SystemExit(f"Empty calendar in {provider_uri}")
        date = pd.Timestamp(calendar[-1]).strftime("%Y-%m-%d")

    instruments = D.list_instruments(
        D.instruments("all"),
        start_time=date,
        end_time=date,
        as_list=True,
    )
    if not instruments:
        raise SystemExit(f"No instruments found for {date} in {provider_uri}")

    sample = instruments[: args.limit]
    features = D.features(
        sample,
        fields_for_provider(provider_uri, sample),
        start_time=date,
        end_time=date,
        freq="day",
        disk_cache=0,
    )

    print(f"provider_uri={provider_uri}")
    print(f"date={date}")
    print(f"instruments={len(instruments)}")
    print(f"sample={sample}")
    print(features.head(args.limit).to_string())


if __name__ == "__main__":
    main()
