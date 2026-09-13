"""J-Quants client, API retry, and daily/bulk quote fetch."""

from __future__ import annotations

import datetime as dt
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd
    from jquantsapi import Client, ClientV2


def _keychain_api_key_service() -> str:
    """macOS Keychain generic-password service name for the API key.

    Evaluated at call time so ``JQQLIB_KEYCHAIN_API_KEY_SERVICE`` set after
    import still takes effect. Override the env var when sharing a Keychain
    with tools that already use a different naming convention.
    """
    return os.getenv("JQQLIB_KEYCHAIN_API_KEY_SERVICE", "jquants-api-key")


def _keychain_refresh_token_service() -> str:
    """macOS Keychain generic-password service name for the refresh token.

    Evaluated at call time so ``JQQLIB_KEYCHAIN_REFRESH_TOKEN_SERVICE`` set
    after import still takes effect.
    """
    return os.getenv("JQQLIB_KEYCHAIN_REFRESH_TOKEN_SERVICE", "jquants-refresh-token")


# Deliberately noisy escape hatch; see create_jquants_client docstring.
_ALLOW_YAML_CREDENTIALS_ENV = "JQQLIB_ALLOW_YAML_CREDENTIALS"


def _read_keychain_secret(service: str) -> str | None:
    """Read a secret VALUE from a macOS Keychain generic-password entry.

    This deliberately passes ``-w`` because the caller needs the value itself
    (an existence-only check must never pass ``-w``). The returned
    value is the caller's responsibility: it must never be logged, printed,
    or included in an exception/error message. Returns ``None`` (never an
    empty string) when the entry is absent, empty, or ``security`` cannot be
    run, so callers can fall through to the next credential source.
    """
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    secret = completed.stdout.strip()
    return secret or None


def _refresh_token_client(refresh_token: str) -> Client:
    """Build the legacy v1 (refresh-token) client.

    ``jquantsapi.Client`` was removed from jquants-api-client 2.6; on such
    versions only the API-key path (``ClientV2``) is available, so fail with an
    actionable message instead of a bare ImportError.
    """
    try:
        from jquantsapi import Client
    except ImportError as exc:
        raise ValueError(
            "A J-Quants refresh token was found, but the installed "
            "jquants-api-client no longer provides the v1 refresh-token client. "
            "Set JQUANTS_API_KEY instead (or pin jquants-api-client<2.6)."
        ) from exc
    return Client(refresh_token=refresh_token)


def create_jquants_client(config: dict) -> Client | ClientV2:
    """Create J-Quants client with Light-plan-first credential strategy.

    Resolution order: environment variable -> macOS Keychain -> error. A
    plaintext ``config.yaml`` credential is never used on the default,
    silent-success path. The supported unattended path is the environment
    (e.g. a cron or launchd job's environment); the Keychain step is a
    macOS-only compatibility fallback that silently no-ops where the
    ``security`` CLI is absent (e.g. Linux), so the repository stays
    Linux-portable rather than promoting Keychain to the canonical path.

    An opt-in, deliberately noisy escape hatch exists for local debugging
    only: set ``JQQLIB_ALLOW_YAML_CREDENTIALS=1`` to allow a non-empty
    ``jquants.api_key`` / ``jquants.refresh_token`` in ``config.yaml`` to be
    used, in which case an ALERT is printed to stderr every time it fires.
    """
    # jquantsapi (and transitively pandas) is imported lazily at each
    # construction site below, not up front: the missing-credentials path must
    # raise ValueError without paying for that import, so a caller that treats
    # "no credentials" as a fast, sanitized no-op is not masked by an unrelated
    # import failure.
    jq_conf = config.get("jquants", {})
    if not isinstance(jq_conf, dict):
        jq_conf = {}

    api_key = os.getenv("JQUANTS_API_KEY") or _read_keychain_secret(_keychain_api_key_service())
    if api_key:
        from jquantsapi import ClientV2

        return ClientV2(api_key=api_key)

    refresh_token = os.getenv("JQUANTS_REFRESH_TOKEN") or _read_keychain_secret(
        _keychain_refresh_token_service()
    )
    if refresh_token:
        return _refresh_token_client(refresh_token)

    if os.getenv(_ALLOW_YAML_CREDENTIALS_ENV) == "1":
        yaml_api_key = jq_conf.get("api_key")
        if yaml_api_key:
            print(
                "ALERT: JQQLIB_ALLOW_YAML_CREDENTIALS=1 is set; using a "
                "config.yaml jquants.api_key credential. This escape hatch is "
                "for local debugging only and must not be relied on for "
                "unattended/scheduled runs.",
                file=sys.stderr,
            )
            from jquantsapi import ClientV2

            return ClientV2(api_key=yaml_api_key)
        yaml_refresh_token = jq_conf.get("refresh_token")
        if yaml_refresh_token:
            print(
                "ALERT: JQQLIB_ALLOW_YAML_CREDENTIALS=1 is set; using a "
                "config.yaml jquants.refresh_token credential. This escape "
                "hatch is for local debugging only and must not be relied on "
                "for unattended/scheduled runs.",
                file=sys.stderr,
            )
            return _refresh_token_client(yaml_refresh_token)

    hint = ""
    if jq_conf.get("api_key") or jq_conf.get("refresh_token"):
        hint = (
            " A jquants.api_key / jquants.refresh_token value in config.yaml is ignored "
            f"unless {_ALLOW_YAML_CREDENTIALS_ENV}=1 (local debugging only)."
        )
    raise ValueError(
        "J-Quants credential is not configured. Set JQUANTS_API_KEY "
        "(recommended for Light plan) or JQUANTS_REFRESH_TOKEN as an "
        "environment variable, or add it to macOS Keychain: "
        f'`security add-generic-password -s {_keychain_api_key_service()} -a "$USER" -w` '
        f"(or -s {_keychain_refresh_token_service()} for the refresh token)." + hint
    )


# Status-less transient errors that are not already caught as
# TimeoutError/ConnectionError/OSError but are still worth a bounded retry
# (e.g. truncated/chunked responses, name-resolution blips, reset peers).
# Authentication failures (401/403) are deliberately excluded: on the static
# ClientV2 api-key path there is nothing to re-auth, so they must fail fast.
_TRANSIENT_STATUSLESS_RE = re.compile(
    r"connection reset|connection aborted|chunked|incomplete read|"
    r"temporarily unavailable|temporary failure in name resolution|"
    r"timed out|read timed out|eof occurred|broken pipe|"
    r"remote end closed|server disconnected",
    re.IGNORECASE,
)


def _is_transient_statusless(error: Exception) -> bool:
    """True for status-less errors that look like a transient network blip.

    Note: connection/timeout errors from requests subclass OSError and are
    already handled by the typed ``except`` clause in call_jquants_with_retry;
    this fuzzy message match only covers the residual status-less cases that
    carry no usable type or status_code.
    """
    return bool(_TRANSIENT_STATUSLESS_RE.search(str(error)))


def call_jquants_with_retry(
    api_call,
    *,
    retries: int,
    base_delay_sec: float,
    max_delay_sec: float,
    jitter_sec: float,
    retry_statusless: bool = True,
    sleeper: Callable[[float], None] = time.sleep,
):
    """Call J-Quants API with exponential backoff retry.

    Retries on transient exceptions only and raises the last exception if exhausted.
    Transient = timeouts/connection/OS errors, HTTP 429/5xx, and (when
    ``retry_statusless`` is set) status-less network blips. Auth failures
    (401/403) and any other status-bearing error fail fast.
    """
    import requests

    last_error: Exception | None = None

    def _backoff(attempt: int) -> None:
        sleep_sec = min(base_delay_sec * (2**attempt), max_delay_sec)
        sleep_sec += random.uniform(0, jitter_sec)
        sleeper(sleep_sec)

    for attempt in range(retries + 1):
        try:
            return api_call()
        except (TimeoutError, ConnectionError, OSError, requests.exceptions.RetryError) as e:
            last_error = e
            if attempt >= retries:
                break
            _backoff(attempt)
        except Exception as e:
            # Retry common transient HTTP-style errors (429/5xx).
            status = getattr(e, "status_code", None)
            if status is None:
                response = getattr(e, "response", None)
                status = getattr(response, "status_code", None)

            transient = status in {429, 500, 502, 503, 504}
            # Status-less network blips: retry only when no status is attached
            # (so 401/403 and other status-bearing errors still fail fast).
            if not transient and status is None and retry_statusless and _is_transient_statusless(e):
                transient = True

            if transient:
                last_error = e
                if attempt >= retries:
                    break
                _backoff(attempt)
                continue
            raise

    if last_error is None:
        raise RuntimeError("Retry logic reached unexpected state without captured error.")
    raise last_error


def iter_date_chunks(start: dt.date, end: dt.date, days: int) -> Iterable[tuple[dt.date, dt.date]]:
    if days < 1:
        raise ValueError(f"days must be 1 or greater, got {days}.")

    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + dt.timedelta(days=days - 1), end)
        yield cursor, chunk_end
        cursor = chunk_end + dt.timedelta(days=1)


class _RequestPacer:
    def __init__(self, interval_sec: float, sleeper: Callable[[float], None]) -> None:
        if interval_sec < 0:
            raise ValueError(f"request_interval_sec must be 0 or greater, got {interval_sec}.")
        self._interval_sec = interval_sec
        self._sleeper = sleeper
        self._seen_request = False

    def before_request(self) -> None:
        if self._seen_request and self._interval_sec > 0:
            self._sleeper(self._interval_sec)
        self._seen_request = True


def iter_month_starts(start_date: dt.date, end_date: dt.date) -> Iterable[dt.date]:
    cursor = dt.date(start_date.year, start_date.month, 1)
    while cursor <= end_date:
        yield cursor
        if cursor.month == 12:
            cursor = dt.date(cursor.year + 1, 1, 1)
        else:
            cursor = dt.date(cursor.year, cursor.month + 1, 1)


def fetch_daily_quotes(
    client: Client | ClientV2,
    start_date: dt.date,
    end_date: dt.date,
    codes: Iterable[str] | None = None,
    chunk_days: int = 30,
    retries: int = 3,
    retry_base_delay_sec: float = 1.0,
    retry_max_delay_sec: float = 8.0,
    retry_jitter_sec: float = 0.3,
    request_interval_sec: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> pd.DataFrame:
    import pandas as pd

    frames: list[pd.DataFrame] = []
    symbols = list(codes or [])
    pacer = _RequestPacer(request_interval_sec, sleeper)

    def _request_quotes(code: str | None, chunk_start: dt.date, chunk_end: dt.date) -> pd.DataFrame:
        if hasattr(client, "get_eq_bars_daily"):
            if code:
                pacer.before_request()
                response = call_jquants_with_retry(
                    lambda: client.get_eq_bars_daily(
                        code=code,
                        from_yyyymmdd=chunk_start.isoformat(),
                        to_yyyymmdd=chunk_end.isoformat(),
                    ),
                    retries=retries,
                    base_delay_sec=retry_base_delay_sec,
                    max_delay_sec=retry_max_delay_sec,
                    jitter_sec=retry_jitter_sec,
                    sleeper=sleeper,
                )
            else:
                try:
                    pacer.before_request()
                    response = call_jquants_with_retry(
                        lambda: client.get_eq_bars_daily(
                            from_yyyymmdd=chunk_start.isoformat(),
                            to_yyyymmdd=chunk_end.isoformat(),
                        ),
                        retries=retries,
                        base_delay_sec=retry_base_delay_sec,
                        max_delay_sec=retry_max_delay_sec,
                        jitter_sec=retry_jitter_sec,
                        sleeper=sleeper,
                    )
                except Exception as exc:
                    status = getattr(exc, "status_code", None)
                    if status is None:
                        response_obj = getattr(exc, "response", None)
                        status = getattr(response_obj, "status_code", None)
                    if status != 400:
                        raise

                    frames_for_days: list[pd.DataFrame] = []
                    for day, _ in iter_date_chunks(chunk_start, chunk_end, 1):
                        pacer.before_request()
                        daily_df = call_jquants_with_retry(
                            lambda day=day: client.get_eq_bars_daily(date_yyyymmdd=day.isoformat()),
                            retries=retries,
                            base_delay_sec=retry_base_delay_sec,
                            max_delay_sec=retry_max_delay_sec,
                            jitter_sec=retry_jitter_sec,
                            sleeper=sleeper,
                        )
                        if not daily_df.empty:
                            frames_for_days.append(daily_df)
                    if not frames_for_days:
                        return pd.DataFrame()
                    response = pd.concat(frames_for_days, ignore_index=True)
            return response

        kwargs = {
            "from_": chunk_start.isoformat(),
            "to": chunk_end.isoformat(),
        }
        if code:
            kwargs["code"] = code
        pacer.before_request()
        response = call_jquants_with_retry(
            lambda: client.get_prices_daily_quotes(**kwargs),
            retries=retries,
            base_delay_sec=retry_base_delay_sec,
            max_delay_sec=retry_max_delay_sec,
            jitter_sec=retry_jitter_sec,
            sleeper=sleeper,
        )
        quotes = response.get("daily_quotes", [])
        return pd.DataFrame(quotes)

    for chunk_start, chunk_end in iter_date_chunks(start_date, end_date, chunk_days):
        if symbols:
            for code in symbols:
                quotes_df = _request_quotes(code, chunk_start, chunk_end)
                if not quotes_df.empty:
                    frames.append(quotes_df)
        else:
            quotes_df = _request_quotes(None, chunk_start, chunk_end)
            if not quotes_df.empty:
                frames.append(quotes_df)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
    return df


def fetch_bulk_daily_quotes(
    client: ClientV2,
    start_date: dt.date,
    end_date: dt.date,
    retries: int = 3,
    retry_base_delay_sec: float = 1.0,
    retry_max_delay_sec: float = 8.0,
    retry_jitter_sec: float = 0.3,
    request_interval_sec: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> pd.DataFrame:
    import pandas as pd
    from jquantsapi import BulkEndpoint

    pacer = _RequestPacer(request_interval_sec, sleeper)
    pacer.before_request()
    bulk_list = client.get_bulk_list(BulkEndpoint.EQ_BARS_DAILY)
    if bulk_list.empty:
        raise ValueError("No bulk daily quote files are available from J-Quants.")

    target_months = {month_start.strftime("%Y%m") for month_start in iter_month_starts(start_date, end_date)}
    keys = bulk_list["Key"].astype(str)
    matched = bulk_list[keys.str.extract(r"(\d{6})", expand=False).isin(target_months)].copy()
    if matched.empty:
        available = sorted(
            {
                m.group(1)
                for key in keys
                for m in [re.search(r"(\d{6})", key)]
                if m is not None
            }
        )
        available_range = f"{available[0]} to {available[-1]}" if available else "unknown"
        raise ValueError(
            "No bulk daily quote files matched the requested period. "
            f"Available bulk month range is {available_range}."
        )

    frames: list[pd.DataFrame] = []
    with tempfile.TemporaryDirectory(prefix="jquants_bulk_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        for _, row in matched.sort_values("Key").iterrows():
            key = str(row["Key"])
            gz_path = tmp_path / Path(key).name
            pacer.before_request()
            call_jquants_with_retry(
                lambda key=key, gz_path=gz_path: client.download_bulk(key, str(gz_path)),
                retries=retries,
                base_delay_sec=retry_base_delay_sec,
                max_delay_sec=retry_max_delay_sec,
                jitter_sec=retry_jitter_sec,
                sleeper=sleeper,
            )
            frames.append(pd.read_csv(gz_path, compression="gzip", low_memory=False))

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    date_series = pd.to_datetime(df["Date"]).dt.tz_localize(None)
    mask = (date_series.dt.date >= start_date) & (date_series.dt.date <= end_date)
    filtered = df.loc[mask].copy()
    filtered["Date"] = date_series.loc[mask]
    return filtered
