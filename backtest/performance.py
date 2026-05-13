"""
绩效评估模块
负责计算回测策略的各项绩效指标
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional


class PerformanceAnalyzer:
    """
    绩效分析器
    
    主要功能：
    1. 计算收益率指标：总收益、年化收益、累计收益
    2. 计算风险指标：波动率、最大回撤
    3. 计算风险调整后收益：夏普比率、卡尔玛比率
    4. 分年度统计
    """
    
    def __init__(self, backtest_data: pd.DataFrame, 
                 date_col: str = "trade_dt",
                 portfolio_col: str = "portfolio_value",
                 return_col: str = "strategy_return"):
        """
        初始化绩效分析器
        
        参数:
            backtest_data: 回测结果DataFrame
            date_col: 日期列名
            portfolio_col: 组合价值列名
            return_col: 收益率列名
        """
        self.data = backtest_data.copy()
        self.date_col = date_col
        self.portfolio_col = portfolio_col
        self.return_col = return_col
        
        # 确保日期列是datetime格式
        if not pd.api.types.is_datetime64_any_dtype(self.data[date_col]):
            self.data[date_col] = pd.to_datetime(self.data[date_col])
        
        # 排序
        self.data = self.data.sort_values(date_col).reset_index(drop=True)
        
        # 如果没有收益率列，从组合价值计算
        if return_col not in self.data.columns and portfolio_col in self.data.columns:
            self.data[return_col] = self.data[portfolio_col].pct_change()
        
        # 删除NaN值
        self.data = self.data.dropna(subset=[return_col])
    
    def calculate_total_return(self) -> float:
        """
        计算总收益率
        
        返回:
            float，总收益率（百分比）
        """
        if self.portfolio_col not in self.data.columns:
            return 0.0
        
        initial_value = self.data[self.portfolio_col].iloc[0]
        final_value = self.data[self.portfolio_col].iloc[-1]
        
        total_return = (final_value - initial_value) / initial_value * 100
        return total_return
    
    def calculate_annualized_return(self, periods_per_year: int = 252) -> float:
        """
        计算年化收益率
        
        参数:
            periods_per_year: 每年的交易周期数，默认252（交易日）
        
        返回:
            float，年化收益率（百分比）
        """
        if self.portfolio_col not in self.data.columns:
            return 0.0
        
        initial_value = self.data[self.portfolio_col].iloc[0]
        final_value = self.data[self.portfolio_col].iloc[-1]
        n_periods = len(self.data)
        
        # 年化收益率 = (期末/期初)^(252/总天数) - 1
        n_years = n_periods / periods_per_year
        annualized_return = (final_value / initial_value) ** (1 / n_years) - 1
        
        return annualized_return * 100
    
    def calculate_volatility(self, periods_per_year: int = 252) -> float:
        """
        计算收益率波动率（年化）
        
        参数:
            periods_per_year: 每年的交易周期数
        
        返回:
            float，年化波动率（百分比）
        """
        daily_std = self.data[self.return_col].std()
        annualized_vol = daily_std * np.sqrt(periods_per_year)
        
        return annualized_vol * 100
    
    def calculate_sharpe_ratio(self, risk_free_rate: float = 0.03, 
                               periods_per_year: int = 252) -> float:
        """
        计算夏普比率
        
        Sharpe Ratio = (年化收益率 - 无风险利率) / 年化波动率
        
        参数:
            risk_free_rate: 无风险利率，默认3%
            periods_per_year: 每年的交易周期数
        
        返回:
            float，夏普比率
        """
        ann_return = self.calculate_annualized_return(periods_per_year) / 100
        ann_vol = self.calculate_volatility(periods_per_year) / 100
        
        if ann_vol == 0:
            return 0.0
        
        sharpe = (ann_return - risk_free_rate) / ann_vol
        return sharpe
    
    def calculate_max_drawdown(self) -> Dict[str, float]:
        """
        计算最大回撤
        
        最大回撤 = max((历史最高点 - 当前点) / 历史最高点)
        
        返回:
            字典，包含最大回撤、回撤开始和结束日期
        """
        if self.portfolio_col not in self.data.columns:
            return {"max_drawdown": 0.0}
        
        # 计算累计最高点
        cummax = self.data[self.portfolio_col].cummax()
        
        # 计算回撤序列
        drawdown = (self.data[self.portfolio_col] - cummax) / cummax
        
        # 找到最大回撤
        max_dd = drawdown.min()
        max_dd_idx = drawdown.idxmin()
        
        # 找到回撤开始点（最大回撤前的最高点）
        if max_dd_idx > 0:
            dd_start_idx = self.data.loc[:max_dd_idx, self.portfolio_col].idxmax()
        else:
            dd_start_idx = 0
        
        return {
            "max_drawdown": abs(max_dd) * 100,  # 百分比
            "drawdown_start": self.data.loc[dd_start_idx, self.date_col],
            "drawdown_end": self.data.loc[max_dd_idx, self.date_col],
            "drawdown_series": drawdown
        }
    
    def calculate_calmar_ratio(self, periods_per_year: int = 252) -> float:
        """
        计算卡尔玛比率
        
        Calmar Ratio = 年化收益率 / 最大回撤
        
        参数:
            periods_per_year: 每年的交易周期数
        
        返回:
            float，卡尔玛比率
        """
        ann_return = self.calculate_annualized_return(periods_per_year)
        max_dd = self.calculate_max_drawdown()["max_drawdown"]
        
        if max_dd == 0:
            return 0.0
        
        calmar = ann_return / max_dd
        return calmar
    
    def calculate_win_rate(self) -> Dict[str, float]:
        """
        计算胜率相关指标
        
        返回:
            字典，包含胜率、盈亏比等
        """
        # 过滤掉NaN值
        returns = self.data[self.return_col].dropna()
        
        if len(returns) == 0:
            return {
                "win_rate": 0.0,
                "profit_loss_ratio": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0
            }
        
        # 盈利和亏损的天数
        wins = returns[returns > 0]
        losses = returns[returns < 0]
        
        win_rate = len(wins) / len(returns) * 100 if len(returns) > 0 else 0
        
        avg_win = wins.mean() if len(wins) > 0 else 0
        avg_loss = abs(losses.mean()) if len(losses) > 0 else 0
        
        profit_loss_ratio = avg_win / avg_loss if avg_loss != 0 else 0
        
        return {
            "win_rate": win_rate,
            "profit_loss_ratio": profit_loss_ratio,
            "avg_win": avg_win * 100,  # 百分比
            "avg_loss": avg_loss * 100,  # 百分比
            "winning_days": len(wins),
            "losing_days": len(losses)
        }
    
    def yearly_performance(self) -> pd.DataFrame:
        """
        分年度绩效统计
        
        返回:
            DataFrame，每年的收益率、夏普比率等指标
        """
        # 提取年份
        self.data["year"] = self.data[self.date_col].dt.year
        
        yearly_stats = []
        
        for year, year_data in self.data.groupby("year"):
            # 该年的总收益率
            if self.portfolio_col in year_data.columns:
                year_return = (
                    (year_data[self.portfolio_col].iloc[-1] - year_data[self.portfolio_col].iloc[0]) 
                    / year_data[self.portfolio_col].iloc[0] * 100
                )
            else:
                year_return = 0.0
            
            # 该年的波动率
            year_vol = year_data[self.return_col].std() * np.sqrt(252) * 100
            
            # 该年的夏普比率（简化）
            year_sharpe = (
                (year_data[self.return_col].mean() * 252 - 0.03) / 
                (year_data[self.return_col].std() * np.sqrt(252))
                if year_data[self.return_col].std() > 0 else 0
            )
            
            # 该年的最大回撤
            if self.portfolio_col in year_data.columns:
                cummax = year_data[self.portfolio_col].cummax()
                drawdown = (year_data[self.portfolio_col] - cummax) / cummax
                year_max_dd = abs(drawdown.min()) * 100
            else:
                year_max_dd = 0.0
            
            yearly_stats.append({
                "year": year,
                "return_%": year_return,
                "volatility_%": year_vol,
                "sharpe_ratio": year_sharpe,
                "max_drawdown_%": year_max_dd,
                "trading_days": len(year_data)
            })
        
        return pd.DataFrame(yearly_stats)
    
    def get_performance_summary(self, periods_per_year: int = 252, 
                               risk_free_rate: float = 0.03) -> Dict:
        """
        获取完整的绩效摘要
        
        参数:
            periods_per_year: 每年的交易周期数
            risk_free_rate: 无风险利率
        
        返回:
            字典，包含所有绩效指标
        """
        # 收益指标
        total_return = self.calculate_total_return()
        ann_return = self.calculate_annualized_return(periods_per_year)
        
        # 风险指标
        volatility = self.calculate_volatility(periods_per_year)
        max_dd = self.calculate_max_drawdown()
        
        # 风险调整后收益
        sharpe = self.calculate_sharpe_ratio(risk_free_rate, periods_per_year)
        calmar = self.calculate_calmar_ratio(periods_per_year)
        
        # 胜率指标
        win_stats = self.calculate_win_rate()
        
        summary = {
            "回测区间": f"{self.data[self.date_col].min().date()} 至 {self.data[self.date_col].max().date()}",
            "交易天数": len(self.data),
            "总收益率_%": round(total_return, 2),
            "年化收益率_%": round(ann_return, 2),
            "年化波动率_%": round(volatility, 2),
            "夏普比率": round(sharpe, 2),
            "最大回撤_%": round(max_dd["max_drawdown"], 2),
            "回撤开始": max_dd.get("drawdown_start", "N/A"),
            "回撤结束": max_dd.get("drawdown_end", "N/A"),
            "卡尔玛比率": round(calmar, 2),
            "胜率_%": round(win_stats["win_rate"], 2),
            "盈亏比": round(win_stats["profit_loss_ratio"], 2),
            "平均盈利_%": round(win_stats["avg_win"], 4),
            "平均亏损_%": round(win_stats["avg_loss"], 4),
        }
        
        return summary

