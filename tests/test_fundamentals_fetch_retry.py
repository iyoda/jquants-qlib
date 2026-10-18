from __future__ import annotations

import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from pandas.testing import assert_frame_equal

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


    def test_code_requested_twice_is_queried_once(self) -> None:
        client = _RecordingClient()

        df = fetch_fundamentals(client, ["10010", "10010"], request_interval_sec=0.0, sleeper=lambda _: None)

        self.assertEqual(client.calls, ["10010"])
        self.assertEqual(list(df["Code"]), ["10010", "10010"])

    def test_consecutive_runs_return_equal_frames_when_a_column_is_null_for_one_code(self) -> None:
        class Client:
            def get_fin_summary(self, code: str) -> pd.DataFrame:
                note = None if code == "10010" else "revised"
                return pd.DataFrame({"DiscDate": ["2026-06-02"], "DiscNo": ["1"], "Note": [note]})

        with tempfile.TemporaryDirectory() as tmp:
            options = _cache_options(Path(tmp))
            first = fetch_fundamentals(Client(), ["10010", "10020"], **options)
            second = fetch_fundamentals(Client(), ["10010", "10020"], **options)

        assert_frame_equal(first, second)


class FetchFundamentalsKilledRunTests(unittest.TestCase):
    def test_run_killed_at_any_cache_write_resumes_to_the_same_single_file_cache(self) -> None:
        codes = ["10010", "9999", "10020"]
        reference = fetch_fundamentals(_RecordingClient(), codes, request_interval_sec=0.0, sleeper=lambda _: None)

        cases = itertools.product((False, True), (1, 2), (False, True))
        for from_0_2_cache, checkpoint_every, kill_after_replace in cases:
            to_query = codes[1:] if from_0_2_cache else codes
            replace_number = 1
            while True:
                with self.subTest(
                    from_0_2_cache=from_0_2_cache,
                    checkpoint_every=checkpoint_every,
                    replace_number=replace_number,
                    kill_after_replace=kill_after_replace,
                ):
                    with tempfile.TemporaryDirectory() as tmp:
                        cache_dir = Path(tmp)
                        if from_0_2_cache:
                            history = _disclosures("10010")
                            _write_0_2_cache(cache_dir, pd.concat([history, history], ignore_index=True), ["10010"])
                        killed = _run_killed_at_replace(
                            codes, cache_dir, replace_number, kill_after_replace, checkpoint_every
                        )
                        committed_writes = replace_number if kill_after_replace else replace_number - 1
                        committed = to_query[: committed_writes * checkpoint_every] if killed else to_query
                        resumed_client, next_client = _RecordingClient(), _RecordingClient()

                        resumed = fetch_fundamentals(resumed_client, codes, **_cache_options(cache_dir))
                        next_run = fetch_fundamentals(next_client, codes, **_cache_options(cache_dir))

                        self.assertEqual(resumed_client.calls, [code for code in to_query if code not in committed])
                        self.assertEqual(next_client.calls, [])
                        assert_frame_equal(resumed, reference)
                        assert_frame_equal(next_run, reference)
                        self.assertEqual(sorted(p.name for p in cache_dir.iterdir()), [CACHE_FILENAME])
                if not killed:
                    break
                replace_number += 1
            self.assertGreater(replace_number, 1)


class FetchFundamentalsLegacyCacheTests(unittest.TestCase):
    def test_cache_left_by_0_2_with_duplicated_rows_is_migrated_without_requerying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            history = _disclosures("10010")
            _write_0_2_cache(cache_dir, pd.concat([history, history], ignore_index=True), ["10010", "9999"])
            client, next_client = _RecordingClient(), _RecordingClient()

            migrated = fetch_fundamentals(client, ["10010", "9999", "10020"], **_cache_options(cache_dir))
            next_run = fetch_fundamentals(next_client, ["10010", "9999", "10020"], **_cache_options(cache_dir))

            self.assertEqual(client.calls, ["10020"])
            self.assertEqual(sorted(migrated["Code"]), ["10010", "10010", "10020", "10020"])
            self.assertEqual(next_client.calls, [])
            assert_frame_equal(migrated, next_run)
            self.assertEqual(sorted(p.name for p in cache_dir.iterdir()), [CACHE_FILENAME])

    def test_0_2_rows_of_a_code_missing_from_the_attempted_record_are_requeried_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            _write_0_2_cache(cache_dir, pd.concat([_disclosures("10010"), _disclosures("10020")]), ["10010"])
            client = _RecordingClient()

            next_client = _RecordingClient()

            df = fetch_fundamentals(client, ["10010", "10020"], **_cache_options(cache_dir))
            next_run = fetch_fundamentals(next_client, ["10010", "10020"], **_cache_options(cache_dir))

            self.assertEqual(client.calls, ["10020"])
            self.assertEqual(sorted(df["Code"]), ["10010", "10010", "10020", "10020"])
            self.assertEqual(next_client.calls, [])
            assert_frame_equal(df, next_run)

    def test_0_2_rows_of_an_unrecorded_code_that_now_returns_empty_do_not_come_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            _write_0_2_cache(cache_dir, pd.concat([_disclosures("10010"), _disclosures("10020")]), ["10010"])
            client, next_client = _RecordingClient(empty={"10020"}), _RecordingClient(empty={"10020"})

            resumed = fetch_fundamentals(client, ["10010", "10020"], **_cache_options(cache_dir))
            next_run = fetch_fundamentals(next_client, ["10010", "10020"], **_cache_options(cache_dir))

            self.assertEqual(client.calls, ["10020"])
            self.assertEqual(next_client.calls, [])
            self.assertEqual(sorted(resumed["Code"]), ["10010", "10010"])
            assert_frame_equal(resumed, next_run)

    def test_0_2_attempted_record_without_a_cache_file_keeps_its_codes_attempted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            _write_0_2_cache(cache_dir, None, ["9999"])
            client, next_client = _RecordingClient(), _RecordingClient()

            first = fetch_fundamentals(client, ["9999"], **_cache_options(cache_dir))
            fetch_fundamentals(next_client, ["9999"], **_cache_options(cache_dir))

            self.assertEqual(client.calls, [])
            self.assertEqual(next_client.calls, [])
            self.assertTrue(first.empty)
            self.assertEqual(sorted(p.name for p in cache_dir.iterdir()), [CACHE_FILENAME])

    def test_0_2_cache_without_an_attempted_record_is_fetched_again_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            _disclosures("10010").to_parquet(cache_dir / CACHE_FILENAME, index=False)
            client = _RecordingClient()

            df = fetch_fundamentals(client, ["10010"], **_cache_options(cache_dir))

            self.assertEqual(client.calls, ["10010"])
            self.assertEqual(sorted(df["Code"]), ["10010", "10010"])


CACHE_FILENAME = "fundamentals_raw_cache.parquet"


def _disclosures(code: str) -> pd.DataFrame:
    return pd.DataFrame(
        {"DiscDate": ["2026-05-01", "2026-06-02"], "DiscNo": ["1", "2"], "Eq": [1.0, 2.0], "Code": [code, code]}
    )


class _RecordingClient:
    def __init__(self, empty: set[str] = frozenset()) -> None:
        self.calls: list[str] = []
        self._empty = empty

    def get_fin_summary(self, code: str) -> pd.DataFrame:
        self.calls.append(code)
        if code in self._empty or code.startswith("9"):
            return pd.DataFrame()
        return _disclosures(code).drop(columns=["Code"])


def _cache_options(cache_dir: Path, checkpoint_every: int = 1) -> dict:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return {
        "request_interval_sec": 0.0,
        "sleeper": lambda _: None,
        "cache_path": cache_dir / CACHE_FILENAME,
        "checkpoint_every": checkpoint_every,
    }


def _write_0_2_cache(cache_dir: Path, rows: pd.DataFrame | None, attempted: list[str]) -> None:
    if rows is not None:
        rows.to_parquet(cache_dir / CACHE_FILENAME, index=False)
    (cache_dir / "fundamentals_raw_cache_attempted.json").write_text(json.dumps(sorted(attempted)), encoding="utf-8")


class _Killed(BaseException):
    pass


def _run_killed_at_replace(
    codes: list[str], cache_dir: Path, replace_number: int, kill_after: bool, checkpoint_every: int
) -> bool:
    real_replace = Path.replace
    count = 0

    def replace_then_maybe_die(self: Path, target: Path) -> Path:
        nonlocal count
        count += 1
        if count == replace_number and not kill_after:
            raise _Killed
        replaced = real_replace(self, target)
        if count == replace_number:
            raise _Killed
        return replaced

    with mock.patch.object(Path, "replace", replace_then_maybe_die):
        try:
            fetch_fundamentals(_RecordingClient(), codes, **_cache_options(cache_dir, checkpoint_every))
        except _Killed:
            return True
    return False


if __name__ == "__main__":
    unittest.main()
