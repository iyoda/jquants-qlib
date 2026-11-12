from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import jsonschema
import pandas as pd

from jqqlib import pipeline
from jqqlib.config import ListingConfig, listing_config, load_config
from jqqlib.contracts import load_schema
from jqqlib.listing_sync import (
    fetch_missing_year_ends,
    provider_sessions,
    target_year_ends,
    write_provider_listings,
)
from jqqlib.manifest import _sha256_json, write_dataset_artifacts
from jqqlib.store_listing import raw_listing_path
from tests.git_isolation import scrub_inherited_git_environment
from tests.support.synthetic import write_synthetic_daily_quotes_csv

scrub_inherited_git_environment()

SESSIONS = ("2030-12-27", "2030-12-30", "2031-01-06", "2031-12-30", "2032-01-05")
YEAR_ENDS = [dt.date(2030, 12, 30), dt.date(2031, 12, 30)]
NEXT_SESSION = {"2030-12-31": "2031-01-06", "2031-12-31": "2032-01-05"}
RETRY: dict[str, Any] = {
    "request_interval_sec": 0.0,
    "retries": 0,
    "retry_base_delay_sec": 0.0,
    "retry_max_delay_sec": 0.0,
    "retry_jitter_sec": 0.0,
    "sleeper": lambda seconds: None,
}


def _frame(day: str, codes: tuple[str, ...] = ("90010", "90020")) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.to_datetime([day] * len(codes)),
            "Code": list(codes),
            "CoName": ["Invented Works"] * len(codes),
            "Mkt": ["0111"] * len(codes),
            "ProdCat": ["011"] * len(codes),
        }
    )


class _Client:
    def __init__(self, failing: dict[str, Exception] | None = None) -> None:
        self.failing = failing or {}
        self.requests: list[str] = []

    def get_eq_master(self, code: str = "", date: str = "") -> pd.DataFrame:
        self.requests.append(date)
        if date in self.failing:
            raise self.failing[date]
        return _frame(NEXT_SESSION.get(date, date))


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.raw_dir = self.root / "listing_raw"
        self.qlib_dir = self.root / "qlib_jp"
        (self.qlib_dir / "calendars").mkdir(parents=True)
        (self.qlib_dir / "calendars" / "day.txt").write_text("\n".join(SESSIONS) + "\n", encoding="utf-8")

    def listing_files(self) -> list[str]:
        subtree = self.qlib_dir / "listing"
        return sorted(path.name for path in subtree.iterdir()) if subtree.exists() else []

    def provider_entries(self) -> list[str]:
        return sorted(path.name for path in self.qlib_dir.iterdir())


class ListingConfigTests(unittest.TestCase):
    def test_absent_or_empty_section_means_disabled_with_defaults(self) -> None:
        expected = ListingConfig(enabled=False, raw_dir=Path("./data/listing_raw"), first_year=None)
        for config in ({}, {"listing": None}, {"listing": {}}):
            with self.subTest(config=config):
                self.assertEqual(listing_config(config), expected)

    def test_enabled_section_is_returned_as_given(self) -> None:
        config = {"listing": {"enabled": True, "raw_dir": "./elsewhere", "first_year": 2030}}
        self.assertEqual(listing_config(config), ListingConfig(True, Path("./elsewhere"), 2030))

    def test_malformed_section_is_refused(self) -> None:
        sections: tuple[object, ...] = (
            [],
            {"enabled": "yes"},
            {"enabled": 1},
            {"raw_dir": ""},
            {"raw_dir": 3},
            {"first_year": "2030"},
            {"first_year": 30},
            {"first_year": True},
            {"enable": True},
            {"enabled": False, 1: "x", "typo": "y"},
        )
        for section in sections:
            with self.subTest(section=section), self.assertRaises(ValueError):
                listing_config({"listing": section})

    def test_load_config_refuses_a_listing_store_inside_the_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            storage = f'storage:\n  parquet_dir: "{tmpdir}/p"\n  qlib_dir: "{tmpdir}/q"\n'
            for raw_dir in ("q", "q/listing", "q/listing/raw"):
                inside = f'  raw_dir: "{tmpdir}/{raw_dir}"\n'
                path.write_text(storage + "listing:\n  enabled: true\n" + inside, encoding="utf-8")
                with self.subTest(raw_dir=raw_dir), self.assertRaisesRegex(ValueError, "outside storage.qlib_dir"):
                    load_config(path)
                path.write_text(storage + "listing:\n" + inside, encoding="utf-8")
                load_config(path)
            path.write_text(
                storage + f'listing:\n  enabled: true\n  raw_dir: "{tmpdir}/q_listing_raw"\n', encoding="utf-8"
            )
            load_config(path)

    def test_load_config_accepts_a_provider_that_holds_the_default_store_while_the_listing_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            path.write_text('storage:\n  parquet_dir: "./data/parquet"\n  qlib_dir: "./data"\n', encoding="utf-8")
            load_config(path)
            path.write_text(
                'storage:\n  parquet_dir: "./data/parquet"\n  qlib_dir: "./data"\nlisting:\n  enabled: true\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "outside storage.qlib_dir"):
                load_config(path)

    def test_load_config_refuses_a_malformed_listing_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            path.write_text(
                'storage:\n  parquet_dir: "./p"\n  qlib_dir: "./q"\nlisting:\n  enabled: "yes"\n', encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                load_config(path)


class TargetYearEndsTests(_Workspace):
    def test_targets_are_the_finished_years_of_the_provider_calendar(self) -> None:
        sessions = provider_sessions(self.qlib_dir)
        self.assertEqual(target_year_ends(sessions, None), YEAR_ENDS)
        self.assertEqual(target_year_ends(sessions, 2031), YEAR_ENDS[1:])
        self.assertEqual(target_year_ends(sessions, 2040), [])

    def test_a_provider_without_a_calendar_has_no_sessions(self) -> None:
        self.assertEqual(provider_sessions(self.root / "absent"), [])


class FetchMissingYearEndsTests(_Workspace):
    def test_every_target_is_fetched_once_and_a_stored_day_is_not_requested_again(self) -> None:
        first = _Client()
        self.assertEqual(fetch_missing_year_ends(first, YEAR_ENDS, self.raw_dir, **RETRY), {})
        self.assertEqual(first.requests, ["2030-12-30", "2030-12-31", "2031-12-30", "2031-12-31"])
        second = _Client()
        self.assertEqual(fetch_missing_year_ends(second, YEAR_ENDS, self.raw_dir, **RETRY), {})
        self.assertEqual(second.requests, [])

    def test_one_failing_day_is_reported_and_does_not_stop_the_others(self) -> None:
        client = _Client({"2030-12-30": ConnectionError("down")})
        failures = fetch_missing_year_ends(client, YEAR_ENDS, self.raw_dir, **RETRY)
        self.assertEqual(failures, {YEAR_ENDS[0]: "ConnectionError"})
        self.assertFalse(raw_listing_path(self.raw_dir, YEAR_ENDS[0]).exists())
        self.assertTrue(raw_listing_path(self.raw_dir, YEAR_ENDS[1]).exists())

    def test_a_failed_day_is_requested_again_on_the_next_run(self) -> None:
        fetch_missing_year_ends(_Client({"2030-12-30": ConnectionError("down")}), YEAR_ENDS, self.raw_dir, **RETRY)
        retry = _Client()
        self.assertEqual(fetch_missing_year_ends(retry, YEAR_ENDS, self.raw_dir, **RETRY), {})
        self.assertEqual(retry.requests, ["2030-12-30", "2030-12-31"])

    def test_a_candidate_that_is_not_the_year_end_is_reported_and_not_stored(self) -> None:
        not_the_last_session = dt.date(2030, 12, 27)
        client = _Client()
        with mock.patch.dict(NEXT_SESSION, {"2030-12-28": "2030-12-30"}):
            failures = fetch_missing_year_ends(client, [not_the_last_session], self.raw_dir, **RETRY)
        self.assertEqual(failures, {not_the_last_session: "ListingError"})
        self.assertFalse(self.raw_dir.exists() and any(self.raw_dir.iterdir()))

    def test_the_request_interval_holds_across_days_even_when_first_requests_fail(self) -> None:
        waits: list[float] = []
        client = _Client({"2030-12-30": ConnectionError("down"), "2031-12-30": ConnectionError("down")})
        paced = {**RETRY, "request_interval_sec": 2.0, "sleeper": waits.append}
        fetch_missing_year_ends(client, YEAR_ENDS, self.raw_dir, **paced)
        self.assertEqual(client.requests, ["2030-12-30", "2031-12-30"])
        self.assertEqual(waits, [2.0])

    def test_a_stored_name_that_cannot_be_examined_counts_as_stored(self) -> None:
        client = _Client()
        with mock.patch("jqqlib.listing_sync.os.lstat", side_effect=PermissionError("denied")):
            self.assertEqual(fetch_missing_year_ends(client, YEAR_ENDS, self.raw_dir, **RETRY), {})
        self.assertEqual(client.requests, [])

    def test_an_unreadable_stored_day_is_left_alone(self) -> None:
        self.raw_dir.mkdir()
        damaged = raw_listing_path(self.raw_dir, YEAR_ENDS[0])
        damaged.write_bytes(b"{not json")
        client = _Client()
        fetch_missing_year_ends(client, YEAR_ENDS[:1], self.raw_dir, **RETRY)
        self.assertEqual(client.requests, [])
        self.assertEqual(damaged.read_bytes(), b"{not json")


class WriteProviderListingsTests(_Workspace):
    def setUp(self) -> None:
        super().setUp()
        fetch_missing_year_ends(_Client(), YEAR_ENDS, self.raw_dir, **RETRY)

    def test_block_lists_exactly_the_files_written_with_their_hashes(self) -> None:
        block = write_provider_listings(self.raw_dir, self.qlib_dir, YEAR_ENDS)
        self.assertEqual(self.listing_files(), ["2030-12-30.json", "2031-12-30.json"])
        self.assertEqual(
            [entry["path"] for entry in block["files"]], ["listing/2030-12-30.json", "listing/2031-12-30.json"]
        )
        self.assertEqual(block["target_days"], ["2030-12-30", "2031-12-30"])
        self.assertEqual(block["unavailable"], [])
        for entry in block["files"]:
            content = (self.qlib_dir / entry["path"]).read_bytes()
            self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())
            document = json.loads(content)
            self.assertEqual(document["effective_date"], entry["day"])
            self.assertEqual(set(document["rows"][0]), {"code", "market_code", "product_category"})

    def test_rebuilding_gives_the_same_bytes_and_leaves_no_scratch(self) -> None:
        first = write_provider_listings(self.raw_dir, self.qlib_dir, YEAR_ENDS)
        second = write_provider_listings(self.raw_dir, self.qlib_dir, YEAR_ENDS)
        self.assertEqual(first, second)
        self.assertEqual(self.provider_entries(), ["calendars", "listing"])

    def test_a_file_that_is_no_longer_a_target_is_removed(self) -> None:
        write_provider_listings(self.raw_dir, self.qlib_dir, YEAR_ENDS)
        (self.qlib_dir / "listing" / "stray.json").write_text("{}", encoding="utf-8")
        write_provider_listings(self.raw_dir, self.qlib_dir, YEAR_ENDS[1:])
        self.assertEqual(self.listing_files(), ["2031-12-30.json"])

    def test_unusable_days_are_reported_with_their_reason_and_get_no_file(self) -> None:
        raw_listing_path(self.raw_dir, YEAR_ENDS[0]).write_bytes(b"{not json")
        never_fetched, failed = dt.date(2028, 12, 29), dt.date(2029, 12, 28)
        block = write_provider_listings(
            self.raw_dir, self.qlib_dir, [*YEAR_ENDS, never_fetched, failed], {failed: "ConnectionError"}
        )
        self.assertEqual(self.listing_files(), ["2031-12-30.json"])
        self.assertEqual(
            block["unavailable"],
            [
                {"day": "2028-12-29", "reason": "not_stored"},
                {"day": "2029-12-28", "reason": "fetch_failed", "error": "ConnectionError"},
                {"day": "2030-12-30", "reason": "unreadable"},
            ],
        )

    def test_a_stored_file_that_cannot_be_opened_is_reported_and_the_rest_is_written(self) -> None:
        blocked = raw_listing_path(self.raw_dir, YEAR_ENDS[0])
        blocked.unlink()
        blocked.mkdir()
        block = write_provider_listings(self.raw_dir, self.qlib_dir, YEAR_ENDS)
        self.assertEqual(block["unavailable"], [{"day": "2030-12-30", "reason": "unreadable"}])
        self.assertEqual(self.listing_files(), ["2031-12-30.json"])

    def test_a_failed_replacement_puts_the_previous_subtree_back(self) -> None:
        write_provider_listings(self.raw_dir, self.qlib_dir, YEAR_ENDS[:1])
        before = (self.qlib_dir / "listing" / "2030-12-30.json").read_bytes()
        real_rename = Path.rename

        for failure in (OSError, KeyboardInterrupt):
            def interrupted(path: Path, target: Path, failure: type[BaseException] = failure) -> Path:
                if path.name.startswith(".listing.tmp-"):
                    raise failure("stopped")
                return real_rename(path, target)

            with self.subTest(failure=failure.__name__):
                with mock.patch.object(Path, "rename", autospec=True, side_effect=interrupted):
                    with self.assertRaises(failure):
                        write_provider_listings(self.raw_dir, self.qlib_dir, YEAR_ENDS)
                self.assertEqual(self.listing_files(), ["2030-12-30.json"])
                self.assertEqual((self.qlib_dir / "listing" / "2030-12-30.json").read_bytes(), before)
                self.assertEqual(self.provider_entries(), ["calendars", "listing"])

    def test_with_no_usable_day_the_subtree_is_removed(self) -> None:
        write_provider_listings(self.raw_dir, self.qlib_dir, YEAR_ENDS)
        block = write_provider_listings(self.root / "empty_raw", self.qlib_dir, YEAR_ENDS)
        self.assertEqual(block["files"], [])
        self.assertEqual(self.provider_entries(), ["calendars"])

    def test_leftover_scratch_from_an_interrupted_run_is_cleared(self) -> None:
        (self.qlib_dir / ".listing.tmp-dead").mkdir()
        (self.qlib_dir / ".listing.old-dead").mkdir()
        write_provider_listings(self.raw_dir, self.qlib_dir, YEAR_ENDS)
        self.assertEqual(self.provider_entries(), ["calendars", "listing"])

    def test_a_provider_reached_through_a_symlink_gets_the_subtree_in_its_target(self) -> None:
        link = self.root / "current"
        link.symlink_to(self.qlib_dir, target_is_directory=True)
        write_provider_listings(self.raw_dir, link, YEAR_ENDS)
        self.assertEqual(self.listing_files(), ["2030-12-30.json", "2031-12-30.json"])
        self.assertTrue(link.is_symlink())


class ProviderListingTests(_Workspace):
    def config(self, **listing: object) -> dict[str, Any]:
        return {"listing": {"raw_dir": str(self.raw_dir), **listing}}

    def test_disabled_without_a_listing_entry_requests_and_writes_nothing(self) -> None:
        with mock.patch.object(pipeline, "create_jquants_client") as create_client:
            for fetch in (True, False):
                self.assertIsNone(pipeline.provider_listing(self.config(enabled=False), self.qlib_dir, fetch=fetch))
                self.assertIsNone(pipeline.provider_listing({}, self.qlib_dir, fetch=fetch))
        create_client.assert_not_called()
        self.assertEqual(self.provider_entries(), ["calendars"])
        self.assertFalse(self.raw_dir.exists())

    def _enabled_build(self) -> dict[str, bytes]:
        fetch_missing_year_ends(_Client(), YEAR_ENDS, self.raw_dir, **RETRY)
        block = write_provider_listings(self.raw_dir, self.qlib_dir, YEAR_ENDS)
        (self.qlib_dir / "dataset_manifest.json").write_text(json.dumps({"listing": block}), encoding="utf-8")
        return {path.name: path.read_bytes() for path in self.raw_dir.iterdir()}

    def test_disabled_removes_the_subtree_the_previous_manifest_records_and_keeps_the_stored_responses(self) -> None:
        stored = self._enabled_build()
        with self.assertLogs("jqqlib", level="WARNING") as logs:
            self.assertIsNone(pipeline.provider_listing(self.config(enabled=False), self.qlib_dir, fetch=True))
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(self.provider_entries(), ["calendars", "dataset_manifest.json"])
        self.assertEqual({path.name: path.read_bytes() for path in self.raw_dir.iterdir()}, stored)
        with self.assertNoLogs("jqqlib", level="WARNING"):
            self.assertIsNone(pipeline.provider_listing(self.config(enabled=False), self.qlib_dir, fetch=True))

    def test_disabled_keeps_a_listing_entry_the_previous_manifest_does_not_fully_record(self) -> None:
        def unrecorded_file() -> None:
            (self.qlib_dir / "listing" / "notes.json").write_text("{}", encoding="utf-8")

        def changed_file() -> None:
            (self.qlib_dir / "listing" / "2030-12-30.json").write_text("{}", encoding="utf-8")

        def nested_directory() -> None:
            (self.qlib_dir / "listing" / "mine").mkdir()

        def no_manifest() -> None:
            (self.qlib_dir / "dataset_manifest.json").unlink()

        def unreadable_manifest() -> None:
            (self.qlib_dir / "dataset_manifest.json").write_text("{not json", encoding="utf-8")

        def manifest_without_a_block() -> None:
            (self.qlib_dir / "dataset_manifest.json").write_text("{}", encoding="utf-8")

        def a_plain_file() -> None:
            shutil.rmtree(self.qlib_dir / "listing")
            (self.qlib_dir / "listing").write_text("mine", encoding="utf-8")

        def a_symlink_to_an_identical_directory() -> None:
            (self.qlib_dir / "listing").rename(self.root / "elsewhere")
            (self.qlib_dir / "listing").symlink_to(self.root / "elsewhere", target_is_directory=True)

        def a_recorded_name_that_is_a_symlink_to_identical_bytes() -> None:
            recorded = self.qlib_dir / "listing" / "2030-12-30.json"
            recorded.rename(self.root / "elsewhere.json")
            recorded.symlink_to(self.root / "elsewhere.json")

        for change in (unrecorded_file, changed_file, nested_directory, no_manifest, unreadable_manifest,
                       manifest_without_a_block, a_plain_file, a_symlink_to_an_identical_directory,
                       a_recorded_name_that_is_a_symlink_to_identical_bytes):
            shutil.rmtree(self.root)
            self.setUp()
            self._enabled_build()
            change()
            before = {
                str(path.relative_to(self.root)): path.read_bytes() if path.is_file() else None
                for path in sorted(self.root.rglob("*"))
            }
            with self.subTest(change=change.__name__):
                with self.assertLogs("jqqlib", level="WARNING") as logs:
                    self.assertIsNone(pipeline.provider_listing({}, self.qlib_dir, fetch=True))
                self.assertIn("left as it is", logs.output[0])
                after = {
                    str(path.relative_to(self.root)): path.read_bytes() if path.is_file() else None
                    for path in sorted(self.root.rglob("*"))
                }
                self.assertEqual(after, before)

    def test_disabled_leaves_an_empty_listing_directory_alone_without_a_warning(self) -> None:
        (self.qlib_dir / "listing").mkdir()
        with self.assertNoLogs("jqqlib", level="WARNING"):
            self.assertIsNone(pipeline.provider_listing({}, self.qlib_dir, fetch=True))
        self.assertEqual(self.provider_entries(), ["calendars", "listing"])

    def test_disabled_survives_a_listing_entry_that_cannot_be_removed(self) -> None:
        self._enabled_build()
        with mock.patch("jqqlib.listing_sync.shutil.rmtree", side_effect=PermissionError("denied")):
            with self.assertLogs("jqqlib", level="WARNING") as logs:
                self.assertIsNone(pipeline.provider_listing({}, self.qlib_dir, fetch=True))
        self.assertIn("could not be examined or removed (PermissionError)", logs.output[0])

    def test_enabled_with_fetch_stores_and_writes_the_targets_from_first_year_on(self) -> None:
        client = _Client()
        with mock.patch.object(pipeline, "create_jquants_client", return_value=client):
            block = pipeline.provider_listing(self.config(enabled=True, first_year=2031), self.qlib_dir, fetch=True)
        assert block is not None
        self.assertEqual(client.requests, ["2031-12-30", "2031-12-31"])
        self.assertEqual([entry["day"] for entry in block["files"]], ["2031-12-30"])
        self.assertEqual(self.listing_files(), ["2031-12-30.json"])

    def test_enabled_without_fetch_never_creates_a_client(self) -> None:
        with mock.patch.object(pipeline, "create_jquants_client") as create_client:
            block = pipeline.provider_listing(self.config(enabled=True), self.qlib_dir, fetch=False)
        create_client.assert_not_called()
        assert block is not None
        self.assertEqual([entry["reason"] for entry in block["unavailable"]], ["not_stored", "not_stored"])

    def test_a_missing_credential_is_recorded_and_not_raised(self) -> None:
        with mock.patch.object(pipeline, "create_jquants_client", side_effect=ValueError("no credential")):
            with self.assertLogs("jqqlib", level="WARNING") as logs:
                block = pipeline.provider_listing(self.config(enabled=True), self.qlib_dir, fetch=True)
        assert block is not None
        self.assertEqual(
            block["unavailable"],
            [
                {"day": "2030-12-30", "reason": "fetch_failed", "error": "ValueError"},
                {"day": "2031-12-30", "reason": "fetch_failed", "error": "ValueError"},
            ],
        )
        self.assertEqual(len(logs.records), 2)
        self.assertEqual(self.provider_entries(), ["calendars"])


class ManifestListingBlockTests(_Workspace):
    def setUp(self) -> None:
        super().setUp()
        self.parquet_path = self.root / "daily_quotes.parquet"
        pd.DataFrame(
            {
                "symbol": ["90010", "90010"],
                "date": ["2030-12-27", "2030-12-30"],
                "open": [1.0, 2.0],
                "high": [1.1, 2.1],
                "low": [0.9, 1.9],
                "close": [1.0, 2.0],
                "volume": [100, 200],
                "amount": [1000, 2000],
                "adjustment_factor": [1.0, 1.0],
            }
        ).to_parquet(self.parquet_path, index=False)
        (self.qlib_dir / "instruments").mkdir()
        (self.qlib_dir / "instruments" / "all.txt").write_text("90010\t2030-12-27\t2030-12-30\n", encoding="utf-8")

    def manifest(self, listing: dict[str, Any] | None) -> dict[str, Any]:
        path, _ = write_dataset_artifacts(
            parquet_path=self.parquet_path, qlib_dir=self.qlib_dir, source={"kind": "test"}, listing=listing
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_without_a_block_the_manifest_has_no_listing_key(self) -> None:
        manifest = self.manifest(None)
        self.assertNotIn("listing", manifest)
        jsonschema.validate(manifest, load_schema("dataset_manifest"))

    def test_the_block_is_recorded_validated_and_covered_by_the_manifest_hash(self) -> None:
        fetch_missing_year_ends(_Client(), YEAR_ENDS[:1], self.raw_dir, **RETRY)
        block = write_provider_listings(self.raw_dir, self.qlib_dir, YEAR_ENDS)
        with_block, without_block = self.manifest(block), self.manifest(None)
        self.assertEqual(with_block["listing"], block)
        self.assertEqual(len(block["files"]), 1)
        jsonschema.validate(with_block, load_schema("dataset_manifest"))
        self.assertEqual(with_block["dataset_id"], without_block["dataset_id"])
        recorded = with_block["lineage"].pop("manifest_sha256")
        self.assertEqual(recorded, _sha256_json(with_block))
        with_block["listing"]["files"][0]["sha256"] = "0" * 64
        self.assertNotEqual(recorded, _sha256_json(with_block))

    def test_the_schema_refuses_a_block_with_an_unknown_reason_or_a_path_outside_the_subtree(self) -> None:
        good = {"schema_version": 1, "target_days": [], "files": [], "unavailable": []}
        bad_reason = {**good, "unavailable": [{"day": "2030-12-30", "reason": "skipped"}]}
        bad_path = {**good, "files": [{"day": "2030-12-30", "path": "features/2030-12-30.json", "sha256": "0" * 64}]}
        extra_key = {**good, "note": "x"}
        for block in (bad_reason, bad_path, extra_key):
            manifest = self.manifest(None)
            manifest["listing"] = block
            with self.subTest(block=block), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(manifest, load_schema("dataset_manifest"))


class BuildWithListingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.csv_path = write_synthetic_daily_quotes_csv(
            self.root / "daily_quotes.csv", symbols=["90010", "90020"], start="2030-12-23", end="2031-01-10"
        )

    def build(self, name: str, listing: str) -> Path:
        base = self.root / name
        config_path = self.root / f"{name}.yaml"
        config_path.write_text(
            f"storage:\n  parquet_dir: {base / 'parquet'}\n  qlib_dir: {base / 'qlib'}\n{listing}", encoding="utf-8"
        )
        return pipeline.run_bootstrap_csv(config_path, self.csv_path)

    def manifest(self, qlib_dir: Path) -> dict[str, Any]:
        return json.loads((qlib_dir / "dataset_manifest.json").read_text(encoding="utf-8"))

    def provider_files(self, qlib_dir: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(qlib_dir)): path.read_bytes()
            for path in sorted(qlib_dir.rglob("*"))
            if path.is_file() and path.suffix != ".json"
        }

    def without_build_details(self, qlib_dir: Path) -> dict[str, Any]:
        text = (qlib_dir / "dataset_manifest.json").read_text(encoding="utf-8")
        stable = json.loads(text.replace(str(qlib_dir.parent), "<build>"))
        self.assertIn("generated_at", stable)
        del stable["generated_at"]
        del stable["source"]
        stable["lineage"] = {"parquet_sha256": stable["lineage"]["parquet_sha256"]}
        return stable

    def test_a_disabled_build_matches_a_build_without_the_section(self) -> None:
        plain = self.build("plain", "")
        disabled = self.build("disabled", f"listing:\n  enabled: false\n  raw_dir: {self.root / 'raw'}\n")
        self.assertEqual(
            sorted(path.name for path in plain.iterdir()), sorted(path.name for path in disabled.iterdir())
        )
        self.assertFalse((disabled / "listing").exists())
        self.assertFalse((self.root / "raw").exists())
        self.assertEqual(self.provider_files(plain), self.provider_files(disabled))
        plain_manifest, disabled_manifest = self.manifest(plain), self.manifest(disabled)
        self.assertNotIn("listing", disabled_manifest)
        self.assertEqual(self.without_build_details(plain), self.without_build_details(disabled))
        self.assertEqual(plain_manifest["dataset_id"], disabled_manifest["dataset_id"])

    def test_run_all_fetches_the_year_end_and_records_it_and_survives_a_failed_fetch(self) -> None:
        self.build("api", f"listing:\n  enabled: true\n  raw_dir: {self.root / 'raw'}\n")
        config_path = self.root / "api.yaml"
        parquet_path = self.root / "api" / "parquet" / "daily_quotes.parquet"
        window = (dt.date(2030, 12, 23), dt.date(2031, 1, 10))
        down = _Client({"2030-12-31": ConnectionError("down")})
        up = _Client()
        following = {"2031-01-01": "2031-01-06"}
        with mock.patch.object(pipeline, "run_etl", return_value=parquet_path), mock.patch.dict(
            NEXT_SESSION, following, clear=True
        ):
            with mock.patch.object(pipeline, "create_jquants_client", return_value=down):
                with self.assertLogs("jqqlib", level="WARNING"):
                    pipeline.run_all(config_path, *window)
            failed = self.manifest(self.root / "api" / "qlib")["listing"]
            with mock.patch.object(pipeline, "create_jquants_client", return_value=up):
                pipeline.run_all(config_path, *window)
            fetched = self.manifest(self.root / "api" / "qlib")["listing"]
        self.assertEqual(failed["files"], [])
        self.assertEqual(
            failed["unavailable"], [{"day": "2030-12-31", "reason": "fetch_failed", "error": "ConnectionError"}]
        )
        self.assertEqual(up.requests, ["2030-12-31", "2031-01-01"])
        self.assertEqual([entry["day"] for entry in fetched["files"]], ["2030-12-31"])
        self.assertEqual(fetched["unavailable"], [])

    def test_a_build_completes_when_a_stored_listing_cannot_be_read(self) -> None:
        raw_dir = self.root / "raw"
        raw_dir.mkdir()
        (raw_dir / "2030-12-31.json").mkdir()
        with self.assertLogs("jqqlib", level="WARNING"):
            qlib_dir = self.build("blocked", f"listing:\n  enabled: true\n  raw_dir: {raw_dir}\n")
        manifest = self.manifest(qlib_dir)
        self.assertEqual(manifest["listing"]["unavailable"], [{"day": "2030-12-31", "reason": "unreadable"}])
        self.assertEqual(manifest["dataset_id"], self.manifest(self.build("plain", ""))["dataset_id"])

    def test_an_enabled_build_writes_the_stored_year_end_and_records_it(self) -> None:
        raw_dir = self.root / "raw"
        year_end = max(
            day for day in provider_sessions(self.build("probe", "")) if day.year == 2030
        )
        client = _Client()
        following = {(year_end + dt.timedelta(days=1)).isoformat(): "2031-01-06"}
        with mock.patch.dict(NEXT_SESSION, following, clear=True):
            self.assertEqual(fetch_missing_year_ends(client, [year_end], raw_dir, **RETRY), {})
        qlib_dir = self.build("enabled", f"listing:\n  enabled: true\n  raw_dir: {raw_dir}\n")
        manifest = self.manifest(qlib_dir)
        jsonschema.validate(manifest, load_schema("dataset_manifest"))
        self.assertEqual(manifest["listing"]["target_days"], [year_end.isoformat()])
        self.assertEqual(manifest["listing"]["unavailable"], [])
        (entry,) = manifest["listing"]["files"]
        self.assertEqual(hashlib.sha256((qlib_dir / entry["path"]).read_bytes()).hexdigest(), entry["sha256"])
        self.assertEqual(manifest["dataset_id"], self.manifest(self.root / "probe" / "qlib")["dataset_id"])


if __name__ == "__main__":
    unittest.main()
