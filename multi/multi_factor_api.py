"""
multi_factor_api.py
多因子 Blueprint — 注册到 Flask app.py 即可

在 app.py 中添加两行：
    from web.multi_factor_api import multi_factor_bp
    app.register_blueprint(multi_factor_bp)
"""

import io
import json
import logging
import threading
import queue
import sys
import os
import math
from datetime import date, datetime

import numpy as np
import pandas as pd
from flask import Blueprint, request, jsonify, Response, stream_with_context

from backtest.stream_logger import stream_logger
from backtest.multi_factor_runner import MultiFactorBatchRunner
from multi.factor_correlation import LibraryCache, FactorVsLibraryAnalyzer
from multi.factor_scorer import FactorScorer
from web.config import DATA_DIR, FACTORS_HIVE_DIR

logger = logging.getLogger(__name__)
multi_factor_bp = Blueprint("multi_factor", __name__, url_prefix="/api/multi_factor")

# ─── 会话级状态（单服务单用户场景够用；多用户需改为 session/redis） ────────
_state: dict = {
    "factors": {},           # {name: wide_df}
    "runner": None,          # MultiFactorBatchRunner 实例
    "results": {},           # {name: SingleFactorResult}
    "batch_running": False,
    "progress_queue": queue.Queue(),
    "last_batch_params": {},  # 最近一次 run_batch 的 JSON 参数（用于详情与单因子页对齐）
}

# ─── 共享价格/市值数据（复用 app.py 的缓存，通过注入提供） ─────────────────
_shared_data: dict = {
    "price_df": None,
    "mkt_val_df": None,
}

# ─── 因子库宽表缓存（供 correlation_matrix 与因子库对比复用，进程内） ───────
_LIBRARY_WIDE_CACHE: dict[str, pd.DataFrame] = {}
_LIBRARY_WIDE_CACHE_LOCK = threading.Lock()
_LIBRARY_LOAD_BATCH = 12

# 多因子 vs 库：LibraryCache（含截面 rank）按「库因子名集合」复用，避免每次点矩阵都重做预处理
_MF_LIBRARY_RANK_CACHE: dict[tuple[str, ...], LibraryCache] = {}
_MF_LIBRARY_RANK_CACHE_LOCK = threading.Lock()
_MF_LIBRARY_RANK_CACHE_MAX = 8


def init_shared_data(price_df: pd.DataFrame, mkt_val_df: pd.DataFrame) -> None:
    """由 app.py 在数据加载后调用，注入共享数据"""
    _shared_data["price_df"] = price_df
    _shared_data["mkt_val_df"] = mkt_val_df


def _coerce_trade_dt_column(s: pd.Series) -> pd.Series:
    """
    将 CSV 的 trade_dt 列转为 datetime。

    注意：若日期是无引号整数 20190102，pandas 可能会按「纳秒时间戳」解释成 1970 年附近。
    对落在 [19000101, 21001231] 的整数列，按 %Y%m%d 解析。
    """
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
    """
    统一 ticker 格式，尽量归一到 Wind 风格：000001.SZ / 600000.SH
    兼容：
    - 000001SZ / 600000SH
    - XSHE/XSHG 后缀
    - 带空格/小写/分隔符
    若无法识别，则仅做 upper + strip。
    """
    s = str(x).strip().upper()
    if not s:
        return s
    s = s.replace(" ", "").replace("_", "").replace("-", "")
    # 常见交易所后缀
    s = s.replace(".XSHE", ".SZ").replace(".XSHG", ".SH")
    s = s.replace("XSHE", ".SZ").replace("XSHG", ".SH")

    # 000001SZ / 000001SH -> 000001.SZ / 000001.SH
    if len(s) == 8 and s[:6].isdigit() and s[6:] in ("SZ", "SH"):
        return f"{s[:6]}.{s[6:]}"
    # 000001.SZ / 000001.SH 保持
    if len(s) == 9 and s[:6].isdigit() and s[6] == "." and s[7:] in ("SZ", "SH"):
        return s
    # 仅 6 位数字：按 A 股常用规则推断交易所
    # 0/3 开头通常为深市，6 开头通常为沪市；其他保持原样
    if len(s) == 6 and s.isdigit():
        if s[0] in ("0", "3"):
            return f"{s}.SZ"
        if s[0] == "6":
            return f"{s}.SH"
        return s
    return s


def _to_jsonable(obj):
    """将 DataFrame/Series/numpy 标量等转换为可 jsonify 的纯 Python 结构。"""
    try:
        import numpy as np
    except Exception:
        np = None

    if obj is None:
        return None
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, pd.DataFrame):
        cleaned = obj.replace([float("inf"), float("-inf")], pd.NA)
        cleaned = cleaned.where(pd.notna(cleaned), None)
        return cleaned.to_dict(orient="records")
    if isinstance(obj, pd.Series):
        ser = obj.replace([float("inf"), float("-inf")], pd.NA)
        ser = ser.where(pd.notna(ser), None)
        return ser.tolist()
    if np is not None and isinstance(obj, (np.integer, np.floating)):
        v = obj.item()
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return v
    if np is not None and isinstance(obj, np.ndarray):
        return _to_jsonable(obj.tolist())
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def _param_pct_echo(v: object) -> float:
    """前端传小数费率(如 0.001)时，echo 成与单因子页一致的百分数(0.1)。"""
    try:
        x = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    if 0 <= x <= 0.5:
        return round(x * 100, 8)
    return round(x, 8)


def _build_single_factor_backtest_payload(factor_name: str, result, batch_params: dict | None) -> dict:
    """
    组装与 /api/run_backtest_db 返回 data 相同结构的字典，供单因子结果区 renderResults 使用。
    """
    p = batch_params if isinstance(batch_params, dict) else {}
    ic_analysis = {
        "statistics": result.ic_stats,
        "decay": result.ic_decay,
        "distribution": result.ic_distribution,
        "cumulative": [],
        "autocorrelation": result.ic_autocorrelation,
    }
    cum_rows = []
    for item in (result.ic_cumulative or []):
        if not isinstance(item, dict):
            continue
        row = dict(item)
        td = row.get("trade_dt")
        if hasattr(td, "strftime"):
            row["trade_dt"] = td.strftime("%Y-%m-%d")
        cum_rows.append(row)
    ic_analysis["cumulative"] = cum_rows

    merge = result.merge_info or {}
    factor_summary = result.factor_summary or {}
    if factor_summary:
        factor_summary = {
            k: (float(v) if isinstance(v, (np.floating, np.integer)) else v)
            for k, v in factor_summary.items()
        }

    return {
        "group_stats": _to_jsonable(result.group_stats),
        "group_nav": _to_jsonable(result.group_nav),
        "long_short": _to_jsonable(result.long_short),
        "factor_summary": _to_jsonable(factor_summary),
        "yearly_ic": _to_jsonable(result.yearly_ic),
        "ic_analysis": _to_jsonable(ic_analysis),
        "params": {
            "factor_name": factor_name,
            "table_name": None,
            "start_date": p.get("start_date"),
            "end_date": p.get("end_date"),
            "rebalance_freq": p.get("rebalance_freq", "daily"),
            "transaction_cost": _param_pct_echo(p.get("transaction_cost")),
            "slippage": _param_pct_echo(p.get("slippage")),
            "risk_free_rate": _param_pct_echo(p.get("risk_free_rate", 0)),
            "weight_method": p.get("weight_method", "equal"),
            "n_groups": int(p.get("n_groups", 5)),
            "data_source": "multi_csv",
            "benchmark": p.get("benchmark", "none"),
            "initial_capital": float(p.get("initial_capital", 1_000_000)),
            "allow_short": bool(p.get("allow_short", True)),
        },
        "elapsed_time": round(float(result.elapsed_sec or 0), 2),
        "actual_date_range": {
            "start": merge.get("start") or "",
            "end": merge.get("end") or "",
        },
    }


def _load_wide_csv(csv_name: str) -> pd.DataFrame | None:
    path = DATA_DIR + f"\\{csv_name}"
    if not os.path.exists(path):
        return None

    df = pd.read_csv(path)
    first_col = df.columns[0]
    if first_col in ("", "Unnamed: 0", "trade_dt", "date"):
        df = df.rename(columns={first_col: "trade_dt"})
    if "trade_dt" not in df.columns:
        return None

    df["trade_dt"] = pd.to_datetime(df["trade_dt"])
    df = df.set_index("trade_dt").sort_index()
    df.columns = df.columns.map(_normalize_ticker)
    return df


def _ensure_shared_data() -> bool:
    """共享数据为空时，自动从本地 CSV 懒加载一次。"""
    if _shared_data["price_df"] is not None and _shared_data["mkt_val_df"] is not None:
        return True

    try:
        # 与 app_new.py 单因子回测对齐：使用开盘价口径 adj_open
        price_df = _load_wide_csv("adjopen_wide.csv")
        mkt_val_df = _load_wide_csv("market_value.csv")
        if price_df is None or mkt_val_df is None:
            return False
        init_shared_data(price_df, mkt_val_df)
        logger.info("多因子共享数据已自动加载: price=%s, mkt=%s", price_df.shape, mkt_val_df.shape)
        try:
            sys.__stderr__.write(
                f"[multi_factor] 自动加载共享数据成功: price={price_df.shape}, mkt={mkt_val_df.shape}\n"
            )
            sys.__stderr__.flush()
        except Exception:
            pass
        return True
    except Exception as e:
        logger.exception("自动加载多因子共享数据失败")
        try:
            sys.__stderr__.write(f"[multi_factor] 自动加载共享数据失败: {e}\n")
            sys.__stderr__.flush()
        except Exception:
            pass
        return False


# ═══════════════════════════════════════════
# 1. 上传因子 CSV
# ═══════════════════════════════════════════
@multi_factor_bp.route("/upload", methods=["POST"])
def upload_factors():
    """
    批量上传多个因子 CSV 文件（宽表格式）

    Form-data:
        files[]: 多个 CSV 文件
        names[]: 对应的因子名称（可选，默认用文件名去掉 .csv）

    Returns:
        {
            "success": true,
            "uploaded": [
                {"name": "ep_ttm", "dates": 1500, "tickers": 3800,
                 "start": "2018-01-02", "end": "2024-12-31"}
            ],
            "errors": []
        }
    """
    files = request.files.getlist("files[]")
    names = request.form.getlist("names[]")

    if not files:
        return jsonify({"success": False, "message": "未收到文件"}), 400

    uploaded = []
    errors = []

    for i, f in enumerate(files):
        factor_name = (
            names[i] if i < len(names) and names[i].strip()
            else f.filename.replace(".csv", "").strip()
        )
        if not factor_name:
            factor_name = f"factor_{i+1}"

        try:
            df = pd.read_csv(io.BytesIO(f.read()))
            if "trade_dt" not in df.columns:
                raise ValueError("缺少 trade_dt 列")
            # 统一列名便于识别长表/宽表
            lower_map = {c.lower().strip(): c for c in df.columns}
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
                df_wide = wide
            else:
                # 默认宽表：trade_dt + 多个股票列
                df = df.rename(columns={trade_col: "trade_dt"})
                df_wide = df.set_index("trade_dt").sort_index()
                df_wide.columns = df_wide.columns.map(_normalize_ticker)
                df_wide = df_wide.apply(pd.to_numeric, errors="coerce")

            df = df_wide

            _state["factors"][factor_name] = df
            uploaded.append({
                "name": factor_name,
                "dates": len(df),
                "tickers": len(df.columns),
                "start": str(df.index.min().date()),
                "end": str(df.index.max().date()),
            })
        except Exception as e:
            errors.append({"file": f.filename, "error": str(e)})

    return jsonify({
        "success": len(uploaded) > 0,
        "uploaded": uploaded,
        "errors": errors,
        "total_factors": len(_state["factors"]),
    })


# ═══════════════════════════════════════════
# 2. 查看/删除已上传因子
# ═══════════════════════════════════════════
@multi_factor_bp.route("/factors", methods=["GET"])
def list_factors():
    """列出已上传的因子"""
    factors_info = []
    for name, df in _state["factors"].items():
        factors_info.append({
            "name": name,
            "dates": len(df),
            "tickers": len(df.columns),
            "start": str(df.index.min().date()) if not df.empty else "",
            "end": str(df.index.max().date()) if not df.empty else "",
        })
    return jsonify({"success": True, "factors": factors_info})


@multi_factor_bp.route("/factors/<name>", methods=["DELETE"])
def delete_factor(name: str):
    """删除一个已上传的因子"""
    if name in _state["factors"]:
        del _state["factors"][name]
        return jsonify({"success": True, "message": f"已删除因子 {name}"})
    return jsonify({"success": False, "message": "因子不存在"}), 404


# ═══════════════════════════════════════════
# 3. 批量回测
# ═══════════════════════════════════════════
@multi_factor_bp.route("/run_batch", methods=["POST"])
def run_batch():
    """
    触发批量回测（异步执行，进度通过 /stream_progress 获取）

    Body (JSON): 同 /api/run_backtest 的参数格式
    """
    if _state["batch_running"]:
        return jsonify({"success": False, "message": "正在运行中，请等待"}), 409

    if not _state["factors"]:
        return jsonify({"success": False, "message": "没有已上传的因子"}), 400

    if not _ensure_shared_data():
        return jsonify({"success": False, "message": "价格数据未加载，且自动加载本地 CSV 失败"}), 500

    price_df = _shared_data["price_df"]
    mkt_val_df = _shared_data["mkt_val_df"]
    if price_df is None or mkt_val_df is None:
        return jsonify({"success": False, "message": "价格数据未加载"}), 500

    params = request.json or {}
    _state["last_batch_params"] = dict(params)

    # 丢弃上次运行残留在队列中的事件，避免 SSE 读到旧的「done」
    pq = _state["progress_queue"]
    while True:
        try:
            pq.get_nowait()
        except queue.Empty:
            break

    def _progress_cb(factor_name, done, total):
        _state["progress_queue"].put({
            "type": "progress",
            "factor": factor_name,
            "done": done,
            "total": total,
        })

    def _run():
        stream_logger.register_thread()
        _state["batch_running"] = True
        _state["results"] = {}
        n_factors = len(_state["factors"])
        try:
            try:
                sys.__stderr__.write(
                    f"[multi_factor] 批量回测线程启动，因子数={n_factors}，workers={min(4, n_factors)}\n"
                )
                sys.__stderr__.flush()
            except Exception:
                pass
            logger.info("批量回测开始: %s 个因子", n_factors)
            _state["progress_queue"].put({
                "type": "started",
                "total": n_factors,
                "message": f"已开始批量回测，共 {n_factors} 个因子",
            })

            runner = MultiFactorBatchRunner(
                price_df=price_df,
                mkt_val_df=mkt_val_df,
                max_workers=min(4, len(_state["factors"])),
                progress_callback=_progress_cb,
            )
            for name, df in _state["factors"].items():
                runner.add_factor(name, df)

            results = runner.run_all(params)
            _state["results"] = results
            _state["runner"] = runner
            _state["progress_queue"].put({"type": "done"})
            try:
                sys.__stderr__.write("[multi_factor] 批量回测全部完成。\n")
                sys.__stderr__.flush()
            except Exception:
                pass
            logger.info("批量回测完成")
        except Exception as e:
            logger.exception("批量回测失败")
            _state["progress_queue"].put({"type": "error", "message": str(e)})
            try:
                sys.__stderr__.write(f"[multi_factor] 批量回测失败: {e}\n")
                sys.__stderr__.flush()
            except Exception:
                pass
        finally:
            _state["batch_running"] = False
            stream_logger.unregister_thread()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({
        "success": True,
        "message": f"已启动批量回测，共 {len(_state['factors'])} 个因子",
        "total": len(_state["factors"]),
    })


@multi_factor_bp.route("/stream_progress", methods=["GET"])
def stream_progress():
    """SSE 流式返回批量回测进度"""

    def _generate():
        while True:
            try:
                msg = _state["progress_queue"].get(timeout=30)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield "data: {\"type\": \"heartbeat\"}\n\n"

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ═══════════════════════════════════════════
# 4. 获取批量结果
# ═══════════════════════════════════════════
@multi_factor_bp.route("/results", methods=["GET"])
def get_results():
    """
    获取所有因子的批量回测汇总结果

    Query params:
        factor: 指定因子名，返回该因子详细结果
        format: summary（默认）/ detail
    """
    try:
        if not _state["results"]:
            return jsonify({"success": False, "message": "暂无回测结果"}), 404

        factor_name = request.args.get("factor")
        fmt = request.args.get("format", "summary")

        if factor_name:
            result = _state["results"].get(factor_name)
            if not result:
                return jsonify({"success": False, "message": f"因子 {factor_name} 不存在"}), 404
            if not result.success:
                return jsonify({
                    "success": True,
                    "factor_name": factor_name,
                    "data": {},
                    "error": result.error_msg or "回测失败",
                })
            detail_data = _build_single_factor_backtest_payload(
                factor_name,
                result,
                _state.get("last_batch_params"),
            )
            return jsonify({
                "success": True,
                "factor_name": factor_name,
                "data": _to_jsonable(detail_data),
                "error": None,
            })

        # 汇总表
        if _state["runner"]:
            summary_df = _state["runner"].get_summary_df()
            summary = summary_df.reset_index().to_dict(orient="records")
        else:
            summary = [
                r.to_summary_dict()
                for r in _state["results"].values()
            ]

        return jsonify({"success": True, "summary": _to_jsonable(summary),
                        "total": len(summary)})
    except Exception as e:
        logger.exception("获取多因子结果失败")
        return jsonify({"success": False, "message": f"results接口异常: {e}"}), 500


# ═══════════════════════════════════════════
# 5. 相关性矩阵
# ═══════════════════════════════════════════
@multi_factor_bp.route("/correlation_matrix", methods=["POST"])
def correlation_matrix():
    """
    计算因子相关性矩阵

    Body:
        {
            "include_library": true,        # 是否包含因子库对比
            "library_factor_names": [...],  # 指定对比的库因子（可选）
            "corr_method": "spearman"       # spearman / pearson
        }
    """
    try:
        if not _state["factors"]:
            return jsonify({"success": False, "message": "没有已上传的因子"}), 400

        body = request.get_json(silent=True) or {}
        corr_method = body.get("corr_method", "spearman")
        include_library = body.get("include_library", True)

        corr_method = (
            "spearman"
            if str(corr_method).lower() not in ("pearson", "spearman")
            else str(corr_method).lower()
        )
        threshold = float(body.get("threshold", 0.7))

        def wide_to_series(wide: pd.DataFrame) -> pd.Series:
            """宽表(index=trade_dt, cols=ticker) -> Series(index=(trade_dt,ticker))."""
            if wide is None or wide.empty:
                return pd.Series(dtype=float)
            df = wide.copy()
            df.index = pd.to_datetime(df.index)
            df.columns = df.columns.map(lambda x: str(x).upper())
            # 避免 pandas 不同版本 stack/dropna 兼容问题，改用 melt 实现
            long_df = (
                df.reset_index()
                .rename(columns={"index": "trade_dt"})
                .melt(id_vars=["trade_dt"], var_name="ticker", value_name="value")
            )
            long_df["value"] = pd.to_numeric(long_df["value"], errors="coerce")
            long_df = long_df.dropna(subset=["value"])
            if long_df.empty:
                return pd.Series(dtype=float)
            s = long_df.set_index(["trade_dt", "ticker"])["value"]
            s.index = s.index.set_names(["trade_dt", "ticker"])
            return s

        # ── 1) 新因子之间相关矩阵 ─────────────────────────────────────────
        factor_names = list(_state["factors"].keys())
        series_map = {name: wide_to_series(df) for name, df in _state["factors"].items()}

        aligned = pd.DataFrame(series_map)
        corr_df = aligned.corr(method=corr_method)
        corr_df = corr_df.reindex(index=factor_names, columns=factor_names)

        matrix = corr_df.fillna(0.0).values.tolist()

        high_pairs = []
        for i in range(len(factor_names)):
            for j in range(i + 1, len(factor_names)):
                v = corr_df.iat[i, j]
                if pd.notna(v) and abs(float(v)) >= threshold:
                    high_pairs.append({
                        "factor_a": factor_names[i],
                        "factor_b": factor_names[j],
                        "mean_corr": round(float(v), 4),
                        "warning": "高度相关，可能信息冗余",
                    })

        new_factor_matrix = {
            "factor_names": factor_names,
            "matrix": matrix,
            "high_corr_pairs": high_pairs,
            "corr_method": corr_method,
            "threshold": threshold,
        }

        result = {"success": True, "new_factor_matrix": new_factor_matrix}

        # ── 2) 与因子库对比（可选）：复用单因子同款两步分析器 ───────────────
        if include_library:
            limit_raw = body.get("library_factor_limit")
            try:
                factor_limit = int(limit_raw) if limit_raw is not None else None
            except Exception:
                factor_limit = None
            library_factors = _get_library_factors_as_wide(
                body.get("library_factor_names"),
                factor_limit=factor_limit,
            )
            if library_factors:
                if not _ensure_shared_data():
                    return jsonify({"success": False, "message": "共享价格数据不可用，无法执行与库因子的两步相关性分析"}), 503
                price_df = _shared_data["price_df"]
                if price_df is None:
                    return jsonify({"success": False, "message": "价格数据为空，无法执行与库因子的两步相关性分析"}), 503

                lib_cache = _get_or_build_multi_factor_library_cache(library_factors)
                analyzer = FactorVsLibraryAnalyzer(
                    new_factors=_state["factors"],
                    library=lib_cache,
                    quick_threshold=threshold,
                )
                analysis_results = analyzer.run(price_df=price_df, future_return_days=1)
                payload = analyzer.build_heatmap_payload(analysis_results)

                raw_matrix = payload.get("matrix", [])
                vs_matrix = []
                for row in raw_matrix:
                    vs_matrix.append([
                        0.0 if (v is None or pd.isna(v)) else round(float(v), 4)
                        for v in row
                    ])

                lib_warns = [
                    {
                        "new_factor": w.get("new_factor"),
                        "library_factor": w.get("lib_factor"),
                        "mean_corr": w.get("cross_corr"),
                        "suggestion": w.get("reason", "与库因子高度相关，建议检查是否重复/可替代"),
                    }
                    for w in payload.get("warnings", [])
                ]

                result["vs_library"] = {
                    "new_factors": payload.get("new_factors", factor_names),
                    "library_factors": payload.get("lib_factors", list(library_factors.keys())),
                    "matrix": vs_matrix,
                    "high_corr_warnings": lib_warns,
                    "corr_method": corr_method,
                    "threshold": threshold,
                    "step2_details": payload.get("step2_details", {}),
                }

        return jsonify(result)
    except Exception as e:
        logger.exception("计算相关性矩阵失败")
        return jsonify({"success": False, "message": str(e)}), 500


# ═══════════════════════════════════════════
# 6. 因子评分
# ═══════════════════════════════════════════
@multi_factor_bp.route("/score", methods=["GET"])
def get_scores():
    """
    获取所有已回测因子的综合评分与入库建议

    Query: factor=xxx（可选，指定单因子）
    """
    if not _state["results"]:
        return jsonify({"success": False, "message": "暂无回测结果，请先运行批量回测"}), 404

    # 获取与库因子的最高相关系数（如有相关性矩阵结果）
    max_corr_map: dict[str, float] = {}

    scorer = FactorScorer()
    scores = []

    target = request.args.get("factor")
    results_to_score = (
        {target: _state["results"][target]}
        if target and target in _state["results"]
        else _state["results"]
    )

    for name, result in results_to_score.items():
        if not result.success:
            continue
        score = scorer.score(
            factor_name=name,
            ic_stats=result.ic_stats,
            group_stats=result.group_stats,
            factor_summary=result.ls_summary or {},
            max_lib_corr=max_corr_map.get(name, 0.0),
        )
        scores.append(score.to_dict())

    # 按总分降序
    scores.sort(key=lambda x: x["total_score"], reverse=True)
    return jsonify({"success": True, "scores": scores})


# ═══════════════════════════════════════════
# 7. 入库
# ═══════════════════════════════════════════
@multi_factor_bp.route("/save_to_library", methods=["POST"])
def save_to_library():
    """
    将选中因子写入 MySQL 因子库

    Body:
        {
            "factor_names": ["ep_ttm", "momentum_20d"],
            "table_name": "factor_library",     # 目标表（可选）
        }
    """
    body = request.json or {}
    factor_names = body.get("factor_names", [])
    table_name = body.get("table_name", "factor_library")

    if not factor_names:
        return jsonify({"success": False, "message": "请指定要入库的因子"}), 400

    saved = []
    errors = []

    for name in factor_names:
        if name not in _state["factors"]:
            errors.append({"name": name, "error": "因子数据不存在，请先上传"})
            continue
        try:
            df = _state["factors"][name]
            _save_factor_to_db(name, df, table_name)
            saved.append(name)
        except Exception as e:
            logger.exception("因子 %s 入库失败", name)
            errors.append({"name": name, "error": str(e)})

    return jsonify({
        "success": len(saved) > 0,
        "saved": saved,
        "errors": errors,
        "message": f"成功入库 {len(saved)} 个因子" + (
            f"，{len(errors)} 个失败" if errors else ""
        ),
    })


# ═══════════════════════════════════════════
# 8. 导出对比报告
# ═══════════════════════════════════════════
@multi_factor_bp.route("/export", methods=["GET"])
def export_results():
    """导出多因子对比报告 CSV"""
    if not _state["results"] or not _state["runner"]:
        return jsonify({"success": False, "message": "暂无回测结果"}), 404

    df = _state["runner"].get_summary_df()
    csv_str = df.to_csv(encoding="utf-8-sig")

    return Response(
        csv_str,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=multi_factor_report.csv"},
    )


# ═══════════════════════════════════════════
# 内部辅助函数
# ═══════════════════════════════════════════

def _get_or_build_multi_factor_library_cache(
    library_factors: dict[str, pd.DataFrame],
) -> LibraryCache:
    """
    同一套库因子集合多次计算相关性矩阵时，复用 LibraryCache（rank 等预处理）。
    注意：底层 Parquet 更新后需重启进程或后续再做缓存失效，否则会沿用旧 rank。
    """
    key = tuple(sorted(library_factors.keys()))
    if not key:
        empty = LibraryCache()
        empty.load_from_dict(library_factors)
        return empty

    with _MF_LIBRARY_RANK_CACHE_LOCK:
        hit = _MF_LIBRARY_RANK_CACHE.get(key)
        if hit is not None:
            logger.info(
                "多因子相关性矩阵：复用 LibraryCache（rank 缓存）库因子数=%d",
                len(key),
            )
            return hit

    built = LibraryCache()
    built.load_from_dict(library_factors)

    with _MF_LIBRARY_RANK_CACHE_LOCK:
        _MF_LIBRARY_RANK_CACHE[key] = built
        if len(_MF_LIBRARY_RANK_CACHE) > _MF_LIBRARY_RANK_CACHE_MAX:
            oldest = next(iter(_MF_LIBRARY_RANK_CACHE.keys()))
            if oldest != key:
                _MF_LIBRARY_RANK_CACHE.pop(oldest, None)

    return built


def _load_library_wides_for_specs(
    gateway,
    pending: list[tuple[str, str]],
) -> None:
    """将缺失的库因子宽表写入 _LIBRARY_WIDE_CACHE；按表分批 load_multiple_factors。"""
    if not pending:
        return
    by_table: dict[str | None, list[str]] = {}
    for name, tbl in pending:
        by_table.setdefault(tbl, []).append(name)

    new_wides: dict[str, pd.DataFrame] = {}
    for table_name, names in by_table.items():
        for i in range(0, len(names), _LIBRARY_LOAD_BATCH):
            chunk = names[i : i + _LIBRARY_LOAD_BATCH]
            batch_df = None
            try:
                batch_df, source = gateway.load_multiple_factors(
                    factor_names=chunk,
                    table_name=table_name,
                )
            except Exception as e:
                logger.warning(
                    "批量加载库因子失败 table=%s chunk=%s: %s",
                    table_name,
                    chunk[:3],
                    e,
                )
            if batch_df is not None and not batch_df.empty:
                if "trade_dt" not in batch_df.columns or "ticker" not in batch_df.columns:
                    batch_df = None
                else:
                    batch_df = batch_df.copy()
                    batch_df["trade_dt"] = pd.to_datetime(batch_df["trade_dt"])
                    batch_df["ticker"] = batch_df["ticker"].astype(str).map(_normalize_ticker)
                    for name in chunk:
                        if name not in batch_df.columns:
                            continue
                        tmp = batch_df[["trade_dt", "ticker", name]].dropna(subset=[name])
                        if tmp.empty:
                            continue
                        wide = tmp.pivot(
                            index="trade_dt", columns="ticker", values=name
                        ).sort_index()
                        new_wides[name] = wide
                        logger.info(
                            "库因子读取成功(批量): %s, table=%s, source=%s",
                            name,
                            table_name,
                            source,
                        )
                    continue

            for name in chunk:
                if name in new_wides:
                    continue
                try:
                    long_df, source = gateway.load_single_factor(
                        name, table_name=table_name
                    )
                    if long_df is None or long_df.empty:
                        continue
                    long_df = long_df.copy()
                    long_df["trade_dt"] = pd.to_datetime(long_df["trade_dt"])
                    long_df["ticker"] = long_df["ticker"].astype(str).map(_normalize_ticker)
                    wide = long_df.pivot(
                        index="trade_dt", columns="ticker", values="factor_value"
                    ).sort_index()
                    new_wides[name] = wide
                    logger.info(
                        "库因子读取成功(单因子): %s, table=%s, source=%s, rows=%s",
                        name,
                        table_name,
                        source,
                        len(long_df),
                    )
                except Exception:
                    continue

    if new_wides:
        with _LIBRARY_WIDE_CACHE_LOCK:
            _LIBRARY_WIDE_CACHE.update(new_wides)


def _get_library_factors_as_wide(
    factor_names=None,
    factor_limit: int | None = None,
) -> dict[str, pd.DataFrame]:
    """
    从因子库加载指定因子为宽表（进程内缓存 + 按表批量读取）。
    若 db 不可用则返回空字典（降级处理）。
    """
    try:
        from data.db_connector import get_factor_database
        from web.services.factor_repository import FactorRepository
        from web.services.factor_data_gateway import FactorDataGateway

        db = get_factor_database()
        repo = FactorRepository(os.path.join(DATA_DIR, "parquet_loaded"), FACTORS_HIVE_DIR)
        gateway = FactorDataGateway(repo, get_factor_database)

        # 获取所有可用因子及其所属表（优先走本地 parquet 元数据）
        factor_to_table: dict[str, str] = {}
        try:
            all_tables = db.get_all_factor_tables()
            for table_info in all_tables or []:
                table_name = table_info.get("name")
                for fname in table_info.get("factors", []) or []:
                    if fname and fname not in factor_to_table:
                        factor_to_table[str(fname)] = str(table_name)
        except Exception:
            factor_to_table = {}

        if factor_names:
            target_specs = []
            for name in factor_names:
                table_name = factor_to_table.get(name)
                if table_name:
                    target_specs.append((name, table_name))
        else:
            target_specs = list(factor_to_table.items())

        if factor_limit is not None and factor_limit > 0:
            target_specs = target_specs[:factor_limit]
        if not target_specs:
            return {}

        with _LIBRARY_WIDE_CACHE_LOCK:
            missing = [
                (n, t) for n, t in target_specs if n not in _LIBRARY_WIDE_CACHE
            ]

        n_hit = len(target_specs) - len(missing)
        if n_hit:
            logger.info(
                "多因子相关性：库因子宽表缓存命中 %d/%d",
                n_hit,
                len(target_specs),
            )

        if missing:
            _load_library_wides_for_specs(gateway, missing)

        with _LIBRARY_WIDE_CACHE_LOCK:
            return {
                n: _LIBRARY_WIDE_CACHE[n]
                for n, _ in target_specs
                if n in _LIBRARY_WIDE_CACHE
            }
    except Exception:
        return {}


def _save_factor_to_db(name: str, wide_df: pd.DataFrame, table_name: str) -> None:
    """
    将宽表因子转为长表写入 MySQL

    长表格式：trade_dt, ticker, factor_name, factor_value
    """
    # 宽表 → 长表
    long_df = wide_df.reset_index().melt(
        id_vars="trade_dt", var_name="ticker", value_name="factor_value"
    )
    long_df["factor_name"] = name
    long_df = long_df.dropna(subset=["factor_value"])
    long_df = long_df[["trade_dt", "ticker", "factor_name", "factor_value"]]

    from data.db_connector import get_db_connector
    connector = get_db_connector()
    if connector is None:
        raise RuntimeError("因子库数据库连接不可用")

    connector.save_factor(long_df, table_name=table_name)
