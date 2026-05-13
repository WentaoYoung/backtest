"""
因子分组回测引擎 (截面分组版)
Factor Grouping Backtest Engine - Cross-Sectional Version

核心思路：
1. 每日截面分组 (Cross-Sectional Grouping)：在每一天内部按因子值排名
2. 多种加权方式：支持等权、市值加权、因子得分加权
3. 收益计算：严格遵循 T日信号 -> T+1日收益 的逻辑

用法示例:
    from backtest.factor_grouping_engine import FactorGroupingEngine

    engine = FactorGroupingEngine(data, factor_col="factor", ...)
    results = engine.run(n_groups=5, weight_method="equal")
"""

import pandas as pd
import numpy as np
from typing import Dict, Literal, Optional
from dataclasses import dataclass


@dataclass
class BacktestResult:
    """回测结果容器"""
    group_stats: pd.DataFrame
    group_nav: pd.DataFrame
    long_short: pd.DataFrame
    daily_group_rets: pd.DataFrame
    n_groups: int


class FactorGroupingEngine:
    """
    因子分组回测引擎 (截面分组版)

    核心逻辑：
    - 截面分组：每天收盘后按当日因子值在截面内分组（G1=最高组, G5=最低组）
    - 信号滞后：T日收盘得到分组 → T+1日开盘买入 → 持有至T+1收盘
    - 收益归属：T+1日收益率归属于T日的分组信号
    """

    def __init__(
        self,
        data: pd.DataFrame,
        factor_col: str = "factor",
        price_col: str = "adj_close",
        date_col: str = "trade_dt",
        ticker_col: str = "ticker",
        mkt_val_col: str = "market_value",
    ):
        """
        初始化回测引擎

        参数:
            data: 长表数据，需包含日期、标的、因子值、价格
            factor_col: 因子列名
            price_col: 复权收盘价列名
            date_col: 日期列名
            ticker_col: 标的代码列名
            mkt_val_col: 市值列名（市值加权时必需）
        """
        self.data = data.copy()
        self.factor_col = factor_col
        self.price_col = price_col
        self.date_col = date_col
        self.ticker_col = ticker_col
        self.mkt_val_col = mkt_val_col

        self._prepare_data()
        self._result: Optional[BacktestResult] = None

    def _prepare_data(self) -> None:
        """数据预处理：日期、排序、计算收益率"""
        if not pd.api.types.is_datetime64_any_dtype(self.data[self.date_col]):
            self.data[self.date_col] = pd.to_datetime(self.data[self.date_col])

        self.data = self.data.sort_values([self.ticker_col, self.date_col]).reset_index(drop=True)

        # 计算个股日收益率 (T日收盘 -> T+1日收盘 的涨跌幅)
        # 即 return[t] = (price[t] - price[t-1]) / price[t-1]
        self.data["_ret"] = self.data.groupby(self.ticker_col)[self.price_col].pct_change()

    def _cross_sectional_group(self, n_groups: int) -> pd.Series:
        """
        每日截面分组：在每一天内部按因子值分位数分组

        返回: Series, 每行的组标签 (G1, G2, ..., Gn)
        """
        def _qcut_per_day(x: pd.Series) -> pd.Series:
            if len(x) < n_groups:
                return pd.Series(np.nan, index=x.index)
            try:
                quantiles = np.linspace(0, 1, n_groups + 1)
                bins = np.nanpercentile(x.values, quantiles * 100)
                bins = np.unique(bins)
                if len(bins) < 2:
                    return pd.Series(np.nan, index=x.index)
                bin_idx = np.digitize(x.values, bins[1:-1], right=True)
                bin_idx = np.clip(bin_idx, 0, n_groups - 1)
                labels = np.array([f"G{i+1}" for i in range(n_groups)])[::-1]
                return pd.Series(labels[bin_idx], index=x.index)
            except Exception:
                return pd.Series(np.nan, index=x.index)

        return self.data.groupby(self.date_col)[self.factor_col].transform(_qcut_per_day)

    def _compute_raw_weight(self, weight_method: Literal["equal", "mkt_val", "factor_score"]) -> pd.Series:
        """计算原始权重（未归一化）"""
        if weight_method == "equal":
            return pd.Series(1.0, index=self.data.index)
        elif weight_method == "mkt_val":
            if self.mkt_val_col not in self.data.columns:
                raise ValueError(f"缺少市值列 '{self.mkt_val_col}'，无法进行市值加权")
            return self.data[self.mkt_val_col]
        elif weight_method == "factor_score":
            return self.data[self.factor_col].abs()
        else:
            raise ValueError(
                f"不支持的加权方式: {weight_method}。"
                "支持: 'equal'(等权), 'mkt_val'(市值加权), 'factor_score'(因子得分加权)"
            )

    def run(
        self,
        n_groups: int = 5,
        weight_method: Literal["equal", "mkt_val", "factor_score"] = "equal",
    ) -> Dict:
        """
        运行因子分组回测

        参数:
            n_groups: 分组数量，默认5组 (G1=最高, Gn=最低)
            weight_method: 加权方式
                - 'equal': 等权
                - 'mkt_val': 市值加权
                - 'factor_score': 因子得分加权 (|因子值|)

        返回:
            dict: {
                'group_stats': 各组统计指标,
                'group_nav': 各组净值曲线,
                'long_short': 多空组合净值,
                'daily_group_rets': 各组日收益,
                'n_groups': 分组数
            }
        """
        # 1. 截面分组
        self.data["_group"] = self._cross_sectional_group(n_groups)

        # 2. 原始权重
        self.data["_raw_weight"] = self._compute_raw_weight(weight_method)

        # 3. 信号滞后 1 期：T日分组/权重 -> 对齐到 T+1 日
        #    即：用 T 日收盘得到的分组，在 T+1 日获得收益
        self.data["_prev_group"] = self.data.groupby(self.ticker_col)["_group"].shift(1)
        self.data["_prev_raw_weight"] = self.data.groupby(self.ticker_col)["_raw_weight"].shift(1)

        # 4. 过滤有效样本
        valid = self.data.dropna(subset=["_prev_group", "_prev_raw_weight", "_ret"]).copy()

        # 5. 组内归一化权重
        sum_weight = valid.groupby([self.date_col, "_prev_group"], observed=True)["_prev_raw_weight"].transform("sum")
        valid["_norm_weight"] = valid["_prev_raw_weight"] / sum_weight

        # 6. 个股对组合的收益贡献
        valid["_contrib"] = valid["_norm_weight"] * valid["_ret"]

        # 7. 聚合：日期 x 分组 -> 组合日收益
        daily_rets = valid.groupby([self.date_col, "_prev_group"], observed=True)["_contrib"].sum().unstack()
        daily_rets = daily_rets.fillna(0)

        # 8. 统计指标
        group_stats = self._calc_group_stats(daily_rets)

        # 9. 净值曲线
        group_nav = (1 + daily_rets).cumprod()
        group_nav.iloc[0] = 1.0

        # 10. Long-Short (最高组 - 最低组) = G1 - G{n}
        ls_ret = daily_rets["G1"] - daily_rets[f"G{n_groups}"]
        ls_nav = (1 + ls_ret).cumprod()
        ls_nav.iloc[0] = 1.0
        ls_df = pd.DataFrame({"Long-Short": ls_nav, "ls_return": ls_ret})

        self._result = BacktestResult(
            group_stats=group_stats,
            group_nav=group_nav.reset_index(),
            long_short=ls_df.reset_index(),
            daily_group_rets=daily_rets,
            n_groups=n_groups,
        )

        return self._to_dict()

    def _calc_group_stats(self, rets: pd.DataFrame) -> pd.DataFrame:
        """根据日收益计算各组统计指标"""
        stats = []
        for col in rets.columns:
            r = rets[col]
            mean_ret = r.mean() * 252
            cum_ret = (1 + r).prod() - 1
            vol = r.std() * np.sqrt(252)
            sharpe = mean_ret / vol if vol > 0 else 0
            nav = (1 + r).cumprod()
            dd = (nav - nav.cummax()) / nav.cummax()
            max_dd = abs(dd.min())
            stats.append({
                "分组": col,
                "累计收益_%": round(cum_ret * 100, 2),
                "年化收益_%": round(mean_ret * 100, 2),
                "年化波动_%": round(vol * 100, 2),
                "最大回撤_%": round(max_dd * 100, 2),
                "夏普比率": round(sharpe, 2),
            })
        return pd.DataFrame(stats)

    def _to_dict(self) -> Dict:
        """转为字典格式（兼容现有 API）"""
        r = self._result
        if r is None:
            raise RuntimeError("请先调用 run() 运行回测")
        return {
            "group_stats": r.group_stats,
            "group_nav": r.group_nav,
            "long_short": r.long_short,
            "n_groups": r.n_groups,
        }

    def print_summary(self) -> None:
        """打印回测摘要"""
        if self._result is None:
            print("请先调用 run() 运行回测")
            return
        r = self._result
        print("\n" + "=" * 60)
        print("因子分组回测摘要 (截面版)")
        print("=" * 60)
        print("\n【各组收益统计】")
        print(r.group_stats.to_string(index=False))
        ls = r.long_short.copy()
        if ls.columns[0] != "trade_dt":
            ls = ls.rename(columns={ls.columns[0]: "trade_dt"})
        from backtest.metrics import calc_long_short_summary

        sm = calc_long_short_summary(ls)
        print(f"\n【Long-Short (G1 - G{r.n_groups})】")
        print(f"  累计收益: {sm['cum_ret_pct']:.2f}%")
        print(f"  年化收益: {sm['ann_ret_pct']:.2f}%")
        print(f"  年化波动: {sm['ann_vol_pct']:.2f}%")
        print(f"  最大回撤: {sm['max_dd_pct']:.2f}%")
        print(f"  夏普比率: {sm['sharpe']:.2f}")
        print("=" * 60)


# ========== 独立测试 ==========
if __name__ == "__main__":
    print("因子分组回测引擎 - 独立测试\n")

    np.random.seed(42)
    n_days, n_stocks = 252, 200
    dates = pd.date_range("2023-01-01", periods=n_days, freq="B")
    tickers = [f"S{i:04d}" for i in range(n_stocks)]

    rows = []
    for t in tickers:
        base = 100.0
        for i, d in enumerate(dates):
            ret = np.random.randn() * 0.02
            base = base * (1 + ret)
            rows.append({
                "trade_dt": d,
                "ticker": t,
                "factor": np.random.randn(),
                "adj_close": base,
                "market_value": np.random.randint(50, 500),
            })
    df = pd.DataFrame(rows)

    engine = FactorGroupingEngine(
        df,
        factor_col="factor",
        price_col="adj_close",
        date_col="trade_dt",
        ticker_col="ticker",
        mkt_val_col="market_value",
    )

    for w in ["equal", "mkt_val", "factor_score"]:
        print(f"\n>>> 加权方式: {w}")
        engine.run(n_groups=5, weight_method=w)
        engine.print_summary()
