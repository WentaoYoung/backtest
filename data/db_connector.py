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
from data.parquet_io import (
    META_COLS,
    duckdb_parquet_expr,
    has_factors_all_parquet,
    read_factors_date_range,
    read_factors_schema_columns,
    resolve_factors_all_paths,
)

try:
    from web.config import PARQUET_DIR, FACTORS_HIVE_DIR
except ImportError:
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(_root, "data")
    PARQUET_DIR = os.path.join(DATA_DIR, "parquet_loaded")
    FACTORS_HIVE_DIR = os.path.join(_root, "factors_hive")


def _hive_glob_sql() -> Optional[str]:
    if not os.path.isdir(FACTORS_HIVE_DIR):
        return None
    for _root, _dirs, files in os.walk(FACTORS_HIVE_DIR):
        for f in files:
            if f.lower().endswith(".parquet"):
                return os.path.join(FACTORS_HIVE_DIR, "**", "*.parquet").replace("\\", "/")
    return None


def _qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


class LocalParquetFactorDatabase:
    """从 factors_all parquet 分片（或 Hive）提供与旧 MySQL 封装相同的方法名。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._schema_cache: Optional[Tuple[str, List[str]]] = None

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

    def _invalidate_schema_cache(self) -> None:
        mode, path = self._read_sql_source(None)
        key = f"{mode}:{path}"
        if self._schema_cache is None or self._schema_cache[0] != key:
            self._schema_cache = None

    def _factor_columns(self) -> List[str]:
        self._invalidate_schema_cache()
        mode, path_sql = self._read_sql_source(None)
        if not path_sql:
            return []
        key = f"{mode}:{path_sql}"
        with self._lock:
            if self._schema_cache is not None and self._schema_cache[0] == key:
                return self._schema_cache[1]
            if mode == "hive":
                con = duckdb.connect(database=":memory:")
                try:
                    expr = duckdb_parquet_expr(mode, path_sql)
                    names = con.execute(f"DESCRIBE SELECT * FROM {expr}").df()["column_name"].astype(str).tolist()
                finally:
                    con.close()
                cols = sorted(c for c in names if c not in META_COLS)
            elif mode == "sharded":
                paths = [p.replace("/", os.sep) for p in path_sql.split("|")]
                cols = read_factors_schema_columns(paths)
            else:
                import pyarrow.parquet as pq

                fs_path = os.path.normpath(path_sql.replace("/", os.sep))
                cols = sorted(c for c in pq.read_schema(fs_path).names if c not in META_COLS)
            self._schema_cache = (key, cols)
            return cols

    def _duckdb_query(self, sql: str, params: Optional[List] = None) -> pd.DataFrame:
        mode, path_sql = self._read_sql_source(None)
        if not path_sql:
            return pd.DataFrame()
        con = duckdb.connect(database=":memory:")
        try:
            if params:
                return con.execute(sql, params).df()
            return con.execute(sql).df()
        finally:
            con.close()

    def _from_expr(self) -> str:
        mode, path_sql = self._read_sql_source(None)
        if not path_sql:
            raise RuntimeError("No parquet source configured")
        return duckdb_parquet_expr(mode, path_sql)

    def _schema_column_names(self) -> List[str]:
        mode, path_sql = self._read_sql_source(None)
        if not path_sql:
            return []
        if mode == "sharded":
            import pyarrow.parquet as pq

            paths = [p.replace("/", os.sep) for p in path_sql.split("|")]
            return list(pq.read_schema(paths[0]).names)
        if mode == "single":
            import pyarrow.parquet as pq

            return list(pq.read_schema(os.path.normpath(path_sql.replace("/", os.sep))).names)
        con = duckdb.connect(database=":memory:")
        try:
            expr = duckdb_parquet_expr(mode, path_sql)
            df = con.execute(f"DESCRIBE SELECT * FROM {expr}").df()
            return df["column_name"].astype(str).tolist()
        finally:
            con.close()

    def _ticker_col(self) -> Optional[str]:
        names = self._schema_column_names()
        if "s_info_windcode" in names:
            return "s_info_windcode"
        if "ticker" in names:
            return "ticker"
        return None

    def load_factor(
        self,
        factor_name: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        table_name: Optional[str] = None,
    ) -> Optional[pd.DataFrame]:
        cols = self._factor_columns()
        if factor_name not in cols:
            return None
        ticker_col = self._ticker_col()
        if ticker_col is None or "trade_dt" not in self._schema_column_names():
            return None
        sd, ed = coerce_yyyy_mm_dd(start_date), coerce_yyyy_mm_dd(end_date)
        from_expr = self._from_expr()
        sql = f"""
            SELECT
                {_qident(ticker_col)} AS ticker,
                {_qident('trade_dt')},
                {_qident(factor_name)} AS factor_value
            FROM {from_expr}
            WHERE {_qident(factor_name)} IS NOT NULL
        """
        params: List = []
        if sd:
            sql += " AND CAST(trade_dt AS DATE) >= ?"
            params.append(sd)
        if ed:
            sql += " AND CAST(trade_dt AS DATE) <= ?"
            params.append(ed)
        out = self._duckdb_query(sql, params)
        if out.empty:
            return out
        out["trade_dt"] = pd.to_datetime(out["trade_dt"])
        out["ticker"] = out["ticker"].astype(str).str.upper()
        return out

    def load_multiple_factors(
        self,
        factor_names: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        table_name: Optional[str] = None,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ) -> Optional[pd.DataFrame]:
        cols = self._factor_columns()
        names = [n for n in factor_names if n in cols]
        if not names:
            return None
        ticker_col = self._ticker_col()
        if ticker_col is None or "trade_dt" not in self._schema_column_names():
            return None
        if progress_callback:
            progress_callback({"progress": 10.0, "stage": "本地Parquet", "detail": "DuckDB读取多因子"})
        sd, ed = coerce_yyyy_mm_dd(start_date), coerce_yyyy_mm_dd(end_date)
        from_expr = self._from_expr()
        select_cols = ", ".join(_qident(f) for f in names)
        where_nn = " OR ".join(f"{_qident(f)} IS NOT NULL" for f in names)
        sql = f"""
            SELECT {_qident(ticker_col)} AS ticker, {_qident('trade_dt')}, {select_cols}
            FROM {from_expr}
            WHERE ({where_nn})
        """
        params: List = []
        if sd:
            sql += " AND CAST(trade_dt AS DATE) >= ?"
            params.append(sd)
        if ed:
            sql += " AND CAST(trade_dt AS DATE) <= ?"
            params.append(ed)
        out = self._duckdb_query(sql, params)
        if progress_callback:
            progress_callback({"progress": 100.0, "stage": "本地Parquet", "detail": "完成"})
        if out.empty:
            return out
        out["trade_dt"] = pd.to_datetime(out["trade_dt"])
        out["ticker"] = out["ticker"].astype(str).str.upper()
        return out

    def get_available_factors_dynamic(self, table_name: Optional[str] = None) -> List[str]:
        return self._factor_columns()

    def get_date_range(self) -> Tuple[Optional[str], Optional[str]]:
        mode, path_sql = self._read_sql_source(None)
        if not path_sql:
            return None, None
        return read_factors_date_range(mode, path_sql)

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
        """按因子列逐个 DuckDB 查询并 pivot，避免一次性加载全表。"""
        cols = self._factor_columns()
        if not cols:
            return {}
        ticker_col = self._ticker_col()
        if ticker_col is None:
            return {}
        from_expr = self._from_expr()
        out: Dict[str, pd.DataFrame] = {}
        for col in cols:
            sql = f"""
                SELECT CAST(trade_dt AS DATE) AS trade_dt,
                       UPPER(CAST({_qident(ticker_col)} AS VARCHAR)) AS ticker,
                       {_qident(col)} AS val
                FROM {from_expr}
                WHERE {_qident(col)} IS NOT NULL
            """
            sub = self._duckdb_query(sql)
            if sub.empty:
                continue
            wide = sub.pivot(index="trade_dt", columns="ticker", values="val")
            wide.index = pd.to_datetime(wide.index)
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
