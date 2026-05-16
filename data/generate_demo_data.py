"""
生成演示用本地数据（Parquet 因子 + 宽表 CSV），无需 MySQL。

用法:
  python -m data.generate_demo_data

或在应用启动时由 ensure_demo_dataset() 自动创建缺失文件。
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

# 允许以脚本方式从项目根目录运行
_PKG_ROOT = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_PKG_ROOT)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _write_parquet_duckdb(df: pd.DataFrame, path: str) -> None:
    import duckdb

    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = duckdb.connect(database=":memory:")
    try:
        con.register("demo_factors", df)
        esc = path.replace("\\", "/").replace("'", "''")
        con.execute(f"COPY demo_factors TO '{esc}' (FORMAT PARQUET)")
    finally:
        con.close()


def generate_demo_dataset(
    *,
    n_days: int = 126,
    n_tickers: int = 24,
    seed: int = 42,
) -> None:
    from web.config import DATA_DIR, PARQUET_DIR

    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n_days, freq="B")

    tickers: list[str] = []
    for i in range(n_tickers):
        code = 600000 + i if i % 2 == 0 else 1 + i
        suf = "SH" if i % 2 == 0 else "SZ"
        tickers.append(f"{code:06d}.{suf}")

    rows: list[dict] = []
    for dt in dates:
        for t in tickers:
            # 截面秩相关友好的合成因子
            z = rng.normal()
            rows.append(
                {
                    "trade_dt": dt,
                    "ticker": t,
                    "LNCAP": float(10 + rng.normal() + 0.02 * (hash(t) % 97)),
                    "REV1": float(rng.normal()),
                    "MOM12": float(0.5 * z + 0.3 * rng.normal()),
                    "BM": float(abs(rng.normal())),
                    "ROE": float(rng.normal() * 0.05),
                    "VOL3M": float(abs(rng.lognormal(0, 0.3))),
                    "ACC": float(rng.normal() * 0.01),
                    "LEV": float(rng.uniform(0.2, 0.8)),
                }
            )

    fac_long = pd.DataFrame(rows)
    from data.parquet_io import factors_all_single_path

    parquet_path = factors_all_single_path(PARQUET_DIR)
    _write_parquet_duckdb(fac_long, parquet_path)

    # 宽表：每只股票独立随机游走价格（与 ticker 对齐）
    idx = len(dates)
    price_mat = np.zeros((idx, n_tickers), dtype=float)
    for j in range(n_tickers):
        price_mat[:, j] = 100.0 * np.exp(np.cumsum(rng.normal(0.00025, 0.014, idx)))
    close_w = pd.DataFrame(price_mat, columns=tickers)
    close_w.insert(0, "trade_dt", dates)
    open_w = close_w.copy()
    for t in tickers:
        open_w[t] = close_w[t] * (1.0 + rng.normal(0.0, 0.0025, idx))

    mkt_w = fac_long.pivot(index="trade_dt", columns="ticker", values="LNCAP").astype(float)
    mkt_w = (mkt_w * 1e8).reset_index()

    os.makedirs(DATA_DIR, exist_ok=True)
    close_w.to_csv(os.path.join(DATA_DIR, "adjclose_wide.csv"), index=False)
    open_w.to_csv(os.path.join(DATA_DIR, "adjopen_wide.csv"), index=False)
    mkt_w.to_csv(os.path.join(DATA_DIR, "market_value.csv"), index=False)

    # 本地基准：简单随机游走净值
    bench = pd.DataFrame(
        {
            "trade_dt": dates,
            "close": 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.008, len(dates)))),
        }
    )
    bench.to_csv(os.path.join(DATA_DIR, "000300.SH.csv"), index=False)

    try:
        from data.generate_sample_factor_csvs import write_all_sample_factor_csvs

        write_all_sample_factor_csvs(data_dir=DATA_DIR)
    except Exception as e:
        print(f"[演示数据] 示例因子 CSV 未生成: {e}")


def ensure_demo_dataset() -> bool:
    """
    若缺少核心文件则生成演示数据。
    返回 True 表示本次运行后因子 Parquet 已存在（含新生成或原本就有）。
    """
    from web.config import DATA_DIR, PARQUET_DIR
    from data.parquet_io import has_factors_all_parquet

    need = not has_factors_all_parquet(PARQUET_DIR)
    for name in ("adjclose_wide.csv", "adjopen_wide.csv", "market_value.csv"):
        if not os.path.isfile(os.path.join(DATA_DIR, name)):
            need = True
            break
    if not need:
        try:
            from data.generate_sample_factor_csvs import ensure_sample_factor_csvs

            ensure_sample_factor_csvs()
        except Exception:
            pass
        return True
    print("[演示数据] 未检测到完整本地数据，正在生成 demo 数据集（无需数据库）…")
    generate_demo_dataset()
    print(f"[演示数据] 已写入: {PARQUET_DIR} 与 {DATA_DIR}")
    return has_factors_all_parquet(PARQUET_DIR)


if __name__ == "__main__":
    ensure_demo_dataset()
