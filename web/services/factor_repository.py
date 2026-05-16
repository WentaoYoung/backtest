import os
import threading
from abc import ABC, abstractmethod
from typing import Callable, FrozenSet, List, Optional, Tuple

import duckdb
import pandas as pd
import pyarrow.parquet as pq

from data.date_params import coerce_yyyy_mm_dd
from data.parquet_io import read_factors_all_dataframe, resolve_factors_all_paths

# parquet 列集合缓存：(规范化键, mtime) -> 列名，避免每次 DESCRIBE / read_schema
_PARQUET_COLS_CACHE: dict[str, Tuple[float, FrozenSet[str]]] = {}
_PARQUET_COLS_LOCK = threading.Lock()

# factors_all 全表缓存：(规范化路径键, mtime) -> DataFrame（单文件或分片拼接）
_SINGLE_PARQUET_FULL_CACHE: dict[str, Tuple[float, pd.DataFrame]] = {}
_SINGLE_PARQUET_FULL_LOCK = threading.Lock()


class BaseFactorRepository(ABC):
    """因子仓储抽象接口，便于替换不同数据源实现。"""

    @abstractmethod
    def resolve_factor_parquet_path(self, table_name: Optional[str] = None) -> Optional[str]:
        raise NotImplementedError

    @abstractmethod
    def load_factor_from_parquet(
        self,
        factor_name: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        table_name: Optional[str] = None,
    ):
        raise NotImplementedError

    @abstractmethod
    def load_multiple_factors_from_parquet(
        self,
        factor_names: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        table_name: Optional[str] = None,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ):
        raise NotImplementedError


class DuckDBFactorRepository(BaseFactorRepository):
    """DuckDB + Parquet 实现；对 factors_all 优先读项目根下 Hive 分区目录，否则回退单文件。"""

    def __init__(self, parquet_dir: str, hive_dir: Optional[str] = None):
        self.parquet_dir = os.path.abspath(parquet_dir)
        if hive_dir is not None:
            self.hive_dir = os.path.abspath(hive_dir)
        else:
            env_h = os.environ.get("FACTORS_HIVE_DIR")
            self.hive_dir = (
                os.path.abspath(env_h)
                if env_h
                else os.path.join(os.path.dirname(os.path.dirname(self.parquet_dir)), "factors_hive")
            )

    @staticmethod
    def _qident(name: str) -> str:
        """DuckDB 标识符安全引用。"""
        return '"' + str(name).replace('"', '""') + '"'

    @staticmethod
    def _parquet_cache_key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def _hive_has_parquet(self) -> bool:
        if not os.path.isdir(self.hive_dir):
            return False
        for _root, _dirs, files in os.walk(self.hive_dir):
            for f in files:
                if f.lower().endswith(".parquet"):
                    return True
        return False

    def _hive_tree_mtime(self) -> float:
        try:
            t = os.path.getmtime(self.hive_dir)
        except OSError:
            return -1.0
        try:
            for name in os.listdir(self.hive_dir):
                p = os.path.join(self.hive_dir, name)
                if os.path.isdir(p):
                    try:
                        t = max(t, os.path.getmtime(p))
                    except OSError:
                        pass
        except OSError:
            pass
        return t

    def _resolve_load_plan(
        self, table_name: Optional[str] = None
    ) -> Optional[Tuple[str, str, str, float]]:
        """
        返回 (mode, path_for_sql, cache_key, cache_mtime)。
        mode 为 'hive' 时 path_for_sql 为带 ** 的 glob（正斜杠）；'single' 时为单文件路径。
        """
        table = (table_name or "factors_all").strip()

        if table == "factors_all" and self._hive_has_parquet():
            glob_sql = os.path.join(self.hive_dir, "**", "*.parquet").replace("\\", "/")
            ck = self._parquet_cache_key(self.hive_dir) + "::HIVE"
            return ("hive", glob_sql, ck, self._hive_tree_mtime())

        if table == "factors_all":
            shard_paths = resolve_factors_all_paths(self.parquet_dir)
            if shard_paths:
                path_sql = "|".join(p.replace("\\", "/") for p in shard_paths)
                ck = self._parquet_cache_key(shard_paths[0]) + f"::SHARDS:{len(shard_paths)}"
                from data.parquet_io import shard_cache_mtime

                return ("sharded", path_sql, ck, shard_cache_mtime(shard_paths))

        single_fs = os.path.join(self.parquet_dir, f"{table}.parquet")
        if os.path.isfile(single_fs):
            p = single_fs.replace("\\", "/")
            ck = self._parquet_cache_key(single_fs)
            try:
                mt = os.path.getmtime(single_fs)
            except OSError:
                mt = -1.0
            return ("single", p, ck, mt)
        return None

    @staticmethod
    def _sql_escape_path(p: str) -> str:
        return p.replace("'", "''")

    def _read_parquet_expr(self, mode: str, path_for_sql: str) -> str:
        if mode == "hive":
            esc = self._sql_escape_path(path_for_sql)
            return f"read_parquet('{esc}', hive_partitioning = true)"
        if mode == "sharded":
            paths = path_for_sql.split("|")
            quoted = ", ".join(f"'{self._sql_escape_path(p)}'" for p in paths)
            return f"read_parquet([{quoted}])"
        esc = self._sql_escape_path(path_for_sql)
        return f"read_parquet('{esc}')"

    def _columns_for_plan(
        self,
        con: Optional[duckdb.DuckDBPyConnection],
        mode: str,
        path_for_sql: str,
        cache_key: str,
        cache_mtime: float,
    ) -> FrozenSet[str]:
        with _PARQUET_COLS_LOCK:
            hit = _PARQUET_COLS_CACHE.get(cache_key)
            if hit is not None and hit[0] == cache_mtime:
                return hit[1]
        if mode in ("single", "sharded"):
            if mode == "sharded":
                paths = [os.path.normpath(p.replace("/", os.sep)) for p in path_for_sql.split("|")]
                schema = pq.read_schema(paths[0])
            else:
                fs_path = os.path.normpath(path_for_sql.replace("/", os.sep))
                schema = pq.read_schema(fs_path)
            cols = frozenset(schema.names)
        else:
            if con is None:
                raise RuntimeError("DuckDB connection required for hive parquet column discovery")
            expr = self._read_parquet_expr(mode, path_for_sql)
            schema_df = con.execute(f"DESCRIBE SELECT * FROM {expr}").df()
            cols = frozenset(schema_df["column_name"].astype(str).tolist())
        with _PARQUET_COLS_LOCK:
            _PARQUET_COLS_CACHE[cache_key] = (cache_mtime, cols)
        return cols

    @staticmethod
    def _read_parquet_full_dataframe(mode: str, path_for_sql: str, cache_mtime: float) -> pd.DataFrame:
        """factors_all 单文件或分片：拼接后 to_pandas（带 mtime 缓存）。"""
        if mode == "sharded":
            paths = [os.path.normpath(p.replace("/", os.sep)) for p in path_for_sql.split("|")]
            cache_path = os.path.normcase("|".join(os.path.abspath(p) for p in paths))
            mt = cache_mtime
        else:
            fs_path = os.path.normpath(path_for_sql.replace("/", os.sep))
            paths = [fs_path]
            cache_path = os.path.normcase(os.path.abspath(fs_path))
            try:
                mt = os.path.getmtime(fs_path)
            except OSError:
                mt = -1.0
        with _SINGLE_PARQUET_FULL_LOCK:
            hit = _SINGLE_PARQUET_FULL_CACHE.get(cache_path)
            if hit is not None and hit[0] == mt:
                return hit[1]
        df = read_factors_all_dataframe(paths)
        with _SINGLE_PARQUET_FULL_LOCK:
            _SINGLE_PARQUET_FULL_CACHE[cache_path] = (mt, df)
        return df

    @staticmethod
    def _partition_prune_sql_and_params(
        available_cols: FrozenSet[str],
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> Tuple[str, List]:
        parts: List[str] = []
        params: List = []
        if "hive_ym" in available_cols:
            if start_date:
                parts.append("hive_ym >= strftime(CAST(? AS DATE), '%Y-%m')")
                params.append(start_date)
            if end_date:
                parts.append("hive_ym <= strftime(CAST(? AS DATE), '%Y-%m')")
                params.append(end_date)
        elif "hive_y" in available_cols:
            if start_date:
                parts.append("hive_y >= strftime(CAST(? AS DATE), '%Y')")
                params.append(start_date)
            if end_date:
                parts.append("hive_y <= strftime(CAST(? AS DATE), '%Y')")
                params.append(end_date)
        if not parts:
            return "", []
        return " AND ".join(parts), params

    def resolve_factor_parquet_path(self, table_name: Optional[str] = None) -> Optional[str]:
        plan = self._resolve_load_plan(table_name)
        if not plan:
            return None
        mode, path_sql, _ck, _mt = plan
        if mode == "hive":
            return self.hive_dir if os.path.isdir(self.hive_dir) else None
        if mode == "sharded":
            return path_sql.split("|")[0]
        table = (table_name or "factors_all").strip()
        path = os.path.join(self.parquet_dir, f"{table}.parquet")
        return path if os.path.isfile(path) else None

    def load_factor_from_parquet(
        self,
        factor_name: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        table_name: Optional[str] = None,
    ):
        plan = self._resolve_load_plan(table_name)
        if not plan:
            return None

        sd = coerce_yyyy_mm_dd(start_date)
        ed = coerce_yyyy_mm_dd(end_date)

        mode, path_sql, cache_key, cache_mtime = plan
        con: Optional[duckdb.DuckDBPyConnection] = None
        try:
            if mode == "hive":
                con = duckdb.connect(database=":memory:")
            available_cols = self._columns_for_plan(con, mode, path_sql, cache_key, cache_mtime)
            available_set = set(available_cols)

            if factor_name not in available_set:
                label = os.path.basename(self.hive_dir) if mode == "hive" else os.path.basename(path_sql)
                print(f"[DuckDB] 本地文件缺少因子列: {factor_name} ({label})")
                return None

            ticker_col = "s_info_windcode" if "s_info_windcode" in available_set else (
                "ticker" if "ticker" in available_set else None
            )
            if ticker_col is None or "trade_dt" not in available_set:
                label = os.path.basename(self.hive_dir) if mode == "hive" else os.path.basename(path_sql)
                print(f"[DuckDB] 本地文件缺少必要列(ticker/trade_dt): {label}")
                return None

            if mode in ("single", "sharded"):
                full_df = self._read_parquet_full_dataframe(mode, path_sql, cache_mtime)
                factor_df = full_df[[ticker_col, "trade_dt", factor_name]].copy()
                factor_df = factor_df.rename(columns={ticker_col: "ticker", factor_name: "factor_value"})
                factor_df = factor_df[factor_df["factor_value"].notna()]
                if sd:
                    factor_df = factor_df[pd.to_datetime(factor_df["trade_dt"]) >= pd.to_datetime(sd)]
                if ed:
                    factor_df = factor_df[pd.to_datetime(factor_df["trade_dt"]) <= pd.to_datetime(ed)]
                if factor_df.empty:
                    return factor_df
                factor_df["trade_dt"] = pd.to_datetime(factor_df["trade_dt"])
                factor_df["ticker"] = factor_df["ticker"].astype(str).str.upper()
                src = os.path.basename(path_sql)
                print(f"[DuckDB] 本地读取成功: {factor_name}, {len(factor_df)} 条 ({src})")
                return factor_df

            if con is None:
                con = duckdb.connect(database=":memory:")
            from_expr = self._read_parquet_expr(mode, path_sql)
            prune_sql, prune_params = self._partition_prune_sql_and_params(
                available_cols, sd, ed
            )

            sql = f"""
                SELECT
                    {self._qident(ticker_col)} AS ticker,
                    {self._qident('trade_dt')},
                    {self._qident(factor_name)} AS factor_value
                FROM {from_expr}
                WHERE {self._qident(factor_name)} IS NOT NULL
            """
            params: List = list(prune_params)
            if prune_sql:
                sql += f" AND ({prune_sql})"
            if sd:
                sql += " AND CAST(trade_dt AS DATE) >= ?"
                params.append(sd)
            if ed:
                sql += " AND CAST(trade_dt AS DATE) <= ?"
                params.append(ed)

            factor_df = con.execute(sql, params).df()
            if factor_df.empty:
                return factor_df

            factor_df["trade_dt"] = pd.to_datetime(factor_df["trade_dt"])
            factor_df["ticker"] = factor_df["ticker"].astype(str).str.upper()
            src = f"Hive:{os.path.basename(self.hive_dir)}" if mode == "hive" else os.path.basename(path_sql)
            print(f"[DuckDB] 本地读取成功: {factor_name}, {len(factor_df)} 条 ({src})")
            return factor_df
        except Exception as e:
            print(f"[DuckDB] 本地读取失败: {e}")
            return None
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass

    def load_multiple_factors_from_parquet(
        self,
        factor_names: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        table_name: Optional[str] = None,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ):
        plan = self._resolve_load_plan(table_name)
        if not plan:
            return None

        sd = coerce_yyyy_mm_dd(start_date)
        ed = coerce_yyyy_mm_dd(end_date)

        mode, path_sql, cache_key, cache_mtime = plan
        con: Optional[duckdb.DuckDBPyConnection] = None
        try:
            if mode == "hive":
                con = duckdb.connect(database=":memory:")
            available_cols = self._columns_for_plan(con, mode, path_sql, cache_key, cache_mtime)
            available_set = set(available_cols)

            ticker_col = "s_info_windcode" if "s_info_windcode" in available_set else (
                "ticker" if "ticker" in available_set else None
            )
            if ticker_col is None or "trade_dt" not in available_set:
                label = os.path.basename(self.hive_dir) if mode == "hive" else os.path.basename(path_sql)
                print(f"[DuckDB] 本地多因子读取缺少必要列(ticker/trade_dt): {label}")
                return None

            missing_factors = [f for f in factor_names if f not in available_set]
            available_factors = [f for f in factor_names if f in available_set]
            if not available_factors:
                print(f"[DuckDB] 本地多因子读取缺少全部目标列: {missing_factors[:5]} (共{len(missing_factors)}个)")
                return None
            if missing_factors:
                print(
                    f"[DuckDB] 本地多因子读取部分缺列，将仅使用本地可用列。"
                    f"可用{len(available_factors)}个，缺失{len(missing_factors)}个"
                )

            if progress_callback:
                progress_callback({"progress": 5.0, "stage": "DuckDB读取", "detail": "开始读取本地Parquet"})

            if mode in ("single", "sharded"):
                full_df = self._read_parquet_full_dataframe(mode, path_sql, cache_mtime)
                cols = [ticker_col, "trade_dt"] + available_factors
                df = full_df[cols].copy()
                nn = df[available_factors].notna().any(axis=1)
                df = df[nn]
                if sd:
                    df = df[pd.to_datetime(df["trade_dt"]) >= pd.to_datetime(sd)]
                if ed:
                    df = df[pd.to_datetime(df["trade_dt"]) <= pd.to_datetime(ed)]
                if df.empty:
                    return df
                if progress_callback:
                    progress_callback({"progress": 90.0, "stage": "DuckDB读取", "detail": f"已读取 {len(df)} 行"})
                df["trade_dt"] = pd.to_datetime(df["trade_dt"])
                df = df.rename(columns={ticker_col: "ticker"})
                df["ticker"] = df["ticker"].astype(str).str.upper()
                if progress_callback:
                    progress_callback({"progress": 100.0, "stage": "DuckDB读取", "detail": "本地Parquet读取完成"})
                src = os.path.basename(path_sql)
                print(f"[DuckDB] 本地多因子读取成功: {len(df)} 条 ({src})")
                return df

            if con is None:
                con = duckdb.connect(database=":memory:")
            from_expr = self._read_parquet_expr(mode, path_sql)
            prune_sql, prune_params = self._partition_prune_sql_and_params(
                available_cols, sd, ed
            )

            select_cols = ", ".join([self._qident(f) for f in available_factors])
            where_not_null = " OR ".join([f"{self._qident(f)} IS NOT NULL" for f in available_factors])
            sql = f"""
                SELECT
                    {self._qident(ticker_col)} AS ticker,
                    {self._qident('trade_dt')},
                    {select_cols}
                FROM {from_expr}
                WHERE ({where_not_null})
            """
            params: List = list(prune_params)
            if prune_sql:
                sql += f" AND ({prune_sql})"
            if sd:
                sql += " AND CAST(trade_dt AS DATE) >= ?"
                params.append(sd)
            if ed:
                sql += " AND CAST(trade_dt AS DATE) <= ?"
                params.append(ed)

            df = con.execute(sql, params).df()
            if df.empty:
                return df

            if progress_callback:
                progress_callback({"progress": 90.0, "stage": "DuckDB读取", "detail": f"已读取 {len(df)} 行"})

            df["trade_dt"] = pd.to_datetime(df["trade_dt"])
            df["ticker"] = df["ticker"].astype(str).str.upper()

            if progress_callback:
                progress_callback({"progress": 100.0, "stage": "DuckDB读取", "detail": "本地Parquet读取完成"})
            src = f"Hive:{os.path.basename(self.hive_dir)}" if mode == "hive" else os.path.basename(path_sql)
            print(f"[DuckDB] 本地多因子读取成功: {len(df)} 条 ({src})")
            return df
        except Exception as e:
            print(f"[DuckDB] 本地多因子读取失败: {e}")
            return None
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass


# 向后兼容：现有调用仍可使用 FactorRepository 名称
FactorRepository = DuckDBFactorRepository
