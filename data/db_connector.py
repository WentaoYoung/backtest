"""
离线因子「数据库」适配层：不连接 MySQL，从本地 Parquet 读取。

与历史 MySQL 版接口保持一致，供 FactorDataGateway、LibraryCache、Web API 调用。
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

import duckdb
import pandas as pd

from data.date_params import coerce_yyyy_mm_dd
from data.parquet_io import has_factors_all_parquet, resolve_factors_all_paths

try:
    from web.config import PARQUET_DIR, FACTORS_HIVE_DIR
except ImportError:
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(_root, "data")
    PARQUET_DIR = os.path.join(DATA_DIR, "parquet_loaded")
    FACTORS_HIVE_DIR = os.path.join(_root, "factors_hive")

_META_COLS = frozenset(
    {"trade_dt", "ticker", "s_info_windcode", "hive_ym", "hive_y", "year", "month", "dt"}
)


def _hive_glob_sql() -> Optional[str]:
    if not os.path.isdir(FACTORS_HIVE_DIR):
        return None
    for _root, _dirs, files in os.walk(FACTORS_HIVE_DIR):
        for f in files:
            if f.lower().endswith(".parquet"):
                return os.path.join(FACTORS_HIVE_DIR, "**", "*.parquet").replace("\\", "/")
    return None


class LocalParquetFactorDatabase:
    """从 factors_all.parquet（或 Hive 分区目录）提供与旧 MySQL 封装相同的方法名。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._full_long: Optional[pd.DataFrame] = None
        self._cache_key: Optional[str] = None

    def _read_sql_source(self, table_name: Optional[str]) -> Tuple[str, str]:
        """与 DuckDBFactorRepository 一致：factors_all 优先 Hive 分区目录。"""
        table = (table_name or "factors_all").strip()
        if table == "factors_all":
            glob_sql = _hive_glob_sql()
            if glob_sql:
                return "hive", glob_sql
            paths = resolve_factors_all_paths(PARQUET_DIR)
            if paths:
                return "sharded", "|".join(p.replace("\\", "/") for p in paths)
        p = os.path.join(PARQUET_DIR, f"{table}.parquet")
        if os.path.isfile(p):
            return "single", p.replace("\\", "/")
        return "", ""

    def _invalidate_if_needed(self) -> None:
        mode, path = self._read_sql_source(None)
        key = f"{mode}:{path}"
        if key != self._cache_key:
            self._full_long = None
            self._cache_key = key

    def _load_full_long(self) -> Optional[pd.DataFrame]:
        self._invalidate_if_needed()
        mode, path_sql = self._read_sql_source(None)
        if not path_sql:
            return None
        with self._lock:
            if self._full_long is not None:
                return self._full_long
            if mode == "hive":
                esc = path_sql.replace("'", "''")
                expr = f"read_parquet('{esc}', hive_partitioning = true)"
                con = duckdb.connect(database=":memory:")
                try:
                    df = con.execute(f"SELECT * FROM {expr}").df()
                finally:
                    con.close()
            elif mode == "sharded":
                from data.parquet_io import read_factors_all_dataframe

                paths = [p.replace("/", os.sep) for p in path_sql.split("|")]
                df = read_factors_all_dataframe(paths)
            else:
                from data.parquet_io import read_factors_all_dataframe

                fs_path = os.path.normpath(path_sql.replace("/", os.sep))
                df = read_factors_all_dataframe([fs_path])
            if "s_info_windcode" in df.columns and "ticker" not in df.columns:
                df = df.rename(columns={"s_info_windcode": "ticker"})
            df["trade_dt"] = pd.to_datetime(df["trade_dt"])
            df["ticker"] = df["ticker"].astype(str).str.upper()
            self._full_long = df
            return df

    def _factor_columns(self, df: pd.DataFrame) -> List[str]:
        return [c for c in df.columns if c not in _META_COLS]

    def load_factor(
        self,
        factor_name: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        table_name: Optional[str] = None,
    ) -> Optional[pd.DataFrame]:
        df = self._load_full_long()
        if df is None or factor_name not in df.columns:
            return None
        sd, ed = coerce_yyyy_mm_dd(start_date), coerce_yyyy_mm_dd(end_date)
        out = df[["ticker", "trade_dt", factor_name]].rename(columns={factor_name: "factor_value"})
        out = out[out["factor_value"].notna()]
        if sd:
            out = out[out["trade_dt"] >= pd.to_datetime(sd)]
        if ed:
            out = out[out["trade_dt"] <= pd.to_datetime(ed)]
        return out

    def load_multiple_factors(
        self,
        factor_names: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        table_name: Optional[str] = None,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ) -> Optional[pd.DataFrame]:
        df = self._load_full_long()
        if df is None:
            return None
        names = [n for n in factor_names if n in df.columns]
        if not names:
            return None
        if progress_callback:
            progress_callback({"progress": 10.0, "stage": "本地Parquet", "detail": "读取多因子"})
        sd, ed = coerce_yyyy_mm_dd(start_date), coerce_yyyy_mm_dd(end_date)
        cols = ["ticker", "trade_dt"] + names
        out = df[cols].copy()
        nn = out[names].notna().any(axis=1)
        out = out[nn]
        if sd:
            out = out[out["trade_dt"] >= pd.to_datetime(sd)]
        if ed:
            out = out[out["trade_dt"] <= pd.to_datetime(ed)]
        if progress_callback:
            progress_callback({"progress": 100.0, "stage": "本地Parquet", "detail": "完成"})
        return out

    def get_available_factors_dynamic(self, table_name: Optional[str] = None) -> List[str]:
        df = self._load_full_long()
        if df is None:
            return []
        return sorted(self._factor_columns(df))

    def get_date_range(self) -> Tuple[Optional[str], Optional[str]]:
        df = self._load_full_long()
        if df is None or df.empty:
            return None, None
        lo = df["trade_dt"].min()
        hi = df["trade_dt"].max()
        return lo.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d")

    def get_all_factor_tables(self) -> List[Dict[str, Any]]:
        factors = self.get_available_factors_dynamic("factors_all")
        if not factors:
            return []
        return [
            {
                "name": "factors_all",
                "display_name": "本地因子表(factors_all)",
                "factors": factors,
            }
        ]

    def get_all_factor_columns(self, table_name: Optional[str] = None) -> List[str]:
        return self.get_available_factors_dynamic(table_name)

    def get_factor_categories(self) -> Dict[str, str]:
        return {
            "LNCAP": "规模",
            "REV1": "反转",
            "MOM12": "动量",
            "BM": "估值",
            "ROE": "盈利",
            "VOL3M": "波动",
            "ACC": "质量",
            "LEV": "杠杆",
        }

    def get_all_factors_wide(self, factor_table: Optional[str] = None) -> Dict[str, pd.DataFrame]:
        df = self._load_full_long()
        if df is None:
            return {}
        out: Dict[str, pd.DataFrame] = {}
        for col in self._factor_columns(df):
            sub = df[["trade_dt", "ticker", col]].dropna(subset=[col])
            wide = sub.pivot(index="trade_dt", columns="ticker", values=col)
            wide.index = pd.to_datetime(wide.index)
            wide.columns = wide.columns.astype(str).str.upper()
            out[col] = wide.sort_index()
        return out


_db_singleton: Optional[LocalParquetFactorDatabase] = None


def get_factor_database() -> LocalParquetFactorDatabase:
    global _db_singleton
    if _db_singleton is None:
        _db_singleton = LocalParquetFactorDatabase()
    return _db_singleton


def test_database_connection() -> Tuple[bool, str]:
    if _hive_glob_sql() or has_factors_all_parquet(PARQUET_DIR):
        return True, "离线模式：使用本地 Parquet 因子数据（无需 MySQL）"
    return False, "未找到本地因子数据。请运行: python -m data.generate_demo_data"


def detect_new_factor_tables(force_rescan: bool = False) -> List[str]:
    return []


def get_db_connector() -> Any:
    """写入因子库到 MySQL 时使用；离线演示未配置，返回 None。"""
    return None
