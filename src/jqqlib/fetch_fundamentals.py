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


_ATTEMPTED_CODES_KEY = b"jqqlib.attempted_codes"


def _legacy_attempted_path(cache_path: Path) -> Path:
    return cache_path.with_name(cache_path.stem + "_attempted.json")


def _read_cache(cache_path: Path) -> tuple[set[str], list[pd.DataFrame], bool]:
    import pandas as pd
    import pyarrow.parquet as pq

    legacy_path = _legacy_attempted_path(cache_path)
    rows = pd.DataFrame()
    if cache_path.exists():
        table = pq.read_table(cache_path)
        rows = table.to_pandas()
        metadata = table.schema.metadata or {}
        if _ATTEMPTED_CODES_KEY in metadata:
            attempted = set(json.loads(metadata[_ATTEMPTED_CODES_KEY]))
            return attempted, [rows] if not rows.empty else [], legacy_path.exists()
    elif not legacy_path.exists():
        return set(), [], False

    attempted = set(json.loads(legacy_path.read_text(encoding="utf-8"))) if legacy_path.exists() else set()
    if not rows.empty:
        rows = rows[rows["Code"].astype(str).isin(attempted)].drop_duplicates(keep="last").reset_index(drop=True)
    return attempted, [rows] if not rows.empty else [], True


def _write_cache(cache_path: Path, attempted: set[str], frames: list[pd.DataFrame]) -> list[pd.DataFrame]:
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    table = pa.Table.from_pandas(rows, preserve_index=False)
    record = json.dumps(sorted(attempted)).encode("utf-8")
    table = table.replace_schema_metadata({**(table.schema.metadata or {}), _ATTEMPTED_CODES_KEY: record})
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    pq.write_table(table, temp_path)
    temp_path.replace(cache_path)
    _legacy_attempted_path(cache_path).unlink(missing_ok=True)
    committed = table.to_pandas()
    return [committed] if not committed.empty else []


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

    ``cache_path``, when given, is one Parquet file holding the rows of every
    code queried so far and, in its footer, the list of those codes (including
    codes with legitimately empty responses). This run skips those codes, and
    each checkpoint, every ``checkpoint_every`` newly queried codes plus once
    more at the end, replaces the file atomically, so a killed or restarted run
    resumes from the last checkpoint. A cache written by jqqlib 0.2.0 or earlier
    (a Parquet file plus ``fundamentals_raw_cache_attempted.json``) is converted
    at the first checkpoint. A code listed twice in ``codes`` is queried once.
    ``max_new_codes``, when given, bounds how many NOT-yet-attempted codes this
    call queries, so one invocation can be kept short enough to finish
    comfortably inside a bounded execution window; call again (same
    ``cache_path``) to fetch the remaining codes incrementally.
    """
    import pandas as pd

    attempted, frames, unsaved = _read_cache(cache_path) if cache_path is not None else (set(), [], False)

    todo = list(dict.fromkeys(str(code) for code in codes if str(code) not in attempted))
    if max_new_codes is not None:
        todo = todo[:max_new_codes]

    pacer = _RequestPacer(request_interval_sec, sleeper)
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
        attempted.add(code)
        if df is not None and not df.empty:
            df = df.copy()
            df["Code"] = code
            frames.append(df)
        unsaved = True
        if cache_path is not None and i % checkpoint_every == 0:
            frames, unsaved = _write_cache(cache_path, attempted, frames), False

    if cache_path is not None and unsaved:
        frames = _write_cache(cache_path, attempted, frames)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)
