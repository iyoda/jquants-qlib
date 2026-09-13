"""Bin/calendar/instrument writer adapted from microsoft/qlib ``scripts/dump_bin.py`` (MIT)."""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd
from qlib.utils import code_to_fname, fname_to_code


def read_csv_data(file_path: Path, date_field_name: str) -> pd.DataFrame:
    df = pd.read_csv(file_path, low_memory=False)
    df[date_field_name] = pd.to_datetime(df[date_field_name])
    return df


class DumpDataAll:
    INSTRUMENTS_START_FIELD = "start_datetime"
    INSTRUMENTS_END_FIELD = "end_datetime"
    INSTRUMENTS_SEP = "\t"
    INSTRUMENTS_FILE_NAME = "all.txt"
    DUMP_FILE_SUFFIX = ".bin"

    def __init__(
        self,
        data_path: str,
        qlib_dir: str,
        *,
        freq: str = "day",
        max_workers: int = 4,
        date_field_name: str = "date",
        file_suffix: str = ".csv",
        include_fields: Iterable[str] = (),
    ) -> None:
        data_dir = Path(data_path).expanduser()
        self.df_files = sorted(data_dir.glob(f"*{file_suffix}")) if data_dir.is_dir() else [data_dir]
        self.qlib_dir = Path(qlib_dir).expanduser()
        self.freq = freq
        self.max_workers = max_workers
        self.date_field_name = date_field_name
        self.include_fields = tuple(include_fields)
        self.calendar_path = self.qlib_dir / "calendars" / f"{self.freq}.txt"
        self.features_dir = self.qlib_dir / "features"
        self.instruments_path = self.qlib_dir / "instruments" / self.INSTRUMENTS_FILE_NAME

    def _get_date_range_and_set(
        self, file_path: Path
    ) -> tuple[tuple[pd.Timestamp, pd.Timestamp] | None, set[pd.Timestamp]]:
        df = read_csv_data(file_path, self.date_field_name)
        if df.empty:
            return None, set()
        dates = df[self.date_field_name]
        return (dates.min(), dates.max()), set(dates)

    def _save_calendars(self, calendars: list[pd.Timestamp]) -> None:
        self.calendar_path.parent.mkdir(parents=True, exist_ok=True)
        values = [pd.Timestamp(v).strftime("%Y-%m-%d") for v in calendars]
        np.savetxt(self.calendar_path, values, fmt="%s", encoding="utf-8")

    def _save_instruments(self, rows: list[str]) -> None:
        self.instruments_path.parent.mkdir(parents=True, exist_ok=True)
        self.instruments_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    @staticmethod
    def _read_calendars(calendar_path: Path) -> list[pd.Timestamp]:
        return sorted(map(pd.Timestamp, pd.read_csv(calendar_path, header=None).iloc[:, 0].tolist()))

    @staticmethod
    def _data_merge_calendar(
        df: pd.DataFrame, calendars_list: list[pd.Timestamp], date_field_name: str
    ) -> pd.DataFrame:
        calendars_df = pd.DataFrame({date_field_name: calendars_list})
        calendars_df[date_field_name] = calendars_df[date_field_name].astype("datetime64[ns]")
        cal_df = calendars_df[
            (calendars_df[date_field_name] >= df[date_field_name].min())
            & (calendars_df[date_field_name] <= df[date_field_name].max())
        ]
        cal_df = cal_df.set_index(date_field_name)
        return df.set_index(date_field_name).reindex(cal_df.index)

    @staticmethod
    def _datetime_index(df: pd.DataFrame, calendar_list: list[pd.Timestamp]) -> int:
        return calendar_list.index(df.index.min())

    @classmethod
    def _dump_bin_file(
        cls,
        file_path: Path,
        *,
        calendar_list: list[pd.Timestamp],
        date_field_name: str,
        include_fields: tuple[str, ...],
        features_dir: Path,
        freq: str,
    ) -> None:
        df = read_csv_data(file_path, date_field_name)
        if df.empty:
            return
        code = fname_to_code(file_path.stem.strip().lower())
        df = df.drop_duplicates(date_field_name)
        aligned = cls._data_merge_calendar(df, calendar_list, date_field_name)
        if aligned.empty:
            return
        date_index = cls._datetime_index(aligned, calendar_list)
        instrument_dir = features_dir / code_to_fname(code).lower()
        instrument_dir.mkdir(parents=True, exist_ok=True)
        for field in include_fields:
            if field not in aligned.columns:
                continue
            bin_path = instrument_dir / f"{field.lower()}.{freq}{cls.DUMP_FILE_SUFFIX}"
            np.hstack([date_index, aligned[field]]).astype("<f").tofile(str(bin_path.resolve()))

    def dump(self) -> None:
        all_datetimes: set[pd.Timestamp] = set()
        date_range_rows: list[str] = []
        for file_path in self.df_files:
            date_range, date_set = self._get_date_range_and_set(file_path)
            all_datetimes |= date_set
            if date_range is None:
                continue
            begin_time, end_time = date_range
            date_range_rows.append(
                self.INSTRUMENTS_SEP.join(
                    [
                        fname_to_code(file_path.stem.strip().lower()).upper(),
                        pd.Timestamp(begin_time).strftime("%Y-%m-%d"),
                        pd.Timestamp(end_time).strftime("%Y-%m-%d"),
                    ]
                )
            )

        calendars = sorted(all_datetimes)
        self._save_calendars(calendars)
        self._save_instruments(date_range_rows)

        self.features_dir.mkdir(parents=True, exist_ok=True)
        dump_one = partial(
            self._dump_bin_file,
            calendar_list=calendars,
            date_field_name=self.date_field_name,
            include_fields=self.include_fields,
            features_dir=self.features_dir,
            freq=self.freq,
        )
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            list(executor.map(dump_one, self.df_files))
