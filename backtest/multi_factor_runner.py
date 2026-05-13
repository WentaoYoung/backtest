"""
multi_factor_runner.py
多因子批量回测引擎

设计原则：
- 完全复用现有 FactorAnalyzer / BacktestEngine，不修改其逻辑
- 支持并发执行（concurrent.futures）
- 统一结果容器，方便前端横向对比
"""

import time
import logging
import concurrent.futures
import sys
import threading
import os
from typing import Optional, Callable

import pandas as pd
import numpy as np

# 复用现有模块，支持“包内导入”和“单文件直接运行”两种方式
try:
    from backtest.factor_analyzer import FactorAnalyzer
    from backtest.backtest_engine import BacktestEngine
    from backtest.metrics import calc_long_short_summary
    from backtest.stream_logger import stream_logger
    from data.date_params import coerce_yyyy_mm_dd
except ModuleNotFoundError:
    # 直接运行 backtest/multi_factor_runner.py 时，补充项目根目录到 sys.path
    _here = os.path.dirname(os.path.abspath(__file__))
    _project_root = os.path.dirname(_here)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)
    from backtest.factor_analyzer import FactorAnalyzer
    from backtest.backtest_engine import BacktestEngine
    from backtest.metrics import calc_long_short_summary
    from backtest.stream_logger import stream_logger
    from data.date_params import coerce_yyyy_mm_dd


def _normalize_ticker(x: object) -> str:
    s = str(x).strip().upper()
    if not s:
        return s
    s = s.replace(" ", "").replace("_", "").replace("-", "")
    s = s.replace(".XSHE", ".SZ").replace(".XSHG", ".SH")
    s = s.replace("XSHE", ".SZ").replace("XSHG", ".SH")
    if len(s) == 8 and s[:6].isdigit() and s[6:] in ("SZ", "SH"):
        return f"{s[:6]}.{s[6:]}"
    if len(s) == 9 and s[:6].isdigit() and s[6] == "." and s[7:] in ("SZ", "SH"):
        return s
    if len(s) == 6 and s.isdigit():
        if s[0] in ("0", "3"):
            return f"{s}.SZ"
        if s[0] == "6":
            return f"{s}.SH"
        return s
    return s

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 单因子回测结果容器
# ─────────────────────────────────────────────
class SingleFactorResult:
    """存放单个因子的完整回测结果"""

    def __init__(self, factor_name: str):
        self.factor_name = factor_name
        self.success = False
        self.error_msg = ""
        self.elapsed_sec = 0.0

        # IC 分析结果（来自 FactorAnalyzer）
        self.ic_stats: dict = {}         # IC均值/ICIR/t统计量等8项
        self.ic_decay: list = []         # IC衰减序列
        self.ic_distribution: dict = {}
        self.ic_cumulative: list = []
        self.ic_autocorrelation: list = []
        self.yearly_ic: list = []

        # 分组回测结果（来自 BacktestEngine）
        self.group_stats: list = []      # 各组收益/夏普/回撤等
        self.group_nav: list = []        # 各组净值时序
        self.long_short: list = []       # L/S净值时序
        # 与单因子 Web 接口一致：factor_summary = FactorAnalyzer.get_factor_summary()
        self.factor_summary: dict = {}  # FactorAnalyzer.get_factor_summary()，与单因子 Web 一致
        self.ls_summary: dict = {}       # 多空组合绩效（汇总表 / 打分用）
        self.merge_info: dict = {}       # merge后样本统计

    def to_summary_dict(self) -> dict:
        """提取横向对比所需的关键指标，供汇总表格使用"""
        if not self.success:
            return {"factor_name": self.factor_name, "success": False,
                    "error": self.error_msg}

        stats = self.ic_stats
        ls = self.ls_summary

        def _pick(d: dict, *keys, default=None):
            for k in keys:
                if k in d and d[k] is not None:
                    return d[k]
            return default

        def _to_num_or_none(v):
            if v is None:
                return None
            try:
                return float(v)
            except Exception:
                return None

        def _round_or_none(v, digits=4):
            n = _to_num_or_none(v)
            return round(n, digits) if n is not None else None

        def _pct_like_to_ratio_or_none(v, by_100: bool):
            n = _to_num_or_none(v)
            if n is None:
                return None
            return n / 100.0 if by_100 else n

        summary = {
            "factor_name": self.factor_name,
            "success": True,
            # IC 相关
            "ic_mean": _round_or_none(_pick(stats, "ic_mean", "IC_mean")),
            "ic_std": _round_or_none(_pick(stats, "ic_std", "IC_std")),
            "icir": _round_or_none(_pick(stats, "icir", "ICIR")),
            "ic_win_rate": _round_or_none(_pick(stats, "ic_win_rate", "IC_win_rate")),
            "t_stat": _round_or_none(_pick(stats, "t_stat")),
            "rank_ic_mean": _round_or_none(_pick(stats, "rank_ic_mean", "Rank_IC")),
            "rank_icir": _round_or_none(_pick(stats, "rank_icir")),
            "ic_stability": _round_or_none(_pick(stats, "ic_stability", "stability")),
            # 分组回测
            "ls_annual_return": _round_or_none(_pct_like_to_ratio_or_none(
                _pick(ls, "annual_return", "ann_ret_pct", "ann_return", "年化收益_%"),
                by_100=("ann_ret_pct" in ls or "年化收益_%" in ls),
            )),
            "ls_sharpe": _round_or_none(_pick(ls, "sharpe_ratio", "sharpe", "夏普比率")),
            "ls_max_drawdown": _round_or_none(_pct_like_to_ratio_or_none(
                _pick(ls, "max_drawdown", "max_dd_pct", "最大回撤_%"),
                by_100=("max_dd_pct" in ls or "最大回撤_%" in ls),
            )),
            "ls_cumulative_return": _round_or_none(_pct_like_to_ratio_or_none(
                _pick(ls, "cumulative_return", "cum_ret_pct", "累计收益_%"),
                by_100=("cum_ret_pct" in ls or "累计收益_%" in ls),
            )),
            "elapsed_sec": round(self.elapsed_sec, 2),
        }

        # 成功但存在缺失指标时，补充可读原因，避免前端显示为 0 或空白难以定位
        missing = []
        if summary["ic_win_rate"] is None:
            missing.append("IC胜率")
        if summary["icir"] is None:
            missing.append("ICIR")
        if summary["t_stat"] is None:
            missing.append("t统计量")
        if summary["ls_annual_return"] is None:
            missing.append("L/S年化")
        if summary["ls_sharpe"] is None:
            missing.append("L/S夏普")
        if summary["ls_max_drawdown"] is None:
            missing.append("最大回撤")

        if missing:
            reason_parts = [f"部分指标未产出：{', '.join(missing)}"]
            if not stats:
                reason_parts.append("IC统计为空（可能因有效样本天数不足）")
            if not ls:
                reason_parts.append("分组回测汇总为空（可能因可交易样本不足）")
            summary["error"] = "；".join(reason_parts)
        else:
            summary["error"] = ""

        return summary


# ─────────────────────────────────────────────
# 多因子批量运行器
# ─────────────────────────────────────────────
class MultiFactorBatchRunner:
    """
    多因子批量回测调度器

    使用示例：
        runner = MultiFactorBatchRunner(
            price_df=price_wide,
            mkt_val_df=mkt_val_wide,
        )
        runner.add_factor("momentum_20d", factor_df)
        runner.add_factor("ep_ttm", factor_df2)
        results = runner.run_all(params)
    """

    def __init__(
        self,
        price_df: pd.DataFrame,
        mkt_val_df: pd.DataFrame,
        max_workers: int = 4,
        progress_callback: Optional[Callable] = None,
    ):
        """
        Parameters
        ----------
        price_df : pd.DataFrame
            复权价格宽表，index=trade_dt，columns=ticker
        mkt_val_df : pd.DataFrame
            市值宽表，格式同上
        max_workers : int
            并发线程数（IO密集型场景；CPU密集型建议用 ProcessPoolExecutor）
        progress_callback : callable, optional
            (factor_name: str, done: int, total: int) -> None
            用于 SSE 进度推送
        """
        self.price_df = price_df
        self.mkt_val_df = mkt_val_df
        self.max_workers = max_workers
        self.progress_callback = progress_callback

        # {factor_name: wide_df}
        self._factors: dict[str, pd.DataFrame] = {}
        # {factor_name: SingleFactorResult}
        self._results: dict[str, SingleFactorResult] = {}

    # ── 因子注册 ──────────────────────────────

    def add_factor(self, name: str, factor_df: pd.DataFrame) -> None:
        """注册一个因子（宽表，index=trade_dt，columns=ticker）"""
        if name in self._factors:
            logger.warning("因子 %s 已存在，将覆盖", name)
        self._factors[name] = factor_df.copy()

    def remove_factor(self, name: str) -> None:
        self._factors.pop(name, None)
        self._results.pop(name, None)

    def factor_names(self) -> list[str]:
        return list(self._factors.keys())

    # ── 核心执行 ──────────────────────────────

    def run_all(self, params: dict) -> dict[str, SingleFactorResult]:
        """
        批量执行所有已注册因子的回测

        Parameters
        ----------
        params : dict
            回测参数，与单因子 /api/run_backtest 一致：
            {
                "start_date": "2018-01-01",
                "end_date":   "2024-12-31",
                "rebalance_freq": "daily",    # daily/weekly/monthly
                "n_groups": 5,
                "weight_method": "equal",     # equal/mkt_val/factor_score
                "transaction_cost": 0.001,
                "slippage": 0.0005,
                "risk_free_rate": 0.03,
            }

        Returns
        -------
        dict : {factor_name: SingleFactorResult}
        """
        if not self._factors:
            raise ValueError("没有注册任何因子，请先调用 add_factor()")

        total = len(self._factors)
        done_count = [0]  # 用 list 让内层函数可修改
        prog_lock = threading.Lock()

        def _run_one(name_df_pair):
            name, factor_df = name_df_pair
            result = self._run_single_factor(name, factor_df, params)
            with prog_lock:
                done_count[0] += 1
                done_n = done_count[0]
            if self.progress_callback:
                try:
                    self.progress_callback(name, done_n, total)
                except Exception:
                    pass
            return name, result

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_workers
        ) as executor:
            futures = {
                executor.submit(_run_one, item): item[0]
                for item in self._factors.items()
            }
            for future in concurrent.futures.as_completed(futures):
                name, result = future.result()
                self._results[name] = result

        return self._results

    def _run_single_factor(
        self,
        factor_name: str,
        factor_df: pd.DataFrame,
        params: dict,
    ) -> SingleFactorResult:
        """执行单因子完整回测流程（直接复用现有两个类）"""
        result = SingleFactorResult(factor_name)
        t0 = time.time()

        stream_logger.register_thread()
        try:
            line = f"[multi_factor] ▶ 开始回测因子: {factor_name}\n"
            try:
                sys.__stderr__.write(line)
                sys.__stderr__.flush()
            except Exception:
                pass
            logger.info("开始回测因子: %s", factor_name)

            # 1. 构造长表（与现有 app.py 中的处理保持完全一致）
            merged = self._build_long_df(factor_name, factor_df, params)
            if merged is None or merged.empty:
                result.error_msg = "数据合并后为空，请检查日期/股票代码对齐"
            else:
                result.merge_info = {
                    "rows": int(len(merged)),
                    "dates": int(merged["trade_dt"].nunique()),
                    "tickers": int(merged["ticker"].nunique()),
                    "start": str(pd.to_datetime(merged["trade_dt"]).min().date()),
                    "end": str(pd.to_datetime(merged["trade_dt"]).max().date()),
                }
                logger.info(
                    "merge样本[%s]: rows=%d, dates=%d, tickers=%d, range=%s~%s",
                    factor_name,
                    result.merge_info["rows"],
                    result.merge_info["dates"],
                    result.merge_info["tickers"],
                    result.merge_info["start"],
                    result.merge_info["end"],
                )
                # 进入回测前做样本长度检查，避免在 BacktestEngine 里才报 numpy 空数组类错误
                n_dates = int(merged["trade_dt"].nunique())
                n_tickers = int(merged["ticker"].nunique())
                obs_per_ticker = merged.groupby("ticker")["trade_dt"].size()
                max_obs = int(obs_per_ticker.max()) if len(obs_per_ticker) else 0
                median_obs = float(obs_per_ticker.median()) if len(obs_per_ticker) else 0.0
                if n_dates < 3 or max_obs < 3:
                    freq = params.get("rebalance_freq", "daily")
                    result.error_msg = (
                        "样本长度不足，无法按旧口径计算收益 "
                        "(return = open[t+2]/open[t+1]-1)。"
                        f" 当前: dates={n_dates}, tickers={n_tickers}, "
                        f"max_obs_per_ticker={max_obs}, median_obs_per_ticker={median_obs:.1f}, "
                        f"rebalance_freq={freq}。"
                        " 建议：扩大日期范围、改为 daily 调仓、或确保因子在更多交易日有值。"
                    )
                    return result

                # 2. 因子分析（IC / IR 等）—— 直接调用现有 FactorAnalyzer
                analyzer = FactorAnalyzer(
                    data=merged,
                    factor_col="factor",
                    price_col="adj_open",
                    date_col="trade_dt",
                    ticker_col="ticker",
                )
                ic_full = analyzer.get_full_ic_analysis()
                result.ic_stats = ic_full.get("statistics", {})
                result.ic_decay = ic_full.get("decay", [])
                result.ic_distribution = ic_full.get("distribution", {})
                result.ic_cumulative = ic_full.get("cumulative", [])
                result.ic_autocorrelation = ic_full.get("autocorrelation", [])
                try:
                    ic_mean_dbg = result.ic_stats.get("IC_mean", result.ic_stats.get("ic_mean"))
                    ic_std_dbg = result.ic_stats.get("IC_std", result.ic_stats.get("ic_std"))
                    icir_dbg = result.ic_stats.get("ICIR", result.ic_stats.get("icir"))
                    logger.info(
                        "IC统计[%s]: IC_mean=%s, IC_std=%s, ICIR=%s (≈ IC_mean / IC_std)",
                        factor_name, ic_mean_dbg, ic_std_dbg, icir_dbg
                    )
                except Exception:
                    pass

                yearly_df = analyzer.yearly_analysis()
                result.yearly_ic = (
                    yearly_df.to_dict(orient="records") if yearly_df is not None else []
                )

                fs = analyzer.get_factor_summary()
                result.factor_summary = {
                    k: (float(v) if isinstance(v, (np.floating, np.integer)) else v)
                    for k, v in fs.items()
                }

                # 3. 分组回测 —— 直接调用现有 BacktestEngine
                engine = BacktestEngine(
                    data=merged,
                    factor_col="factor",
                    price_col="adj_open",
                    date_col="trade_dt",
                    ticker_col="ticker",
                    mkt_val_col="market_value",
                )
                bt_result = engine.run_group_backtest(
                    n_groups=params.get("n_groups", 5),
                    weight_method=params.get("weight_method", "equal"),
                )
                result.group_stats = bt_result.get("group_stats", [])
                result.group_nav = bt_result.get("group_nav", [])
                result.long_short = bt_result.get("long_short", [])
                result.ls_summary = bt_result.get("factor_summary", {}) or {}
                # 当前 backtest_engine 默认不返回 factor_summary，这里按旧项目口径补算一份
                if not result.ls_summary and isinstance(result.long_short, pd.DataFrame) and not result.long_short.empty:
                    try:
                        ls_summary = calc_long_short_summary(result.long_short)
                        result.ls_summary = {
                            "annual_return": float(ls_summary.get("ann_ret_pct", 0.0)) / 100.0,
                            "sharpe_ratio": float(ls_summary.get("sharpe", 0.0)),
                            "max_drawdown": float(ls_summary.get("max_dd_pct", 0.0)) / 100.0,
                            "cumulative_return": float(ls_summary.get("cum_ret_pct", 0.0)) / 100.0,
                        }
                    except Exception:
                        result.ls_summary = {}
                try:
                    ls_ann_dbg = result.ls_summary.get("annual_return", result.ls_summary.get("ann_return"))
                    ls_sharpe_dbg = result.ls_summary.get("sharpe_ratio", result.ls_summary.get("sharpe"))
                    ls_vol_dbg = result.ls_summary.get("annual_volatility", result.ls_summary.get("ann_vol"))
                    logger.info(
                        "L/S统计[%s]: annual_return=%s, annual_volatility=%s, sharpe=%s",
                        factor_name, ls_ann_dbg, ls_vol_dbg, ls_sharpe_dbg
                    )
                except Exception:
                    pass

                result.success = True

        except Exception as e:
            logger.exception("因子 %s 回测失败", factor_name)
            result.error_msg = str(e)

        finally:
            stream_logger.unregister_thread()

        result.elapsed_sec = time.time() - t0
        try:
            status = "OK" if result.success else f"失败: {result.error_msg}"
            line = f"[multi_factor] ◀ 结束因子 {factor_name} ({status}) 用时 {result.elapsed_sec:.2f}s\n"
            sys.__stderr__.write(line)
            sys.__stderr__.flush()
        except Exception:
            pass
        logger.info("结束因子: %s success=%s elapsed=%.2fs", factor_name, result.success, result.elapsed_sec)
        return result

    def _build_long_df(
        self,
        factor_name: str,
        factor_df: pd.DataFrame,
        params: dict,
    ) -> Optional[pd.DataFrame]:
        """
        将宽表因子 + 价格 + 市值合并为长表
        逻辑与现有 app.py get_cached_data() 保持完全一致
        """
        start = params.get("start_date")
        end = params.get("end_date")
        start_s = coerce_yyyy_mm_dd(start)
        end_s = coerce_yyyy_mm_dd(end)

        # 日期过滤
        price = self.price_df.copy()
        mkt = self.mkt_val_df.copy()
        factor = factor_df.copy()

        for df in [price, mkt, factor]:
            df.index = pd.to_datetime(df.index)
            df.index.name = "trade_dt"
            df.columns = df.columns.map(_normalize_ticker)

        start_ts = pd.to_datetime(start_s, errors="coerce") if start_s else None
        end_ts = pd.to_datetime(end_s, errors="coerce") if end_s else None
        if start_s and pd.isna(start_ts):
            raise ValueError(f"start_date 无法解析: {start}")
        if end_s and pd.isna(end_ts):
            raise ValueError(f"end_date 无法解析: {end}")

        raw_factor_min, raw_factor_max = factor.index.min(), factor.index.max()
        raw_price_min, raw_price_max = price.index.min(), price.index.max()

        if start_ts is not None:
            price = price[price.index >= start_ts]
            mkt = mkt[mkt.index >= start_ts]
            factor = factor[factor.index >= start_ts]
        if end_ts is not None:
            price = price[price.index <= end_ts]
            mkt = mkt[mkt.index <= end_ts]
            factor = factor[factor.index <= end_ts]

        if factor.empty or price.empty:
            raise ValueError(
                f"对齐失败：factor/price 为空（factor_rows={len(factor)}, price_rows={len(price)}）。"
                f"筛选参数 start={start_ts}, end={end_ts}; "
                f"factor原始区间=[{raw_factor_min}~{raw_factor_max}], "
                f"price原始区间=[{raw_price_min}~{raw_price_max}]。"
                f"请检查日期筛选是否过窄。"
            )

        # 先做日期/股票交集检查（避免后面 melt+merge 后才发现为空）
        common_dates = factor.index.intersection(price.index)
        common_tickers = pd.Index(factor.columns).intersection(pd.Index(price.columns))
        if len(common_dates) == 0:
            raise ValueError(
                f"对齐失败：日期无交集。"
                f"factor_date=[{factor.index.min().date()}~{factor.index.max().date()}], "
                f"price_date=[{price.index.min().date()}~{price.index.max().date()}]"
            )
        if len(common_tickers) == 0:
            raise ValueError(
                f"对齐失败：ticker 无交集。"
                f"factor_tickers={len(factor.columns)}, price_tickers={len(price.columns)}。"
                f"示例factor={list(map(str, factor.columns[:5]))}, "
                f"示例price={list(map(str, price.columns[:5]))}"
            )

        # 只保留交集范围，减少 melt 规模并避免全 NaN 导致 merge 空
        factor = factor.loc[common_dates, common_tickers]
        price = price.loc[common_dates, common_tickers]
        if not mkt.empty:
            mkt = mkt.reindex(index=common_dates, columns=common_tickers)

        # 宽表转长表：先强制索引名为 trade_dt，避免 reset_index 后列名不一致
        def _wide_to_long(df: pd.DataFrame, value_name: str) -> pd.DataFrame:
            tmp = df.copy()
            tmp.index = tmp.index.rename("trade_dt")
            return tmp.reset_index().melt(
                id_vars="trade_dt", var_name="ticker", value_name=value_name
            )

        factor_long = _wide_to_long(factor, "factor")
        price_long = _wide_to_long(price, "adj_open")
        mkt_long = _wide_to_long(mkt, "market_value")

        # 统一 ticker 大写
        for df in [factor_long, price_long, mkt_long]:
            df["ticker"] = df["ticker"].str.upper()

        # 合并
        merged = factor_long.merge(
            price_long, on=["trade_dt", "ticker"], how="inner"
        ).merge(mkt_long, on=["trade_dt", "ticker"], how="left")

        # 与旧项目一致：在“已合并的长表”上做调仓频率筛选，避免先 resample 到非交易日导致空数据
        freq = params.get("rebalance_freq", "daily")
        if freq == "weekly":
            merged = merged[merged["trade_dt"].dt.dayofweek == 0]
        elif freq == "monthly":
            merged = (
                merged.groupby([merged["trade_dt"].dt.to_period("M"), "ticker"], as_index=False)
                .first()
            )

        merged = merged.dropna(subset=["factor", "adj_open"])
        if merged.empty:
            # 给出更具体的空原因：有效值交集为 0
            factor_nz = int(pd.notna(factor.values).sum())
            price_nz = int(pd.notna(price.values).sum())
            raise ValueError(
                f"对齐失败：有效值交集为 0（合并后为空）。"
                f"共同日期={len(common_dates)}，共同ticker={len(common_tickers)}，"
                f"factor非空单元={factor_nz}，price非空单元={price_nz}。"
                f"建议检查：因子值是否全空/价格列名是否一致/日期筛选是否过窄。"
            )
        return merged

    # ── 结果查询 ──────────────────────────────

    def get_summary_df(self) -> pd.DataFrame:
        """返回所有因子关键指标的横向对比 DataFrame"""
        rows = [r.to_summary_dict() for r in self._results.values()]
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).set_index("factor_name")

    def get_result(self, factor_name: str) -> Optional[SingleFactorResult]:
        return self._results.get(factor_name)

    def get_all_results(self) -> dict[str, SingleFactorResult]:
        return dict(self._results)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MultiFactorBatchRunner 单文件调试入口")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="运行内置随机数据Demo并输出汇总结果",
    )
    args = parser.parse_args()

    if not args.demo:
        print("该文件默认作为模块被 API 调用。")
        print("如需验证计算链路，请执行：")
        print("  python backtest/multi_factor_runner.py --demo")
        raise SystemExit(0)

    np.random.seed(42)
    dates = pd.date_range("2020-01-01", periods=520, freq="B")
    tickers = [f"{i:06d}.SZ" for i in range(1, 101)]

    # 构造两个示例因子：
    # f1: 含可控有效信号（应产生稳定正向IC）
    # f2: 纯噪声（IC应接近0）
    f1 = pd.DataFrame(
        np.random.normal(0, 1, size=(len(dates), len(tickers))),
        index=dates,
        columns=tickers,
    )
    f2 = pd.DataFrame(
        np.random.normal(0, 1, size=(len(dates), len(tickers))),
        index=dates,
        columns=tickers,
    )

    # 根据 f1 构造“可验证”价格路径：
    # 令 open[t+2]/open[t+1]-1 与 t 日 f1 有单调关系，便于检验分析链路。
    n_dates, n_tickers = len(dates), len(tickers)
    step_ret = np.zeros((n_dates - 1, n_tickers), dtype=float)
    idio_noise = np.random.normal(0, 0.008, size=(n_dates - 1, n_tickers))
    market_noise = np.random.normal(0.0002, 0.002, size=(n_dates - 1, 1))
    for k in range(1, n_dates - 1):
        signal = f1.iloc[k - 1].to_numpy()
        signal = (signal - signal.mean()) / (signal.std() + 1e-12)
        step_ret[k] = 0.001 + 0.004 * signal + idio_noise[k] + market_noise[k, 0]
    step_ret[0] = np.random.normal(0.0005, 0.01, size=n_tickers)

    price = pd.DataFrame(index=dates, columns=tickers, dtype=float)
    price.iloc[0] = 100.0
    for k in range(n_dates - 1):
        price.iloc[k + 1] = price.iloc[k].to_numpy() * (1.0 + step_ret[k])

    # 构造市值宽表（正值）
    mkt = pd.DataFrame(
        np.abs(np.random.normal(1e9, 2.5e8, size=(len(dates), len(tickers)))),
        index=dates,
        columns=tickers,
    )

    runner = MultiFactorBatchRunner(price_df=price, mkt_val_df=mkt, max_workers=2)
    runner.add_factor("demo_factor_1", f1)
    runner.add_factor("demo_factor_2", f2)
    runner.run_all({
        "start_date": str(dates.min().date()),
        "end_date": str(dates.max().date()),
        "rebalance_freq": "daily",
        "n_groups": 5,
        "weight_method": "equal",
    })

    df = runner.get_summary_df()
    print("\n=== Demo 汇总结果 ===")
    if df.empty:
        print("无结果")
    else:
        cols = ["success", "ic_mean", "icir", "ls_annual_return", "ls_sharpe", "elapsed_sec", "error"]
        show_cols = [c for c in cols if c in df.columns]
        print(df[show_cols].to_string())
