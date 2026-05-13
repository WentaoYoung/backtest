from typing import Dict

import numpy as np
import pandas as pd


def _infer_periods_per_year_from_index(index: pd.Index, default: float = 252.0) -> float:
    """
    按收益序列索引的日期间隔推断「每年有多少个收益期」。
    周/月调仓时每期收益是一周或一月，不能再固定 *252，否则年化会被放大数倍（出现 -300% 等离谱值）。
    """
    if index is None or len(index) < 2:
        return default
    try:
        dt = pd.to_datetime(pd.Index(index), errors="coerce")
        if dt.isna().all():
            return default
        dt = pd.Series(dt).dropna().sort_values()
        if len(dt) < 2:
            return default
        diffs_days = dt.diff().dt.days.dropna()
        if len(diffs_days) == 0:
            return default
        median_gap_days = float(diffs_days.median())
        if median_gap_days <= 0 or not np.isfinite(median_gap_days):
            return default
        return float(365.25 / median_gap_days)
    except Exception:
        return default


def calc_group_stats_from_rets(rets_df: pd.DataFrame) -> pd.DataFrame:
    """根据收益率矩阵计算各组统计指标（年化/波动按索引日期推断频率，与 L/S 汇总一致）。"""
    periods_per_year = _infer_periods_per_year_from_index(rets_df.index)
    stats = []
    for col in rets_df.columns:
        returns = rets_df[col]

        mean_ret = returns.mean() * periods_per_year
        cum_return = (1 + returns).prod() - 1
        vol = returns.std() * np.sqrt(periods_per_year)
        sharpe = mean_ret / vol if vol > 0 else 0

        nav = (1 + returns).cumprod()
        cummax = nav.cummax()
        drawdown = (nav - cummax) / cummax
        max_dd = abs(drawdown.min())

        stats.append(
            {
                "分组": col,
                "累计收益_%": round(cum_return * 100, 2),
                "年化收益_%": round(mean_ret * 100, 2),
                "年化波动_%": round(vol * 100, 2),
                "最大回撤_%": round(max_dd * 100, 2),
                "夏普比率": round(sharpe, 2),
            }
        )
    return pd.DataFrame(stats)


def calc_long_short_summary(ls_df: pd.DataFrame, annual_days: int = 252) -> Dict[str, float]:
    """计算 Long-Short 关键统计"""
    ls_ret = ls_df["ls_return"].dropna()
    ls_nav = ls_df["Long-Short"]

    cum_ret = ls_nav.iloc[-1] - 1
    # 年化计算：优先从 trade_dt 推断收益频率，避免把“月度/周度收益”当作“日度收益”
    # ls_df 通常来自 BacktestEngine，包含 trade_dt（已在多处 reset_index 后保留）。
    periods_per_year = annual_days
    n_years = None
    try:
        if "trade_dt" in ls_df.columns:
            dt = pd.to_datetime(ls_df["trade_dt"])
            # 与 ls_ret 对齐（ls_ret 来自 ls_df.dropna 后的原索引）
            dt = dt.loc[ls_ret.index]
            if len(dt) >= 2:
                dt = dt.sort_values()
                total_days = (dt.iloc[-1] - dt.iloc[0]).days
                n_years = total_days / 365.25 if total_days > 0 else 0
                # 使用中位数间隔推断每年收益期数（适用于 daily/weekly/monthly 抽稀）
                diffs_days = dt.diff().dt.days.dropna()
                if len(diffs_days) > 0:
                    median_gap_days = float(diffs_days.median())
                    if median_gap_days > 0:
                        periods_per_year = 365.25 / median_gap_days
    except Exception:
        # 推断失败则回退到旧逻辑
        n_years = None
        periods_per_year = annual_days

    if n_years is None:
        n_years = len(ls_ret) / annual_days if annual_days > 0 else 0

    ann_ret_val = (ls_nav.iloc[-1]) ** (1 / n_years) - 1 if n_years > 0 else 0

    ann_vol = ls_ret.std() * np.sqrt(periods_per_year) if periods_per_year > 0 else 0
    sharpe = ann_ret_val / ann_vol if ann_vol > 0 else 0

    ls_dd = (ls_nav - ls_nav.cummax()) / ls_nav.cummax()
    max_dd = abs(ls_dd.min())

    return {
        "cum_ret_pct": cum_ret * 100,
        "ann_ret_pct": ann_ret_val * 100,
        "ann_vol_pct": ann_vol * 100,
        "max_dd_pct": max_dd * 100,
        "sharpe": sharpe,
    }
