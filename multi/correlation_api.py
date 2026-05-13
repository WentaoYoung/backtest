"""
correlation_api.py
新因子 vs 因子库相关性分析的 Flask Blueprint

注册方式（在 app.py 里加两行）：
    from web.correlation_api import correlation_bp, set_library_cache
    app.register_blueprint(correlation_bp)

服务启动后，在数据加载完成处调用：
    from web.correlation_api import set_library_cache
    set_library_cache(cache)   # cache 是已 load() 的 LibraryCache 实例
"""

import io
import logging
import os
import time
import hashlib

import pandas as pd
from flask import Blueprint, request, jsonify, Response

from data.date_params import both_date_bounds_blank
from multi.factor_correlation import LibraryCache, FactorVsLibraryAnalyzer
from web.config import DATA_DIR, FACTORS_HIVE_DIR
from web.services.factor_repository import FactorRepository
from web.services.factor_data_gateway import FactorDataGateway

logger = logging.getLogger(__name__)

correlation_bp = Blueprint("correlation", __name__, url_prefix="/api/correlation")

# ── 全局缓存（服务级别单例）──────────────────────────────────────────────────
_library_cache: LibraryCache | None = None
_price_df: pd.DataFrame | None = None
_library_catalog: dict = {
    "loaded": False,
    "factor_names": [],
    "categories": {},
    "factor_to_table": {},
    "load_time_sec": 0.0,
}
_price_source: str = "unknown"
_library_wide_cache: dict[str, pd.DataFrame] = {}
_library_source_cache: dict[str, str] = {}
_analysis_cache_store: dict[tuple[str, ...], LibraryCache] = {}
_WARMUP_BATCH_SIZE = 24


def _library_calendar_bounds() -> tuple[str | None, str | None]:
    """
    因子库实际 trade_dt 范围，用于与「上传新因子推断的裁剪区间」求交。
    优先用启动时已注入的 LibraryCache（无额外 IO）；否则回退 LocalParquetFactorDatabase。
    """
    global _library_cache
    if _library_cache is not None and getattr(_library_cache, "wide", None):
        for df in _library_cache.wide.values():
            if df is None or df.empty:
                continue
            idx = pd.to_datetime(df.index, errors="coerce").dropna()
            if len(idx) == 0:
                continue
            return idx.min().strftime("%Y-%m-%d"), idx.max().strftime("%Y-%m-%d")
    try:
        from data.db_connector import get_factor_database

        return get_factor_database().get_date_range()
    except Exception:
        logger.exception("读取因子库日历范围失败")
        return None, None


def _clamp_library_load_bounds(
    lib_start: str | None,
    lib_end: str | None,
) -> tuple[str | None, str | None, str | None]:
    """
    将上传新因子推断的 [lib_start, lib_end] 与因子库 parquet 实际日历求交，
    避免「上传区间落在库外」导致裁剪后读库为空，却显示失败 0 条。

    返回 (eff_start, eff_end, err400)。err400 非空表示与因子库完全无交集，应直接 400。
    """
    if lib_start is None or lib_end is None:
        return lib_start, lib_end, None

    data_lo, data_hi = _library_calendar_bounds()
    if not data_lo or not data_hi:
        return lib_start, lib_end, None

    try:
        us, ue = pd.Timestamp(lib_start), pd.Timestamp(lib_end)
        ds, de = pd.Timestamp(data_lo), pd.Timestamp(data_hi)
    except Exception:
        return lib_start, lib_end, None

    lo, hi = max(us, ds), min(ue, de)
    if lo > hi:
        return (
            lib_start,
            lib_end,
            f"上传因子推断的日期区间（约 {lib_start}～{lib_end}）与本地因子库 "
            f"（factors_all.parquet）日历（{data_lo}～{data_hi}）无交集，无法加载库因子对比。"
            f"请核对上传 CSV 的 trade_dt 是否在因子库覆盖范围内，或更新 parquet。",
        )

    ns, ne = lo.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d")
    if ns != lib_start or ne != lib_end:
        logger.info(
            "相关性分析：库因子读取区间已与因子库日历求交：%s～%s → %s～%s",
            lib_start,
            lib_end,
            ns,
            ne,
        )
    return ns, ne, None


def _new_factors_to_library_date_bounds(
    new_factors: dict[str, pd.DataFrame],
    future_days: int,
) -> tuple[str | None, str | None]:
    """
    从新因子宽表推断日期区间，供 DuckDB 裁剪 parquet 扫描。
    结束日期略向后延伸，避免 IC（未来收益）在样本末尾对齐不足。
    """
    ts_min, ts_max = None, None
    for df in new_factors.values():
        if df is None or df.empty:
            continue
        idx = pd.to_datetime(df.index, errors="coerce")
        idx = idx[~pd.isna(idx)]
        if len(idx) == 0:
            continue
        lo, hi = idx.min(), idx.max()
        ts_min = lo if ts_min is None else min(ts_min, lo)
        ts_max = hi if ts_max is None else max(ts_max, hi)
    if ts_min is None or ts_max is None:
        return None, None
    start_s = ts_min.strftime("%Y-%m-%d")
    pad_days = min(max(int(future_days) * 3, 7), 60)
    end_s = (ts_max + pd.Timedelta(days=pad_days)).strftime("%Y-%m-%d")
    return start_s, end_s


def _library_wide_cache_key(
    factor_name: str,
    start_date: str | None,
    end_date: str | None,
) -> str:
    """裁剪读取时必须区分区间，避免与全样本缓存串键。"""
    if both_date_bounds_blank(start_date, end_date):
        return factor_name
    return f"{factor_name}\x00{start_date or ''}\x00{end_date or ''}"


def _clear_library_factor_cache() -> None:
    """清空服务级库因子缓存（宽表）。"""
    global _library_wide_cache, _library_source_cache, _analysis_cache_store
    _library_wide_cache = {}
    _library_source_cache = {}
    _analysis_cache_store = {}


def _get_analysis_cache(
    selected_names: list[str],
    selected_wide: dict[str, pd.DataFrame],
    selected_categories: dict[str, str],
) -> tuple[LibraryCache, bool]:
    """
    复用已构建的 LibraryCache，避免重复 rank 预处理。
    key 使用本次参与计算的库因子名称集合（有序）。
    """
    key = tuple(selected_names)
    cached = _analysis_cache_store.get(key)
    if cached is not None:
        return cached, True

    cache = LibraryCache()
    cache.load_from_dict(selected_wide, selected_categories)
    _analysis_cache_store[key] = cache
    # 控制缓存体积，避免组合过多占用内存
    if len(_analysis_cache_store) > 8:
        oldest_key = next(iter(_analysis_cache_store.keys()))
        if oldest_key != key:
            _analysis_cache_store.pop(oldest_key, None)
    return cache, False


def _warmup_library_factor_cache(
    gateway: FactorDataGateway,
    factor_names: list[str],
    factor_to_table: dict[str, str],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[int, list[dict]]:
    """
    把本次需要的库因子写入内存缓存（已缓存则跳过）。
    始终按表分批 load_multiple_factors，避免缓存部分命中时逐因子 load_single。
    """
    loaded = 0
    errors: list[dict] = []
    pending = [
        name
        for name in factor_names
        if _library_wide_cache_key(name, start_date, end_date) not in _library_wide_cache
    ]
    if not pending:
        return 0, errors

    # 按表分组，批量读取，避免按因子重复扫描 parquet
    table_groups: dict[str | None, list[str]] = {}
    for name in pending:
        table_groups.setdefault(factor_to_table.get(name), []).append(name)

    for table_name, names_in_table in table_groups.items():
        # 分块批量读，避免一次拉过多列导致慢/内存高
        for i in range(0, len(names_in_table), _WARMUP_BATCH_SIZE):
            chunk_names = names_in_table[i:i + _WARMUP_BATCH_SIZE]
            try:
                batch_df, source = gateway.load_multiple_factors(
                    factor_names=chunk_names,
                    table_name=table_name,
                    start_date=start_date,
                    end_date=end_date,
                )
            except Exception as e:
                # 批量失败时降级单因子读取，保证可用性
                errors.append({
                    "factor": f"[table:{table_name or 'default'}]",
                    "error": f"批量加载失败，降级单因子: {e}",
                })
                batch_df, source = None, "fallback_single"

            if batch_df is not None and not batch_df.empty:
                if "trade_dt" not in batch_df.columns or "ticker" not in batch_df.columns:
                    errors.append({
                        "factor": f"[table:{table_name or 'default'}]",
                        "error": "批量数据缺少 trade_dt/ticker 列",
                    })
                    batch_df = None
                else:
                    batch_df["trade_dt"] = pd.to_datetime(batch_df["trade_dt"])
                    batch_df["ticker"] = batch_df["ticker"].astype(str).str.upper()
                    for name in chunk_names:
                        if name not in batch_df.columns:
                            continue
                        tmp = batch_df[["trade_dt", "ticker", name]].dropna(subset=[name])
                        if tmp.empty:
                            continue
                        wide = tmp.pivot(index="trade_dt", columns="ticker", values=name).sort_index()
                        ck = _library_wide_cache_key(name, start_date, end_date)
                        _library_wide_cache[ck] = wide
                        _library_source_cache[ck] = source
                        loaded += 1
                    continue

            # 批量为空/异常时，兜底按单因子读取
            for name in chunk_names:
                ck = _library_wide_cache_key(name, start_date, end_date)
                if ck in _library_wide_cache:
                    continue
                try:
                    long_df, single_source = gateway.load_single_factor(
                        name,
                        start_date=start_date,
                        end_date=end_date,
                        table_name=table_name,
                    )
                    if long_df is None or long_df.empty:
                        continue
                    long_df["trade_dt"] = pd.to_datetime(long_df["trade_dt"])
                    long_df["ticker"] = long_df["ticker"].astype(str).str.upper()
                    wide = long_df.pivot(index="trade_dt", columns="ticker", values="factor_value").sort_index()
                    _library_wide_cache[ck] = wide
                    _library_source_cache[ck] = single_source
                    loaded += 1
                except Exception as e:
                    errors.append({"factor": name, "error": str(e)})

    return loaded, errors


def _coerce_trade_dt_column(s: pd.Series) -> pd.Series:
    """更稳健地解析 trade_dt，避免 20190102 被误解析到 1970 年。"""
    if s is None or len(s) == 0:
        return pd.to_datetime(s, errors="coerce")
    if pd.api.types.is_datetime64_any_dtype(s):
        return s

    num = pd.to_numeric(s, errors="coerce")
    finite = num.dropna()
    if len(finite) > 0 and (finite == finite.round(0)).all():
        vi = finite.astype("int64")
        if vi.min() >= 19000101 and vi.max() <= 21001231:
            ii = num.round(0).astype("Int64")
            return pd.to_datetime(ii, format="%Y%m%d", errors="coerce")

    if getattr(s, "dtype", None) == object:
        ss = s.astype(str).str.strip()
        if ss.notna().all() and ss.str.match(r"^\d{8}$", na=False).all():
            return pd.to_datetime(ss, format="%Y%m%d", errors="coerce")

    return pd.to_datetime(s, errors="coerce")


def _normalize_ticker(x: object) -> str:
    """统一 ticker 到 Wind 风格：000001.SZ / 600000.SH。"""
    s = str(x).strip().upper()
    if not s:
        return s
    s = s.replace(" ", "").replace("_", "").replace("-", "")
    s = s.replace(".XSHE", ".SZ").replace(".XSHG", ".SH")
    s = s.replace("XSHE", ".SZ").replace("XSHG", ".SH")
    if len(s) == 8 and s[:6].isdigit() and s[6:] in ("SZ", "SH"):
        return f"{s[:6]}.{s[6:]}"
    if len(s) == 9 and s[:6].isdigit() and s[6] == "." and s[7:] in ("SZ", "SH"):
        return s
    if len(s) == 6 and s.isdigit():
        if s[0] in ("0", "3"):
            return f"{s}.SZ"
        if s[0] == "6":
            return f"{s}.SH"
        return s
    return s


def set_library_cache(cache: LibraryCache) -> None:
    global _library_cache, _library_catalog
    _library_cache = cache
    categories = {}
    factor_to_table = {}
    try:
        from data.db_connector import get_factor_database
        db = get_factor_database()
        for table_info in db.get_all_factor_tables() or []:
            table_name = table_info.get("name")
            for factor_name in table_info.get("factors", []) or []:
                if factor_name not in factor_to_table:
                    factor_to_table[factor_name] = table_name
    except Exception:
        factor_to_table = {}

    for name in cache.factor_names:
        cat = cache.categories.get(name, "其他")
        categories.setdefault(cat, []).append(name)
        factor_to_table.setdefault(name, None)
    _library_catalog = {
        "loaded": cache.is_loaded,
        "factor_names": list(cache.factor_names),
        "categories": categories,
        "factor_to_table": factor_to_table,
        "load_time_sec": round(cache._load_time, 2),
    }
    _clear_library_factor_cache()
    logger.info("LibraryCache 已注入，因子数=%d", len(cache.factor_names))


def set_price_df(price_df: pd.DataFrame) -> None:
    global _price_df, _price_source
    _price_df = price_df
    _price_source = "injected: adjopen_wide.csv"


def _load_local_price_df() -> pd.DataFrame | None:
    """兜底从本地 adjopen_wide.csv 加载开盘价宽表。"""
    global _price_source
    path = os.path.join(DATA_DIR, "adjopen_wide.csv")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        first_col = df.columns[0]
        if first_col in ("", "Unnamed: 0", "trade_dt", "date"):
            df = df.rename(columns={first_col: "trade_dt"})
        if "trade_dt" not in df.columns:
            return None
        df["trade_dt"] = pd.to_datetime(df["trade_dt"], errors="coerce")
        df = df.dropna(subset=["trade_dt"]).set_index("trade_dt").sort_index()
        df.columns = df.columns.map(lambda x: str(x).upper())
        _price_source = "local fallback: adjopen_wide.csv"
        return df
    except Exception as e:
        logger.exception("本地价格数据加载失败: %s", e)
        return None


def _describe_frame_range(df: pd.DataFrame | None) -> dict:
    if df is None or df.empty:
        return {"start": None, "end": None, "rows": 0, "columns": 0}
    idx = pd.to_datetime(df.index, errors="coerce")
    idx = idx[~pd.isna(idx)]
    if len(idx) == 0:
        start = None
        end = None
    else:
        start = idx.min().strftime("%Y-%m-%d")
        end = idx.max().strftime("%Y-%m-%d")
    return {
        "start": start,
        "end": end,
        "rows": int(len(df.index)),
        "columns": int(len(df.columns)),
    }


def _build_analysis_snapshot(
    *,
    new_factors: dict[str, pd.DataFrame],
    compare_mode: str,
    compare_category: str,
    threshold: float,
    future_days: int,
    selected_names: list[str],
    loaded_names: list[str],
    price_df: pd.DataFrame | None,
) -> dict:
    new_factor_ranges = {
        name: _describe_frame_range(df)
        for name, df in sorted(new_factors.items(), key=lambda x: x[0])
    }
    snapshot_core = {
        "compare_mode": compare_mode,
        "compare_category": compare_category or None,
        "threshold": float(threshold),
        "future_days": int(future_days),
        "new_factor_names": sorted(new_factors.keys()),
        "selected_library_factor_names": sorted(selected_names),
        "loaded_library_factor_names": sorted(loaded_names),
        "price_source": _price_source,
        "price_range": _describe_frame_range(price_df),
        "new_factor_ranges": new_factor_ranges,
    }
    snapshot_text = repr(snapshot_core).encode("utf-8", errors="ignore")
    snapshot_core["signature"] = hashlib.md5(snapshot_text).hexdigest()[:12]
    return snapshot_core


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/correlation/library_status
# 查询库因子缓存状态
# ══════════════════════════════════════════════════════════════════════════════
@correlation_bp.route("/library_status", methods=["GET"])
def library_status():
    if not _library_catalog.get("loaded"):
        return jsonify({"loaded": False, "factor_count": 0})

    return jsonify({
        "loaded": True,
        "factor_count": len(_library_catalog.get("factor_names", [])),
        "factor_names": _library_catalog.get("factor_names", []),
        "categories": _library_catalog.get("categories", {}),
        "load_time_sec": _library_catalog.get("load_time_sec", 0.0),
        "price_source": _price_source,
        "price_range": _describe_frame_range(_price_df),
    })


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/correlation/reload_library
# 重新从数据库加载库因子（更新因子库后调用）
# ══════════════════════════════════════════════════════════════════════════════
@correlation_bp.route("/reload_library", methods=["POST"])
def reload_library():
    global _library_cache, _library_catalog
    body = request.json or {}
    factor_table = body.get("factor_table")
    try:
        from data.db_connector import get_factor_database
        t0 = time.time()
        connector = get_factor_database()
        tables = connector.get_all_factor_tables()
        categories_map = connector.get_factor_categories()

        factor_names = []
        factor_to_table = {}
        categories = {}
        for table_info in tables or []:
            table_name = table_info.get("name")
            for factor_name in sorted(table_info.get("factors", []) or []):
                if factor_name in factor_to_table:
                    continue
                factor_names.append(factor_name)
                factor_to_table[factor_name] = table_name
                cat = categories_map.get(factor_name, "其他")
                categories.setdefault(cat, []).append(factor_name)

        factor_names = sorted(factor_names)
        categories = {
            cat: sorted(items)
            for cat, items in sorted(categories.items(), key=lambda x: x[0])
        }

        _library_cache = None
        _clear_library_factor_cache()
        _library_catalog = {
            "loaded": True,
            "factor_names": factor_names,
            "categories": categories,
            "factor_to_table": factor_to_table,
            "load_time_sec": round(time.time() - t0, 2),
        }
        return jsonify({
            "success": True,
            "factor_count": len(factor_names),
            "load_time_sec": _library_catalog["load_time_sec"],
        })
    except Exception as e:
        logger.exception("重载因子库失败")
        return jsonify({"success": False, "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/correlation/analyze
# 核心接口：上传新因子 CSV，执行两步相关性分析，返回热力图数据
# ══════════════════════════════════════════════════════════════════════════════
@correlation_bp.route("/analyze", methods=["POST"])
def analyze():
    """
    Form-data:
        files[]    : 一个或多个因子 CSV（宽表，首列 trade_dt）
        names[]    : 对应的因子名（可选，默认用文件名）
        threshold  : Step1 快筛阈值，默认 0.5（可选）
        future_days: IC 计算的未来收益天数，默认 1（可选）

    Returns:
        热力图 payload（见 build_heatmap_payload 的文档）
    """
    req_t0 = time.time()
    if not _library_catalog.get("loaded"):
        return jsonify({
            "success": False,
            "message": "因子库尚未加载，请先调用 /api/correlation/reload_library"
        }), 503

    global _price_df
    if _price_df is None:
        _price_df = _load_local_price_df()
    if _price_df is None:
        return jsonify({
            "success": False,
            "message": "开盘价数据未注入，且本地 adjopen_wide.csv 自动加载失败"
        }), 503

    files = request.files.getlist("files[]")
    names = request.form.getlist("names[]")
    threshold = float(request.form.get("threshold", 0.5))
    future_days = int(request.form.get("future_days", 1))
    compare_mode = (request.form.get("compare_mode") or "all").strip().lower()
    compare_category = (request.form.get("compare_category") or "").strip()
    selected_library_factors = [
        str(x).strip() for x in request.form.getlist("selected_library_factors[]")
        if str(x).strip()
    ]

    if not files:
        return jsonify({"success": False, "message": "未收到文件"}), 400

    # ── 解析上传的新因子 ──────────────────────
    new_factors: dict[str, pd.DataFrame] = {}
    parse_errors = []

    t_parse0 = time.time()
    for i, f in enumerate(files):
        factor_name = (
            names[i].strip() if i < len(names) and names[i].strip()
            else f.filename.replace(".csv", "").strip()
        ) or f"new_factor_{i+1}"

        try:
            df = pd.read_csv(io.BytesIO(f.read()))
            if "trade_dt" not in df.columns:
                raise ValueError("缺少 trade_dt 列，请检查文件格式")
            lower_map = {str(c).lower().strip(): c for c in df.columns}
            trade_col = lower_map.get("trade_dt", "trade_dt")
            ticker_col = lower_map.get("ticker")
            value_col = (
                lower_map.get("factor")
                or lower_map.get("factor_value")
                or lower_map.get("value")
            )

            df[trade_col] = _coerce_trade_dt_column(df[trade_col])
            df = df.dropna(subset=[trade_col])

            # 兼容长表：trade_dt/ticker/factor_value -> pivot 成宽表
            if ticker_col and value_col:
                tmp = df[[trade_col, ticker_col, value_col]].copy()
                tmp = tmp.rename(columns={trade_col: "trade_dt", ticker_col: "ticker", value_col: "factor"})
                tmp["ticker"] = tmp["ticker"].map(_normalize_ticker)
                tmp["factor"] = pd.to_numeric(tmp["factor"], errors="coerce")
                tmp = tmp.dropna(subset=["ticker"])
                wide = tmp.pivot(index="trade_dt", columns="ticker", values="factor").sort_index()
                new_factors[factor_name] = wide
            else:
                # 默认宽表：trade_dt + 多个股票列
                df = df.rename(columns={trade_col: "trade_dt"})
                df = df.dropna(subset=["trade_dt"]).set_index("trade_dt").sort_index()
                df.columns = [_normalize_ticker(c) for c in df.columns]
                df = df.apply(pd.to_numeric, errors="coerce")
                new_factors[factor_name] = df
        except Exception as e:
            parse_errors.append({"file": f.filename, "error": str(e)})

    if not new_factors:
        return jsonify({
            "success": False,
            "message": "所有文件解析失败",
            "errors": parse_errors,
        }), 400

    parse_cost = time.time() - t_parse0

    # ── 根据用户选择筛选库因子 ────────────────────
    factor_names = list(_library_catalog.get("factor_names", []))
    categories = _library_catalog.get("categories", {}) or {}
    factor_to_category = {}
    for cat_name, names in categories.items():
        for factor_name in names or []:
            factor_to_category[factor_name] = cat_name
    selected_names = list(factor_names)

    if compare_mode == "category":
        if not compare_category:
            return jsonify({"success": False, "message": "请选择要对比的因子类别"}), 400
        selected_names = [n for n in factor_names if factor_to_category.get(n, "其他") == compare_category]
    elif compare_mode == "custom":
        if not selected_library_factors:
            return jsonify({"success": False, "message": "请至少选择一个库因子"}), 400
        selected_set = set(selected_library_factors)
        selected_names = [n for n in factor_names if n in selected_set]
        if not selected_names:
            return jsonify({"success": False, "message": "所选库因子均不在当前缓存中"}), 400

    selected_names = sorted(selected_names)

    repo = FactorRepository(os.path.join(DATA_DIR, "parquet_loaded"), FACTORS_HIVE_DIR)
    from data.db_connector import get_factor_database
    gateway = FactorDataGateway(repo, get_factor_database)
    factor_to_table = _library_catalog.get("factor_to_table", {})
    selected_wide: dict[str, pd.DataFrame] = {}
    selected_categories = {}
    load_errors = []
    cache_hits = 0
    cache_misses = 0

    lib_start, lib_end = _new_factors_to_library_date_bounds(new_factors, future_days)
    lib_start, lib_end, bounds_err = _clamp_library_load_bounds(lib_start, lib_end)
    if bounds_err:
        return jsonify({"success": False, "message": bounds_err}), 400

    if lib_start and lib_end:
        logger.info(
            "相关性分析：库因子读取裁剪区间 ~ %s .. %s（与因子库日历求交后，用于 parquet 裁剪）",
            lib_start,
            lib_end,
        )

    # 始终对「本次选中且缓存未命中」的库因子批量预热（不再仅在全局空缓存时批量）
    t_load0 = time.time()
    t_warm = time.time()
    warm_loaded, warm_errors = _warmup_library_factor_cache(
        gateway=gateway,
        factor_names=selected_names,
        factor_to_table=factor_to_table,
        start_date=lib_start,
        end_date=lib_end,
    )
    load_errors.extend(warm_errors)
    logger.info(
        "相关性分析库因子加载：新增=%d, 缓存总数=%d, 失败=%d, 耗时=%.2fs",
        warm_loaded,
        len(_library_wide_cache),
        len(warm_errors),
        time.time() - t_warm,
    )

    # 汇总选中宽表；极少数批量失败的可单因子兜底（带同一日期裁剪）
    for name in selected_names:
        ck = _library_wide_cache_key(name, lib_start, lib_end)
        wide = _library_wide_cache.get(ck)
        if wide is None:
            cache_misses += 1
            table_name = factor_to_table.get(name)
            try:
                long_df, source = gateway.load_single_factor(
                    name,
                    start_date=lib_start,
                    end_date=lib_end,
                    table_name=table_name,
                )
                if long_df is None or long_df.empty:
                    continue
                long_df["trade_dt"] = pd.to_datetime(long_df["trade_dt"])
                long_df["ticker"] = long_df["ticker"].astype(str).str.upper()
                wide = long_df.pivot(index="trade_dt", columns="ticker", values="factor_value").sort_index()
                _library_wide_cache[ck] = wide
                _library_source_cache[ck] = source
                logger.info(
                    "相关性分析缓存补加载库因子: %s, table=%s, source=%s, rows=%s",
                    name,
                    table_name,
                    source,
                    len(long_df),
                )
            except Exception as e:
                load_errors.append({"factor": name, "error": str(e)})
                continue
        else:
            cache_hits += 1
        selected_wide[name] = wide
        selected_categories[name] = factor_to_category.get(name, "其他")

    load_cost = time.time() - t_load0

    if not selected_wide:
        failed_names = [x.get("factor") for x in load_errors if x.get("factor")]
        failed_preview = "、".join(failed_names[:10]) if failed_names else "无"
        hint = (
            "常见原因：① 所选因子名与 parquet 列名不一致；② 日期裁剪后该因子在区间内全为缺失；"
            "③ 长表列名异常（库侧需 trade_dt + ticker/s_info_windcode）。"
            "注意：factors_all 中 trade_dt 为普通列即可，分析时会 pivot 为宽表索引，与是否为 index 无关。"
        )
        return jsonify({
            "success": False,
            "message": (
                f"筛选后没有可用于对比的库因子。"
                f"已选择 {len(selected_names)} 个，成功加载 0 个，失败 {len(load_errors)} 个。"
                f"失败示例：{failed_preview}。{hint}"
            ),
            "load_errors": load_errors,
        }), 400

    t_cache = time.time()
    analysis_cache, analysis_cache_hit = _get_analysis_cache(
        selected_names=selected_names,
        selected_wide=selected_wide,
        selected_categories=selected_categories,
    )
    cache_cost = time.time() - t_cache
    logger.info(
        "相关性分析分析缓存: key_size=%d, store_size=%d, hit=%s, 耗时=%.2fs",
        len(selected_names),
        len(_analysis_cache_store),
        analysis_cache_hit,
        cache_cost,
    )
    analysis_snapshot = _build_analysis_snapshot(
        new_factors=new_factors,
        compare_mode=compare_mode,
        compare_category=compare_category,
        threshold=threshold,
        future_days=future_days,
        selected_names=selected_names,
        loaded_names=list(selected_wide.keys()),
        price_df=_price_df,
    )

    # ── 执行两步分析 ──────────────────────────
    t_run0 = time.time()
    try:
        analyzer = FactorVsLibraryAnalyzer(
            new_factors=new_factors,
            library=analysis_cache,
            quick_threshold=threshold,
        )
        results = analyzer.run(
            price_df=_price_df,
            future_return_days=future_days,
        )
        payload = analyzer.build_heatmap_payload(results)
    except Exception as e:
        logger.exception("相关性分析失败")
        return jsonify({"success": False, "message": str(e)}), 500
    run_cost = time.time() - t_run0
    total_cost = time.time() - req_t0
    step2_candidate_count = int(sum(len(r.pair_details) for r in results.values()))
    step2_candidate_count_by_factor = {
        name: int(len(r.pair_details))
        for name, r in results.items()
    }
    logger.info(
        "相关性分析耗时明细: total=%.2fs parse=%.2fs load=%.2fs build_cache=%.2fs run=%.2fs lib_cache_hit=%d lib_cache_miss=%d analysis_cache_hit=%s",
        total_cost,
        parse_cost,
        load_cost,
        cache_cost,
        run_cost,
        cache_hits,
        cache_misses,
        analysis_cache_hit,
    )

    return jsonify({
        "success": True,
        "parse_errors": parse_errors,
        "data": payload,
        "library_selection": {
            "compare_mode": compare_mode,
            "compare_category": compare_category or None,
            "selected_count": len(selected_wide),
            "selected_factor_names": list(selected_wide.keys()),
        },
        "load_errors": load_errors,
        "analysis_snapshot": analysis_snapshot,
        "quick_threshold_used": float(threshold),
        "step2_candidate_count": step2_candidate_count,
        "step2_candidate_count_by_factor": step2_candidate_count_by_factor,
        "perf": {
            "total_sec": round(total_cost, 3),
            "parse_sec": round(parse_cost, 3),
            "load_sec": round(load_cost, 3),
            "build_cache_sec": round(cache_cost, 3),
            "run_sec": round(run_cost, 3),
            "library_cache_hit_count": int(cache_hits),
            "library_cache_miss_count": int(cache_misses),
            "analysis_cache_hit": bool(analysis_cache_hit),
        },
        # 每个新因子的详细结果（含精检明细）
        "factor_details": {n: r.to_dict() for n, r in results.items()},
    })


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/correlation/factor_detail?name=ep_ttm
# 获取单个新因子的精检明细（已分析过才有数据）
# ══════════════════════════════════════════════════════════════════════════════
@correlation_bp.route("/factor_detail", methods=["GET"])
def factor_detail():
    """返回缓存在 _last_results 中的单因子明细（避免重复计算）"""
    # 简单实现：前端直接从 /analyze 的响应里拿 factor_details
    # 此接口备用，后续可接 session 级缓存
    return jsonify({"success": False, "message": "请直接使用 /analyze 返回的 factor_details"}), 400
