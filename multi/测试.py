import pandas as pd
import numpy as np
from akquant import Strategy, run_backtest

# 1. 准备数据 (示例使用随机数据)
# 实际场景可使用 pd.read_csv("data.csv")
def generate_data():
    dates = pd.date_range(start="2023-01-01", end="2023-12-31")
    n = len(dates)
    price = 100 * np.cumprod(1 + np.random.normal(0.0005, 0.02, n))
    return pd.DataFrame({
        "date": dates,
        "open": price, "high": price * 1.01, "low": price * 0.99, "close": price,
        "volume": 10000,
        "symbol": "600000"
    })

# 2. 定义策略
class MyStrategy(Strategy):
    def on_bar(self, bar):
        # 简单的策略逻辑 (示例)
        # 实际回测推荐使用 IndicatorSet 进行向量化计算
        position = self.get_position(bar.symbol)
        if position == 0:
            self.buy(symbol=bar.symbol, quantity=100)
        elif position > 0:
            self.sell(symbol=bar.symbol, quantity=100)

# 3. 运行回测
df = generate_data()
result = run_backtest(
    strategy=MyStrategy,  # 传递类或实例
    data=df,              # 显式传入数据
    symbols="600000",      # 浦发银行
    initial_cash=500_000.0,       # 初始资金
    commission_rate=0.0003     # 万三佣金
)

# 4. 查看结果
print(f"Total Return: {result.metrics.total_return_pct:.2f}%")
print(f"Sharpe Ratio: {result.metrics.sharpe_ratio:.2f}")
print(f"Max Drawdown: {result.metrics.max_drawdown_pct:.2f}%")

# 5. 获取详细数据 (DataFrame)
# 绩效指标表
print(result.metrics_df)
# 交易记录表
print(result.trades_df)
# 每日持仓表
print(result.positions_df)