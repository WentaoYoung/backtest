from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from backtest.metrics import calc_group_stats_from_rets, calc_long_short_summary
from backtest.profiler import SectionTimer
from backtest.types1 import BacktestConfig, MatrixBundle, WeightMethod


class BacktestEngine:
    """
    因子分组回测引擎 (截面分组版)

    核心思路：
    1. 每日截面分组：在每一天内部按因子值排名
    2. 多种加权方式：支持等权、市值加权、因子得分加权
    3. 收益计算：T 日信号 -> T+1 日收益
    """

    def __init__(
        self,
        data: pd.DataFrame,
        factor_col: str = "factor",
        price_col: str = "adj_open",
        date_col: str = "trade_dt",
        ticker_col: str = "ticker",
        mkt_val_col: str = "market_value",
    ):
        self.config = BacktestConfig(
            factor_col=factor_col,
            price_col=price_col,
            date_col=date_col,
            ticker_col=ticker_col,
            mkt_val_col=mkt_val_col,
        )
        self.data = self._prepare_data(data)
        self.group_results = None
        self._weight_matrix = None
        self._mkt_val_matrix = None

    def _prepare_data(self, data: pd.DataFrame) -> pd.DataFrame:
        """基础清洗和收益序列构造"""
        cfg = self.config
        df = data.copy()

        if df is None or df.empty:
            raise ValueError("回测输入数据为空（data 为空）。请检查因子/价格/日期对齐。")

        if not pd.api.types.is_datetime64_any_dtype(df[cfg.date_col]):
            df[cfg.date_col] = pd.to_datetime(df[cfg.date_col])

        df = df.sort_values([cfg.ticker_col, cfg.date_col]).reset_index(drop=True)

        # T 日信号对应 T+1 日收益，基于未来两天开盘价计算持有收益
        df["open_lead1"] = df.groupby(cfg.ticker_col)[cfg.price_col].shift(-1)
        df["open_lead2"] = df.groupby(cfg.ticker_col)[cfg.price_col].shift(-2)
        df["return"] = df["open_lead2"] / df["open_lead1"] - 1

        df = df.dropna(subset=["return"])
        if df.empty:
            raise ValueError(
                "回测输入数据在计算收益后为空（口径: return = open[t+2]/open[t+1]-1）。"
                "请检查日期范围、价格缺失情况或因子可用天数。"
            )

        df = df.drop(columns=["open_lead1", "open_lead2"], errors="ignore")
        return df

    def _build_matrices(self) -> MatrixBundle:
        """构建日期×股票矩阵"""
        cfg = self.config

        if self.data is None or self.data.empty:
            raise ValueError("回测输入数据为空（prepare 后为空），无法构建矩阵。")

        date_codes, dates = pd.factorize(self.data[cfg.date_col])
        ticker_codes, tickers = pd.factorize(self.data[cfg.ticker_col])
        if len(date_codes) == 0 or len(ticker_codes) == 0:
            raise ValueError("回测输入数据为空编码（date/ticker 编码长度为 0），无法构建矩阵。")
        d_size, n_size = date_codes.max() + 1, ticker_codes.max() + 1

        factor_mat = np.full((d_size, n_size), np.nan)
        return_mat = np.full((d_size, n_size), np.nan)
        factor_mat[date_codes, ticker_codes] = self.data[cfg.factor_col].values
        return_mat[date_codes, ticker_codes] = self.data["return"].values

        mkt_val_mat = None
        if cfg.mkt_val_col in self.data.columns:
            mkt_val_mat = np.full((d_size, n_size), np.nan)
            mkt_val_mat[date_codes, ticker_codes] = self.data[cfg.mkt_val_col].values
            mkt_val_mat = np.nan_to_num(mkt_val_mat, nan=0.0)

        return MatrixBundle(
            factor_mat=factor_mat,
            return_mat=return_mat,
            mkt_val_mat=mkt_val_mat,
            dates=pd.Index(dates),
            tickers=pd.Index(tickers),
        )

    @staticmethod
    def _build_groups(factor_mat: np.ndarray, n_groups: int) -> np.ndarray:
        """按日截面因子排序分组，返回每个股票所属组别矩阵"""
        d_size, n_size = factor_mat.shape
        factor_safe = np.where(np.isnan(factor_mat), np.inf, factor_mat)

        order = np.argsort(factor_safe, axis=1)
        ranks = np.empty_like(order, dtype=float)
        ranks[np.arange(d_size)[:, None], order] = np.arange(1, n_size + 1, dtype=float)

        valid_count = (~np.isnan(factor_mat)).sum(axis=1, keepdims=True).astype(float)
        ranks = ranks / valid_count

        group_mat = np.floor(ranks * n_groups).astype(float)
        group_mat[group_mat >= n_groups] = n_groups - 1
        # 翻转编号：G1 = 因子截面最高组，G{n} = 最低组（与多空 G1−G{n}、前端「Top组」一致）
        group_mat = (n_groups - 1) - group_mat
        group_mat[np.isnan(factor_mat)] = np.nan
        group_mat[valid_count.flatten() < n_groups] = np.nan
        return group_mat

    def _build_raw_weights(
        self, weight_method: str, factor_mat: np.ndarray, mkt_val_mat: Optional[np.ndarray]
    ) -> np.ndarray:
        """生成归一化前权重"""
        if weight_method == WeightMethod.EQUAL.value:
            raw_weight_mat = np.ones_like(factor_mat)
        elif weight_method == WeightMethod.MKT_VAL.value:
            if mkt_val_mat is None:
                raise ValueError(f"缺少市值列 '{self.config.mkt_val_col}'，无法进行市值加权")
            raw_weight_mat = mkt_val_mat.copy()
        elif weight_method == WeightMethod.FACTOR_SCORE.value:
            raw_weight_mat = np.abs(factor_mat)
        else:
            valid_methods = [method.value for method in WeightMethod]
            raise ValueError(f"不支持的加权方式: {weight_method}，支持: {valid_methods}")

        valid_mask = ~np.isnan(factor_mat)
        return np.where(valid_mask, raw_weight_mat, 0.0)

    @staticmethod
    def _calc_daily_group_returns(
        return_mat: np.ndarray, group_mat: np.ndarray, raw_weight_mat: np.ndarray, n_groups: int
    ) -> Tuple[np.ndarray, np.ndarray]:
        """矩阵化计算分组日收益，返回(日收益矩阵, 归一化权重矩阵)"""
        d_size, _ = group_mat.shape
        group_int_safe = np.nan_to_num(group_mat, nan=-1).astype(int)

        valid = (group_int_safe >= 0) & (raw_weight_mat > 0)
        weighted = np.where(valid, raw_weight_mat, 0.0)

        group_sum = np.zeros((d_size, n_groups))
        np.add.at(group_sum, (np.arange(d_size)[:, None], group_int_safe), weighted)

        group_int = np.where(np.isnan(group_mat), -1, group_mat).astype(np.int32)
        group_int = np.clip(group_int, 0, n_groups - 1)
        group_sum_per_stock = group_sum[np.arange(d_size)[:, None], group_int]

        weight_mat = np.where(group_sum_per_stock > 0, raw_weight_mat / group_sum_per_stock, 0.0)

        contrib = weight_mat * np.nan_to_num(return_mat, nan=0.0)
        daily_group_rets_arr = np.zeros((d_size, n_groups))
        np.add.at(daily_group_rets_arr, (np.arange(d_size)[:, None], group_int_safe), contrib)
        # 对于当日某组没有任何有效持仓的情况，收益设为 NaN（而不是 0）
        daily_group_rets_arr[group_sum <= 0] = np.nan
        return daily_group_rets_arr, weight_mat

    def run_group_backtest(self, n_groups: int = 5, weight_method: str = "equal") -> Dict:
        """
        运行因子分组回测（矩阵化优化版）

        参数:
            n_groups: 分组数量，默认5组
            weight_method: 加权方式 ('equal'=等权, 'mkt_val'=市值加权, 'factor_score'=因子得分加权)
        """
        timer = SectionTimer("回测引擎", print_sections=True)
        print("\n开始因子分组回测 (截面模式-矩阵优化)...")
        print(f"  分组数量: {n_groups}")
        print(f"  加权方式: {weight_method}")

        timer.start("构建矩阵")
        matrices = self._build_matrices()
        factor_mat = matrices.factor_mat
        return_mat = matrices.return_mat
        mkt_val_mat = matrices.mkt_val_mat
        dates = matrices.dates
        tickers = matrices.tickers
        timer.end("构建矩阵")

        timer.start("截面分组(向量化)")
        group_mat = self._build_groups(factor_mat, n_groups)
        timer.end("截面分组(向量化)")

        timer.start("权重计算(矩阵)")
        raw_weight_mat = self._build_raw_weights(weight_method, factor_mat, mkt_val_mat)
        timer.end("权重计算(矩阵)")

        timer.start("收益计算(矩阵)")
        daily_group_rets_arr, weight_mat = self._calc_daily_group_returns(
            return_mat=return_mat,
            group_mat=group_mat,
            raw_weight_mat=raw_weight_mat,
            n_groups=n_groups,
        )
        timer.end("收益计算(矩阵)")

        # 若有效收益全空或全为0，直接给出明确错误，避免后续 fillna(0) 误导为“回测全0”
        valid_ret_count = int(np.isfinite(daily_group_rets_arr).sum())
        nonzero_ret_count = int(np.count_nonzero(np.abs(np.nan_to_num(daily_group_rets_arr, nan=0.0)) > 1e-12))
        if valid_ret_count == 0 or nonzero_ret_count == 0:
            valid_per_day = (~np.isnan(factor_mat)).sum(axis=1)
            min_valid = int(valid_per_day.min()) if len(valid_per_day) else 0
            med_valid = float(np.median(valid_per_day)) if len(valid_per_day) else 0.0
            max_valid = int(valid_per_day.max()) if len(valid_per_day) else 0
            raise ValueError(
                "回测有效收益为空/全0，无法形成有效净值曲线。"
                f" n_groups={n_groups}, dates={len(dates)}, tickers={len(tickers)}, "
                f"per_day_valid(min/med/max)={min_valid}/{med_valid:.1f}/{max_valid}。"
                " 可能原因：分组数过高、因子/价格缺失多、频率筛选后样本过少。"
            )

        timer.start("统计指标计算")
        group_cols = [f"G{i + 1}" for i in range(n_groups)]
        # 统计使用原始缺失值，避免把“无持仓日”误当作 0 收益拉低指标
        daily_group_rets_raw = pd.DataFrame(daily_group_rets_arr, index=dates, columns=group_cols)
        group_stats = calc_group_stats_from_rets(daily_group_rets_raw)
        timer.end("统计指标计算")

        timer.start("净值曲线计算")
        # 净值曲线中无持仓日按 0 收益处理，保持曲线可累计
        daily_group_rets = daily_group_rets_raw.fillna(0.0)
        group_nav = (1 + daily_group_rets).cumprod()
        group_nav.iloc[0] = 1.0

        ls_ret = daily_group_rets["G1"] - daily_group_rets[f"G{n_groups}"]
        ls_nav = (1 + ls_ret).cumprod()
        ls_nav.iloc[0] = 1.0
        ls_df = pd.DataFrame({"Long-Short": ls_nav, "ls_return": ls_ret})
        timer.end("净值曲线计算")

        self._weight_matrix = pd.DataFrame(weight_mat, index=dates, columns=tickers)
        self._mkt_val_matrix = (
            pd.DataFrame(mkt_val_mat, index=dates, columns=tickers) if mkt_val_mat is not None else None
        )

        self.group_results = {
            "group_stats": group_stats,
            "group_nav": group_nav.reset_index().rename(columns={"index": "trade_dt"}),
            "long_short": ls_df.reset_index().rename(columns={"index": "trade_dt"}),
            "n_groups": n_groups,
        }

        timer.report()
        print("[OK] 回测完成！")
        return self.group_results

    def print_summary(self):
        """打印回测摘要"""
        if self.group_results is None:
            print("请先运行 run_group_backtest()")
            return

        print("\n" + "=" * 70)
        print("因子分组回测摘要")
        print("=" * 70)

        print("\n【各组收益统计】")
        print(self.group_results["group_stats"].to_string(index=False))

        ls_summary = calc_long_short_summary(self.group_results["long_short"])

        print(f"\n【Long-Short组合 (G1 - G{self.group_results['n_groups']})】")
        print(f"  累计收益: {ls_summary['cum_ret_pct']:.2f}%")
        print(f"  年化收益: {ls_summary['ann_ret_pct']:.2f}%")
        print(f"  年化波动: {ls_summary['ann_vol_pct']:.2f}%")
        print(f"  最大回撤: {ls_summary['max_dd_pct']:.2f}%")
        print(f"  夏普比率: {ls_summary['sharpe']:.2f}")

        returns = self.group_results["group_stats"]["累计收益_%"].values
        is_increasing = all(returns[i] <= returns[i + 1] for i in range(len(returns) - 1))
        is_decreasing = all(returns[i] >= returns[i + 1] for i in range(len(returns) - 1))

        print("\n【因子单调性】")
        if is_increasing:
            print("  [OK] 因子具有正向单调性（G1 < G2 < ... < G5）")
        elif is_decreasing:
            print("  [OK] 因子具有反向单调性（G1 > G2 > ... > G5）")
        else:
            print("  ✗ 因子不具有单调性")

        print("=" * 70)

    def export_results(self, output_dir: str = "./results"):
        """导出结果"""
        import os

        os.makedirs(output_dir, exist_ok=True)
        if self.group_results is None:
            print("请先运行 run_group_backtest()")
            return

        self.group_results["group_stats"].to_csv(
            f"{output_dir}/group_stats.csv", index=False, encoding="utf-8-sig"
        )
        self.group_results["group_nav"].to_csv(
            f"{output_dir}/group_nav.csv", index=False, encoding="utf-8-sig"
        )
        self.group_results["long_short"].to_csv(
            f"{output_dir}/long_short.csv", index=False, encoding="utf-8-sig"
        )
        print(f"[OK] 结果已导出到: {output_dir}/")
