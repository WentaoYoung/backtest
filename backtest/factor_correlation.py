"""
因子相关性分析模块
用于检测新因子与已有因子之间的相关性

功能：
1. 横截面相关性分析 - 每日截面上新因子与已有因子的相关性（图表1：因子相关性）
2. IC序列分析 - 各因子的IC时间序列（图表2：IC）
3. 收益率序列分析 - 各因子多空组合收益率（图表3：收益率）

重构说明：
- 支持阈值筛选，只计算/显示相关性超过阈值的因子
- 简化数据结构，只返回三张图表需要的数据
"""

import pandas as pd
import numpy as np
import time
from typing import Dict, List, Tuple, Optional
from scipy import stats as scipy_stats
from functools import wraps

def timer(func):
    """打印函数执行时间的装饰器"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        end = time.perf_counter()
        elapsed = end - start
        print(f"⏱️ {func.__name__} 执行耗时: {elapsed:.4f} 秒")
        return result
    return wrapper

class FactorCorrelationAnalyzer:
    """
    因子相关性分析器
    
    用于检测新因子与已有因子之间的相关性，帮助判断：
    - 新因子是否与已有因子高度相关（可能冗余）
    - 新因子能否提供增量信息
    """
    
    def __init__(self, 
                 data: pd.DataFrame,
                 new_factor_col: str,
                 existing_factor_cols: List[str],
                 price_col: str = "adj_open",
                 date_col: str = "trade_dt",
                 ticker_col: str = "ticker",
                 max_dates: int = 500):
        """
        初始化因子相关性分析器
        
        参数:
            data: DataFrame，必须包含日期、标的代码、价格、新因子、已有因子
            new_factor_col: 新因子列名
            existing_factor_cols: 已有因子列名列表
            price_col: 价格列名
            date_col: 日期列名
            ticker_col: 标的代码列名
            max_dates: 最大日期数量（用于采样加速，默认500个交易日）
        """
        self.data = data.copy()
        self.new_factor_col = new_factor_col
        self.existing_factor_cols = existing_factor_cols
        self.price_col = price_col
        self.date_col = date_col
        self.ticker_col = ticker_col
        
        # 确保日期列是datetime格式
        if not pd.api.types.is_datetime64_any_dtype(self.data[date_col]):
            self.data[date_col] = pd.to_datetime(self.data[date_col])
        
        # 数据采样（如果日期太多，采样加速计算）
        unique_dates = self.data[date_col].unique()
        n_dates = len(unique_dates)
        if n_dates > max_dates:
            print(f"⚡ 数据采样: {n_dates} 个交易日 -> {max_dates} 个 (加速计算)")
            # 均匀采样日期
            sampled_dates = np.sort(unique_dates)[::n_dates // max_dates][:max_dates]
            self.data = self.data[self.data[date_col].isin(sampled_dates)]
        
        # 排序
        self.data = self.data.sort_values([ticker_col, date_col]).reset_index(drop=True)
        
        # 计算收益率
        self._calculate_returns()
        
        # 缓存
        self._cache = {}
    
    def _calculate_returns(self, periods: int = 1):
        """计算未来收益率 (T+1日收益)"""
        self.data["return"] = (
            self.data.groupby(self.ticker_col)[self.price_col]
            .pct_change(periods)
            .shift(-periods)
        )
        self.data = self.data.dropna(subset=["return"])
    
    # =========================================================================
    # 1. 横截面相关性分析
    # =========================================================================
    @timer
    def calculate_cross_sectional_correlation(self, method: str = "spearman") -> Dict:
        """
        计算横截面相关性
        
        每日截面上，计算新因子与每个已有因子的相关系数。
        这反映了两个因子在选股上的相似程度。
        
        参数:
            method: 'pearson' 或 'spearman'
        
        返回:
            字典，包含:
            - daily_corr: DataFrame，每日相关系数 (日期 x 因子)
            - summary: 汇总统计
        """
        cache_key = f"cross_sectional_{method}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        results = {}
        
        for existing_factor in self.existing_factor_cols:
            # 定义每日计算相关系数的函数
            def daily_corr(group, factor_col=existing_factor):
                if len(group) < 20:
                    return np.nan
                new_vals = group[self.new_factor_col]
                exist_vals = group[factor_col]
                # 去除任一列为NaN的行
                valid_mask = new_vals.notna() & exist_vals.notna()
                if valid_mask.sum() < 20:
                    return np.nan
                return new_vals[valid_mask].corr(exist_vals[valid_mask], method=method)
            
            # 按日期分组计算截面相关系数
            daily_series = self.data.groupby(self.date_col).apply(
                lambda g: daily_corr(g, existing_factor)
            )
            results[existing_factor] = daily_series
        
        # 转换为DataFrame
        daily_corr_df = pd.DataFrame(results)
        daily_corr_df.index.name = self.date_col
        
        # 计算汇总统计
        summary = []
        for col in daily_corr_df.columns:
            series = daily_corr_df[col].dropna()
            if len(series) == 0:
                continue
            summary.append({
                "factor": col,
                "mean_corr": float(series.mean()),
                "std_corr": float(series.std()),
                "median_corr": float(series.median()),
                "min_corr": float(series.min()),
                "max_corr": float(series.max()),
                "positive_pct": float((series > 0).sum() / len(series) * 100),  # 正相关占比
                "high_corr_pct": float((series.abs() > 0.3).sum() / len(series) * 100),  # 高相关占比
                "samples": int(len(series))
            })
        
        # 格式化日期
        daily_corr_records = []
        for date, row in daily_corr_df.iterrows():
            record = {"trade_dt": date.strftime("%Y-%m-%d") if hasattr(date, 'strftime') else str(date)}
            for col in daily_corr_df.columns:
                if pd.notna(row[col]):
                    record[col] = float(row[col])
            daily_corr_records.append(record)
        
        result = {
            "daily_corr": daily_corr_records,
            "summary": summary
        }
        
        self._cache[cache_key] = result
        return result
    
    # =========================================================================
    # 2. IC序列相关性分析
    # =========================================================================
    @timer
    def calculate_ic_correlation(self, method: str = "spearman") -> Dict:
        """
        计算IC序列相关性
        
        新因子的每日IC序列与已有因子的每日IC序列之间的相关性。
        如果两个因子的IC序列高度相关，说明它们在相同的时间段表现好/差，
        可能捕获了相似的市场信息。
        
        参数:
            method: IC计算方法 ('pearson' 或 'spearman')
        
        返回:
            字典，包含:
            - ic_series: 各因子的IC序列
            - ic_correlation_matrix: IC序列相关性矩阵
            - summary: 汇总统计
        """
        cache_key = f"ic_correlation_{method}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        # 计算每个因子的IC序列
        def calc_daily_ic(group, factor_col):
            if len(group) < 20:
                return np.nan
            factor_vals = group[factor_col]
            return_vals = group["return"]
            valid_mask = factor_vals.notna() & return_vals.notna()
            if valid_mask.sum() < 20:
                return np.nan
            return factor_vals[valid_mask].corr(return_vals[valid_mask], method=method)
        
        # 计算所有因子的IC序列
        all_factors = [self.new_factor_col] + self.existing_factor_cols
        ic_dict = {}
        
        for factor in all_factors:
            ic_series = self.data.groupby(self.date_col).apply(
                lambda g: calc_daily_ic(g, factor)
            ).dropna()
            ic_dict[factor] = ic_series
        
        # 转换为DataFrame
        ic_df = pd.DataFrame(ic_dict)
        
        # 计算IC序列之间的相关性矩阵
        ic_corr_matrix = ic_df.corr()
        
        # 新因子与已有因子IC的相关性统计
        new_factor_ic = ic_df.get(self.new_factor_col)
        summary = []
        
        for existing_factor in self.existing_factor_cols:
            existing_ic = ic_df.get(existing_factor)
            if new_factor_ic is None or existing_ic is None:
                continue
            
            # 对齐数据
            aligned = pd.concat([new_factor_ic, existing_ic], axis=1).dropna()
            if len(aligned) < 5:
                continue
            
            corr_value = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
            
            # 计算显著性 (t检验)
            n = len(aligned)
            if abs(corr_value) < 1:
                t_stat = corr_value * np.sqrt(n - 2) / np.sqrt(1 - corr_value**2)
            else:
                t_stat = 0
            p_value = 2 * (1 - scipy_stats.t.cdf(abs(t_stat), df=n-2)) if n > 2 else 1
            
            summary.append({
                "existing_factor": existing_factor,
                "ic_correlation": float(corr_value),
                "t_stat": float(t_stat),
                "p_value": float(p_value),
                "is_significant": bool(p_value < 0.05),
                "samples": int(n)
            })
        
        # 格式化IC序列数据用于前端
        ic_series_formatted = []
        for date in ic_df.index:
            row = {"trade_dt": date.strftime("%Y-%m-%d") if hasattr(date, 'strftime') else str(date)}
            for factor in all_factors:
                if factor in ic_df.columns and pd.notna(ic_df.loc[date, factor]):
                    row[factor] = float(ic_df.loc[date, factor])
            ic_series_formatted.append(row)
        
        # 格式化相关性矩阵
        ic_corr_matrix_dict = {}
        for col in ic_corr_matrix.columns:
            ic_corr_matrix_dict[col] = {}
            for idx in ic_corr_matrix.index:
                ic_corr_matrix_dict[col][idx] = float(ic_corr_matrix.loc[idx, col])
        
        result = {
            "ic_series": ic_series_formatted,
            "ic_correlation_matrix": ic_corr_matrix_dict,
            "summary": summary
        }
        
        self._cache[cache_key] = result
        return result
    
    # =========================================================================
    # 3. 收益率序列相关性分析
    # =========================================================================
    @timer
    def calculate_return_correlation(self, n_groups: int = 5, 
                                      weight_method: str = "equal") -> Dict:
        """
        计算收益率序列相关性
        
        分别用新因子和已有因子构建投资组合，计算组合收益率之间的相关性。
        如果两个因子构建的组合收益高度相关，说明它们产生了相似的投资结果。
        
        参数:
            n_groups: 分组数量
            weight_method: 加权方式 ('equal', 'mkt_val', 'factor_score')
        
        返回:
            字典，包含:
            - portfolio_returns: 各因子多头组合的日收益序列
            - long_short_returns: 各因子多空组合的日收益序列
            - correlation_matrix: 收益率相关性矩阵
            - summary: 汇总统计
        """
        cache_key = f"return_correlation_{n_groups}_{weight_method}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        all_factors = [self.new_factor_col] + self.existing_factor_cols
        
        # 存储各因子的组合收益
        long_returns = {}  # 多头组合收益
        long_short_returns = {}  # 多空组合收益
        
        for factor in all_factors:
            # 截面分组
            def daily_qcut(x, n=n_groups):
                try:
                    if len(x) < n:
                        return pd.Series([np.nan] * len(x), index=x.index)
                    return pd.qcut(x, n, labels=[f"G{n-i}" for i in range(n)], duplicates='drop')
                except ValueError:
                    return pd.Series([np.nan] * len(x), index=x.index)
            
            # 创建临时数据
            temp_data = self.data[[self.date_col, self.ticker_col, factor, "return"]].copy()
            temp_data = temp_data.dropna(subset=[factor])
            
            if len(temp_data) == 0:
                continue
            
            temp_data["group"] = temp_data.groupby(self.date_col)[factor].transform(daily_qcut)
            
            # 信号对齐 (T日信号 -> T+1日收益)
            temp_data["prev_group"] = temp_data.groupby(self.ticker_col)["group"].shift(1)
            
            # 等权计算组收益
            valid_data = temp_data.dropna(subset=["prev_group", "return"])
            
            if len(valid_data) == 0:
                continue
            
            # 计算各组日收益
            group_daily_ret = (
                valid_data.groupby([self.date_col, "prev_group"], observed=True)["return"]
                .mean()
                .unstack()
            )
            
            if group_daily_ret.empty:
                continue
            
            # 多头组合 (最高分组)
            top_group = "G1"
            bottom_group = f"G{n_groups}"
            
            if top_group in group_daily_ret.columns:
                long_returns[factor] = group_daily_ret[top_group]
            
            # 多空组合
            if top_group in group_daily_ret.columns and bottom_group in group_daily_ret.columns:
                long_short_returns[factor] = group_daily_ret[top_group] - group_daily_ret[bottom_group]
        
        # 转换为DataFrame
        long_ret_df = pd.DataFrame(long_returns)
        ls_ret_df = pd.DataFrame(long_short_returns)
        
        # 计算相关性矩阵
        long_corr_matrix = long_ret_df.corr() if not long_ret_df.empty else pd.DataFrame()
        ls_corr_matrix = ls_ret_df.corr() if not ls_ret_df.empty else pd.DataFrame()
        
        # 新因子与已有因子的相关性统计
        summary = []
        new_factor_long_ret = long_ret_df.get(self.new_factor_col)
        new_factor_ls_ret = ls_ret_df.get(self.new_factor_col)
        
        for existing_factor in self.existing_factor_cols:
            existing_long_ret = long_ret_df.get(existing_factor)
            existing_ls_ret = ls_ret_df.get(existing_factor)
            
            row = {"existing_factor": existing_factor}
            
            # 多头组合相关性
            if new_factor_long_ret is not None and existing_long_ret is not None:
                aligned = pd.concat([new_factor_long_ret, existing_long_ret], axis=1).dropna()
                if len(aligned) >= 5:
                    long_corr = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
                    row["long_correlation"] = float(long_corr)
                    row["long_samples"] = int(len(aligned))
            
            # 多空组合相关性
            if new_factor_ls_ret is not None and existing_ls_ret is not None:
                aligned = pd.concat([new_factor_ls_ret, existing_ls_ret], axis=1).dropna()
                if len(aligned) >= 5:
                    ls_corr = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
                    row["long_short_correlation"] = float(ls_corr)
                    row["long_short_samples"] = int(len(aligned))
            
            if "long_correlation" in row or "long_short_correlation" in row:
                summary.append(row)
        
        # 格式化收益序列
        long_ret_formatted = []
        for date in long_ret_df.index:
            row = {"trade_dt": date.strftime("%Y-%m-%d") if hasattr(date, 'strftime') else str(date)}
            for factor in all_factors:
                if factor in long_ret_df.columns and pd.notna(long_ret_df.loc[date, factor]):
                    row[factor] = float(long_ret_df.loc[date, factor])
            long_ret_formatted.append(row)
        
        ls_ret_formatted = []
        for date in ls_ret_df.index:
            row = {"trade_dt": date.strftime("%Y-%m-%d") if hasattr(date, 'strftime') else str(date)}
            for factor in all_factors:
                if factor in ls_ret_df.columns and pd.notna(ls_ret_df.loc[date, factor]):
                    row[factor] = float(ls_ret_df.loc[date, factor])
            ls_ret_formatted.append(row)
        
        # 格式化相关性矩阵
        def format_corr_matrix(matrix):
            if matrix.empty:
                return {}
            result = {}
            for col in matrix.columns:
                result[col] = {}
                for idx in matrix.index:
                    result[col][idx] = float(matrix.loc[idx, col])
            return result
        
        result = {
            "long_portfolio_returns": long_ret_formatted,
            "long_short_returns": ls_ret_formatted,
            "long_correlation_matrix": format_corr_matrix(long_corr_matrix),
            "long_short_correlation_matrix": format_corr_matrix(ls_corr_matrix),
            "summary": summary
        }
        
        self._cache[cache_key] = result
        return result
    
    # =========================================================================
    # 综合分析
    # =========================================================================
    @timer
    def get_full_correlation_analysis(self, 
                                       method: str = "spearman",
                                       n_groups: int = 5,
                                       weight_method: str = "equal") -> Dict:
        """
        获取完整的因子相关性分析
        
        包含三种相关性分析：
        1. 横截面相关性 - 因子值层面
        2. IC序列相关性 - 预测能力层面
        3. 收益率相关性 - 组合收益层面
        
        返回:
            字典，包含所有三种相关性分析结果和综合评估
        """
        print("开始因子相关性分析...")
        
        print("  1. 计算横截面相关性...")
        cross_sectional = self.calculate_cross_sectional_correlation(method)
        
        print("  2. 计算IC序列相关性...")
        ic_correlation = self.calculate_ic_correlation(method)
        
        print("  3. 计算收益率序列相关性...")
        return_correlation = self.calculate_return_correlation(n_groups, weight_method)
        
        # 综合评估
        overall_assessment = self._assess_overall_correlation(
            cross_sectional, ic_correlation, return_correlation
        )
        
        print("[OK] 因子相关性分析完成!")
        
        # 提取 factor_scores 到顶层，方便前端渲染
        factor_scores = overall_assessment.get("score_breakdown", {}).get("factor_scores", [])
        high_correlation_factors = overall_assessment.get("factors_exceeding_threshold", [])
        
        return {
            "cross_sectional": cross_sectional,
            "ic_correlation": ic_correlation,
            "return_correlation": return_correlation,
            "overall_assessment": overall_assessment,
            "new_factor": self.new_factor_col,
            "existing_factors": self.existing_factor_cols,
            # 提升到顶层，前端直接使用
            "factor_scores": factor_scores,
            "high_correlation_factors": high_correlation_factors
        }
    
    def calculate_weighted_correlation_score(self, 
                                            cross_sectional: Dict,
                                            ic_correlation: Dict,
                                            return_correlation: Dict,
                                            threshold: float = 0.7) -> Dict:
        """
        计算综合相关性得分
        
        对每个已有因子，计算加权相关性得分：
        - 横截面相关性: 权重 0.3
        - IC序列相关性: 权重 0.4
        - 收益率相关性: 权重 0.3
        
        参数:
            cross_sectional: 横截面相关性结果
            ic_correlation: IC序列相关性结果
            return_correlation: 收益率相关性结果
            threshold: 高相关阈值，默认0.5
        
        返回:
            字典，包含:
            - factor_scores: 每个因子的综合得分列表（按得分降序）
            - high_correlation_factors: 超过阈值的因子列表（预警）
            - overall_score: 整体综合得分（所有因子的平均）
        """
        # 构建因子得分字典
        factor_scores_dict = {}
        
        # 1. 处理横截面相关性（权重0.3）
        if cross_sectional.get("summary"):
            for item in cross_sectional["summary"]:
                factor = item["factor"]
                if factor not in factor_scores_dict:
                    factor_scores_dict[factor] = {
                        "factor": factor,
                        "cross_sectional_score": 0.0,
                        "ic_score": 0.0,
                        "return_score": 0.0,
                        "weighted_score": 0.0,
                        "details": {}
                    }
                # 使用平均相关性的绝对值作为得分
                factor_scores_dict[factor]["cross_sectional_score"] = abs(item["mean_corr"])
                factor_scores_dict[factor]["details"]["cross_sectional_mean"] = item["mean_corr"]
                factor_scores_dict[factor]["details"]["cross_sectional_std"] = item["std_corr"]
        
        # 2. 处理IC序列相关性（权重0.4）
        if ic_correlation.get("summary"):
            for item in ic_correlation["summary"]:
                factor = item["existing_factor"]
                if factor not in factor_scores_dict:
                    factor_scores_dict[factor] = {
                        "factor": factor,
                        "cross_sectional_score": 0.0,
                        "ic_score": 0.0,
                        "return_score": 0.0,
                        "weighted_score": 0.0,
                        "details": {}
                    }
                # 使用IC相关性的绝对值作为得分
                factor_scores_dict[factor]["ic_score"] = abs(item["ic_correlation"])
                factor_scores_dict[factor]["details"]["ic_correlation"] = item["ic_correlation"]
                factor_scores_dict[factor]["details"]["ic_p_value"] = item["p_value"]
                factor_scores_dict[factor]["details"]["ic_significant"] = item["is_significant"]
        
        # 3. 处理收益率相关性（权重0.3）
        if return_correlation.get("summary"):
            for item in return_correlation["summary"]:
                factor = item["existing_factor"]
                if factor not in factor_scores_dict:
                    factor_scores_dict[factor] = {
                        "factor": factor,
                        "cross_sectional_score": 0.0,
                        "ic_score": 0.0,
                        "return_score": 0.0,
                        "weighted_score": 0.0,
                        "details": {}
                    }
                # 优先使用多空组合相关性，如果没有则使用多头相关性
                if "long_short_correlation" in item:
                    factor_scores_dict[factor]["return_score"] = abs(item["long_short_correlation"])
                    factor_scores_dict[factor]["details"]["long_short_correlation"] = item["long_short_correlation"]
                elif "long_correlation" in item:
                    factor_scores_dict[factor]["return_score"] = abs(item["long_correlation"])
                    factor_scores_dict[factor]["details"]["long_correlation"] = item["long_correlation"]
        
        # 4. 计算加权综合得分
        factor_scores_list = []
        for factor, score_data in factor_scores_dict.items():
            weighted_score = (
                score_data["cross_sectional_score"] * 0.3 +
                score_data["ic_score"] * 0.4 +
                score_data["return_score"] * 0.3
            )
            score_data["weighted_score"] = float(weighted_score)
            factor_scores_list.append(score_data)
        
        # 按得分降序排序
        factor_scores_list.sort(key=lambda x: x["weighted_score"], reverse=True)
        
        # 筛选高相关因子（超过阈值）
        high_correlation_factors = [
            item for item in factor_scores_list 
            if item["weighted_score"] >= threshold
        ]
        
        # 计算整体综合得分（所有因子的平均）
        if factor_scores_list:
            overall_score = sum(item["weighted_score"] for item in factor_scores_list) / len(factor_scores_list)
        else:
            overall_score = 0.0
        
        return {
            "factor_scores": factor_scores_list,
            "high_correlation_factors": high_correlation_factors,
            "overall_score": float(overall_score),
            "threshold": threshold,
            "total_factors": len(factor_scores_list)
        }

    @timer
    def _assess_overall_correlation(self, cross_sectional: Dict, 
                                     ic_correlation: Dict, 
                                     return_correlation: Dict,
                                     threshold: float = 0.7) -> Dict:
        """
        综合评估因子相关性
        
        显示所有相关性结果，由用户手动判断（不自动给出建议）
        高亮标识超过阈值（默认0.7）的因子
        
        参数:
            cross_sectional: 横截面相关性结果
            ic_correlation: IC序列相关性结果
            return_correlation: 收益率相关性结果
            threshold: 高相关阈值，默认0.7
        """
        # 计算综合得分
        score_result = self.calculate_weighted_correlation_score(
            cross_sectional, ic_correlation, return_correlation,
            threshold=threshold
        )
        
        # 获取各维度的最大相关性
        max_cross_corr = 0
        max_ic_corr = 0
        max_return_corr = 0
        
        if cross_sectional.get("summary"):
            max_cross_corr = max(abs(s["mean_corr"]) for s in cross_sectional["summary"])
        
        if ic_correlation.get("summary"):
            max_ic_corr = max(abs(s["ic_correlation"]) for s in ic_correlation["summary"])
        
        if return_correlation.get("summary"):
            ls_corrs = [abs(s.get("long_short_correlation", 0)) for s in return_correlation["summary"]]
            if ls_corrs:
                max_return_corr = max(ls_corrs)
        
        overall_score = score_result["overall_score"]
        high_corr_count = len(score_result["high_correlation_factors"])
        total_factors = score_result["total_factors"]
        
        # ============================================================
        # 选项D: 显示所有相关性结果，由用户手动判断（不自动给出建议）
        # 只高亮显示超过阈值的因子，不做自动判断
        # ============================================================
        
        # 标识超过阈值的因子（用于前端高亮显示）
        high_corr_factors_detail = []
        for factor_score in score_result.get("factor_scores", []):
            # 判断各个维度是否超过阈值
            details = factor_score.get("details", {})
            exceeds_threshold = {
                "factor": factor_score["factor"],
                "weighted_score": factor_score["weighted_score"],
                "is_high_correlation": factor_score["weighted_score"] >= threshold,
                "thresholds_exceeded": []
            }
            
            # 检查横截面相关性
            cross_corr = abs(details.get("cross_sectional_mean", 0))
            if cross_corr >= threshold:
                exceeds_threshold["thresholds_exceeded"].append({
                    "type": "cross_sectional",
                    "value": cross_corr,
                    "label": "横截面相关性"
                })
            
            # 检查IC序列相关性
            ic_corr = abs(details.get("ic_correlation", 0))
            if ic_corr >= threshold:
                exceeds_threshold["thresholds_exceeded"].append({
                    "type": "ic_correlation", 
                    "value": ic_corr,
                    "label": "IC序列相关性"
                })
            
            # 检查收益率相关性
            return_corr = abs(details.get("long_short_correlation", details.get("long_correlation", 0)))
            if return_corr >= threshold:
                exceeds_threshold["thresholds_exceeded"].append({
                    "type": "return_correlation",
                    "value": return_corr,
                    "label": "收益率相关性"
                })
            
            # 只要有任意一个维度超过阈值，或综合得分超过阈值，就标记为需要关注
            exceeds_threshold["needs_attention"] = (
                len(exceeds_threshold["thresholds_exceeded"]) > 0 or 
                factor_score["weighted_score"] >= threshold
            )
            
            high_corr_factors_detail.append(exceeds_threshold)
        
        # 按综合得分降序排序
        high_corr_factors_detail.sort(key=lambda x: x["weighted_score"], reverse=True)
        
        return {
            # 不再提供自动判断的风险等级和建议
            "mode": "manual_judgment",  # 标识这是手动判断模式
            "threshold": threshold,
            "overall_score": overall_score,
            "high_correlation_count": high_corr_count,
            "total_factors": total_factors,
            "details": {
                "max_cross_sectional_corr": float(max_cross_corr),
                "max_ic_corr": float(max_ic_corr),
                "max_return_corr": float(max_return_corr)
            },
            "score_breakdown": score_result,
            # 新增：所有因子的详细相关性信息（带高亮标识）
            "all_factors_correlation": high_corr_factors_detail,
            # 新增：超过阈值的因子列表（方便快速查看）
            "factors_exceeding_threshold": [
                f for f in high_corr_factors_detail if f["needs_attention"]
            ],
            # 提示信息（不是建议，只是说明）
            "note": f"以上展示所有因子的相关性结果，相关性超过{threshold}的因子已标记。请根据业务逻辑自行判断是否添加新因子。"
        }
    
    # =========================================================================
    # 简化版分析（重构后的主入口）
    # =========================================================================
    @timer
    def filter_factors_by_correlation_threshold(self, threshold: float = 0, method: str = "spearman") -> List[str]:
        """
        根据相关性阈值筛选因子
        
        计算每个因子与当前因子的平均横截面相关性，只保留超过阈值的因子
        
        参数:
            threshold: 相关性阈值，默认0表示保留所有因子
            method: 相关性计算方法 ('pearson' 或 'spearman')
        
        返回:
            筛选后的因子列表
        """
        if threshold <= 0:
            return self.existing_factor_cols.copy()
        
        # 计算横截面相关性
        cross_sectional = self.calculate_cross_sectional_correlation(method)
        
        # 筛选超过阈值的因子
        filtered_factors = []
        for item in cross_sectional.get("summary", []):
            mean_corr = abs(item["mean_corr"])
            if mean_corr >= threshold:
                filtered_factors.append(item["factor"])
        
        print(f"⚡ 阈值筛选: {len(self.existing_factor_cols)} 个因子 -> {len(filtered_factors)} 个 (阈值={threshold})")
        
        return filtered_factors
    @timer
    def get_simplified_correlation_analysis(self, 
                                            threshold: float = 0,
                                            method: str = "spearman",
                                            n_groups: int = 5) -> Dict:
        """
        获取简化版的因子相关性分析（重构后的主入口）
        
        只返回三张图表需要的数据：
        1. 因子相关性图表 - 横轴时间，纵轴相关性
        2. IC图表 - 横轴时间，纵轴IC
        3. 收益率图表 - 横轴时间，纵轴多空累计净值
        
        参数:
            threshold: 相关性阈值，只显示超过此阈值的因子，默认0表示显示所有
            method: 相关性/IC计算方法
            n_groups: 分组数量（用于计算多空收益）
        
        返回:
            简化的数据结构，只包含三张图表的数据
        """
        print(f"开始简化版因子相关性分析 (阈值={threshold})...")
        
        # 1. 根据阈值筛选因子
        filtered_factors = self.filter_factors_by_correlation_threshold(threshold, method)
        
        if len(filtered_factors) == 0:
            print(f" ️ 没有因子超过阈值 {threshold}，将显示所有因子")
            filtered_factors = self.existing_factor_cols.copy()
        
        # 2. 计算图表1：因子相关性（只计算筛选后的因子）
        print("  1. 计算因子相关性...")
        factor_correlation_chart = self._calculate_factor_correlation_chart(filtered_factors, method)
        
        # 3. 计算图表2：IC序列
        print("  2. 计算IC序列...")
        ic_chart = self._calculate_ic_chart(filtered_factors, method)
        
        # 4. 计算图表3：多空收益率
        print("  3. 计算多空收益率...")
        return_chart = self._calculate_return_chart(filtered_factors, n_groups)
        
        print("[OK] 简化版因子相关性分析完成!")
        
        return {
            # 图表1：因子相关性（横轴时间，纵轴相关性）
            "factor_correlation_chart": factor_correlation_chart,
            
            # 图表2：IC（横轴时间，纵轴IC）
            "ic_chart": ic_chart,
            
            # 图表3：多空收益率（横轴时间，纵轴累计净值）
            "return_chart": return_chart,
            
            # 元信息
            "new_factor": self.new_factor_col,
            "filtered_factors": filtered_factors,
            "threshold": threshold,
            "total_factors_in_table": len(self.existing_factor_cols)
        }
    @timer
    def _calculate_factor_correlation_chart(self, factors: List[str], method: str = "spearman") -> List[Dict]:
        """
        计算图表1的数据：因子相关性随时间变化
        
        每个时间点，计算每个筛选后的因子与当前因子的横截面相关性
        """
        results = {}
        
        for existing_factor in factors:
            def daily_corr(group, factor_col=existing_factor):
                if len(group) < 20:
                    return np.nan
                new_vals = group[self.new_factor_col]
                exist_vals = group[factor_col]
                valid_mask = new_vals.notna() & exist_vals.notna()
                if valid_mask.sum() < 20:
                    return np.nan
                return new_vals[valid_mask].corr(exist_vals[valid_mask], method=method)
            
            daily_series = self.data.groupby(self.date_col).apply(
                lambda g: daily_corr(g, existing_factor)
            )
            results[existing_factor] = daily_series
        
        # 转换为前端需要的格式
        daily_corr_df = pd.DataFrame(results)
        chart_data = []
        for date in daily_corr_df.index:
            row = {"trade_dt": date.strftime("%Y-%m-%d") if hasattr(date, 'strftime') else str(date)}
            for factor in factors:
                if factor in daily_corr_df.columns and pd.notna(daily_corr_df.loc[date, factor]):
                    row[factor] = float(daily_corr_df.loc[date, factor])
            chart_data.append(row)
        
        return chart_data
    @timer
    def _calculate_ic_chart(self, factors: List[str], method: str = "spearman") -> List[Dict]:
        """
        计算图表2的数据：各因子IC随时间变化
        
        包括当前因子和筛选后的因子的IC
        """
        def calc_daily_ic(group, factor_col):
            if len(group) < 20:
                return np.nan
            factor_vals = group[factor_col]
            return_vals = group["return"]
            valid_mask = factor_vals.notna() & return_vals.notna()
            if valid_mask.sum() < 20:
                return np.nan
            return factor_vals[valid_mask].corr(return_vals[valid_mask], method=method)
        
        # 包括当前因子和筛选后的因子
        all_factors = [self.new_factor_col] + factors
        ic_dict = {}
        
        for factor in all_factors:
            ic_series = self.data.groupby(self.date_col).apply(
                lambda g: calc_daily_ic(g, factor)
            )
            ic_dict[factor] = ic_series
        
        # 转换为前端需要的格式
        ic_df = pd.DataFrame(ic_dict)
        chart_data = []
        for date in ic_df.index:
            row = {"trade_dt": date.strftime("%Y-%m-%d") if hasattr(date, 'strftime') else str(date)}
            for factor in all_factors:
                if factor in ic_df.columns and pd.notna(ic_df.loc[date, factor]):
                    row[factor] = float(ic_df.loc[date, factor])
            chart_data.append(row)
        
        return chart_data
    @timer
    def _calculate_return_chart(self, factors: List[str], n_groups: int = 5) -> List[Dict]:
        """
        计算图表3的数据：各因子多空组合累计净值随时间变化
        
        包括当前因子和筛选后的因子的多空收益
        """
        all_factors = [self.new_factor_col] + factors
        long_short_returns = {}
        
        for factor in all_factors:
            def daily_qcut(x, n=n_groups):
                try:
                    if len(x) < n:
                        return pd.Series([np.nan] * len(x), index=x.index)
                    return pd.qcut(x, n, labels=[f"G{n-i}" for i in range(n)], duplicates='drop')
                except ValueError:
                    return pd.Series([np.nan] * len(x), index=x.index)
            
            temp_data = self.data[[self.date_col, self.ticker_col, factor, "return"]].copy()
            temp_data = temp_data.dropna(subset=[factor])
            
            if len(temp_data) == 0:
                continue
            
            temp_data["group"] = temp_data.groupby(self.date_col)[factor].transform(daily_qcut)
            temp_data["prev_group"] = temp_data.groupby(self.ticker_col)["group"].shift(1)
            valid_data = temp_data.dropna(subset=["prev_group", "return"])
            
            if len(valid_data) == 0:
                continue
            
            group_daily_ret = (
                valid_data.groupby([self.date_col, "prev_group"], observed=True)["return"]
                .mean()
                .unstack()
            )
            
            if group_daily_ret.empty:
                continue
            
            top_group = "G1"
            bottom_group = f"G{n_groups}"
            
            if top_group in group_daily_ret.columns and bottom_group in group_daily_ret.columns:
                long_short_returns[factor] = group_daily_ret[top_group] - group_daily_ret[bottom_group]
        
        # 转换为累计净值
        ls_ret_df = pd.DataFrame(long_short_returns)
        
        if ls_ret_df.empty:
            return []
        
        # 计算累计净值（从1开始）
        cum_nav_df = (1 + ls_ret_df).cumprod()
        
        # 转换为前端需要的格式
        chart_data = []
        for date in cum_nav_df.index:
            row = {"trade_dt": date.strftime("%Y-%m-%d") if hasattr(date, 'strftime') else str(date)}
            for factor in all_factors:
                if factor in cum_nav_df.columns and pd.notna(cum_nav_df.loc[date, factor]):
                    row[factor] = float(cum_nav_df.loc[date, factor])
            chart_data.append(row)
        
        return chart_data

