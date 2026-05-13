import os
import threading
import time
from typing import Any, Callable, Dict

import numpy as np
import pandas as pd

from backtest.backtest_engine import BacktestEngine
from backtest.factor_analyzer import FactorAnalyzer


def execute_database_backtest(
    *,
    params: Dict[str, Any],
    request_id: str,
    resolve_factor_parquet_path: Callable[[str], str],
    load_factor_from_database: Callable[[str, str, str, str], pd.DataFrame],
    load_benchmark_data: Callable[[str, str, str], pd.Series],
    set_last_backtest: Callable[[pd.DataFrame, Dict[str, Any]], None],
    set_progress: Callable[..., None],
) -> Dict[str, Any]:
    """执行数据库因子回测主流程，返回响应数据。"""
    start_time = time.time()

    factor_name = params.get("factor_name", "LNCAP")
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    table_name = params.get("table_name")
    local_parquet = resolve_factor_parquet_path(table_name)

    print(f"\n{'=' * 60}")
    print(f"使用数据库因子回测: {factor_name}")
    print(f"日期范围: {start_date} ~ {end_date}")
    if local_parquet:
        print(f"[因子连接方式] 优先 DuckDB 本地Parquet: {os.path.basename(local_parquet)}")
    else:
        print("[因子连接方式] 本地Parquet未命中，使用 MySQL 数据库")
    print(f"{'=' * 60}")

    set_progress(request_id, 5, "加载因子数据", detail="正在连接数据库...")
    load_done = threading.Event()
    loading_start_time = time.time()

    def _loading_progress_tick():
        while not load_done.wait(timeout=0.5):
            elapsed = time.time() - loading_start_time
            progress = min(59, 5 + int(elapsed / 2))
            set_progress(
                request_id,
                progress,
                "加载因子数据",
                detail=f"正在查询数据库... ({elapsed:.0f}s)",
                data_current=int(elapsed),
                data_total=100,
            )

    ticker = threading.Thread(target=_loading_progress_tick, daemon=True)
    ticker.start()
    try:
        data = load_factor_from_database(factor_name, start_date, end_date, table_name)
    finally:
        load_done.set()
    ticker.join(timeout=2.0)

    if data is None or len(data) == 0:
        raise ValueError("数据库返回数据为空")

    set_progress(request_id, 60, "数据预处理", detail="正在处理调仓频率...")

    rebalance_freq = params.get("rebalance_freq", "daily")
    transaction_cost = float(params.get("transaction_cost", 0)) / 100
    slippage = float(params.get("slippage", 0)) / 100
    risk_free_rate = float(params.get("risk_free_rate", 0)) / 100
    weight_method = params.get("weight_method", "equal")
    n_groups = int(params.get("n_groups", 5))
    benchmark = params.get("benchmark", "none")

    if rebalance_freq == "weekly":
        data = data[data["trade_dt"].dt.dayofweek == 0]
    elif rebalance_freq == "monthly":
        data = data.groupby([data["trade_dt"].dt.to_period("M"), "ticker"]).first().reset_index(drop=True)

    if len(data) == 0:
        raise ValueError("筛选后数据为空")

    set_last_backtest(
        data.copy(),
        {
            "factor_name": factor_name,
            "table_name": table_name,
            "start_date": start_date,
            "end_date": end_date,
            "data_source": "database",
        },
    )

    set_progress(request_id, 60, "初始化回测引擎", detail="正在构建回测对象...")
    engine = BacktestEngine(
        data,
        factor_col="factor",
        price_col="adj_open",
        date_col="trade_dt",
        ticker_col="ticker",
        mkt_val_col="market_value",
    )

    set_progress(request_id, 70, "分组回测计算", detail="正在执行分组回测...")
    results = engine.run_group_backtest(n_groups=n_groups, weight_method=weight_method)

    stats = results["group_stats"].copy()
    nav_data = results["group_nav"].copy()
    ls_data = results["long_short"].copy()
    if "index" in nav_data.columns:
        nav_data = nav_data.rename(columns={"index": "trade_dt"})
    if "index" in ls_data.columns:
        ls_data = ls_data.rename(columns={"index": "trade_dt"})

    set_progress(request_id, 70, "因子分析", detail="正在计算因子摘要")
    analyzer = FactorAnalyzer(
        data,
        factor_col="factor",
        price_col="adj_open",
        date_col="trade_dt",
        ticker_col="ticker",
    )
    factor_summary = analyzer.get_factor_summary()
    yearly_ic = analyzer.yearly_analysis()
    ic_analysis = analyzer.get_full_ic_analysis(method="pearson")

    if "cumulative" in ic_analysis:
        for item in ic_analysis["cumulative"]:
            if isinstance(item.get("trade_dt"), pd.Timestamp):
                item["trade_dt"] = item["trade_dt"].strftime("%Y-%m-%d")

    elapsed_time = time.time() - start_time

    response_data = {
        "group_stats": stats.to_dict("records"),
        "group_nav": nav_data.to_dict("records"),
        "long_short": ls_data.to_dict("records"),
        "factor_summary": {
            k: (float(v) if isinstance(v, (np.floating, np.integer)) else v)
            for k, v in factor_summary.items()
        },
        "yearly_ic": yearly_ic.to_dict("records"),
        "ic_analysis": ic_analysis,
        "params": {
            "factor_name": factor_name,
            "table_name": table_name,
            "start_date": start_date,
            "end_date": end_date,
            "rebalance_freq": rebalance_freq,
            "transaction_cost": transaction_cost * 100,
            "slippage": slippage * 100,
            "risk_free_rate": risk_free_rate * 100,
            "weight_method": weight_method,
            "n_groups": n_groups,
            "data_source": "database",
            "benchmark": benchmark,
        },
        "elapsed_time": round(elapsed_time, 2),
        "actual_date_range": {
            "start": str(data["trade_dt"].min().date()),
            "end": str(data["trade_dt"].max().date()),
        },
    }

    if benchmark != "none":
        set_progress(request_id, 90, "基准对齐", detail=f"正在加载基准 {benchmark}")
        try:
            benchmark_nav = load_benchmark_data(benchmark, start_date, end_date)
            if not benchmark_nav.empty:
                nav_dates = pd.to_datetime(nav_data["trade_dt"])
                benchmark_aligned = benchmark_nav.reindex(nav_dates, method="ffill").fillna(1.0)
                excess_nav_data = nav_data.copy()
                for col in excess_nav_data.columns:
                    if col != "trade_dt":
                        excess_nav_data[col] = excess_nav_data[col] / benchmark_aligned.values
                benchmark_nav_formatted = pd.DataFrame(
                    {
                        "trade_dt": nav_dates.dt.strftime("%Y-%m-%d"),
                        "benchmark_nav": benchmark_aligned.values,
                    }
                )
                response_data["benchmark_nav"] = benchmark_nav_formatted.to_dict("records")
                response_data["excess_nav"] = excess_nav_data.to_dict("records")
        except Exception as e:
            print(f"[WARN]️ 加载基准指数数据失败: {e}")

    set_progress(request_id, 100, "完成", status="completed", detail=f"回测完成，总耗时 {elapsed_time:.2f} 秒")
    time.sleep(0.2)
    print(f"[OK] 回测完成，耗时: {elapsed_time:.2f} 秒")

    return response_data


def execute_csv_backtest(
    *,
    params: Dict[str, Any],
    request_id: str,
    load_custom_factor_csv: Callable[[str, str, str], pd.DataFrame],
    load_benchmark_data: Callable[[str, str, str], pd.Series],
    set_last_backtest: Callable[[pd.DataFrame, Dict[str, Any]], None],
    set_progress: Callable[..., None],
    simulate_progress: Callable[[str, int, int, str, str, int], None],
) -> Dict[str, Any]:
    """执行 CSV 因子回测主流程，返回响应数据。"""
    start_time = time.time()

    csv_data = params.get("csv_data")
    if not csv_data:
        raise ValueError("未提供 CSV 数据")

    start_date = params.get("start_date")
    end_date = params.get("end_date")

    set_progress(request_id, 5, "解析 CSV", detail="正在解析 CSV 并合并价格数据")
    data = None
    simulate_progress(request_id, 5, 25, "解析 CSV", "正在解析 CSV 并合并价格数据", expected_seconds=20)
    try:
        data = load_custom_factor_csv(csv_data, start_date, end_date)
    finally:
        set_progress(request_id, 25, "数据预处理", detail=f"已加载 {len(data) if data is not None else 0} 条数据")

    if data is None or len(data) == 0:
        raise ValueError("CSV 解析失败或与价格数据无交集")

    rebalance_freq = params.get("rebalance_freq", "daily")
    transaction_cost = float(params.get("transaction_cost", 0)) / 100
    slippage = float(params.get("slippage", 0)) / 100
    risk_free_rate = float(params.get("risk_free_rate", 0)) / 100
    weight_method = params.get("weight_method", "equal")
    n_groups = int(params.get("n_groups", 5))
    benchmark = params.get("benchmark", "none")

    set_progress(request_id, 30, "数据预处理", detail="正在处理调仓频率")
    if rebalance_freq == "weekly":
        data = data[data["trade_dt"].dt.dayofweek == 0]
    elif rebalance_freq == "monthly":
        data = data.groupby([data["trade_dt"].dt.to_period("M"), "ticker"]).first().reset_index(drop=True)

    if len(data) == 0:
        raise ValueError("筛选后数据为空")

    set_progress(request_id, 35, "初始化回测引擎", detail="正在构建回测对象")
    engine = BacktestEngine(
        data,
        factor_col="factor",
        price_col="adj_open",
        date_col="trade_dt",
        ticker_col="ticker",
        mkt_val_col="market_value",
    )

    set_progress(request_id, 40, "回测计算", detail="正在执行分组回测")
    simulate_progress(request_id, 40, 60, "回测计算", "正在执行分组回测", expected_seconds=15)
    try:
        results = engine.run_group_backtest(n_groups=n_groups, weight_method=weight_method)
    finally:
        set_progress(request_id, 60, "回测计算", detail="回测计算完成")

    set_last_backtest(
        data.copy(),
        {
            "factor_name": "CSV上传",
            "table_name": None,
            "start_date": start_date,
            "end_date": end_date,
            "data_source": "csv",
        },
    )

    stats = results["group_stats"].copy()
    nav_data = results["group_nav"].copy()
    ls_data = results["long_short"].copy()
    if "index" in nav_data.columns:
        nav_data = nav_data.rename(columns={"index": "trade_dt"})
    if "index" in ls_data.columns:
        ls_data = ls_data.rename(columns={"index": "trade_dt"})

    total_cost_rate = transaction_cost + slippage
    if total_cost_rate > 0:
        cost_factor = (1 - total_cost_rate) ** (len(nav_data) / 252)
        for col in nav_data.columns:
            if pd.api.types.is_numeric_dtype(nav_data[col]):
                nav_data[col] = nav_data[col].values * cost_factor

    nav_data["trade_dt"] = pd.to_datetime(nav_data["trade_dt"]).dt.strftime("%Y-%m-%d")
    ls_data["trade_dt"] = pd.to_datetime(ls_data["trade_dt"]).dt.strftime("%Y-%m-%d")

    set_progress(request_id, 65, "因子分析", detail="正在初始化因子分析器")
    analyzer = FactorAnalyzer(
        data,
        factor_col="factor",
        price_col="adj_open",
        date_col="trade_dt",
        ticker_col="ticker",
    )

    set_progress(request_id, 70, "因子分析", detail="正在计算因子摘要")
    factor_summary = analyzer.get_factor_summary()
    set_progress(request_id, 75, "因子分析", detail="正在计算年度IC")
    yearly_ic = analyzer.yearly_analysis()

    set_progress(request_id, 80, "因子分析", detail="正在计算完整IC分析")
    simulate_progress(request_id, 80, 90, "因子分析", "正在计算完整IC分析", expected_seconds=10)
    try:
        ic_analysis = analyzer.get_full_ic_analysis(method="pearson")
    finally:
        set_progress(request_id, 90, "因子分析", detail="因子分析完成")

    if "cumulative" in ic_analysis:
        for item in ic_analysis["cumulative"]:
            if isinstance(item.get("trade_dt"), pd.Timestamp):
                item["trade_dt"] = item["trade_dt"].strftime("%Y-%m-%d")

    elapsed_time = time.time() - start_time
    response_data = {
        "group_stats": stats.to_dict("records"),
        "group_nav": nav_data.to_dict("records"),
        "long_short": ls_data.to_dict("records"),
        "factor_summary": {
            k: (float(v) if isinstance(v, (np.floating, np.integer)) else v) for k, v in factor_summary.items()
        },
        "yearly_ic": yearly_ic.to_dict("records"),
        "ic_analysis": ic_analysis,
        "params": {
            "factor_name": "CSV上传",
            "table_name": None,
            "start_date": start_date,
            "end_date": end_date,
            "rebalance_freq": rebalance_freq,
            "transaction_cost": transaction_cost * 100,
            "slippage": slippage * 100,
            "risk_free_rate": risk_free_rate * 100,
            "weight_method": weight_method,
            "n_groups": n_groups,
            "data_source": "csv",
            "benchmark": benchmark,
        },
        "elapsed_time": round(elapsed_time, 2),
        "actual_date_range": {
            "start": str(data["trade_dt"].min().date()),
            "end": str(data["trade_dt"].max().date()),
        },
    }

    if benchmark != "none":
        try:
            set_progress(request_id, 92, "基准对齐", detail="正在加载基准指数")
            benchmark_nav = load_benchmark_data(benchmark, start_date, end_date)
            if not benchmark_nav.empty:
                nav_dates = pd.to_datetime(nav_data["trade_dt"])
                benchmark_aligned = benchmark_nav.reindex(nav_dates, method="ffill").fillna(1.0)
                excess_nav_data = nav_data.copy()
                for col in excess_nav_data.columns:
                    if col != "trade_dt":
                        excess_nav_data[col] = excess_nav_data[col] / benchmark_aligned.values
                response_data["benchmark_nav"] = pd.DataFrame(
                    {
                        "trade_dt": nav_dates.dt.strftime("%Y-%m-%d"),
                        "benchmark_nav": benchmark_aligned.values,
                    }
                ).to_dict("records")
                response_data["excess_nav"] = excess_nav_data.to_dict("records")
            set_progress(request_id, 95, "基准对齐", detail="基准数据加载完成")
        except Exception as e:
            print(f"[WARN]️ 基准加载失败: {e}")

    set_progress(request_id, 98, "生成结果", detail="正在生成回测报告")
    set_progress(request_id, 100, "完成", status="completed", detail=f"回测完成，耗时 {elapsed_time:.2f} 秒")
    return response_data
