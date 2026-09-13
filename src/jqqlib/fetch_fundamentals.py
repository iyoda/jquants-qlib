"""J-Quants fundamentals fetch with retry and raw cache."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from .fetch import _RequestPacer, call_jquants_with_retry

if TYPE_CHECKING:
    import pandas as pd
    from jquantsapi import ClientV2


def _read_cache(cache_path: Path) -> list[pd.DataFrame]:
    import pandas as pd

    if not cache_path.exists():
        return []
    cached = pd.read_parquet(cache_path)
    return [cached] if not cached.empty else []


def _atomic_write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(temp_path, index=False)
    temp_path.replace(path)


def _attempted_path_for(cache_path: Path) -> Path:
    """Sidecar file next to ``cache_path`` recording every code that has been
    QUERIED (whether or not it returned data). The raw-data cache alone cannot
    serve as the "already handled" set: a code with no financial-summary
    disclosures yet (e.g. an ETF/REIT/newly-listed code) legitimately returns
    an empty response and would otherwise never appear in the data cache, so a
    resumed run would re-query it forever instead of making forward progress
    into codes it hasn't tried yet."""
    return cache_path.with_name(cache_path.stem + "_attempted.json")


def _read_attempted(attempted_path: Path) -> set[str]:
    if not attempted_path.exists():
        return set()
    return set(json.loads(attempted_path.read_text(encoding="utf-8")))


def _write_attempted(attempted: set[str], attempted_path: Path) -> None:
    attempted_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = attempted_path.with_suffix(attempted_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(sorted(attempted)), encoding="utf-8")
    temp_path.replace(attempted_path)


def fetch_fundamentals(
    client: ClientV2,
    codes: Iterable[str],
    retries: int = 3,
    retry_base_delay_sec: float = 1.0,
    retry_max_delay_sec: float = 8.0,
    retry_jitter_sec: float = 0.3,
    request_interval_sec: float = 1.1,
    sleeper: Callable[[float], None] = time.sleep,
    cache_path: Path | None = None,
    checkpoint_every: int = 50,
    max_new_codes: int | None = None,
) -> pd.DataFrame:
    """Fetch full quarterly financial-summary history for each code (v2 /fins/summary).

    One call per code returns that code's entire disclosed history (unlike the
    price ETL, this is not date-chunked -- ``get_fin_summary`` has no range
    parameter). Paced with the same request-interval + retry policy as the
    price fetchers to respect the endpoint's request-rate limit. A per-code loop
    built on the shared call_jquants_with_retry/_RequestPacer helpers, with a
    resumable-cache discipline (a full-universe backfill spans many
    minutes over a real network; a mid-run crash/kill must not throw away
    already-paid-for API calls).

    ``cache_path``, when given, is read for already-QUERIED codes (skipped from
    this run's ``codes`` -- tracked via a sidecar "attempted" file, not just
    the codes that happened to return data; see ``_attempted_path_for``) and
    checkpointed every ``checkpoint_every`` newly queried codes plus once more
    at the end, so a killed/restarted run resumes from the last checkpoint
    instead of requerying everything (including codes with legitimately empty
    responses).
    ``max_new_codes``, when given, bounds how many NOT-yet-attempted codes this
    call queries, so one invocation can be kept short enough to finish
    comfortably inside a bounded execution window; call again (same
    ``cache_path``) to fetch the remaining codes incrementally.
    """
    import pandas as pd

    cached_frames = _read_cache(cache_path) if cache_path is not None else []
    attempted_path = _attempted_path_for(cache_path) if cache_path is not None else None
    attempted = _read_attempted(attempted_path) if attempted_path is not None else set()

    todo = [str(code) for code in codes if str(code) not in attempted]
    if max_new_codes is not None:
        todo = todo[:max_new_codes]

    pacer = _RequestPacer(request_interval_sec, sleeper)
    new_frames: list[pd.DataFrame] = []
    newly_attempted: set[str] = set()

    def _checkpoint() -> None:
        if cache_path is None or attempted_path is None:
            return
        if new_frames:
            combined = pd.concat(cached_frames + new_frames, ignore_index=True)
            _atomic_write_parquet(combined, cache_path)
        if newly_attempted:
            _write_attempted(attempted | newly_attempted, attempted_path)

    for i, code in enumerate(todo, 1):
        pacer.before_request()
        df = call_jquants_with_retry(
            lambda code=code: client.get_fin_summary(code=code),
            retries=retries,
            base_delay_sec=retry_base_delay_sec,
            max_delay_sec=retry_max_delay_sec,
            jitter_sec=retry_jitter_sec,
            sleeper=sleeper,
        )
        # Only recorded as attempted once the call actually returns (success or
        # legitimate empty result) -- a raised exception means an unknown state,
        # so it must remain eligible for retry on the next invocation.
        newly_attempted.add(code)
        if df is not None and not df.empty:
            df = df.copy()
            # Ensure the join key is present/consistent regardless of what the
            # API response echoes back, matching the behaviour validated during the fundamentals probe.
            df["Code"] = str(code)
            new_frames.append(df)
        if i % checkpoint_every == 0:
            _checkpoint()

    _checkpoint()

    all_frames = cached_frames + new_frames
    if not all_frames:
        return pd.DataFrame()

    return pd.concat(all_frames, ignore_index=True)
