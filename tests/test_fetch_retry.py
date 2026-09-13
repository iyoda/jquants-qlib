from __future__ import annotations

import datetime as dt
import gzip
import unittest

import pandas as pd

from jqqlib.fetch import call_jquants_with_retry, fetch_bulk_daily_quotes, fetch_daily_quotes, iter_date_chunks


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _retry(api_call):
    return call_jquants_with_retry(
        api_call,
        retries=3,
        base_delay_sec=0.0,
        max_delay_sec=0.0,
        jitter_sec=0.0,
    )


class CallWithRetryTests(unittest.TestCase):
    def test_status_less_transient_is_retried_then_succeeds(self) -> None:
        attempts = {"n": 0}

        def api_call():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise Exception("Connection reset by peer")
            return "ok"

        self.assertEqual(_retry(api_call), "ok")
        self.assertEqual(attempts["n"], 2)

    def test_401_fails_fast_without_retry(self) -> None:
        attempts = {"n": 0}

        def api_call():
            attempts["n"] += 1
            raise _StatusError("unauthorized", 401)

        with self.assertRaises(_StatusError):
            _retry(api_call)
        self.assertEqual(attempts["n"], 1)

    def test_403_fails_fast_without_retry(self) -> None:
        attempts = {"n": 0}

        def api_call():
            attempts["n"] += 1
            raise _StatusError("forbidden", 403)

        with self.assertRaises(_StatusError):
            _retry(api_call)
        self.assertEqual(attempts["n"], 1)

    def test_429_is_retried(self) -> None:
        attempts = {"n": 0}

        def api_call():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _StatusError("rate limited", 429)
            return "ok"

        self.assertEqual(_retry(api_call), "ok")
        self.assertEqual(attempts["n"], 3)

    def test_503_is_retried(self) -> None:
        attempts = {"n": 0}

        def api_call():
            attempts["n"] += 1
            raise _StatusError("unavailable", 503)

        with self.assertRaises(_StatusError):
            _retry(api_call)
        self.assertEqual(attempts["n"], 4)  # 1 + 3 retries

    def test_non_transient_status_less_error_raises_immediately(self) -> None:
        attempts = {"n": 0}

        def api_call():
            attempts["n"] += 1
            raise ValueError("malformed config, not transient")

        with self.assertRaises(ValueError):
            _retry(api_call)
        self.assertEqual(attempts["n"], 1)

    def test_status_less_retry_can_be_disabled(self) -> None:
        attempts = {"n": 0}

        def api_call():
            attempts["n"] += 1
            raise Exception("Connection reset by peer")

        with self.assertRaisesRegex(Exception, "Connection reset by peer"):
            call_jquants_with_retry(
                api_call,
                retries=3,
                base_delay_sec=0.0,
                max_delay_sec=0.0,
                jitter_sec=0.0,
                retry_statusless=False,
            )
        self.assertEqual(attempts["n"], 1)

    def test_retry_backoff_uses_injected_sleeper(self) -> None:
        attempts = {"n": 0}
        sleeps: list[float] = []

        def api_call():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise _StatusError("rate limited", 429)
            return "ok"

        self.assertEqual(
            call_jquants_with_retry(
                api_call,
                retries=1,
                base_delay_sec=2.0,
                max_delay_sec=5.0,
                jitter_sec=0.0,
                sleeper=sleeps.append,
            ),
            "ok",
        )
        self.assertEqual(sleeps, [2.0])


class FetchRequestPacingTests(unittest.TestCase):
    def test_iter_date_chunks_rejects_non_positive_days(self) -> None:
        with self.assertRaisesRegex(ValueError, "days must be 1 or greater"):
            list(iter_date_chunks(dt.date(2026, 6, 1), dt.date(2026, 6, 2), 0))

    def test_client_v2_code_requests_sleep_between_requests_only(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def get_eq_bars_daily(self, **kwargs):
                self.calls.append(kwargs)
                return pd.DataFrame({"Date": [kwargs["from_yyyymmdd"]], "Code": [kwargs["code"]]})

        client = Client()
        sleeps: list[float] = []

        fetch_daily_quotes(
            client,
            dt.date(2026, 6, 1),
            dt.date(2026, 6, 1),
            codes=["10010", "10020"],
            request_interval_sec=0.25,
            sleeper=sleeps.append,
        )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(sleeps, [0.25])

    def test_client_v2_all_symbol_chunk_requests_are_paced(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def get_eq_bars_daily(self, **kwargs):
                self.calls.append(kwargs)
                return pd.DataFrame({"Date": [kwargs["from_yyyymmdd"]], "Code": ["10010"]})

        client = Client()
        sleeps: list[float] = []

        fetch_daily_quotes(
            client,
            dt.date(2026, 6, 1),
            dt.date(2026, 6, 2),
            chunk_days=1,
            request_interval_sec=0.5,
            sleeper=sleeps.append,
        )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(sleeps, [0.5])

    def test_client_v2_400_fallback_daily_requests_are_paced(self) -> None:
        class StatusError(Exception):
            status_code = 400

        class Client:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def get_eq_bars_daily(self, **kwargs):
                self.calls.append(kwargs)
                if "from_yyyymmdd" in kwargs:
                    raise StatusError("unsupported range")
                return pd.DataFrame({"Date": [kwargs["date_yyyymmdd"]], "Code": ["10010"]})

        client = Client()
        sleeps: list[float] = []

        fetch_daily_quotes(
            client,
            dt.date(2026, 6, 1),
            dt.date(2026, 6, 2),
            request_interval_sec=0.75,
            sleeper=sleeps.append,
        )

        self.assertEqual(len(client.calls), 3)
        self.assertEqual(sleeps, [0.75, 0.75])

    def test_legacy_daily_quote_requests_are_paced(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def get_prices_daily_quotes(self, **kwargs):
                self.calls.append(kwargs)
                return {"daily_quotes": [{"Date": kwargs["from_"], "Code": "10010"}]}

        client = Client()
        sleeps: list[float] = []

        fetch_daily_quotes(
            client,
            dt.date(2026, 6, 1),
            dt.date(2026, 6, 2),
            chunk_days=1,
            request_interval_sec=1.0,
            sleeper=sleeps.append,
        )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(sleeps, [1.0])

    def test_bulk_list_and_download_requests_are_paced(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get_bulk_list(self, endpoint):
                self.calls.append(f"list:{endpoint}")
                return pd.DataFrame({"Key": ["eq/202606.csv.gz", "eq/202607.csv.gz"]})

            def download_bulk(self, key: str, output_path: str) -> None:
                self.calls.append(f"download:{key}")
                with gzip.open(output_path, "wt", encoding="utf-8") as f:
                    f.write("Date,Code,Open,High,Low,Close,Volume,TurnoverValue\n")
                    month = "2026-06-01" if "202606" in key else "2026-07-01"
                    f.write(f"{month},10010,1,2,0.5,1.5,100,150\n")

        client = Client()
        sleeps: list[float] = []

        fetch_bulk_daily_quotes(
            client,
            dt.date(2026, 6, 1),
            dt.date(2026, 7, 31),
            request_interval_sec=0.2,
            sleeper=sleeps.append,
        )

        self.assertEqual(len(client.calls), 3)
        self.assertEqual(sleeps, [0.2, 0.2])


if __name__ == "__main__":
    unittest.main()
