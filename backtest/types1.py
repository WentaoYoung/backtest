from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd


class WeightMethod(str, Enum):
    """支持的加权方式"""

    EQUAL = "equal"
    MKT_VAL = "mkt_val"
    FACTOR_SCORE = "factor_score"


@dataclass(frozen=True)
class BacktestConfig:
    """回测参数配置"""

    factor_col: str = "factor"
    price_col: str = "adj_open"
    date_col: str = "trade_dt"
    ticker_col: str = "ticker"
    mkt_val_col: str = "market_value"


@dataclass
class MatrixBundle:
    """矩阵化计算中间结果"""

    factor_mat: np.ndarray
    return_mat: np.ndarray
    mkt_val_mat: Optional[np.ndarray]
    dates: pd.Index
    tickers: pd.Index
