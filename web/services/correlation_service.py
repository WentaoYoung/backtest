import time
from typing import Any, Callable, Dict

import pandas as pd

from backtest.factor_correlation import FactorCorrelationAnalyzer
from data.date_params import coerce_yyyy_mm_dd


def execute_factor_correlation(
    *,
    params: Dict[str, Any],
    get_last_backtest_data: Callable[[], pd.DataFrame],
    get_last_backtest_params: Callable[[], Dict[str, Any]],
    load_custom_factor_csv: Callable[[str], pd.DataFrame],
    load_factor_from_database: Callable[[str, str, str, str], pd.DataFrame],
    get_target_factor_columns: Callable[[str], list],
    load_multiple_factors: Callable[..., Any],
    log: Callable[[str], None],
) -> Dict[str, Any]:
    """执行因子相关性分析主流程，返回分析结果。"""
    overall_start = time.time()
    target_table_name = params.get("correlation_table_name")
    source_type = params.get("source_type", "current")
    n_groups = int(params.get("n_groups", 5))
    method = params.get("method", "spearman")
    threshold = float(params.get("threshold", 0))

    log(f"参数: table={target_table_name}, source={source_type}, threshold={threshold}")

    if source_type == "local":
        raise ValueError("相关性分析的本地CSV上传已禁用，请选择“当前回测因子”或“因子库选择”")

    if not target_table_name:
        raise ValueError("未指定对比因子表")

    step_start = time.time()
    log("步骤1/4: 获取源因子数据...")

    df_source = None
    new_factor_col = "factor"
    backtest_params = None

    if source_type == "current":
        df_source = get_last_backtest_data()
        backtest_params = get_last_backtest_params()
        if df_source is None:
            raise ValueError("当前没有缓存的回测数据，请先运行回测")
        log(f"  使用最近回测缓存数据: {len(df_source)} 条记录, 耗时 {time.time() - step_start:.2f}s")
    elif source_type == "local":
        csv_data = params.get("new_factor_data")
        if not csv_data:
            raise ValueError("未提供CSV数据")
        df_source = load_custom_factor_csv(csv_data)
        log(f"  从本地CSV加载数据: {len(df_source)} 条记录, 耗时 {time.time() - step_start:.2f}s")
    elif source_type == "database":
        factor_name = params.get("factor_name")
        table_name = params.get("table_name")
        start_date = params.get("start_date")
        end_date = params.get("end_date")
        df_source = load_factor_from_database(factor_name, start_date, end_date, table_name)
        new_factor_col = "factor"
        log(f"  从数据库加载源因子: {len(df_source)} 条记录, 耗时 {time.time() - step_start:.2f}s")

    if df_source is None or df_source.empty:
        raise ValueError("加载源因子数据失败")

    step_start = time.time()
    log("步骤2/4: 获取对比因子表数据...")

    _rs = coerce_yyyy_mm_dd(backtest_params.get("start_date")) if backtest_params else None
    _re = coerce_yyyy_mm_dd(backtest_params.get("end_date")) if backtest_params else None
    if backtest_params and _rs and _re:
        min_date = _rs
        max_date = _re
        log(f"  使用回测日期范围: {min_date} ~ {max_date}")
    else:
        min_date = df_source["trade_dt"].min().strftime("%Y-%m-%d")
        max_date = df_source["trade_dt"].max().strftime("%Y-%m-%d")

    target_factors = get_target_factor_columns(target_table_name)
    log(f"  目标因子列: {target_factors}")

    log("  开始加载对比因子数据...")

    def correlation_progress_cb(progress_info: dict):
        pct = progress_info.get("progress", 0.0)
        stage = progress_info.get("stage", "处理中")
        detail = progress_info.get("detail", "")
        log(f"  [进度] {pct:>6.2f}% | {stage} | {detail}")

    df_target, target_source = load_multiple_factors(
        target_factors,
        start_date=min_date,
        end_date=max_date,
        table_name=target_table_name,
        progress_callback=correlation_progress_cb,
    )
    source_label = "DuckDB本地Parquet" if target_source == "duckdb_parquet" else "MySQL数据库"
    log(f"  [因子连接方式] 对比因子实际来源: {source_label}")
    log(f"  对比因子加载完成: {len(df_target)} 条记录, 耗时 {time.time() - step_start:.2f}s")

    if df_target.empty:
        raise ValueError(f"无法从表 {target_table_name} 加载数据")

    # 以实际加载到的列为准，避免“请求列与本地可用列不完全一致”导致后续计算异常
    loaded_target_factors = [
        c for c in df_target.columns
        if c not in ("ticker", "trade_dt")
    ]
    if not loaded_target_factors:
        raise ValueError(f"表 {target_table_name} 未加载到任何可用因子列")
    if len(loaded_target_factors) != len(target_factors):
        missing = [c for c in target_factors if c not in loaded_target_factors]
        log(f"  [提示] 实际加载列数={len(loaded_target_factors)}，缺失列数={len(missing)}")
        if missing:
            log(f"  [提示] 缺失列示例: {missing[:10]}")

    step_start = time.time()
    log("步骤3/4: 合并源因子与对比因子数据...")
    merged_df = pd.merge(
        df_source[["trade_dt", "ticker", "adj_open", new_factor_col]],
        df_target,
        on=["trade_dt", "ticker"],
        how="inner",
    )

    if len(merged_df) == 0:
        raise ValueError("源因子与对比因子表在日期或股票上无交集")

    log(f"  合并后数据量: {len(merged_df)} 条, 耗时 {time.time() - step_start:.2f}s")

    step_start = time.time()
    log("步骤4/4: 执行相关性分析计算...")
    analyzer = FactorCorrelationAnalyzer(
        merged_df,
        new_factor_col=new_factor_col,
        existing_factor_cols=loaded_target_factors,
        price_col="adj_open",
        ticker_col="ticker",
    )
    analysis_result = analyzer.get_simplified_correlation_analysis(
        threshold=threshold,
        method=method,
        n_groups=n_groups,
    )

    elapsed = time.time() - step_start
    log(f"  分析计算完成, 耗时 {elapsed:.2f}s")
    analysis_result["elapsed_time"] = time.time() - overall_start
    log(f"✅ 全部分析完成，总耗时 {analysis_result['elapsed_time']:.2f}s")

    return analysis_result
