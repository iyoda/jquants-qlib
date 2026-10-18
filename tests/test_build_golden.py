from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd

from jqqlib.pipeline import main
from tests.support.synthetic import write_synthetic_daily_quotes_csv

GOLDEN_PATH = Path(__file__).resolve().parent / "support" / "golden_build.json"
REGENERATE_ENV_VAR = "JQQLIB_REGENERATE_GOLDEN"
REGENERATE_HINT = (
    "The build outputs differ from tests/support/golden_build.json. If the change is intended, regenerate it with "
    f"`{REGENERATE_ENV_VAR}=1 python -m unittest tests.test_build_golden` and review the diff of the golden file."
)
FIXED_UTC_NOW = "2026-06-08T09:00:00Z"
FIXED_PRODUCER_GIT_COMMIT = "0123456789abcdef0123456789abcdef01234567"
TMP_ROOT_PLACEHOLDER = "<tmp>"
PARQUET_SHA256_PLACEHOLDER = "<sha256 of daily_quotes.parquet, whose bytes embed the pandas and pyarrow versions>"
DATASET_ID_HASH_PLACEHOLDER = "<dataset_id hash, derived from the Parquet sha256>"
QUALITY_REPORT_SHA256_PLACEHOLDER = "<sha256 of dataset_quality_report.json, which carries the dataset_id>"
MANIFEST_SHA256_PLACEHOLDER = "<sha256 of the manifest without manifest_sha256, which carries the Parquet sha256>"
MANIFEST_FILENAME = "dataset_manifest.json"
QUALITY_REPORT_FILENAME = "dataset_quality_report.json"


def _write_config(root: Path, adjustment: str) -> Path:
    path = root / "config.yaml"
    path.write_text(
        f"storage:\n  parquet_dir: {root / 'parquet'}\n  qlib_dir: {root / 'qlib'}\n"
        f"qlib:\n  adjustment: {adjustment}\n",
        encoding="utf-8",
    )
    return path


def _mark_rights_issue(csv_path: Path, *, code: str, date: str) -> None:
    header, *rows = csv_path.read_text(encoding="utf-8").splitlines()
    marked = [f"{row},{'3' if row.startswith(f'{code},{date},') else ''}" for row in rows]
    csv_path.write_text("\n".join([f"{header},ExRT", *marked]) + "\n", encoding="utf-8")


def _bootstrap_csv_with_a_split_and_a_rights_issue(root: Path) -> list[str]:
    csv_path = write_synthetic_daily_quotes_csv(
        root / "daily_quotes.csv",
        symbols=["10010", "10020"],
        start="2026-06-01",
        end="2026-06-05",
        events={"10010": {"2026-06-03": 0.5}, "10020": {"2026-06-04": 0.8}},
    )
    _mark_rights_issue(csv_path, code="10020", date="2026-06-04")
    return ["bootstrap-csv", "--config", str(_write_config(root, "vendor_factor")), "--csv", str(csv_path)]


def _convert_raw_parquet_with_report_findings(root: Path) -> list[str]:
    rows = [
        ("10010", "2026-06-01", 100.5, 104.0, 99.0, 103.25, 1200.0, 123900.0, 1.0),
        ("10010", "2026-06-02", None, None, None, None, None, None, 1.0),
        ("10010", "2026-06-03", None, 105.0, 101.0, 104.0, 800.0, 83200.0, 1.0),
        ("10010", "2026-06-04", 102.0, 98.0, 101.0, 99.5, 900.0, 89550.0, 0.5),
        ("10010", "2026-06-05", 99.0, 100.0, 97.0, 98.0, -50.0, 4900.0, 1.0),
        ("10020", "2026-06-01", 200.0, 210.0, 195.0, 205.0, 500.0, 102500.0, 1.0),
        ("10020", "2026-06-02", 205.0, 206.0, 200.0, 201.0, 400.0, None, 1.0),
        ("10020", "2026-06-04", 201.0, 202.0, 199.0, 200.0, 300.0, -60000.0, 1.0),
        ("10020", "2026-06-04", 201.0, 202.0, 199.0, 200.0, 300.0, -60000.0, 1.0),
    ]
    columns = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "adjustment_factor"]
    parquet_dir = root / "parquet"
    parquet_dir.mkdir()
    pd.DataFrame(rows, columns=columns).to_parquet(parquet_dir / "daily_quotes.parquet", index=False)
    return ["convert", "--config", str(_write_config(root, "none"))]


BUILD_CASES: dict[str, Callable[[Path], list[str]]] = {
    "bootstrap-csv, qlib.adjustment vendor_factor": _bootstrap_csv_with_a_split_and_a_rights_issue,
    "convert, qlib.adjustment none": _convert_raw_parquet_with_report_findings,
}


def _run_jqqlib(argv: list[str]) -> list[str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with (
        patch("sys.argv", ["jqqlib", *argv]),
        patch("jqqlib.manifest._utc_now", return_value=FIXED_UTC_NOW),
        patch("jqqlib.manifest._producer_git_commit", return_value=FIXED_PRODUCER_GIT_COMMIT),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        try:
            main()
        except SystemExit as exc:
            raise AssertionError(f"jqqlib {argv[0]} exited {exc.code}:\n{stderr.getvalue()}") from None
    return stdout.getvalue().splitlines()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_of_manifest_without_manifest_sha256(manifest: dict[str, Any]) -> str:
    lineage = {key: value for key, value in manifest["lineage"].items() if key != "manifest_sha256"}
    canonical = json.dumps({**manifest, "lineage": lineage}, sort_keys=True, separators=(",", ":"))
    return _sha256(canonical.encode("utf-8"))


def _read_indented_sorted_json(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    if text != json.dumps(payload, indent=2, sort_keys=True) + "\n":
        raise AssertionError(f"{path.name} is no longer indented, key-sorted JSON ending in a newline")
    return payload


def _parquet_table(path: Path) -> dict[str, Any]:
    frame = pd.read_parquet(path)
    return {
        "columns": [str(column) for column in frame.columns],
        "rows": frame.astype(object).where(frame.notna(), None).to_numpy().tolist(),
    }


def _qlib_file_sha256(qlib_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(qlib_dir).as_posix(): _sha256(path.read_bytes())
        for path in sorted(qlib_dir.rglob("*"))
        if path.is_file() and path.name not in {MANIFEST_FILENAME, QUALITY_REPORT_FILENAME}
    }


def _with_placeholders(value: Any, placeholders: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _with_placeholders(item, placeholders) for key, item in value.items()}
    if isinstance(value, list):
        return [_with_placeholders(item, placeholders) for item in value]
    if isinstance(value, str):
        for observed, placeholder in placeholders.items():
            value = value.replace(observed, placeholder)
    return value


def _build_outputs(build_case: Callable[[Path], list[str]]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        stdout = _run_jqqlib(build_case(root))
        qlib_dir = root / "qlib"
        parquet_path = root / "parquet" / "daily_quotes.parquet"
        manifest = _read_indented_sorted_json(qlib_dir / MANIFEST_FILENAME)
        report = _read_indented_sorted_json(qlib_dir / QUALITY_REPORT_FILENAME)
        placeholders = {
            str(root): TMP_ROOT_PLACEHOLDER,
            _sha256(parquet_path.read_bytes()): PARQUET_SHA256_PLACEHOLDER,
            _sha256((qlib_dir / QUALITY_REPORT_FILENAME).read_bytes()): QUALITY_REPORT_SHA256_PLACEHOLDER,
            _sha256_of_manifest_without_manifest_sha256(manifest): MANIFEST_SHA256_PLACEHOLDER,
        }
        dataset_id = re.fullmatch(r"jqqlib-v2-([0-9a-f]{16})", str(manifest.get("dataset_id")))
        if dataset_id is not None:
            placeholders[dataset_id.group(1)] = DATASET_ID_HASH_PLACEHOLDER
        outputs = {
            "stdout": stdout,
            "parquet": _parquet_table(parquet_path),
            MANIFEST_FILENAME: manifest,
            QUALITY_REPORT_FILENAME: report,
            "qlib files sha256": _qlib_file_sha256(qlib_dir),
        }
        return _with_placeholders(outputs, placeholders)


class GoldenBuildTests(unittest.TestCase):
    maxDiff = None

    def test_bootstrap_csv_and_convert_outputs_match_the_golden_build(self) -> None:
        actual = {name: _build_outputs(build_case) for name, build_case in BUILD_CASES.items()}
        if os.environ.get(REGENERATE_ENV_VAR) == "1":
            GOLDEN_PATH.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(sorted(actual), sorted(golden), REGENERATE_HINT)
        for name, outputs in actual.items():
            self.assertEqual(sorted(outputs), sorted(golden[name]), REGENERATE_HINT)
            for section, value in outputs.items():
                with self.subTest(build=name, output=section):
                    self.assertEqual(value, golden[name][section], REGENERATE_HINT)


if __name__ == "__main__":
    unittest.main()
