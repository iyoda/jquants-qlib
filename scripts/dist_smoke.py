"""Check an installed distribution; invoke from outside the repository.

Run WITHOUT ``PYTHONPATH`` pointing at the checkout: ``jqqlib`` must resolve from
the installed wheel, and the wheel does not ship ``tests/``, so the CSV fixture
below is the inline fallback. That fallback is the path CI exercises; the richer
``tests.support.synthetic`` helper is covered by the unittest suite instead.

The CLI is driven through the ``jqqlib`` console script installed next to the
running interpreter, not ``python -m jqqlib.pipeline``, so the entry point that
pip users get is the one under test.
"""

import csv
import datetime as dt
import importlib.metadata
import importlib.resources
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import jsonschema
import numpy as np
import pandas as pd
import yaml

from jqqlib.contracts import load_schema
from jqqlib.normalize import BOOTSTRAP_CSV_COLUMNS

SMOKE_SYMBOLS = ["10010", "10020"]
SMOKE_START, SMOKE_END = "2026-06-01", "2026-06-05"


def console_script() -> str:
    """Locate the ``jqqlib`` console script of the interpreter running this smoke."""
    candidate = Path(sys.executable).parent / "jqqlib"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("jqqlib")
    if found is None:
        raise RuntimeError(f"jqqlib console script not found next to {sys.executable} or on PATH")
    return found


JQQLIB = console_script()


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([JQQLIB, *arguments], check=True, capture_output=True, text=True)


def check_version() -> None:
    expected = importlib.metadata.version("jquants-qlib")
    output = run_cli("--version").stdout.strip()
    if output != f"jqqlib {expected}":
        raise RuntimeError(f"`jqqlib --version` printed {output!r}, expected 'jqqlib {expected}'")
    print(f"Console script OK: {JQQLIB} --version -> {output}")


def check_py_typed() -> None:
    if not importlib.resources.files("jqqlib").joinpath("py.typed").is_file():
        raise RuntimeError("py.typed marker is missing from the installed jqqlib package")
    print("py.typed marker OK")


def smoke_dates() -> list[str]:
    start, end = dt.date.fromisoformat(SMOKE_START), dt.date.fromisoformat(SMOKE_END)
    days = ((start + dt.timedelta(days=offset)) for offset in range((end - start).days + 1))
    return [day.isoformat() for day in days if day.weekday() < 5]


def write_inline_csv(path: Path) -> Path:
    # Same column contract and shape as tests.support.synthetic, with fixed prices.
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=BOOTSTRAP_CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for index, code in enumerate(SMOKE_SYMBOLS):
            for offset, day in enumerate(smoke_dates()):
                opening = 1000 + index * 100 + offset
                closing = opening + 5
                volume = 1000 + offset * 100
                writer.writerow({
                    "Code": code, "Date": day, "Open": opening, "High": closing + 5,
                    "Low": opening - 5, "Close": closing, "Volume": volume,
                    "TurnoverValue": closing * volume,
                })
    return path


def write_smoke_csv(path: Path) -> Path:
    try:
        from tests.support.synthetic import write_synthetic_daily_quotes_csv
    except ModuleNotFoundError as exc:
        if exc.name not in {"tests", "tests.support", "tests.support.synthetic"}:
            raise
        print("Synthetic fixture: inline fallback (checkout helper unavailable)")
        return write_inline_csv(path)
    print("Synthetic fixture: tests.support.synthetic")
    return write_synthetic_daily_quotes_csv(
        path, symbols=SMOKE_SYMBOLS, start=SMOKE_START, end=SMOKE_END,
    )


def check_init_config(root: Path) -> None:
    # The packaged example config must ship in the wheel (package-data), not only in the checkout.
    target = root / "config.example.written.yaml"
    run_cli("init-config", str(target))
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "storage" not in document:
        raise RuntimeError("`jqqlib init-config` did not write the packaged example config")
    print(f"init-config OK: {target.name} ({len(document)} top-level keys)")


def check_conversion(root: Path) -> None:
    csv_path = write_smoke_csv(root / "daily_quotes.csv")
    parquet_dir, qlib_dir = root / "parquet", root / "qlib"
    config = root / "settings.yaml"
    config.write_text(yaml.safe_dump({
        "storage": {"parquet_dir": str(parquet_dir), "qlib_dir": str(qlib_dir)},
    }), encoding="utf-8")
    for arguments in (
        ["bootstrap-csv", "--config", str(config), "--csv", str(csv_path)],
        ["convert", "--config", str(config)],
    ):
        subprocess.run([JQQLIB, *arguments], check=True)

    expected = pd.read_csv(csv_path, dtype={"Code": str})
    expected_rows = len(SMOKE_SYMBOLS) * len(smoke_dates())
    if len(expected) != expected_rows:
        raise RuntimeError(f"Fixture has {len(expected)} rows, expected {expected_rows}")
    actual = pd.read_parquet(parquet_dir / "daily_quotes.parquet")
    if len(actual) != expected_rows:
        raise RuntimeError(f"Parquet has {len(actual)} rows, expected {expected_rows}")

    calendar = (qlib_dir / "calendars" / "day.txt").read_text().splitlines()
    if calendar != smoke_dates():
        raise RuntimeError("Qlib calendar differs from fixture dates")
    instruments = (qlib_dir / "instruments" / "all.txt").read_text().splitlines()
    codes = sorted(line.split("\t")[0].lower() for line in instruments if line.strip())
    if codes != sorted(code.lower() for code in SMOKE_SYMBOLS):
        raise RuntimeError(f"Qlib instruments {codes} differ from fixture symbols")

    feature_count = 0
    factor_last: dict[str, float] = {}
    for code in SMOKE_SYMBOLS:
        feature_dir = qlib_dir / "features" / code.lower()
        features = list(feature_dir.glob("*.bin"))
        if not features or any(path.stat().st_size <= 4 for path in features):
            raise RuntimeError(f"Qlib features for {code} are missing or empty")
        feature_count += len(features)
        factor_bin = feature_dir / "factor.day.bin"
        if not factor_bin.is_file() or factor_bin.stat().st_size <= 4:
            raise RuntimeError(f"Qlib $factor bin missing or empty for {code}: {factor_bin}")
        # Qlib bins are little-endian float32: [start_index, ...field values]. $factor is 1.0
        # on each symbol's latest date for the default vendor_factor dump.
        series = np.fromfile(factor_bin, dtype="<f")
        if series.size < 2:
            raise RuntimeError(f"Qlib $factor bin for {code} has no values")
        last = float(series[-1])
        if abs(last - 1.0) > 1e-6:
            raise RuntimeError(f"$factor on last date for {code} is {last!r}, expected 1.0")
        factor_last[code] = last

    for name in ("dataset_manifest", "dataset_quality_report"):
        document = json.loads((qlib_dir / f"{name}.json").read_text())
        jsonschema.validate(document, load_schema(name))
    print(
        f"Synthetic CSV → Parquet → Qlib OK: {len(actual)} rows, "
        f"{len(codes)} instruments, {feature_count} feature files, "
        f"$factor last={factor_last}"
    )


def main() -> None:
    run_cli("--help")
    check_version()
    check_py_typed()
    for name in ("dataset_manifest", "dataset_quality_report"):
        schema = load_schema(name)
        if not isinstance(schema, dict) or not {"$schema", "properties"} <= schema.keys():
            raise RuntimeError(f"Invalid packaged schema: {name}")
        print(f"Packaged schema OK: {name}")
    with tempfile.TemporaryDirectory(prefix="jqqlib-dist-smoke-") as tmp:
        check_init_config(Path(tmp))
        check_conversion(Path(tmp))


if __name__ == "__main__":
    main()
