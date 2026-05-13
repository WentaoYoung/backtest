"""
单因子回测框架
提供因子分析、策略回测、绩效评估、可视化等功能
"""

__version__ = "1.0.0"

from .factor_analyzer import FactorAnalyzer
from .backtest_engine import BacktestEngine
from .performance import PerformanceAnalyzer
from .visualizer import Visualizer

__all__ = [
    "FactorAnalyzer",
    "BacktestEngine",
    "PerformanceAnalyzer",
    "Visualizer",
]

