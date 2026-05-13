"""
因子分析模块 - 优化版
核心优化：缓存透视表，避免重复计算
"""

import sys
from pathlib import Path
import pandas as pd
import numpy as np
import time
from typing import Dict, Tuple, Optional, List

# 添加项目路径
current_dir = Path(__file__).parent
project_root = current_dir.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backtest.profiler import SectionTimer

class FactorAnalyzer:
    """
    因子分析器：用于评估单因子的预测能力（优化版）

    优化点：
    1. 缓存透视表，所有方法复用
    2. 缓存 IC 计算结果
    3. 年度分析向量化
    """

    def __init__(self, data: pd.DataFrame,
                 factor_col: str = "factor",
                 price_col: str = "adj_open",
                 date_col: str = "trade_dt",
                 ticker_col: str = "ticker",
                 max_dates: int = 800):
        """
        初始化因子分析器
        """
        self.data = data.copy()
        self.factor_col = factor_col
        self.price_col = price_col
        self.date_col = date_col
        self.ticker_col = ticker_col

        # 缓存
        self._ic_cache = {}      # IC 计算结果缓存
        self._pivot_cache = None  # 透视表缓存（关键优化）

        # 确保日期列是datetime格式
        if not pd.api.types.is_datetime64_any_dtype(self.data[date_col]):
            self.data[date_col] = pd.to_datetime(self.data[date_col])

        # 数据采样（如果日期太多，采样加速计算）
        unique_dates = self.data[date_col].unique()
        n_dates = len(unique_dates)
        if n_dates > max_dates:
            print(f"  因子分析数据采样: {n_dates} 个交易日 -> {max_dates} 个 (加速计算)")
            sampled_dates = np.sort(unique_dates)[::n_dates // max_dates][:max_dates]
            self.data = self.data[self.data[date_col].isin(sampled_dates)]

        # 排序
        self.data = self.data.sort_values([ticker_col, date_col]).reset_index(drop=True)

        # 计算收益率
        self._calculate_returns()

    def _calculate_returns(self, periods: int = 1):
        """
        计算未来收益率：T+1 开盘买入，T+2 开盘卖出。
        收益率 = (open_{t+2} / open_{t+1}) - 1
        该收益率与 T 日的因子值对齐。
        """
        # 确保数据按股票和日期排序
        self.data = self.data.sort_values([self.ticker_col, self.date_col])

        # 计算每只股票的未来两日开盘价
        self.data["open_lead1"] = self.data.groupby(self.ticker_col)[self.price_col].shift(-1)
        self.data["open_lead2"] = self.data.groupby(self.ticker_col)[self.price_col].shift(-2)

        # 计算收益率
        self.data["return"] = self.data["open_lead2"] / self.data["open_lead1"] - 1

        # 删除缺失值（最后两个交易日无法计算）
        self.data = self.data.dropna(subset=["return"])

        # 可选：删除临时列
        self.data = self.data.drop(columns=["open_lead1", "open_lead2"])
    # ========== 核心优化：缓存透视表 ==========
    def _get_pivot_data(self):
        """
        获取透视表数据（带缓存）
        所有需要透视表的方法都调用这个，只做一次透视
        """
        if self._pivot_cache is None:
            print("    [TIME] 创建透视表...")
            t0 = time.time()

            # 创建因子透视表
            factor_pivot = self.data.pivot_table(
                index=self.date_col,
                columns=self.ticker_col,
                values=self.factor_col
            )

            # 创建收益透视表
            return_pivot = self.data.pivot_table(
                index=self.date_col,
                columns=self.ticker_col,
                values="return"
            )

            # 对齐日期
            common_dates = factor_pivot.index.intersection(return_pivot.index)
            factor_pivot = factor_pivot.loc[common_dates]
            return_pivot = return_pivot.loc[common_dates]

            # 缓存
            self._pivot_cache = {
                'factor': factor_pivot,
                'return': return_pivot,
                'dates': common_dates,
                'shape': factor_pivot.shape
            }

            print(f"      透视表完成: {self._pivot_cache['shape']}, 耗时: {time.time()-t0:.3f}s")

        return self._pivot_cache

    # ========== IC 计算（使用缓存透视表） ==========
    def calculate_ic(self, method: str = "pearson") -> pd.Series:
        """
        计算每日的截面IC
        """
        cache_key = f"ic_{method}"
        if cache_key in self._ic_cache:
            return self._ic_cache[cache_key].copy()

        timer = SectionTimer(f"calculate_ic({method})", print_sections=True)

        # 使用缓存的透视表
        timer.start("获取透视表")
        pivot = self._get_pivot_data()
        factor_pivot = pivot['factor']
        return_pivot = pivot['return']
        timer.end("获取透视表")

        # 转换为 numpy 数组
        timer.start("转换为numpy")
        factor_arr = factor_pivot.values
        return_arr = return_pivot.values
        dates = factor_pivot.index
        timer.end("转换为numpy")

        # 计算 IC
        timer.start("IC计算")

        if method == "spearman":
            from scipy.stats import spearmanr

        ic_values = []
        ic_dates = []

        for i, date in enumerate(dates):
            factor_row = factor_arr[i]
            return_row = return_arr[i]

            valid_mask = ~(np.isnan(factor_row) | np.isnan(return_row))
            n_valid = valid_mask.sum()

            if n_valid < 5:
                continue

            if method == "spearman":
                corr, _ = spearmanr(factor_row[valid_mask], return_row[valid_mask])
            else:
                corr = np.corrcoef(factor_row[valid_mask], return_row[valid_mask])[0, 1]

            if not np.isnan(corr):
                ic_values.append(corr)
                ic_dates.append(date)

        timer.end("IC计算")

        ic_series = pd.Series(ic_values, index=ic_dates, name="IC")

        # 缓存
        self._ic_cache[cache_key] = ic_series.copy()

        timer.report()
        return ic_series

    # ========== IC 衰减（已优化，使用缓存透视表） ==========
    def calculate_ic_decay(self, max_lag: int = 10, method: str = "pearson") -> pd.DataFrame:
        """
        计算 IC 衰减：第 t 日截面因子 vs 第 t+lag 个交易日截面收益的相关（沿日历滞后）。

        使用 return_wide.shift(-lag)，使第 t 行与 factor(t)、return(t+lag) 对齐；
        修正此前 iloc 切片 + index 交集导致「仍为同日 IC」的问题。
        """
        cache_key = f"ic_decay_{method}_{max_lag}"
        if cache_key in self._ic_cache:
            return self._ic_cache[cache_key].copy()

        start_time = time.time()

        # 使用缓存的透视表
        pivot = self._get_pivot_data()
        factor_wide = pivot['factor']
        return_wide = pivot['return']

        print(f"    [TIME] calculate_ic_decay (max_lag={max_lag})...")
        t0 = time.time()

        decay_results = []

        for lag in range(1, max_lag + 1):
            ret_lag = return_wide.shift(-lag)
            if method == "spearman":
                f_rank = factor_wide.rank(axis=1, method="average", na_option="keep")
                r_rank = ret_lag.rank(axis=1, method="average", na_option="keep")
                daily_ic = f_rank.corrwith(r_rank, axis=1)
            else:
                daily_ic = factor_wide.corrwith(ret_lag, axis=1)

            daily_ic = daily_ic.replace([np.inf, -np.inf], np.nan).dropna()

            if len(daily_ic) > 0:
                ic_arr = daily_ic.values
                std_ic = float(ic_arr.std(ddof=1)) if len(ic_arr) > 1 else 0.0
                decay_results.append({
                    "lag": lag,
                    "IC_mean": float(ic_arr.mean()),
                    "IC_std": std_ic,
                    "IR": float(ic_arr.mean() / std_ic) if std_ic != 0 else 0,
                    "samples": len(daily_ic)
                })

        print(f"      IC计算完成, 耗时: {time.time()-t0:.3f}s")

        result = pd.DataFrame(decay_results)
        total_time = time.time() - start_time
        print(f"    [TIME] ic_decay_total: {total_time:.3f}s")

        self._ic_cache[cache_key] = result.copy()
        return result

    # ========== 年度分析（优化版） ==========
    def yearly_analysis(self) -> pd.DataFrame:
        """
        分年度因子分析 - 使用缓存的透视表
        """
        timer = SectionTimer("yearly_analysis", print_sections=True)

        # 使用缓存的透视表
        timer.start("获取透视表")
        pivot = self._get_pivot_data()
        factor_wide = pivot['factor']
        return_wide = pivot['return']
        timer.end("获取透视表")

        timer.start("按年度计算")
        yearly_stats = []

        # 获取所有年份
        years = factor_wide.index.year.unique()

        for year in sorted(years):
            # 筛选该年度的数据
            year_mask = factor_wide.index.year == year
            if year_mask.sum() == 0:
                continue

            factor_year = factor_wide[year_mask]
            return_year = return_wide[year_mask]

            # 计算每日 IC
            daily_ic = factor_year.corrwith(return_year, axis=1).dropna()

            if len(daily_ic) == 0:
                continue

            # ========== 计算全年度不分组平均收益 ==========
            # 1. 复用有效日期，杜绝 NaN
            valid_dates = daily_ic.index
            return_valid = return_year.loc[valid_dates]

            # 2. axis=1 求每天所有股票平均，dropna() 防止某天全空导致污染，最后求年均
            mean_return = return_valid.mean(axis=1).dropna().mean()
            # ==============================================

            # [WARN]️ 注意：这里的 Key 必须和你原始代码一模一样！
            yearly_stats.append({
                "year": year,
                "IC_pearson": daily_ic.mean(),
                "IC_std": daily_ic.std(),
                "IR": daily_ic.mean() / daily_ic.std() if daily_ic.std() != 0 else 0,
                "trading_days": len(daily_ic),
                # 新增收益字段，前端表格的 field 必须配成 "mean_return"
                "mean_return": mean_return
            })

        timer.end("按年度计算")

        result = pd.DataFrame(yearly_stats)
        timer.report()
        return result

    # ========== IC 统计指标 ==========
    def calculate_ic_ir(self, method: str = "pearson") -> Dict[str, float]:
        """计算IC和IR"""
        cache_key = f"ic_ir_{method}"
        if cache_key in self._ic_cache:
            cached = self._ic_cache[cache_key]
            return {
                "IC": cached["IC"],
                "IC_std": cached["IC_std"],
                "IR": cached["IR"],
                "IC_series": cached["IC_series"].copy()
            }

        ic_series = self.calculate_ic(method=method).dropna()

        ic_mean = ic_series.mean()
        ic_std = ic_series.std()
        ir = ic_mean / ic_std if ic_std != 0 else 0

        result = {
            "IC": ic_mean,
            "IC_std": ic_std,
            "IR": ir,
            "IC_series": ic_series
        }

        self._ic_cache[cache_key] = {
            "IC": ic_mean,
            "IC_std": ic_std,
            "IR": ir,
            "IC_series": ic_series.copy()
        }

        return result

    def get_factor_summary(self) -> Dict:
        """获取因子分析总览"""
        #基础统计
        factor_stats = self.data[self.factor_col].describe()

        ic_ir = self.calculate_ic_ir(method="pearson")
        # RANK IC
        ic_spearman_result = self.calculate_ic_ir(method="spearman")

        return {
            "factor_name": self.factor_col,
            "data_points": len(self.data),
            "n_tickers": self.data[self.ticker_col].nunique(),
            "date_range": f"{self.data[self.date_col].min()} to {self.data[self.date_col].max()}",
            "factor_mean": factor_stats["mean"],
            "factor_std": factor_stats["std"],
            "factor_min": factor_stats["min"],
            "factor_max": factor_stats["max"],
            "IC_pearson": ic_ir["IC"],
            "IC_spearman": ic_spearman_result["IC"],
            "IC_std": ic_ir["IC_std"],
            "IR": ic_ir["IR"],
        }

    def calculate_ic_distribution(self, method: str = "pearson", bins: int = 30) -> Dict:
        """计算IC分布统计
        用于绘制IC分布直方图"""
        ic_series = self.calculate_ic(method=method).dropna()
        #计算直方图数据
        hist, bin_edges = np.histogram(ic_series, bins=bins)
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2

        return {
            "hist_counts": hist.tolist(),
            "bin_centers": bin_centers.tolist(),
            "bin_edges": bin_edges.tolist(),
            "ic_values": ic_series.tolist(),
            "ic_dates": [d.strftime("%Y-%m-%d") for d in ic_series.index],
            "mean": float(ic_series.mean()),
            "std": float(ic_series.std()),
            "median": float(ic_series.median()),
            "min": float(ic_series.min()),
            "max": float(ic_series.max())
        }

    def calculate_ic_cumulative(self, method: str = "pearson") -> pd.DataFrame:
        """计算累计IC序列，用于观察IC随时间的累计表现"""
        ic_series = self.calculate_ic(method=method).dropna()
        cumulative_ic = ic_series.cumsum()#计算累计IC

        return pd.DataFrame({
            "trade_dt": ic_series.index,
            "daily_ic": ic_series.values,
            "cumulative_ic": cumulative_ic.values
        })

    def calculate_ic_autocorrelation(self, max_lag: int = 10, method: str = "pearson") -> pd.DataFrame:

        """计算IC自相关系数
        测量IC序列在不同滞后期的自相关性
        高自相关性表示因子表现具有持续性
        参数：max_lag:最大滞后期数
            method:IC计算方法
        返回：dataframe‘包含各滞后期的自相关系数

        """
        ic_series = self.calculate_ic(method=method).dropna()

        autocorr_results = []
        for lag in range(1, max_lag + 1):
            if len(ic_series) > lag:
                autocorr = ic_series.autocorr(lag=lag)
                autocorr_results.append({
                    "lag": lag,
                    "autocorr": autocorr if not np.isnan(autocorr) else 0
                })

        return pd.DataFrame(autocorr_results)

    def calculate_ic_statistics(self, method: str = "pearson") -> Dict:
        """计算完整的IC统计指标
        包含：IC胜率、IC均值、ICIR、T统计量、稳定性、偏度、峰度
        参数：method、相关系数计算方法
        返回：字典、IC统计指标"""
        cache_key = f"ic_statistics_{method}"
        if cache_key in self._ic_cache:
            return self._ic_cache[cache_key]

        from scipy import stats as scipy_stats

        ic_series = self.calculate_ic(method=method).dropna()

        n = len(ic_series)
        ic_mean = ic_series.mean()
        ic_std = ic_series.std()
        #IC胜率
        ic_win_rate = (ic_series > 0).sum() / n if n > 0 else 0

        icir = ic_mean / ic_std if ic_std != 0 else 0
        #t统计量 检验UC均值是否不显著为0
        t_stat = ic_mean / (ic_std / np.sqrt(n)) if ic_std != 0 and n > 0 else 0
        #p值 双侧检验
        p_value = 2 * (1 - scipy_stats.t.cdf(abs(t_stat), df=n-1)) if n > 1 else 1
        #稳定性
        stability = (ic_series.abs() > 0.02).sum() / n if n > 0 else 0
        #偏度
        skewness = scipy_stats.skew(ic_series) if n > 2 else 0
        #峰度
        kurtosis = scipy_stats.kurtosis(ic_series) if n > 3 else 0

        if method == "pearson":
            rank_ic_series = self.calculate_ic(method="spearman").dropna()
            rank_ic_mean = rank_ic_series.mean()
        else:
            rank_ic_mean = ic_mean

        result = {
            "IC_mean": float(ic_mean),
            "IC_std": float(ic_std),
            "ICIR": float(icir),
            "IC_win_rate": float(ic_win_rate),
            "t_stat": float(t_stat),
            "p_value": float(p_value),
            "stability": float(stability),
            "skewness": float(skewness),
            "kurtosis": float(kurtosis),
            "Rank_IC": float(rank_ic_mean),
            "samples": int(n),
            "IC_positive_pct": float((ic_series > 0).sum() / n * 100) if n > 0 else 0,
            "IC_negative_pct": float((ic_series < 0).sum() / n * 100) if n > 0 else 0
        }

        self._ic_cache[cache_key] = result
        return result

    def get_full_ic_analysis(self, method: str = "pearson", verbose: bool = True) -> Dict:
        """获取完整的IC分析结果"""
        cache_key = f"full_ic_analysis_{method}"
        if cache_key in self._ic_cache:
            if verbose:
                print("    (使用缓存)")
            return self._ic_cache[cache_key]

        result = {}

        if verbose:
            print("    计算IC统计指标...")
        t0 = time.time()
        result["statistics"] = self.calculate_ic_statistics(method=method)
        if verbose:
            print(f"    [OK] IC统计指标完成 ({time.time()-t0:.1f}s)")

        if verbose:
            print("    计算IC衰减 (10个滞后期)...")
        t0 = time.time()
        result["decay"] = self.calculate_ic_decay(max_lag=10, method=method).to_dict("records")
        if verbose:
            print(f"    [OK] IC衰减完成 ({time.time()-t0:.1f}s)")

        if verbose:
            print("    计算IC分布...")
        t0 = time.time()
        result["distribution"] = self.calculate_ic_distribution(method=method, bins=30)
        if verbose:
            print(f"    [OK] IC分布完成 ({time.time()-t0:.1f}s)")

        if verbose:
            print("    计算累计IC...")
        t0 = time.time()
        result["cumulative"] = self.calculate_ic_cumulative(method=method).to_dict("records")
        if verbose:
            print(f"    [OK] 累计IC完成 ({time.time()-t0:.1f}s)")

        if verbose:
            print("    计算IC自相关...")
        t0 = time.time()
        result["autocorrelation"] = self.calculate_ic_autocorrelation(max_lag=10, method=method).to_dict("records")
        if verbose:
            print(f"    [OK] IC自相关完成 ({time.time()-t0:.1f}s)")

        self._ic_cache[cache_key] = result
        return result

    def factor_quantile_analysis(self, n_quantiles: int = 5) -> pd.DataFrame:
        """
        因子截面分位数分析（各分位内对「与因子同日对齐」的 forward return 聚合）。

        注：sharpe 列为分位内日收益均值/标准差，未年化，不宜与组合夏普直接比较。
        """
        def daily_qcut(x):
            try:
                if len(x) < n_quantiles:
                    return np.nan
                return pd.qcut(x, q=n_quantiles,
                              labels=[f"Q{i+1}" for i in range(n_quantiles)],
                              duplicates="drop")
            except ValueError:
                return np.nan

        self.data["quantile"] = self.data.groupby(self.date_col)[self.factor_col].transform(daily_qcut)
        valid_data = self.data.dropna(subset=["quantile"])

        quantile_stats = valid_data.groupby("quantile")["return"].agg([
            ("mean_return", "mean"),
            ("std_return", "std"),
            ("count", "count")
        ])
        quantile_stats["sharpe"] = quantile_stats["mean_return"] / quantile_stats["std_return"]

        return quantile_stats

    def clear_cache(self):
        """清理所有缓存（释放内存）"""
        self._ic_cache.clear()
        self._pivot_cache = None
        print("[OK] 缓存已清理")
