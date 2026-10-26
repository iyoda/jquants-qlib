from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd
import pyarrow

from jqqlib.dump import parquet_to_qlib
from jqqlib.normalize_fundamentals import audit_pit, normalize_fundamentals
from jqqlib.store import NoDataFetchedError, write_parquet


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table(frame):
    return json.loads(frame.to_json(orient="split", date_format="iso", date_unit="ns"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--legacy-parquet", type=Path)
    args = parser.parse_args()
    root = args.output_root
    root.mkdir(parents=True, exist_ok=False)
    rows = []
    for code in ("0010", "123A"):
        for day, factor in (("2026-06-01", 1.0), ("2026-06-02", 0.5), ("2026-06-03", 0.8)):
            rows.append(dict(Code=code, Date=day, Open=100.0, High=110.0, Low=90.0,
                             Close=105.0, Volume=1000.0, TurnoverValue=None,
                             AdjFactor=factor, ExRT="3" if factor == 0.8 else None))
    rows[2]["Volume"] = 100000004.00000001
    source = pd.DataFrame(rows)
    parquet_dir = root / "parquet"
    parquet_dir.mkdir()
    if args.legacy_parquet:
        shutil.copyfile(args.legacy_parquet, parquet_dir / "daily_quotes.parquet")
    path = write_parquet(source, parquet_dir, merge_mode="upsert")
    original = digest(path)
    write_parquet(source, parquet_dir, merge_mode="upsert")
    assert digest(path) == original
    correction = pd.DataFrame([dict(rows[0], Close=106.0), dict(rows[0], Close=107.0),
                               dict(rows[0], Date="2026-06-04")])
    write_parquet(correction, parquet_dir, merge_mode="upsert")
    corrected = digest(path)
    write_parquet(correction, parquet_dir, merge_mode="upsert")
    assert digest(path) == corrected
    failures = [pd.DataFrame(), pd.DataFrame([dict(rows[0], AdjFactor=0.0)])]
    failures += [pd.DataFrame([dict(rows[0], Code=value)]) for value in (None, "", " ")]
    for invalid in failures:
        try:
            write_parquet(invalid, parquet_dir, merge_mode="upsert")
        except (RuntimeError, NoDataFetchedError):
            pass
        else:
            raise AssertionError("invalid input accepted")
        assert digest(path) == corrected
    qlib = root / "qlib"
    parquet_to_qlib(path, qlib)
    hashes = {str(p.relative_to(qlib)): digest(p) for p in sorted(qlib.rglob("*")) if p.is_file()}
    raw = pd.DataFrame([
        dict(Code="123A", DiscDate="2026-06-01", DiscTime="16:00", DiscNo="2", Eq=200.0, CurPerType=""),
        dict(Code="123A", DiscDate="2026-06-01", DiscTime="15:00", DiscNo="1", Eq=100.0, CurPerType="FY"),
        dict(Code="123A", DiscDate="2026-06-02", DiscTime="15:00", DiscNo="3", Eq=None, CurPerType=None),
    ])
    panels = []
    for unit in ("ns", "us", "ms", "s"):
        calendar = pd.date_range("2026-06-01", periods=4).as_unit(unit)
        panel = normalize_fundamentals(raw, calendar)
        assert audit_pit(panel)["pit_audit_pass"]
        panels.append(table(panel.reset_index()))
    assert all(panel == panels[0] for panel in panels)
    report = dict(parquet=table(pd.read_parquet(path)), qlib=hashes, fundamentals=panels[0])
    (root / "content.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    (root / "environment.json").write_text(json.dumps(dict(pandas=pd.__version__, pyarrow=pyarrow.__version__,
                                                         parquet_sha256=corrected), indent=2) + "\n")
    print("Migration probe passed:", pd.__version__)


if __name__ == "__main__":
    main()
