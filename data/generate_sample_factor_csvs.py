"""
生成可与本地 adjopen_wide 对齐的示例因子 CSV，用于网页上传：

- 主页「CSV 因子回测」：宽表或长表
- /correlation 页：上传「新因子」宽表（多文件 = 多个新因子）
- /multi_factor 页：每个因子一个宽表 CSV

用法（在项目根目录）:
  python -m data.generate_sample_factor_csvs

依赖已有 data/adjopen_wide.csv（由 generate_demo_data 生成）。
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_PKG_ROOT = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_PKG_ROOT)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

SAMPLE_DIR = "sample_factor_csvs"


def _load_adjopen_wide(data_dir: str) -> tuple[pd.DataFrame, list[str]]:
    path = os.path.join(data_dir, "adjopen_wide.csv")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"缺少 {path}，请先运行: python -m data.generate_demo_data")
    df = pd.read_csv(path)
    first = df.columns[0]
    if first in ("", "Unnamed: 0", "trade_dt", "date"):
        df = df.rename(columns={first: "trade_dt"})
    df["trade_dt"] = pd.to_datetime(df["trade_dt"])
    tickers = [c for c in df.columns if c != "trade_dt"]
    return df, [str(t).strip() for t in tickers]


def write_all_sample_factor_csvs(*, data_dir: str | None = None, seed: int = 7) -> str:
    from web.config import DATA_DIR

    base = data_dir or DATA_DIR
    out_dir = os.path.join(base, SAMPLE_DIR)
    os.makedirs(out_dir, exist_ok=True)

    px, tickers = _load_adjopen_wide(base)
    mat = px[tickers].to_numpy(dtype=float)
    n, k = mat.shape
    rng = np.random.default_rng(seed)

    # 合成「动量」：与短期收益正相关 + 噪声
    mom = np.zeros_like(mat)
    for i in range(1, n):
        prev = mat[i - 1]
        cur = mat[i]
        ret = (cur - prev) / np.clip(prev, 1e-6, None)
        mom[i] = ret * 50.0 + rng.normal(0, 0.35, size=k)
    mom[0] = rng.normal(0, 1.0, size=k)
    wide_mom = pd.DataFrame(mom, columns=tickers)
    wide_mom.insert(0, "trade_dt", px["trade_dt"].dt.strftime("%Y-%m-%d"))
    p_mom = os.path.join(out_dir, "demo_momentum_wide.csv")
    wide_mom.to_csv(p_mom, index=False, encoding="utf-8-sig")

    # 长表（同一动量因子）
    long_df = wide_mom.melt(id_vars="trade_dt", var_name="ticker", value_name="factor")
    long_df.to_csv(os.path.join(out_dir, "demo_momentum_long.csv"), index=False, encoding="utf-8-sig")

    # 「价值」：与 log 价负相关（便宜股因子）+ 噪声
    logp = np.log(np.clip(mat, 1e-6, None))
    cs_mean = logp.mean(axis=1, keepdims=True)
    val = (cs_mean - logp) / (np.nanstd(logp, axis=1, keepdims=True) + 1e-6) + rng.normal(0, 0.2, size=logp.shape)
    wide_val = pd.DataFrame(val, columns=tickers)
    wide_val.insert(0, "trade_dt", px["trade_dt"].dt.strftime("%Y-%m-%d"))
    wide_val.to_csv(os.path.join(out_dir, "demo_value_wide.csv"), index=False, encoding="utf-8-sig")

    # 相关性页：两个不同截面结构的「新因子」
    alpha = np.roll(mom, 2, axis=0) * 0.6 + rng.normal(0, 0.5, size=mom.shape)
    beta = np.cumsum(rng.normal(0, 0.02, size=mom.shape), axis=0) + rng.normal(0, 0.8, size=mom.shape)
    for name, arr in ("correlation_alpha", alpha), ("correlation_beta", beta):
        w = pd.DataFrame(arr, columns=tickers)
        w.insert(0, "trade_dt", px["trade_dt"].dt.strftime("%Y-%m-%d"))
        w.to_csv(os.path.join(out_dir, f"{name}_wide.csv"), index=False, encoding="utf-8-sig")

    # 多因子页：各自独立宽表
    qual = -val + rng.normal(0, 0.15, size=val.shape)
    wq = pd.DataFrame(qual, columns=tickers)
    wq.insert(0, "trade_dt", px["trade_dt"].dt.strftime("%Y-%m-%d"))
    wq.to_csv(os.path.join(out_dir, "mf_quality_wide.csv"), index=False, encoding="utf-8-sig")

    growth = mom * 0.4 + val * 0.3 + rng.normal(0, 0.25, size=mom.shape)
    wg = pd.DataFrame(growth, columns=tickers)
    wg.insert(0, "trade_dt", px["trade_dt"].dt.strftime("%Y-%m-%d"))
    wg.to_csv(os.path.join(out_dir, "mf_growth_wide.csv"), index=False, encoding="utf-8-sig")

    # 简短说明（非 markdown，纯文本）
    note = os.path.join(out_dir, "说明.txt")
    with open(note, "w", encoding="utf-8") as f:
        f.write(
            "这些 CSV 的列名（股票代码）与 data/adjopen_wide.csv 一致，可直接用于上传。\n\n"
            "主页 CSV 回测：\n"
            "  demo_momentum_wide.csv  或  demo_momentum_long.csv  或  demo_value_wide.csv\n\n"
            "因子相关性页 /correlation（新因子宽表，可多选文件）：\n"
            "  correlation_alpha_wide.csv  correlation_beta_wide.csv\n\n"
            "多因子页 /multi_factor（每个文件一个因子）：\n"
            "  mf_quality_wide.csv  mf_growth_wide.csv\n"
        )

    return out_dir


def ensure_sample_factor_csvs() -> None:
    """若示例目录不存在或为空，则生成。"""
    from web.config import DATA_DIR

    out_dir = os.path.join(DATA_DIR, SAMPLE_DIR)
    marker = os.path.join(out_dir, "demo_momentum_wide.csv")
    if os.path.isfile(marker):
        return
    if not os.path.isfile(os.path.join(DATA_DIR, "adjopen_wide.csv")):
        return
    write_all_sample_factor_csvs()


if __name__ == "__main__":
    d = write_all_sample_factor_csvs()
    print(f"已写入示例因子 CSV: {d}")
