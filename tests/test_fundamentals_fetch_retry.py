from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from jqqlib.fetch_fundamentals import fetch_fundamentals


class FetchFundamentalsTests(unittest.TestCase):
    def test_fetches_one_call_per_code_and_paces_between_requests_only(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get_fin_summary(self, code: str) -> pd.DataFrame:
                self.calls.append(code)
                return pd.DataFrame({"DiscDate": ["2026-06-02"], "Eq": [100.0]})

        client = Client()
        sleeps: list[float] = []

        df = fetch_fundamentals(
            client,
            ["10010", "10020"],
            request_interval_sec=1.1,
            sleeper=sleeps.append,
        )

        self.assertEqual(client.calls, ["10010", "10020"])
        self.assertEqual(sleeps, [1.1])  # paced between requests only, not before the first
        self.assertEqual(sorted(df["Code"].unique()), ["10010", "10020"])

    def test_join_key_is_forced_to_the_requested_code(self) -> None:
        class Client:
            def get_fin_summary(self, code: str) -> pd.DataFrame:
                # Simulate a response that omits/garbles the Code column.
                return pd.DataFrame({"DiscDate": ["2026-06-02"], "Eq": [1.0]})

        df = fetch_fundamentals(Client(), ["10010"], request_interval_sec=0.0, sleeper=lambda _: None)

        self.assertEqual(list(df["Code"]), ["10010"])

    def test_empty_responses_for_some_codes_are_skipped(self) -> None:
        class Client:
            def get_fin_summary(self, code: str) -> pd.DataFrame:
                if code == "9999":
                    return pd.DataFrame()
                return pd.DataFrame({"DiscDate": ["2026-06-02"], "Eq": [1.0]})

        df = fetch_fundamentals(
            Client(), ["9999", "10010"], request_interval_sec=0.0, sleeper=lambda _: None
        )

        self.assertEqual(sorted(df["Code"].unique()), ["10010"])

    def test_all_codes_empty_returns_empty_frame(self) -> None:
        class Client:
            def get_fin_summary(self, code: str) -> pd.DataFrame:
                return pd.DataFrame()

        df = fetch_fundamentals(Client(), ["10010", "10020"], request_interval_sec=0.0, sleeper=lambda _: None)

        self.assertTrue(df.empty)

    def test_transient_statusless_error_is_retried_then_succeeds(self) -> None:
        attempts = {"n": 0}

        class Client:
            def get_fin_summary(self, code: str) -> pd.DataFrame:
                attempts["n"] += 1
                if attempts["n"] < 2:
                    raise Exception("Connection reset by peer")
                return pd.DataFrame({"DiscDate": ["2026-06-02"], "Eq": [1.0]})

        df = fetch_fundamentals(
            Client(),
            ["10010"],
            retries=3,
            retry_base_delay_sec=0.0,
            retry_max_delay_sec=0.0,
            retry_jitter_sec=0.0,
            request_interval_sec=0.0,
            sleeper=lambda _: None,
        )

        self.assertEqual(attempts["n"], 2)
        self.assertEqual(list(df["Code"]), ["10010"])

    def test_auth_failure_fails_fast_without_retry(self) -> None:
        class _StatusError(Exception):
            def __init__(self, message: str, status_code: int) -> None:
                super().__init__(message)
                self.status_code = status_code

        attempts = {"n": 0}

        class Client:
            def get_fin_summary(self, code: str) -> pd.DataFrame:
                attempts["n"] += 1
                raise _StatusError("unauthorized", 401)

        with self.assertRaises(_StatusError):
            fetch_fundamentals(
                Client(),
                ["10010"],
                retries=3,
                retry_base_delay_sec=0.0,
                retry_max_delay_sec=0.0,
                retry_jitter_sec=0.0,
                request_interval_sec=0.0,
                sleeper=lambda _: None,
            )

        self.assertEqual(attempts["n"], 1)


class FetchFundamentalsResumeCacheTests(unittest.TestCase):
    def test_second_call_skips_codes_already_in_cache(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get_fin_summary(self, code: str) -> pd.DataFrame:
                self.calls.append(code)
                return pd.DataFrame({"DiscDate": ["2026-06-02"], "Eq": [1.0]})

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "fundamentals_raw_cache.parquet"
            client = Client()

            first = fetch_fundamentals(
                client, ["10010", "10020"], request_interval_sec=0.0, sleeper=lambda _: None,
                cache_path=cache_path,
            )
            self.assertEqual(client.calls, ["10010", "10020"])
            self.assertEqual(sorted(first["Code"].unique()), ["10010", "10020"])
            self.assertTrue(cache_path.exists())

            # A second call for the same (plus one new) code must not re-fetch
            # the already-cached codes.
            second = fetch_fundamentals(
                client, ["10010", "10020", "10030"], request_interval_sec=0.0, sleeper=lambda _: None,
                cache_path=cache_path,
            )
            self.assertEqual(client.calls, ["10010", "10020", "10030"])
            self.assertEqual(sorted(second["Code"].unique()), ["10010", "10020", "10030"])

    def test_codes_with_empty_response_are_not_requeried_on_resume(self) -> None:
        # Regression: a code with no financial-summary disclosures yet (ETF,
        # REIT, newly-listed, ...) legitimately returns an empty frame and must
        # still count as "attempted" -- otherwise every resumed run would
        # requery the same empty codes forever instead of making forward
        # progress into the untried remainder of the universe.
        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get_fin_summary(self, code: str) -> pd.DataFrame:
                self.calls.append(code)
                if code == "9999":
                    return pd.DataFrame()
                return pd.DataFrame({"DiscDate": ["2026-06-02"], "Eq": [1.0]})

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "fundamentals_raw_cache.parquet"
            client = Client()

            first = fetch_fundamentals(
                client, ["9999", "10010"], request_interval_sec=0.0, sleeper=lambda _: None,
                cache_path=cache_path,
            )
            self.assertEqual(client.calls, ["9999", "10010"])
            self.assertEqual(sorted(first["Code"].unique()), ["10010"])

            second = fetch_fundamentals(
                client, ["9999", "10010", "10020"], request_interval_sec=0.0, sleeper=lambda _: None,
                cache_path=cache_path,
            )
            # "9999" must NOT appear again -- only the genuinely new "10020".
            self.assertEqual(client.calls, ["9999", "10010", "10020"])
            self.assertEqual(sorted(second["Code"].unique()), ["10010", "10020"])

    def test_code_whose_request_raised_is_requested_again_on_resume(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.fail_codes = {"10020"}

            def get_fin_summary(self, code: str) -> pd.DataFrame:
                self.calls.append(code)
                if code in self.fail_codes:
                    raise ValueError("unknown outcome")
                return pd.DataFrame({"DiscDate": ["2026-06-02"], "Eq": [1.0]})

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "fundamentals_raw_cache.parquet"
            client = Client()
            with self.assertRaises(ValueError):
                fetch_fundamentals(
                    client, ["10010", "10020"], request_interval_sec=0.0, sleeper=lambda _: None,
                    cache_path=cache_path, checkpoint_every=1,
                )

            client.fail_codes.clear()
            resumed = fetch_fundamentals(
                client, ["10010", "10020"], request_interval_sec=0.0, sleeper=lambda _: None,
                cache_path=cache_path,
            )

            self.assertEqual(client.calls, ["10010", "10020", "10020"])
            self.assertEqual(sorted(resumed["Code"].unique()), ["10010", "10020"])

    def test_max_new_codes_bounds_a_single_invocation_and_resumes_later(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get_fin_summary(self, code: str) -> pd.DataFrame:
                self.calls.append(code)
                return pd.DataFrame({"DiscDate": ["2026-06-02"], "Eq": [1.0]})

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "fundamentals_raw_cache.parquet"
            client = Client()

            first = fetch_fundamentals(
                client,
                ["10010", "10020", "10030"],
                request_interval_sec=0.0,
                sleeper=lambda _: None,
                cache_path=cache_path,
                max_new_codes=2,
            )
            self.assertEqual(client.calls, ["10010", "10020"])
            self.assertEqual(sorted(first["Code"].unique()), ["10010", "10020"])

            second = fetch_fundamentals(
                client,
                ["10010", "10020", "10030"],
                request_interval_sec=0.0,
                sleeper=lambda _: None,
                cache_path=cache_path,
                max_new_codes=2,
            )
            self.assertEqual(client.calls, ["10010", "10020", "10030"])
            self.assertEqual(sorted(second["Code"].unique()), ["10010", "10020", "10030"])

    def test_checkpoint_persists_partial_progress_before_a_crash(self) -> None:
        # Simulate a crash partway through a batch: the checkpoint written at
        # checkpoint_every=1 must be on disk even though the overall call raises.
        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get_fin_summary(self, code: str) -> pd.DataFrame:
                self.calls.append(code)
                if code == "10020":
                    raise ValueError("simulated crash mid-batch")
                return pd.DataFrame({"DiscDate": ["2026-06-02"], "Eq": [1.0]})

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "fundamentals_raw_cache.parquet"
            client = Client()

            with self.assertRaises(ValueError):
                fetch_fundamentals(
                    client,
                    ["10010", "10020", "10030"],
                    request_interval_sec=0.0,
                    sleeper=lambda _: None,
                    cache_path=cache_path,
                    checkpoint_every=1,
                )

            self.assertTrue(cache_path.exists())
            cached = pd.read_parquet(cache_path)
            self.assertEqual(sorted(cached["Code"].unique()), ["10010"])


    @staticmethod
    def _cache_left_by_a_run_killed_between_cache_and_attempted_writes(cache_path: Path) -> None:
        class Client:
            def get_fin_summary(self, code: str) -> pd.DataFrame:
                return pd.DataFrame({"DiscDate": ["2026-05-01", "2026-06-02"], "DiscNo": ["1", "2"], "Eq": [1.0, 2.0]})

        fetch_fundamentals(Client(), ["10010"], request_interval_sec=0.0, sleeper=lambda _: None, cache_path=cache_path)
        unrecorded = pd.DataFrame({"DiscDate": ["2026-06-02"], "DiscNo": ["1"], "Eq": [3.0], "Code": ["10020"]})
        pd.concat([pd.read_parquet(cache_path), unrecorded], ignore_index=True).to_parquet(cache_path, index=False)

    def test_rows_cached_before_the_attempted_record_was_written_are_not_duplicated_on_resume(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get_fin_summary(self, code: str) -> pd.DataFrame:
                self.calls.append(code)
                return pd.DataFrame({"DiscDate": ["2026-06-02"], "DiscNo": ["1"], "Eq": [4.0]})

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "fundamentals_raw_cache.parquet"
            self._cache_left_by_a_run_killed_between_cache_and_attempted_writes(cache_path)
            client = Client()

            df = fetch_fundamentals(
                client,
                ["10010", "10020", "10030"],
                request_interval_sec=0.0,
                sleeper=lambda _: None,
                cache_path=cache_path,
            )

            self.assertEqual(client.calls, ["10020", "10030"])
            self.assertEqual(sorted(df["Code"]), ["10010", "10010", "10020", "10030"])
            self.assertEqual(sorted(df.loc[df["Code"] == "10010", "Eq"]), [1.0, 2.0])
            self.assertEqual(sorted(pd.read_parquet(cache_path)["Code"]), ["10010", "10010", "10020", "10030"])

    def test_unrecorded_cached_rows_of_a_code_that_now_returns_empty_are_not_revived_by_the_next_run(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get_fin_summary(self, code: str) -> pd.DataFrame:
                self.calls.append(code)
                return pd.DataFrame()

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "fundamentals_raw_cache.parquet"
            self._cache_left_by_a_run_killed_between_cache_and_attempted_writes(cache_path)
            resumed_client, next_client = Client(), Client()

            options = {"request_interval_sec": 0.0, "sleeper": lambda _: None, "cache_path": cache_path}
            resumed = fetch_fundamentals(resumed_client, ["10010", "10020"], **options)
            next_run = fetch_fundamentals(next_client, ["10010", "10020"], **options)

            self.assertEqual(resumed_client.calls, ["10020"])
            self.assertEqual(next_client.calls, [])
            self.assertEqual(sorted(resumed["Code"]), ["10010", "10010"])
            self.assertEqual(sorted(next_run["Code"]), ["10010", "10010"])


    def test_cache_holding_only_unrecorded_rows_is_removed_when_the_re_query_returns_empty(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get_fin_summary(self, code: str) -> pd.DataFrame:
                self.calls.append(code)
                return pd.DataFrame()

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "fundamentals_raw_cache.parquet"
            unrecorded = pd.DataFrame({"DiscDate": ["2026-06-02"], "DiscNo": ["1"], "Eq": [3.0], "Code": ["10020"]})
            unrecorded.to_parquet(cache_path, index=False)
            resumed_client, next_client = Client(), Client()

            options = {"request_interval_sec": 0.0, "sleeper": lambda _: None, "cache_path": cache_path}
            resumed = fetch_fundamentals(resumed_client, ["10020"], **options)
            cache_exists_after_resume = cache_path.exists()
            next_run = fetch_fundamentals(next_client, ["10020"], **options)

            self.assertEqual(resumed_client.calls, ["10020"])
            self.assertEqual(next_client.calls, [])
            self.assertTrue(resumed.empty)
            self.assertFalse(cache_exists_after_resume)
            self.assertTrue(next_run.empty)

if __name__ == "__main__":
    unittest.main()
