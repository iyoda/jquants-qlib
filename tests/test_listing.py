from __future__ import annotations

import datetime as dt
import json
import unittest

import pandas as pd

from jqqlib.listing import (
    ListingError,
    completed_year_ends,
    confirm_year_end,
    listing_document,
    normalize_listing,
)

REQUESTED = dt.date(2031, 12, 30)


def _row(code: str = "90010", **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "Date": "2031-12-30",
        "Code": code,
        "CoName": "Invented Works",
        "CoNameEn": "Invented Works",
        "S17": "1",
        "S33": "0050",
        "ScaleCat": "-",
        "Mkt": "0111",
        "MktNm": "invented",
        "Mrgn": "1",
        "ProdCat": "011",
    }
    row.update(overrides)
    return row


class CompletedYearEndsTests(unittest.TestCase):
    def test_a_year_without_a_session_in_the_following_year_is_not_finished(self) -> None:
        sessions = [dt.date(2030, 12, 30), dt.date(2031, 1, 6), dt.date(2031, 12, 30)]
        self.assertEqual(completed_year_ends(sessions), {2030: dt.date(2030, 12, 30)})

    def test_the_candidate_is_the_last_session_whatever_the_input_order(self) -> None:
        sessions = [dt.date(2031, 1, 6), dt.date(2030, 12, 30), dt.date(2030, 12, 27), dt.date(2030, 3, 1)]
        self.assertEqual(completed_year_ends(sessions), {2030: dt.date(2030, 12, 30)})

    def test_no_sessions_means_no_candidate(self) -> None:
        self.assertEqual(completed_year_ends([]), {})

    def test_repeated_sessions_and_midnight_timestamps_yield_plain_days(self) -> None:
        sessions = [pd.Timestamp("2030-12-30"), dt.date(2030, 12, 30), dt.datetime(2031, 1, 6)]
        result = completed_year_ends(sessions)
        self.assertEqual(result, {2030: dt.date(2030, 12, 30)})
        self.assertIs(type(result[2030]), dt.date)

    def test_a_session_with_a_time_of_day_or_a_time_zone_is_refused(self) -> None:
        for session in (dt.datetime(2030, 12, 30, 15, 0), pd.Timestamp("2030-12-30", tz="Asia/Tokyo")):
            with self.subTest(session=session), self.assertRaises(ListingError):
                completed_year_ends([session, dt.date(2031, 1, 6)])

    def test_a_missing_final_session_yields_an_earlier_candidate_that_only_confirmation_rejects(self) -> None:
        sessions_missing_the_30th = [dt.date(2030, 12, 27), dt.date(2031, 1, 6)]
        candidate = completed_year_ends(sessions_missing_the_30th)[2030]
        self.assertEqual(candidate, dt.date(2030, 12, 27))
        with self.assertRaises(ListingError):
            confirm_year_end(candidate, dt.date(2030, 12, 30))


class ConfirmYearEndTests(unittest.TestCase):
    def test_next_business_day_in_the_following_year_confirms_the_candidate(self) -> None:
        confirm_year_end(dt.date(2030, 12, 30), dt.date(2031, 1, 6))
        confirm_year_end(dt.date(2030, 12, 31), dt.date(2031, 1, 6))

    def test_an_answer_dated_on_or_before_the_candidate_is_refused(self) -> None:
        for effective in (dt.date(2030, 12, 30), dt.date(2030, 12, 27)):
            with self.subTest(effective=effective), self.assertRaises(ListingError):
                confirm_year_end(dt.date(2030, 12, 30), effective)

    def test_a_later_session_of_the_same_year_is_refused(self) -> None:
        with self.assertRaises(ListingError):
            confirm_year_end(dt.date(2030, 12, 27), dt.date(2030, 12, 30))

    def test_an_answer_more_than_one_year_ahead_is_refused(self) -> None:
        with self.assertRaises(ListingError):
            confirm_year_end(dt.date(2030, 12, 30), dt.date(2032, 1, 5))


class NormalizeListingTests(unittest.TestCase):
    def test_only_code_market_and_product_category_survive(self) -> None:
        self.assertEqual(
            normalize_listing([_row()], requested=REQUESTED),
            [{"code": "90010", "market_code": "0111", "product_category": "011"}],
        )

    def test_rows_are_sorted_by_code(self) -> None:
        rows = [_row("90020"), _row("9A010"), _row("90010")]
        self.assertEqual(
            [row["code"] for row in normalize_listing(rows, requested=REQUESTED)], ["90010", "90020", "9A010"]
        )

    def test_alphanumeric_and_non_zero_suffix_codes_are_kept(self) -> None:
        rows = [_row("9A010"), _row("90015")]
        self.assertEqual(
            [row["code"] for row in normalize_listing(rows, requested=REQUESTED)], ["90015", "9A010"]
        )

    def test_a_midnight_timestamp_or_a_date_counts_as_its_day(self) -> None:
        for date in (pd.Timestamp("2031-12-30"), dt.datetime(2031, 12, 30), dt.date(2031, 12, 30)):
            with self.subTest(date=date):
                self.assertEqual(len(normalize_listing([_row(Date=date)], requested=REQUESTED)), 1)

    def test_a_date_that_only_starts_like_the_requested_day_is_refused(self) -> None:
        dates = (
            "2031-12-30garbage",
            "2031-12-30T99:99:99",
            "2031-12-30 00:00:00",
            " 2031-12-30",
            "2031-13-30",
            pd.Timestamp("2031-12-30 09:00"),
            pd.Timestamp("2031-12-30", tz="Asia/Tokyo"),
            20311230,
        )
        for date in dates:
            with self.subTest(date=date), self.assertRaises(ListingError):
                normalize_listing([_row(Date=date)], requested=REQUESTED)

    def test_a_requested_day_with_a_time_of_day_is_refused(self) -> None:
        with self.assertRaises(ListingError):
            normalize_listing([_row()], requested=dt.datetime(2031, 12, 30, 9, 0))

    def test_codes_that_are_not_text_exactly_as_published_are_refused(self) -> None:
        overrides: tuple[dict[str, object], ...] = (
            {"Code": 90010},
            {"Code": 90010.0},
            {"Code": " 90010"},
            {"Mkt": 111},
            {"ProdCat": 11},
            {"Mkt": "0111 "},
        )
        for override in overrides:
            with self.subTest(override=override), self.assertRaises(ListingError):
                normalize_listing([_row(**override)], requested=REQUESTED)

    def test_a_response_dated_other_than_requested_is_refused_in_both_directions(self) -> None:
        for date in ("2032-01-05", "2031-12-29"):
            with self.subTest(date=date), self.assertRaises(ListingError):
                normalize_listing([_row(), _row("90020", Date=date)], requested=REQUESTED)

    def test_an_empty_response_is_refused(self) -> None:
        with self.assertRaises(ListingError):
            normalize_listing([], requested=REQUESTED)

    def test_a_missing_null_or_blank_required_field_is_refused(self) -> None:
        for field in ("Date", "Code", "Mkt", "ProdCat"):
            for value in (None, float("nan"), pd.NA, pd.NaT, "", " "):
                with self.subTest(field=field, value=value), self.assertRaises(ListingError):
                    normalize_listing([_row(**{field: value})], requested=REQUESTED)
            absent = _row()
            del absent[field]
            with self.subTest(field=field, value="absent"), self.assertRaises(ListingError):
                normalize_listing([absent], requested=REQUESTED)

    def test_a_malformed_code_is_refused(self) -> None:
        for code in ("9001", "900100", "9a010", "9001-", "９００１０"):
            with self.subTest(code=code), self.assertRaises(ListingError):
                normalize_listing([_row(code)], requested=REQUESTED)

    def test_a_repeated_code_is_refused(self) -> None:
        with self.assertRaises(ListingError):
            normalize_listing([_row(), _row(Mkt="0112")], requested=REQUESTED)

    def test_an_unknown_market_or_product_code_is_refused(self) -> None:
        for override in ({"Mkt": "0199"}, {"ProdCat": "099"}, {"ProdCat": "11"}):
            with self.subTest(override=override), self.assertRaises(ListingError):
                normalize_listing([_row(**override)], requested=REQUESTED)


class ListingDocumentTests(unittest.TestCase):
    def test_row_order_does_not_change_the_bytes(self) -> None:
        rows = [_row("90010"), _row("90020", Mkt="0113"), _row("9A010", ProdCat="014", Mkt="0109")]
        self.assertEqual(
            listing_document(rows, requested=REQUESTED), listing_document(rows[::-1], requested=REQUESTED)
        )

    def test_document_carries_both_dates_and_no_other_response_field(self) -> None:
        document = json.loads(listing_document([_row()], requested=REQUESTED))
        self.assertEqual(
            document,
            {
                "schema": 1,
                "requested_date": "2031-12-30",
                "effective_date": "2031-12-30",
                "rows": [{"code": "90010", "market_code": "0111", "product_category": "011"}],
            },
        )

    def test_bytes_are_compact_sorted_and_end_without_a_newline(self) -> None:
        self.assertEqual(
            listing_document([_row("90020", Mkt="0113"), _row()], requested=REQUESTED),
            b'{"effective_date":"2031-12-30","requested_date":"2031-12-30","rows":['
            b'{"code":"90010","market_code":"0111","product_category":"011"},'
            b'{"code":"90020","market_code":"0113","product_category":"011"}],"schema":1}',
        )

    def test_a_refused_response_yields_no_document(self) -> None:
        with self.assertRaises(ListingError):
            listing_document([_row(Date="2032-01-05")], requested=REQUESTED)


if __name__ == "__main__":
    unittest.main()
