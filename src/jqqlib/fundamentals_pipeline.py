"""CLI for the independent fundamentals as-of/PIT pipeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import load_config
from .fetch import create_jquants_client
from .fetch_fundamentals import fetch_fundamentals
from .normalize_fundamentals import PitAuditError, assert_pit_audit_passes, audit_pit, normalize_fundamentals
from .store import DEFAULT_PARQUET_FILENAME
from .store_fundamentals import (
    DEFAULT_RAW_CACHE_FILENAME,
    write_fundamentals_parquet,
    write_pit_audit_report,
)

if TYPE_CHECKING:
    import pandas as pd


def _fundamentals_conf(config: dict) -> dict[str, Any]:
    conf = config.get("fundamentals", {})
    if not isinstance(conf, dict):
        raise ValueError("fundamentals configuration must be a mapping.")
    return conf


def read_price_universe_codes(config: dict) -> list[str]:
    """Derive the fundamentals fetch universe from the codes already present in
    the price ETL's parquet dataset. Fundamentals are only useful once they can
    be joined to price data, and this keeps the two ETLs independent -- this
    reads the OHLCV parquet's ``symbol`` column but never writes to it, so it
    cannot disturb the price pipeline's output.
    """
    import pandas as pd

    parquet_path = Path(config["storage"]["parquet_dir"]) / DEFAULT_PARQUET_FILENAME
    if not parquet_path.exists():
        raise FileNotFoundError(
            f"Price dataset not found at {parquet_path}; run the price ETL first."
        )
    df = pd.read_parquet(parquet_path, columns=["symbol"])
    return sorted(df["symbol"].astype(str).unique())


def _fundamentals_parquet_dir(config: dict) -> Path:
    conf = _fundamentals_conf(config)
    return Path(conf.get("parquet_dir", config["storage"]["parquet_dir"]))


def _load_calendar(config: dict) -> pd.DatetimeIndex:
    import pandas as pd

    conf = _fundamentals_conf(config)
    default_calendar = Path(config["storage"]["qlib_dir"]) / "calendars" / "day.txt"
    calendar_path = Path(conf.get("calendar_path", default_calendar))
    if not calendar_path.exists():
        raise FileNotFoundError(f"Qlib calendar not found at {calendar_path}.")
    dates = [line.strip() for line in calendar_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return pd.DatetimeIndex(pd.to_datetime(sorted(dates)))


def run_fundamentals_etl(
    config_path: Path,
    *,
    codes: list[str] | None = None,
    max_new_codes: int | None = None,
) -> tuple[Path, Path]:
    """Fetch -> normalize (as-of/PIT) -> audit (fail-closed) -> store.

    Does not touch the price ETL's daily_quotes.parquet or the qlib bin dump;
    this is an independent artifact (fundamentals_asof.parquet +
    fundamentals_pit_audit.json) consumed via plain pandas; the qlib bin dump
    path is deliberately not used for fundamentals.

    The raw fetch is cached at ``fundamentals_raw_cache.parquet`` next to the
    output parquet, so a full-universe backfill can be run as several bounded
    invocations (``max_new_codes`` caps how many not-yet-cached codes this call
    fetches) without losing already-paid-for API calls if one invocation is
    killed -- the panel/audit are (re)built from whatever is cached so far, so
    each invocation's output is a valid (if partial) snapshot.
    """
    config = load_config(config_path)
    etl_conf = _fundamentals_conf(config)

    fetch_codes = codes if codes is not None else read_price_universe_codes(config)
    parquet_dir = _fundamentals_parquet_dir(config)
    cache_path = parquet_dir / DEFAULT_RAW_CACHE_FILENAME

    client = create_jquants_client(config)
    raw = fetch_fundamentals(
        client,
        fetch_codes,
        retries=int(etl_conf.get("retries", 3)),
        retry_base_delay_sec=float(etl_conf.get("retry_base_delay_sec", 1.0)),
        retry_max_delay_sec=float(etl_conf.get("retry_max_delay_sec", 8.0)),
        retry_jitter_sec=float(etl_conf.get("retry_jitter_sec", 0.3)),
        request_interval_sec=float(etl_conf.get("request_interval_sec", 1.1)),
        cache_path=cache_path,
        max_new_codes=max_new_codes,
    )
    if raw.empty:
        raise ValueError("No fundamentals data fetched from J-Quants API.")

    calendar = _load_calendar(config)
    panel = normalize_fundamentals(raw, calendar)
    audit = audit_pit(panel)
    assert_pit_audit_passes(audit)

    parquet_path = write_fundamentals_parquet(panel, parquet_dir)
    audit_path = write_pit_audit_report(audit, parquet_dir)
    return parquet_path, audit_path


def parse_config_path(v: str) -> Path:
    return Path(v)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m jqqlib.fundamentals_pipeline",
        description="J-Quants fundamentals (as-of PIT panel) pipeline",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser(
        "run-fundamentals-etl",
        help="Fetch + normalize (as-of/PIT) + audit (fail-closed) + store fundamentals",
    )
    run_cmd.add_argument("--config", required=True, type=parse_config_path)
    run_cmd.add_argument(
        "--max-new-codes",
        default=None,
        type=int,
        help="Cap how many not-yet-cached codes this invocation fetches (for bounded, resumable runs).",
    )

    args = parser.parse_args()
    try:
        if args.command == "run-fundamentals-etl":
            parquet_path, audit_path = run_fundamentals_etl(args.config, max_new_codes=args.max_new_codes)
            print(f"Fundamentals parquet created: {parquet_path}")
            print(f"PIT audit report created: {audit_path}")
    except PitAuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except (ValueError, OSError) as exc:
        # Same boundary as jqqlib.pipeline.main: configuration, credential and
        # missing-input errors are one line on stderr, not a traceback.
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
