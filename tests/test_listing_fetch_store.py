from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from jqqlib.fetch_listing import fetch_listing_master, fetch_year_end_listing
from jqqlib.listing import ListingError, YearEndListing, listing_document, single_effective_day
from jqqlib.store_listing import (
    raw_listing_path,
    read_raw_listing,
    stored_listing_days,
    write_raw_listing,
)

YEAR_END = dt.date(2031, 12, 30)
NEXT_SESSION = dt.date(2032, 1, 5)
RETRY = {"retries": 0, "retry_base_delay_sec": 0.0, "retry_max_delay_sec": 0.0, "retry_jitter_sec": 0.0}


def _frame(day: dt.date, codes: tuple[str, ...] = ("90020", "90010")) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.to_datetime([day.isoformat()] * len(codes)),
            "Code": list(codes),
            "CoName": ["Invented Works"] * len(codes),
            "Mkt": ["0111"] * len(codes),
            "ProdCat": ["011"] * len(codes),
        }
    )


class _Client:
    def __init__(self, answers: dict[str, pd.DataFrame | None]) -> None:
        self.answers = answers
        self.requests: list[tuple[str, str]] = []

    def get_eq_master(self, code: str = "", date: str = "") -> pd.DataFrame | None:
        self.requests.append((code, date))
        return self.answers[date]


def _client(**overrides: pd.DataFrame | None) -> _Client:
    answers: dict[str, pd.DataFrame | None] = {
        "2031-12-30": _frame(YEAR_END),
        "2031-12-31": _frame(NEXT_SESSION),
    }
    answers.update({key.replace("_", "-"): value for key, value in overrides.items()})
    return _Client(answers)


def _fetch(client: object, pauses: list[float] | None = None) -> YearEndListing:
    sink = pauses if pauses is not None else []
    return fetch_year_end_listing(client, YEAR_END, request_interval_sec=0.5, sleeper=sink.append, **RETRY)


def _listing() -> YearEndListing:
    return _fetch(_client())


class SingleEffectiveDayTests(unittest.TestCase):
    def test_rows_sharing_one_date_yield_that_day(self) -> None:
        self.assertEqual(single_effective_day(_frame(NEXT_SESSION).to_dict("records")), NEXT_SESSION)

    def test_an_empty_response_or_mixed_dates_are_refused(self) -> None:
        mixed = [*_frame(NEXT_SESSION).to_dict("records"), *_frame(YEAR_END, ("90030",)).to_dict("records")]
        for rows in ([], mixed, [{"Date": None}]):
            with self.subTest(rows=len(rows)), self.assertRaises(ListingError):
                single_effective_day(rows)


class FetchYearEndListingTests(unittest.TestCase):
    def test_the_day_and_the_day_after_are_requested_for_all_issues_with_a_pause_between(self) -> None:
        client, pauses = _client(), []
        listing = _fetch(client, pauses)
        self.assertEqual(client.requests, [("", "2031-12-30"), ("", "2031-12-31")])
        self.assertEqual(pauses, [0.5])
        self.assertEqual((listing.day, listing.confirmation_requested, listing.confirmation_effective),
                         (YEAR_END, dt.date(2031, 12, 31), NEXT_SESSION))
        self.assertEqual([row["CoName"] for row in listing.rows], ["Invented Works", "Invented Works"])

    def test_a_candidate_answered_with_another_day_is_refused_before_the_confirmation_request(self) -> None:
        client = _client(**{"2031_12_30": _frame(NEXT_SESSION)})
        with self.assertRaises(ListingError):
            _fetch(client)
        self.assertEqual(client.requests, [("", "2031-12-30")])

    def test_an_empty_or_absent_answer_is_refused(self) -> None:
        for answer in (_frame(YEAR_END, ()), None):
            with self.subTest(answer=type(answer).__name__), self.assertRaises(ListingError):
                _fetch(_client(**{"2031_12_30": answer}))

    def test_a_later_session_in_the_same_year_refuses_the_candidate(self) -> None:
        with self.assertRaises(ListingError):
            _fetch(_client(**{"2031_12_31": _frame(dt.date(2031, 12, 31))}))

    def test_a_confirmation_answer_with_mixed_dates_is_refused(self) -> None:
        mixed = pd.concat([_frame(NEXT_SESSION), _frame(dt.date(2031, 12, 31), ("90030",))], ignore_index=True)
        with self.assertRaises(ListingError):
            _fetch(_client(**{"2031_12_31": mixed}))

    def test_a_client_without_the_listing_endpoint_is_refused(self) -> None:
        with self.assertRaises(ListingError):
            fetch_listing_master(object(), YEAR_END, **RETRY)


class RawListingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.raw_dir = Path(directory.name) / "listing_raw"

    def test_a_stored_response_reads_back_to_the_same_document(self) -> None:
        listing = _listing()
        path = write_raw_listing(self.raw_dir, listing)
        self.assertEqual(path, raw_listing_path(self.raw_dir, YEAR_END))
        stored = read_raw_listing(self.raw_dir, YEAR_END)
        self.assertEqual(listing_document(stored.rows, requested=YEAR_END),
                         listing_document(listing.rows, requested=YEAR_END))
        self.assertEqual(stored.confirmation_effective, NEXT_SESSION)
        self.assertEqual(sorted(row["CoName"] for row in stored.rows), ["Invented Works", "Invented Works"])

    def test_stored_bytes_do_not_depend_on_row_order(self) -> None:
        other_dir = self.raw_dir.parent / "other"
        first = write_raw_listing(self.raw_dir, _listing())
        reordered = _fetch(_client(**{"2031_12_30": _frame(YEAR_END, ("90010", "90020"))}))
        self.assertEqual(first.read_bytes(), write_raw_listing(other_dir, reordered).read_bytes())

    def test_storing_the_same_response_again_changes_nothing(self) -> None:
        path = write_raw_listing(self.raw_dir, _listing())
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        write_raw_listing(self.raw_dir, _listing())
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_a_different_response_for_a_stored_day_is_refused_and_the_file_is_kept(self) -> None:
        path = write_raw_listing(self.raw_dir, _listing())
        before = path.read_bytes()
        changed = _fetch(_client(**{"2031_12_30": _frame(YEAR_END, ("90010", "90020", "90030"))}))
        with self.assertRaises(ListingError):
            write_raw_listing(self.raw_dir, changed)
        self.assertEqual(path.read_bytes(), before)

    def test_an_unchecked_response_is_not_stored_and_leaves_no_file(self) -> None:
        listing = _listing()
        wrong_year = YearEndListing(listing.day, listing.confirmation_requested, dt.date(2031, 12, 31), listing.rows)
        wrong_rows = YearEndListing(listing.day, listing.confirmation_requested, NEXT_SESSION,
                                    tuple(_frame(NEXT_SESSION).to_dict("records")))
        wrong_request = YearEndListing(listing.day, dt.date(2032, 1, 1), NEXT_SESSION, listing.rows)
        for name, bad in (("year", wrong_year), ("rows", wrong_rows), ("request", wrong_request)):
            with self.subTest(name=name), self.assertRaises(ListingError):
                write_raw_listing(self.raw_dir, bad)
        self.assertEqual(list(self.raw_dir.glob("*")) if self.raw_dir.exists() else [], [])

    def test_a_writer_that_loses_the_race_does_not_replace_the_stored_file(self) -> None:
        winner = _fetch(_client(**{"2031_12_30": _frame(YEAR_END, ("90010", "90020", "90030"))}))
        winner_dir = self.raw_dir.parent / "winner"
        winner_bytes = write_raw_listing(winner_dir, winner).read_bytes()
        path = raw_listing_path(self.raw_dir, YEAR_END)
        real_link = os.link

        def another_writer_finishes_first(source, target):
            Path(target).write_bytes(winner_bytes)
            return real_link(source, target)

        with mock.patch("jqqlib.store_listing.os.link", side_effect=another_writer_finishes_first):
            with self.assertRaises(ListingError):
                write_raw_listing(self.raw_dir, _listing())
        self.assertEqual(path.read_bytes(), winner_bytes)
        self.assertEqual([entry.name for entry in self.raw_dir.iterdir()], [path.name])

    def test_a_failed_write_leaves_neither_the_file_nor_a_temporary(self) -> None:
        with mock.patch("jqqlib.store_listing.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                write_raw_listing(self.raw_dir, _listing())
        self.assertEqual(list(self.raw_dir.iterdir()), [])

    def test_a_dangling_link_at_the_stored_name_is_refused_and_kept(self) -> None:
        self.raw_dir.mkdir(parents=True)
        path = raw_listing_path(self.raw_dir, YEAR_END)
        path.symlink_to(self.raw_dir / "absent.json")
        with self.assertRaises(ListingError):
            write_raw_listing(self.raw_dir, _listing())
        self.assertTrue(path.is_symlink())

    def test_every_response_field_is_stored_with_missing_values_as_null(self) -> None:
        frame = pd.DataFrame(
            {
                "Date": pd.to_datetime(["2031-12-30", "2031-12-30"]),
                "Code": ["90020", "90010"],
                "CoName": ["Invented Works", None],
                "ScaleCat": ["-", float("nan")],
                "Mkt": ["0111", "0113"],
                "ProdCat": ["011", "011"],
                "Lots": [100, 1],
                "Ratio": [0.5, 1.5],
                "Flag": [True, False],
            }
        )
        path = write_raw_listing(self.raw_dir, _fetch(_client(**{"2031_12_30": frame})))
        self.assertEqual(
            path.read_bytes(),
            b'{"confirmation":{"effective_date":"2032-01-05","requested_date":"2031-12-31"},'
            b'"effective_date":"2031-12-30","requested_date":"2031-12-30","rows":['
            b'{"CoName":null,"Code":"90010","Date":"2031-12-30","Flag":false,"Lots":1,"Mkt":"0113",'
            b'"ProdCat":"011","Ratio":1.5,"ScaleCat":null},'
            b'{"CoName":"Invented Works","Code":"90020","Date":"2031-12-30","Flag":true,"Lots":100,"Mkt":"0111",'
            b'"ProdCat":"011","Ratio":0.5,"ScaleCat":"-"}],"schema":1}',
        )

    def test_reading_refuses_an_absent_damaged_or_tampered_file(self) -> None:
        with self.assertRaises(ListingError):
            read_raw_listing(self.raw_dir, YEAR_END)
        path = write_raw_listing(self.raw_dir, _listing())
        good = json.loads(path.read_bytes())

        def stored(mutate) -> None:
            document = json.loads(json.dumps(good))
            mutate(document)
            path.write_text(json.dumps(document), encoding="utf-8")

        mutations = {
            "another day": lambda d: d.__setitem__("effective_date", "2031-12-29"),
            "same-year confirmation": lambda d: d["confirmation"].__setitem__("effective_date", "2031-12-31"),
            "confirmation for another request": lambda d: d["confirmation"].__setitem__("requested_date", "2032-01-01"),
            "row dated later": lambda d: d["rows"][0].__setitem__("Date", "2032-01-05"),
            "unknown schema": lambda d: d.__setitem__("schema", 2),
            "boolean schema": lambda d: d.__setitem__("schema", True),
            "float schema": lambda d: d.__setitem__("schema", 1.0),
            "nested value": lambda d: d["rows"][0].__setitem__("CoName", ["x"]),
            "extra key": lambda d: d.__setitem__("note", "x"),
            "rows not a list": lambda d: d.__setitem__("rows", {}),
        }
        for name, mutate in mutations.items():
            stored(mutate)
            with self.subTest(name=name), self.assertRaises(ListingError):
                read_raw_listing(self.raw_dir, YEAR_END)
        path.write_bytes(b"{not json")
        with self.assertRaises(ListingError):
            read_raw_listing(self.raw_dir, YEAR_END)

    def test_stored_days_lists_day_named_files_only_oldest_first(self) -> None:
        self.raw_dir.mkdir(parents=True)
        for name in ("2031-12-30.json", "2030-12-30.json", "notes.json", "20301230.json", ".2031-12-30.json.x.tmp"):
            (self.raw_dir / name).write_text("{}", encoding="utf-8")
        self.assertEqual(stored_listing_days(self.raw_dir), [dt.date(2030, 12, 30), dt.date(2031, 12, 30)])
        self.assertEqual(stored_listing_days(self.raw_dir / "absent"), [])


if __name__ == "__main__":
    unittest.main()
