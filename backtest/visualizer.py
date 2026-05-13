"""
可视化模块
负责生成回测相关的图表
"""

import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from typing import Optional, List, Tuple
import matplotlib.dates as mdates


# 使用英文标签，避免中文乱码
plt.rcParams['font.family'] = 'DejaVu Sans'
plt.rcParams['axes.unicode_minus'] = False


class Visualizer:
    """
    可视化工具类
    
    主要功能：
    1. 绘制因子值时间序列图
    2. 绘制组合净值曲线
    3. 绘制回撤曲线
    4. 绘制收益分布直方图
    5. 绘制分年度收益柱状图
    """
    
    def __init__(self, figsize: Tuple[int, int] = (12, 6), style: str = "seaborn-v0_8-darkgrid"):
        """
        初始化可视化工具
        
        参数:
            figsize: 图表大小，默认(12, 6)
            style: 图表风格
        """
        self.figsize = figsize
        
        # 设置样式（兼容不同matplotlib版本）
        try:
            plt.style.use(style)
        except:
            try:
                plt.style.use("seaborn-darkgrid")
            except:
                pass  # 使用默认样式
    
    def plot_factor_series(self, data: pd.DataFrame, 
                          date_col: str = "trade_dt",
                          factor_col: str = "factor",
                          title: str = "Factor Value Time Series",
                          save_path: Optional[str] = None) -> None:
        """
        绘制因子值时间序列图
        
        参数:
            data: DataFrame，包含日期和因子值
            date_col: 日期列名
            factor_col: 因子列名
            title: 图表标题
            save_path: 保存路径，如果为None则不保存
        """
        fig, ax = plt.subplots(figsize=self.figsize)
        
        # 绘制因子值
        ax.plot(data[date_col], data[factor_col], linewidth=1.5, color='#1f77b4', label='Factor Value')
        
        # 添加均值线
        mean_value = data[factor_col].mean()
        ax.axhline(y=mean_value, color='r', linestyle='--', linewidth=1, 
                   label=f'Mean: {mean_value:.2f}')
        
        # 设置标题和标签
        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xlabel('Date', fontsize=12)
        ax.set_ylabel('Factor Value', fontsize=12)
        ax.legend(loc='best')
        ax.grid(True, alpha=0.3)
        
        # 格式化x轴日期
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.xticks(rotation=45)
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"图表已保存至: {save_path}")
        
        plt.show()
    
    def plot_portfolio_value(self, data: pd.DataFrame,
                            date_col: str = "trade_dt",
                            portfolio_col: str = "portfolio_value",
                            benchmark_col: Optional[str] = None,
                            title: str = "Portfolio NAV Curve",
                            save_path: Optional[str] = None) -> None:
        """
        绘制投资组合净值曲线
        
        参数:
            data: DataFrame，包含日期和组合价值
            date_col: 日期列名
            portfolio_col: 组合价值列名
            benchmark_col: 基准列名（可选）
            title: 图表标题
            save_path: 保存路径
        """
        fig, ax = plt.subplots(figsize=self.figsize)
        
        # 归一化到初始值为1，便于比较
        initial_value = data[portfolio_col].iloc[0]
        normalized_portfolio = data[portfolio_col] / initial_value
        
        # 绘制策略净值
        ax.plot(data[date_col], normalized_portfolio, 
                linewidth=2, color='#2ca02c', label='Strategy NAV')
        
        # 如果有基准，也绘制基准
        if benchmark_col and benchmark_col in data.columns:
            initial_benchmark = data[benchmark_col].iloc[0]
            normalized_benchmark = data[benchmark_col] / initial_benchmark
            ax.plot(data[date_col], normalized_benchmark,
                   linewidth=2, color='#ff7f0e', linestyle='--', label='Benchmark')
        
        # 添加1.0基准线
        ax.axhline(y=1.0, color='gray', linestyle=':', linewidth=1, alpha=0.5)
        
        # 设置标题和标签
        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xlabel('Date', fontsize=12)
        ax.set_ylabel('NAV (Normalized)', fontsize=12)
        ax.legend(loc='best')
        ax.grid(True, alpha=0.3)
        
        # 格式化x轴日期
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.xticks(rotation=45)
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"图表已保存至: {save_path}")
        
        plt.show()
    
    def plot_drawdown(self, data: pd.DataFrame,
                     date_col: str = "trade_dt",
                     portfolio_col: str = "portfolio_value",
                     title: str = "回撤曲线",
                     save_path: Optional[str] = None) -> None:
        """
        绘制回撤曲线
        
        参数:
            data: DataFrame，包含日期和组合价值
            date_col: 日期列名
            portfolio_col: 组合价值列名
            title: 图表标题
            save_path: 保存路径
        """
        # 计算回撤序列
        cummax = data[portfolio_col].cummax()
        drawdown = (data[portfolio_col] - cummax) / cummax * 100  # 百分比
        
        fig, ax = plt.subplots(figsize=self.figsize)
        
        # 绘制回撤
        ax.fill_between(data[date_col], drawdown, 0, 
                        color='#d62728', alpha=0.3, label='回撤')
        ax.plot(data[date_col], drawdown, 
                linewidth=1.5, color='#d62728')
        
        # 标注最大回撤
        max_dd = drawdown.min()
        max_dd_date = data.loc[drawdown.idxmin(), date_col]
        ax.scatter([max_dd_date], [max_dd], color='red', s=100, zorder=5)
        ax.annotate(f'最大回撤: {max_dd:.2f}%', 
                   xy=(max_dd_date, max_dd),
                   xytext=(10, -20), textcoords='offset points',
                   bbox=dict(boxstyle='round,pad=0.5', fc='yellow', alpha=0.7),
                   arrowprops=dict(arrowstyle='->', connectionstyle='arc3,rad=0'))
        
        # 设置标题和标签
        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xlabel('日期', fontsize=12)
        ax.set_ylabel('回撤 (%)', fontsize=12)
        ax.legend(loc='best')
        ax.grid(True, alpha=0.3)
        
        # 格式化x轴日期
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.xticks(rotation=45)
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"图表已保存至: {save_path}")
        
        plt.show()
    
    def plot_returns_distribution(self, data: pd.DataFrame,
                                 return_col: str = "strategy_return",
                                 bins: int = 50,
                                 title: str = "收益率分布",
                                 save_path: Optional[str] = None) -> None:
        """
        绘制收益率分布直方图
        
        参数:
            data: DataFrame，包含收益率
            return_col: 收益率列名
            bins: 直方图bins数量
            title: 图表标题
            save_path: 保存路径
        """
        fig, ax = plt.subplots(figsize=self.figsize)
        
        # 过滤NaN值并转换为百分比
        returns = data[return_col].dropna() * 100
        
        # 绘制直方图
        n, bins_edges, patches = ax.hist(returns, bins=bins, 
                                         color='#1f77b4', alpha=0.7, 
                                         edgecolor='black', linewidth=0.5)
        
        # 添加正态分布拟合曲线
        mu = returns.mean()
        sigma = returns.std()
        x = np.linspace(returns.min(), returns.max(), 100)
        y = ((1 / (np.sqrt(2 * np.pi) * sigma)) * 
             np.exp(-0.5 * (1 / sigma * (x - mu))**2))
        y = y * len(returns) * (bins_edges[1] - bins_edges[0])  # 缩放到直方图尺度
        
        ax.plot(x, y, 'r--', linewidth=2, label=f'正态分布 (μ={mu:.4f}%, σ={sigma:.4f}%)')
        
        # 添加均值线
        ax.axvline(x=mu, color='green', linestyle='--', linewidth=2, 
                  label=f'均值: {mu:.4f}%')
        
        # 设置标题和标签
        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xlabel('收益率 (%)', fontsize=12)
        ax.set_ylabel('频数', fontsize=12)
        ax.legend(loc='best')
        ax.grid(True, alpha=0.3, axis='y')
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"图表已保存至: {save_path}")
        
        plt.show()
    
    def plot_yearly_returns(self, yearly_data: pd.DataFrame,
                           year_col: str = "year",
                           return_col: str = "return_%",
                           title: str = "分年度收益率",
                           save_path: Optional[str] = None) -> None:
        """
        绘制分年度收益率柱状图
        
        参数:
            yearly_data: DataFrame，包含年度统计数据
            year_col: 年份列名
            return_col: 收益率列名
            title: 图表标题
            save_path: 保存路径
        """
        fig, ax = plt.subplots(figsize=self.figsize)
        
        years = yearly_data[year_col]
        returns = yearly_data[return_col]
        
        # 根据正负收益设置颜色
        colors = ['#2ca02c' if r >= 0 else '#d62728' for r in returns]
        
        # 绘制柱状图
        bars = ax.bar(years, returns, color=colors, alpha=0.7, edgecolor='black')
        
        # 在柱子上标注数值
        for bar in bars:
            height = bar.get_height()
            ax.text(bar.get_x() + bar.get_width()/2., height,
                   f'{height:.2f}%',
                   ha='center', va='bottom' if height >= 0 else 'top',
                   fontsize=9)
        
        # 添加零线
        ax.axhline(y=0, color='black', linewidth=1)
        
        # 设置标题和标签
        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xlabel('年份', fontsize=12)
        ax.set_ylabel('收益率 (%)', fontsize=12)
        ax.grid(True, alpha=0.3, axis='y')
        
        # 设置x轴刻度
        ax.set_xticks(years)
        ax.set_xticklabels(years, rotation=45)
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"图表已保存至: {save_path}")
        
        plt.show()
    
    def plot_group_nav_dict(self, group_nav_dict: dict[str, pd.DataFrame],
                      date_col: str = "trade_dt",
                      nav_col: str = "nav",
                      highlight_ls: bool = True,
                      title: str = "因子分组净值曲线",
                      save_path: Optional[str] = None) -> None:
        """
        绘制因子分组净值曲线（核心图表）- 字典输入版本
        
        展示各分位数组的净值走势，验证因子单调性
        
        参数:
            group_nav_dict: 字典，key为组名，value为包含日期和净值的DataFrame
            date_col: 日期列名
            nav_col: 净值列名
            highlight_ls: 是否高亮Long-Short曲线
            title: 图表标题
            save_path: 保存路径
        """
        fig, ax = plt.subplots(figsize=(14, 8))
        
        # 定义颜色映射（从冷色到暖色，体现因子从低到高）
        colors = ['#d62728', '#ff7f0e', '#bcbd22', '#2ca02c', '#1f77b4']  # 红-橙-黄-绿-蓝
        
        # 绘制各组净值曲线
        for i, (group_name, group_data) in enumerate(group_nav_dict.items()):
            if group_data is None:
                continue
            
            # Long-Short特殊处理
            if group_name == "Long-Short":
                if highlight_ls:
                    # 归一化到初始值为1
                    initial_nav = group_data[nav_col].iloc[0]
                    normalized_nav = group_nav_data[group].values / initial_value
                    
                    ax.plot(group_data[date_col], normalized_nav, 
                           linewidth=3, color='black', linestyle='--',
                           label=f'{group_name} (多空组合)', alpha=0.8, zorder=10)
                continue
            
            # 归一化到初始值为1
            initial_nav = group_data[nav_col].iloc[0]
            normalized_nav = group_data[nav_col] / initial_nav
            
            # 根据组名选择颜色
            q_num = int(group_name[1]) if len(group_name) > 1 else i
            color_idx = min(q_num - 1, len(colors) - 1)
            
            ax.plot(group_data[date_col], normalized_nav, 
                   linewidth=2, color=colors[color_idx],
                   label=f'{group_name} (因子{"最低" if q_num == 1 else "最高" if q_num == 5 else ""}组)',
                   alpha=0.8)
        
        # 添加1.0基准线
        ax.axhline(y=1.0, color='gray', linestyle=':', linewidth=1, alpha=0.5)
        
        # 设置标题和标签
        ax.set_title(title, fontsize=16, fontweight='bold', pad=20)
        ax.set_xlabel('日期', fontsize=13)
        ax.set_ylabel('净值（归一化，初始=1）', fontsize=13)
        ax.legend(loc='best', fontsize=11, framealpha=0.9)
        ax.grid(True, alpha=0.3)
        
        # 格式化x轴日期
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.xticks(rotation=45)
        
        # 添加说明文字
        textstr = '说明：曲线从下到上应体现因子单调性\n若Q5>Q4>...>Q1，说明因子有效'
        props = dict(boxstyle='round', facecolor='wheat', alpha=0.3)
        ax.text(0.02, 0.98, textstr, transform=ax.transAxes, fontsize=10,
               verticalalignment='top', bbox=props)
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"图表已保存至: {save_path}")
        
        plt.show()
    
    def plot_group_stats_bar(self, group_stats: pd.DataFrame,
                             metric_col: str = "年化收益率_%",
                             title: str = "因子分组收益率对比",
                             save_path: Optional[str] = None) -> None:
        """
        绘制分组指标对比图（柱状图）
        
        参数:
            group_stats: DataFrame，分组统计数据
            metric_col: 要对比的指标列名
            title: 图表标题
            save_path: 保存路径
        """
        fig, ax = plt.subplots(figsize=(12, 6))
        
        # 过滤掉Long-Short（单独处理）
        regular_groups = group_stats[group_stats["分组"] != "Long-Short"]
        ls_group = group_stats[group_stats["分组"] == "Long-Short"]
        
        groups = regular_groups["分组"]
        values = regular_groups[metric_col]
        
        # 根据正负设置颜色
        colors = ['#2ca02c' if v >= 0 else '#d62728' for v in values]
        
        # 绘制柱状图
        bars = ax.bar(groups, values, color=colors, alpha=0.7, edgecolor='black', linewidth=1.5)
        
        # 如果有Long-Short，单独绘制
        if len(ls_group) > 0:
            ls_value = ls_group[metric_col].values[0]
            ls_color = '#1f77b4' if ls_value >= 0 else '#d62728'
            ax.bar("Long-Short", ls_value, color=ls_color, alpha=0.9, 
                  edgecolor='black', linewidth=2, hatch='//')
        
        # 在柱子上标注数值
        for i, (bar, value) in enumerate(zip(bars, values)):
            height = bar.get_height()
            ax.text(bar.get_x() + bar.get_width()/2., height,
                   f'{value:.2f}%',
                   ha='center', va='bottom' if height >= 0 else 'top',
                   fontsize=11, fontweight='bold')
        
        # 如果有Long-Short，也标注
        if len(ls_group) > 0:
            ls_value = ls_group[metric_col].values[0]
            ax.text(len(bars), ls_value,
                   f'{ls_value:.2f}%',
                   ha='center', va='bottom' if ls_value >= 0 else 'top',
                   fontsize=11, fontweight='bold')
        
        # 添加零线
        ax.axhline(y=0, color='black', linewidth=1.5)
        
        # 设置标题和标签
        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xlabel('分组', fontsize=12)
        ax.set_ylabel(metric_col, fontsize=12)
        ax.grid(True, alpha=0.3, axis='y')
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"图表已保存至: {save_path}")
        
        plt.show()
    
    def plot_comprehensive_report(self, data: pd.DataFrame,
                                 date_col: str = "trade_dt",
                                 factor_col: str = "factor",
                                 portfolio_col: str = "portfolio_value",
                                 return_col: str = "strategy_return",
                                 save_path: Optional[str] = None) -> None:
        """
        绘制综合报告（4个子图）
        
        参数:
            data: DataFrame，完整的回测数据
            date_col: 日期列名
            factor_col: 因子列名
            portfolio_col: 组合价值列名
            return_col: 收益率列名
            save_path: 保存路径
        """
        fig = plt.figure(figsize=(16, 12))
        
        # 1. 因子值时间序列
        ax1 = plt.subplot(2, 2, 1)
        ax1.plot(data[date_col], data[factor_col], linewidth=1.5, color='#1f77b4')
        ax1.set_title('因子值时间序列', fontsize=12, fontweight='bold')
        ax1.set_xlabel('日期')
        ax1.set_ylabel('因子值')
        ax1.grid(True, alpha=0.3)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45)
        
        # 2. 组合净值曲线
        ax2 = plt.subplot(2, 2, 2)
        initial_value = data[portfolio_col].iloc[0]
        normalized_portfolio = data[portfolio_col] / initial_value
        ax2.plot(data[date_col], normalized_portfolio, linewidth=2, color='#2ca02c')
        ax2.set_title('投资组合净值曲线', fontsize=12, fontweight='bold')
        ax2.set_xlabel('日期')
        ax2.set_ylabel('净值（归一化）')
        ax2.grid(True, alpha=0.3)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45)
        
        # 3. 回撤曲线
        ax3 = plt.subplot(2, 2, 3)
        cummax = data[portfolio_col].cummax()
        drawdown = (data[portfolio_col] - cummax) / cummax * 100
        ax3.fill_between(data[date_col], drawdown, 0, color='#d62728', alpha=0.3)
        ax3.plot(data[date_col], drawdown, linewidth=1.5, color='#d62728')
        ax3.set_title('回撤曲线', fontsize=12, fontweight='bold')
        ax3.set_xlabel('日期')
        ax3.set_ylabel('回撤 (%)')
        ax3.grid(True, alpha=0.3)
        ax3.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.setp(ax3.xaxis.get_majorticklabels(), rotation=45)
        
        # 4. 收益率分布
        ax4 = plt.subplot(2, 2, 4)
        returns = data[return_col].dropna() * 100
        ax4.hist(returns, bins=50, color='#1f77b4', alpha=0.7, edgecolor='black')
        ax4.axvline(x=returns.mean(), color='red', linestyle='--', linewidth=2)
        ax4.set_title('收益率分布', fontsize=12, fontweight='bold')
        ax4.set_xlabel('收益率 (%)')
        ax4.set_ylabel('频数')
        ax4.grid(True, alpha=0.3, axis='y')
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"图表已保存至: {save_path}")
        
        plt.show()
    
    def plot_group_nav(self, group_nav_data: pd.DataFrame,
                      date_col: str = "trade_dt",
                      title: str = "Factor Group NAV Comparison",
                      save_path: Optional[str] = None) -> None:
        """
        绘制因子分组净值曲线对比图
        
        参数:
            group_nav_data: DataFrame，包含日期和各组净值
            date_col: 日期列名
            title: 图表标题
            save_path: 保存路径
        """
        fig, ax = plt.subplots(figsize=self.figsize)
        
        # 获取所有分组列（除了日期列）
        group_cols = [col for col in group_nav_data.columns if col != date_col]
        
        # 定义颜色（从低到高：红到绿）
        colors = ['#d62728', '#ff7f0e', '#bcbd22', '#2ca02c', '#17becf']
        
        # 归一化净值（初始值=1）
        for idx, group in enumerate(group_cols):
            initial_value = group_nav_data[group].iloc[0]
            normalized_nav = group_nav_data[group].values / initial_value
            
            color = colors[idx % len(colors)]
            ax.plot(group_nav_data[date_col], normalized_nav, 
                   linewidth=2, label=group, color=color)
        
        # 添加基准线
        ax.axhline(y=1.0, color='gray', linestyle=':', linewidth=1, alpha=0.5)
        
        # 设置标题和标签
        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xlabel('Date', fontsize=12)
        ax.set_ylabel('NAV (Normalized)', fontsize=12)
        ax.legend(loc='best', fontsize=10)
        ax.grid(True, alpha=0.3)
        
        # 格式化x轴日期
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.xticks(rotation=45)
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"图表已保存至: {save_path}")
        
        plt.show()
    
    def plot_long_short_nav(self, ls_nav_data: pd.DataFrame,
                           date_col: str = "trade_dt",
                           nav_col: str = "Long-Short",
                           title: str = "Long-Short Portfolio NAV",
                           save_path: Optional[str] = None) -> None:
        """
        绘制Long-Short组合净值曲线
        
        参数:
            ls_nav_data: DataFrame，包含日期和Long-Short净值
            date_col: 日期列名
            nav_col: 净值列名
            title: 图表标题
            save_path: 保存路径
        """
        fig, ax = plt.subplots(figsize=self.figsize)
        
        # 归一化净值
        initial_value = ls_nav_data[nav_col].iloc[0]
        normalized_nav = ls_nav_data[nav_col] / initial_value
        
        # 绘制净值曲线
        ax.plot(ls_nav_data[date_col], normalized_nav, 
               linewidth=2.5, color='#9467bd', label='Long-Short')
        
        # 添加基准线
        ax.axhline(y=1.0, color='gray', linestyle=':', linewidth=1, alpha=0.5)
        
        # 标注最终收益
        final_return = (normalized_nav.iloc[-1] - 1) * 100
        ax.text(0.02, 0.98, f'Cumulative Return: {final_return:.2f}%',
               transform=ax.transAxes, fontsize=12,
               verticalalignment='top',
               bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
        
        # 设置标题和标签
        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xlabel('Date', fontsize=12)
        ax.set_ylabel('NAV (Normalized)', fontsize=12)
        ax.legend(loc='best', fontsize=10)
        ax.grid(True, alpha=0.3)
        
        # 格式化x轴日期
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.xticks(rotation=45)
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"图表已保存至: {save_path}")
        
        plt.show()
    
    def plot_group_comparison(self, group_nav_data: pd.DataFrame,
                             ls_nav_data: pd.DataFrame,
                             yearly_ic_data: Optional[pd.DataFrame] = None,
                             date_col: str = "trade_dt",
                             save_path: Optional[str] = None) -> None:
        """
        绘制分组对比综合图（2x2布局）
        
        参数:
            group_nav_data: DataFrame，各组净值数据
            ls_nav_data: DataFrame，Long-Short净值数据
            yearly_ic_data: DataFrame，分年度IC数据（包含 year 和 IC_pearson 列）
            date_col: 日期列名
            save_path: 保存路径
        """
        fig = plt.figure(figsize=(16, 10))
        
        # 获取所有分组列
        group_cols = [col for col in group_nav_data.columns if col != date_col]
        colors = ['#d62728', '#ff7f0e', '#bcbd22', '#2ca02c', '#17becf']
        
        # 1. 各组净值曲线（左上）
        ax1 = plt.subplot(2, 2, 1)
        for idx, group in enumerate(group_cols):
            initial_value = group_nav_data[group].iloc[0]
            normalized_nav = group_nav_data[group].values/ initial_value
            color = colors[idx % len(colors)]
            ax1.plot(group_nav_data[date_col], normalized_nav, 
                    linewidth=2, label=group, color=color)
        ax1.axhline(y=1.0, color='gray', linestyle=':', linewidth=1, alpha=0.5)
        ax1.set_title('Group NAV Curves', fontsize=12, fontweight='bold')
        ax1.set_xlabel('Date')
        ax1.set_ylabel('NAV (Normalized)')
        ax1.legend(loc='best', fontsize=9)
        ax1.grid(True, alpha=0.3)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45)
        
        # 2. Long-Short净值曲线（右上）
        ax2 = plt.subplot(2, 2, 2)
        initial_ls = ls_nav_data["Long-Short"].iloc[0]
        normalized_ls = ls_nav_data["Long-Short"] / initial_ls
        ax2.plot(ls_nav_data[date_col], normalized_ls, 
                linewidth=2.5, color='#9467bd', label='Long-Short')
        ax2.axhline(y=1.0, color='gray', linestyle=':', linewidth=1, alpha=0.5)
        final_return = (normalized_ls.iloc[-1] - 1) * 100
        ax2.text(0.02, 0.98, f'Cum. Return: {final_return:.2f}%',
                transform=ax2.transAxes, fontsize=10,
                verticalalignment='top',
                bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
        ax2.set_title('Long-Short NAV', fontsize=12, fontweight='bold')
        ax2.set_xlabel('Date')
        ax2.set_ylabel('NAV (Normalized)')
        ax2.legend(loc='best')
        ax2.grid(True, alpha=0.3)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45)
        
        # 3. 各组累计收益对比（左下）
        ax3 = plt.subplot(2, 2, 3)
        final_returns = []
        for group in group_cols:
            initial = group_nav_data[group].iloc[0]
            final = group_nav_data[group].iloc[-1]
            ret = (final / initial - 1) * 100
            final_returns.append(ret)
        
        bars = ax3.bar(group_cols, final_returns, 
                      color=[colors[i % len(colors)] for i in range(len(group_cols))],
                      alpha=0.7, edgecolor='black')
        
        # 标注数值
        for bar in bars:
            height = bar.get_height()
            ax3.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.1f}%',
                    ha='center', va='bottom' if height >= 0 else 'top',
                    fontsize=9)
        
        ax3.axhline(y=0, color='black', linewidth=1)
        ax3.set_title('Cumulative Returns by Group', fontsize=12, fontweight='bold')
        ax3.set_xlabel('Group')
        ax3.set_ylabel('Cumulative Return (%)')
        ax3.grid(True, alpha=0.3, axis='y')
        
        # 4. 分年度IC柱状图（右下）- 替换原分布图
        ax4 = plt.subplot(2, 2, 4)
        if yearly_ic_data is not None and not yearly_ic_data.empty:
            years = yearly_ic_data["year"]
            ic_values = yearly_ic_data["IC_pearson"]
            
            # 根据正负设置颜色
            ic_colors = ['#d62728' if ic < 0 else '#2ca02c' for ic in ic_values]
            
            ic_bars = ax4.bar(years, ic_values, color=ic_colors, alpha=0.7, edgecolor='black')
            
            # 标注数值
            for bar in ic_bars:
                height = bar.get_height()
                ax4.text(bar.get_x() + bar.get_width()/2., height,
                        f'{height:.3f}',
                        ha='center', va='bottom' if height >= 0 else 'top',
                        fontsize=9)
            
            ax4.axhline(y=0, color='black', linewidth=1)
            ax4.set_title('Yearly IC (Pearson)', fontsize=12, fontweight='bold')
            ax4.set_xlabel('Year')
            ax4.set_ylabel('IC Value')
            ax4.set_xticks(years) # 确保每年都显示刻度
            ax4.grid(True, alpha=0.3, axis='y')
        else:
            ax4.text(0.5, 0.5, 'No Yearly IC Data Provided', 
                    ha='center', va='center', fontsize=12)
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"图表已保存至: {save_path}")
        
        plt.show()

