"""
factor_correlation.py
新因子 vs 因子库相关性分析（两步走）

Step 1  截面快筛：向量化 Spearman，算 N新因子 × 70库因子 的均值截面相关矩阵
        耗时：秒级（利用 rank + 矩阵运算避免逐日 for 循环）

Step 2  IC精检：只对 Step1 命中高相关配对（|ρ| > threshold）
        算双方 IC 时序的 Pearson 相关，判断"预测信息是否重叠"

设计原则：
- 库因子在服务启动时一次性加载进内存，后续所有请求复用
- 新因子上传后只需对齐日期/股票，不重新加载库
- 所有计算结果可序列化为 JSON，直接返回前端渲染热力图
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── 阈值常量 ──────────────────────────────────────────────────────────────────
QUICK_SCREEN_THRESHOLD = 0.5   # Step1 截面相关超过此值才进入 Step2
HIGH_CORR_THRESHOLD    = 0.7   # 最终认定"高度相关/冗余"的阈值
IC_CORR_THRESHOLD      = 0.5   # IC 序列相关超过此值认定"信息冗余"
MIN_COMMON_STOCKS      = 20    # 单日截面至少有这么多公共股票才参与计算
MIN_IC_DAYS            = 60    # IC 序列至少有这么多有效交易日才算 IC 相关


# ══════════════════════════════════════════════════════════════════════════════
# 库因子缓存（单例，启动时加载一次）
# ══════════════════════════════════════════════════════════════════════════════
class LibraryCache:
    """
    把库里70个因子一次性加载并预处理，后续请求直接复用。

    使用方式：
        cache = LibraryCache()
        cache.load(db_connector)          # 服务启动时调用一次

        analyzer = FactorVsLibraryAnalyzer(new_factors, cache)
        result   = analyzer.run(price_df)
    """

    def __init__(self) -> None:
        self.wide: dict[str, pd.DataFrame] = {}
        self._ranked: dict[str, pd.DataFrame] = {}
        self.categories: dict[str, str] = {}
        self._loaded = False
        self._load_time: float = 0.0

    def load(self, db_connector, factor_table: str = None) -> None:
        """
        从数据库批量拉取所有库因子并缓存。

        db_connector 需要提供：
            .get_all_factors_wide(table) -> dict[str, pd.DataFrame]
                返回 {factor_name: wide_df}，wide_df index=trade_dt，columns=ticker
            .get_factor_categories()     -> dict[str, str]  （可选）
                返回 {factor_name: category}
        """
        t0 = time.time()
        logger.info("开始加载因子库到内存…")

        try:
            raw = db_connector.get_all_factors_wide(factor_table)
        except Exception as e:
            logger.error("因子库加载失败: %s", e)
            raise

        self.wide = {}
        self._ranked = {}
        for name, df in raw.items():
            df = df.copy()
            df.index = pd.to_datetime(df.index)
            df.columns = df.columns.str.upper()
            self.wide[name] = df
            self._ranked[name] = df.rank(
                axis=1, method="average", na_option="keep"
            )

        try:
            self.categories = db_connector.get_factor_categories()
        except Exception:
            self.categories = {}

        self._loaded = True
        self._load_time = time.time() - t0
        logger.info(
            "因子库加载完成：%d 个因子，耗时 %.1fs",
            len(self.wide), self._load_time,
        )

    def load_from_dict(
        self,
        wide_dict: dict[str, pd.DataFrame],
        categories: dict[str, str] | None = None,
    ) -> None:
        """直接从字典加载（测试 / 离线场景）"""
        self.wide = {}
        self._ranked = {}
        for name, df in wide_dict.items():
            df = df.copy()
            df.index = pd.to_datetime(df.index)
            df.columns = df.columns.str.upper()
            self.wide[name] = df
            self._ranked[name] = df.rank(
                axis=1, method="average", na_option="keep"
            )
        self.categories = categories or {}
        self._loaded = True

    @property
    def factor_names(self) -> list[str]:
        return list(self.wide.keys())

    @property
    def is_loaded(self) -> bool:
        return self._loaded


# ══════════════════════════════════════════════════════════════════════════════
# 数据类
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class PairDetail:
    """单个（新因子, 库因子）配对的精检结果"""
    new_factor: str
    lib_factor: str
    cross_corr: Optional[float]
    ic_corr: Optional[float]
    is_redundant: bool
    redundancy_reason: str
    common_dates: int = 0
    common_tickers: int = 0
    valid_days: int = 0
    insufficient_reason: str = ""


@dataclass
class NewFactorResult:
    """单个新因子的完整对比结果"""
    name: str
    cross_corr_row: dict[str, Optional[float]] = field(default_factory=dict)
    cross_corr_meta: dict[str, dict] = field(default_factory=dict)
    pair_details: list[PairDetail] = field(default_factory=list)
    max_cross_corr: float = 0.0
    max_ic_corr: Optional[float] = None
    is_redundant: bool = False
    redundancy_summary: str = ""
    elapsed_sec: float = 0.0

    def to_heatmap_data(self, lib_order: list[str]) -> list[Optional[float]]:
        return [
            round(v, 4) if (v is not None and pd.notna(v)) else None
            for v in (self.cross_corr_row.get(lib) for lib in lib_order)
        ]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "cross_corr_row": {
                k: (round(v, 4) if (v is not None and pd.notna(v)) else None)
                for k, v in self.cross_corr_row.items()
            },
            "cross_corr_meta": self.cross_corr_meta,
            "max_cross_corr": round(self.max_cross_corr, 4),
            "max_ic_corr": round(self.max_ic_corr, 4) if self.max_ic_corr is not None else None,
            "is_redundant": self.is_redundant,
            "redundancy_summary": self.redundancy_summary,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "pair_details": [
                {
                    "lib_factor": p.lib_factor,
                    "cross_corr": round(p.cross_corr, 4) if p.cross_corr is not None else None,
                    "ic_corr": round(p.ic_corr, 4) if p.ic_corr is not None else None,
                    "is_redundant": p.is_redundant,
                    "reason": p.redundancy_reason,
                    "common_dates": p.common_dates,
                    "common_tickers": p.common_tickers,
                    "valid_days": p.valid_days,
                    "insufficient_reason": p.insufficient_reason,
                }
                for p in self.pair_details
            ],
        }


# ══════════════════════════════════════════════════════════════════════════════
# 核心分析器
# ══════════════════════════════════════════════════════════════════════════════
class FactorVsLibraryAnalyzer:
    """
    N 个新因子 vs 库因子的两步相关性分析器

    用法：
        analyzer = FactorVsLibraryAnalyzer(
            new_factors = {"ep_ttm": df1, "momentum": df2},
            library     = cache,
        )
        results  = analyzer.run(price_df)
        heatmap  = analyzer.build_heatmap_payload(results)
    """

    def __init__(
        self,
        new_factors: dict[str, pd.DataFrame],
        library: LibraryCache,
        quick_threshold: float = QUICK_SCREEN_THRESHOLD,
        high_corr_threshold: float = HIGH_CORR_THRESHOLD,
        ic_corr_threshold: float = IC_CORR_THRESHOLD,
    ) -> None:
        self.library = library
        self.quick_threshold = quick_threshold
        self.high_corr_threshold = high_corr_threshold
        self.ic_corr_threshold = ic_corr_threshold

        self.new_factors: dict[str, pd.DataFrame] = {}
        self._new_ranked: dict[str, pd.DataFrame] = {}
        for name, df in new_factors.items():
            df = df.copy()
            df.index = pd.to_datetime(df.index)
            df.columns = df.columns.str.upper()
            self.new_factors[name] = df
            self._new_ranked[name] = df.rank(
                axis=1, method="average", na_option="keep"
            )

    # ══════════════════════════════════════════
    # 主入口
    # ══════════════════════════════════════════

    def run(
        self,
        price_df: pd.DataFrame,
        future_return_days: int = 1,
    ) -> dict[str, NewFactorResult]:
        """
        对所有新因子执行两步分析，返回 {new_factor_name: NewFactorResult}
        """
        if not self.library.is_loaded:
            raise RuntimeError("LibraryCache 尚未加载，请先调用 cache.load()")

        # 预计算未来收益（所有因子共用）
        price_df = price_df.copy()
        price_df.index = pd.to_datetime(price_df.index)
        price_df.columns = price_df.columns.str.upper()
        fwd_ret = price_df.pct_change(future_return_days).shift(-future_return_days)

        # 库因子 IC 缓存（跨新因子复用，避免重复计算）
        _lib_ic_cache: dict[str, pd.Series] = {}

        results: dict[str, NewFactorResult] = {}

        for new_name in self.new_factors:
            t0 = time.time()
            result = NewFactorResult(name=new_name)

            # ── Step 1：截面快筛 ─────────────────────
            cross_corr_row, cross_corr_meta = self._step1_cross_section(new_name)
            result.cross_corr_row = cross_corr_row
            result.cross_corr_meta = cross_corr_meta
            result.max_cross_corr = max(
                (abs(v) for v in cross_corr_row.values() if v is not None and pd.notna(v)), default=0.0
            )

            # ── Step 2：IC精检（只对快筛命中的配对）────
            candidates: list[str] = []
            for lib, corr in cross_corr_row.items():
                # Step1 可能返回 None（无公共日期/股票不足），这里要做严格跳过
                if corr is None or (pd.notna(corr) is False):
                    continue
                try:
                    corr_f = float(corr)
                except Exception:
                    continue
                if abs(corr_f) >= self.quick_threshold:
                    candidates.append(lib)

            if candidates:
                new_df = self.new_factors[new_name]
                new_ic = self._calc_ic(new_df, fwd_ret)

                for lib_name in candidates:
                    if lib_name not in _lib_ic_cache:
                        _lib_ic_cache[lib_name] = self._calc_ic(
                            self.library.wide[lib_name], fwd_ret
                        )
                    ic_corr = self._step2_ic_corr(new_ic, _lib_ic_cache[lib_name])
                    cross_v = cross_corr_row[lib_name]

                    cross_ok = cross_v is not None and pd.notna(cross_v)
                    is_redundant = (
                        (cross_ok and abs(float(cross_v)) >= self.high_corr_threshold)
                        or (ic_corr is not None and abs(ic_corr) >= self.ic_corr_threshold)
                    )
                    result.pair_details.append(PairDetail(
                        new_factor=new_name,
                        lib_factor=lib_name,
                        cross_corr=cross_v,
                        ic_corr=ic_corr,
                        is_redundant=is_redundant,
                        redundancy_reason=self._build_reason(cross_v, ic_corr),
                        common_dates=int(cross_corr_meta.get(lib_name, {}).get("common_dates", 0)),
                        common_tickers=int(cross_corr_meta.get(lib_name, {}).get("common_tickers", 0)),
                        valid_days=int(cross_corr_meta.get(lib_name, {}).get("valid_days", 0)),
                        insufficient_reason=str(cross_corr_meta.get(lib_name, {}).get("insufficient_reason", "")),
                    ))

                ic_corrs = [p.ic_corr for p in result.pair_details if p.ic_corr is not None]
                result.max_ic_corr = max((abs(v) for v in ic_corrs), default=None)
                redundant = [p for p in result.pair_details if p.is_redundant]
                result.is_redundant = len(redundant) > 0
                result.redundancy_summary = self._build_summary(redundant)

            result.elapsed_sec = time.time() - t0
            results[new_name] = result
            logger.info(
                "新因子 %s：截面最高=%.3f，精检%d对，耗时%.2fs",
                new_name, result.max_cross_corr,
                len(result.pair_details), result.elapsed_sec,
            )

        return results

    # ══════════════════════════════════════════
    # Step 1：向量化截面 Spearman
    # ══════════════════════════════════════════

    def _step1_cross_section(self, new_name: str) -> tuple[dict[str, Optional[float]], dict[str, dict]]:
        """
        向量化计算新因子与所有库因子的截面均值 Spearman 相关。

        核心原理：
            Spearman(X, Y) ≡ Pearson(rank(X), rank(Y))
            把因子值提前做截面排名（LibraryCache 初始化时已算好），
            再对每日截面用 numpy 向量化求 Pearson，比逐对 scipy.spearmanr 快约 50x。
        """
        new_ranked: pd.DataFrame = self._new_ranked[new_name]   # (T, S)
        lib_names = self.library.factor_names
        results: dict[str, Optional[float]] = {}
        meta: dict[str, dict] = {}

        for lib_name in lib_names:
            lib_ranked: pd.DataFrame = self.library._ranked[lib_name]

            # 公共日期 & 公共股票
            common_dates = new_ranked.index.intersection(lib_ranked.index)
            if len(common_dates) == 0:
                results[lib_name] = None
                meta[lib_name] = {
                    "common_dates": 0,
                    "common_tickers": 0,
                    "valid_days": 0,
                    "insufficient_reason": "无公共日期",
                }
                continue

            nr = new_ranked.loc[common_dates]
            lr = lib_ranked.loc[common_dates]
            common_tickers = nr.columns.intersection(lr.columns)
            if len(common_tickers) < MIN_COMMON_STOCKS:
                results[lib_name] = None
                meta[lib_name] = {
                    "common_dates": int(len(common_dates)),
                    "common_tickers": int(len(common_tickers)),
                    "valid_days": 0,
                    "insufficient_reason": f"公共股票数不足{MIN_COMMON_STOCKS}",
                }
                continue

            a = nr[common_tickers].values.astype(float)   # (T, S)
            b = lr[common_tickers].values.astype(float)   # (T, S)

            corr_series = self._row_pearson(a, b)
            valid_days = int(corr_series.size)
            results[lib_name] = float(np.nanmean(corr_series)) if corr_series.size > 0 else None
            meta[lib_name] = {
                "common_dates": int(len(common_dates)),
                "common_tickers": int(len(common_tickers)),
                "valid_days": valid_days,
                "insufficient_reason": "" if valid_days > 0 else "有效截面天数为0",
            }

        return results, meta

    @staticmethod
    def _row_pearson(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """
        对两个矩阵 (T, S) 逐行计算 Pearson 相关系数，返回 (T,) 数组。
        忽略含 NaN 的位置，不足 MIN_COMMON_STOCKS 的行记为 NaN。
        """
        T = a.shape[0]
        corrs = np.full(T, np.nan)
        for t in range(T):
            ra, rb = a[t], b[t]
            mask = ~(np.isnan(ra) | np.isnan(rb))
            n = mask.sum()
            if n < MIN_COMMON_STOCKS:
                continue
            ra_m = ra[mask] - ra[mask].mean()
            rb_m = rb[mask] - rb[mask].mean()
            denom = np.sqrt((ra_m ** 2).sum() * (rb_m ** 2).sum())
            if denom < 1e-10:
                continue
            corrs[t] = np.dot(ra_m, rb_m) / denom
        return corrs[~np.isnan(corrs)]

    # ══════════════════════════════════════════
    # Step 2：IC 序列相关
    # ══════════════════════════════════════════

    @staticmethod
    def _calc_ic(factor_df: pd.DataFrame, fwd_ret: pd.DataFrame) -> pd.Series:
        """
        计算因子的日度 Rank IC 序列。
        Rank IC = 每日 Spearman(因子截面排名, 未来收益截面排名)
        """
        factor_df = factor_df.copy()
        factor_df.index = pd.to_datetime(factor_df.index)
        factor_df.columns = factor_df.columns.str.upper()

        common_dates = factor_df.index.intersection(fwd_ret.index)
        common_tickers = factor_df.columns.intersection(fwd_ret.columns)
        if len(common_dates) == 0 or len(common_tickers) == 0:
            return pd.Series(dtype=float)

        f = factor_df.loc[common_dates, common_tickers]
        r = fwd_ret.loc[common_dates, common_tickers]

        f_rank = f.rank(axis=1, method="average", na_option="keep").values.astype(float)
        r_rank = r.rank(axis=1, method="average", na_option="keep").values.astype(float)

        ic_vals, dates = [], []
        for i, date in enumerate(common_dates):
            fa, ra = f_rank[i], r_rank[i]
            mask = ~(np.isnan(fa) | np.isnan(ra))
            if mask.sum() < MIN_COMMON_STOCKS:
                continue
            fa_m = fa[mask] - fa[mask].mean()
            ra_m = ra[mask] - ra[mask].mean()
            denom = np.sqrt((fa_m ** 2).sum() * (ra_m ** 2).sum())
            if denom < 1e-10:
                continue
            ic = np.dot(fa_m, ra_m) / denom
            if not np.isnan(ic):
                ic_vals.append(ic)
                dates.append(date)

        return pd.Series(ic_vals, index=dates)

    @staticmethod
    def _step2_ic_corr(ic_a: pd.Series, ic_b: pd.Series) -> Optional[float]:
        """两条 IC 序列对齐后的 Pearson 相关"""
        combined = pd.DataFrame({"a": ic_a, "b": ic_b}).dropna()
        if len(combined) < MIN_IC_DAYS:
            return None
        return float(combined["a"].corr(combined["b"]))

    # ══════════════════════════════════════════
    # 热力图 payload
    # ══════════════════════════════════════════

    def build_heatmap_payload(
        self,
        results: dict[str, NewFactorResult],
        sort_lib_by_category: bool = True,
    ) -> dict:
        """
        构建前端热力图 JSON。

        {
            "new_factors":     [str, ...],          # 行：新因子名
            "lib_factors":     [str, ...],          # 列：库因子名（按类别分组）
            "lib_categories":  [str, ...],          # 每列对应的类别
            "matrix":          [[float, ...],...],  # N行 × 70列，截面均值相关
            "warnings":        [{...}, ...],        # 高相关预警
            "step2_details":   {new_factor: [{...}]},
            "factor_status":   {new_factor: {...}},
        }
        """
        lib_order = self._get_lib_order(sort_by_category=sort_lib_by_category)
        lib_categories = [self.library.categories.get(n, "其他") for n in lib_order]

        new_names = list(results.keys())
        matrix = [results[n].to_heatmap_data(lib_order) for n in new_names]

        warnings = []
        step2_details = {}
        for n, result in results.items():
            for p in result.pair_details:
                if p.is_redundant:
                    warnings.append({
                        "new_factor": p.new_factor,
                        "lib_factor": p.lib_factor,
                        "cross_corr": round(p.cross_corr, 4),
                        "ic_corr": round(p.ic_corr, 4) if p.ic_corr is not None else None,
                        "reason": p.redundancy_reason,
                    })
            if result.pair_details:
                step2_details[n] = sorted(
                    [
                        {
                            "lib_factor": p.lib_factor,
                            "cross_corr": round(p.cross_corr, 4) if p.cross_corr is not None else None,
                            "ic_corr": round(p.ic_corr, 4) if p.ic_corr is not None else None,
                            "is_redundant": p.is_redundant,
                            "reason": p.redundancy_reason,
                            "common_dates": p.common_dates,
                            "common_tickers": p.common_tickers,
                            "valid_days": p.valid_days,
                            "insufficient_reason": p.insufficient_reason,
                        }
                        for p in result.pair_details
                    ],
                    key=lambda x: abs(x["cross_corr"]) if x["cross_corr"] is not None else -1,
                    reverse=True,
                )

        factor_status = {
            n: {
                "is_redundant": r.is_redundant,
                "max_cross_corr": round(r.max_cross_corr, 4),
                "max_ic_corr": round(r.max_ic_corr, 4) if r.max_ic_corr is not None else None,
                "summary": r.redundancy_summary,
                "cross_corr_meta": r.cross_corr_meta,
            }
            for n, r in results.items()
        }

        return {
            "new_factors": new_names,
            "lib_factors": lib_order,
            "lib_categories": lib_categories,
            "matrix": matrix,
            "warnings": warnings,
            "step2_details": step2_details,
            "factor_status": factor_status,
        }

    def _get_lib_order(self, sort_by_category: bool = True) -> list[str]:
        names = self.library.factor_names
        if not sort_by_category or not self.library.categories:
            return sorted(names)
        return sorted(
            names,
            key=lambda n: (self.library.categories.get(n, "zzz"), n)
        )

    # ══════════════════════════════════════════
    # 辅助
    # ══════════════════════════════════════════

    @staticmethod
    def _build_reason(cross_corr: Optional[float], ic_corr: Optional[float]) -> str:
        cross_ok = cross_corr is not None and pd.notna(cross_corr)
        parts = [f"截面相关={cross_corr:.2f}" if cross_ok else "截面相关=NA"]
        if ic_corr is not None and pd.notna(ic_corr):
            parts.append(f"IC序列相关={ic_corr:.2f}")
        if cross_ok and abs(cross_corr) >= HIGH_CORR_THRESHOLD:
            parts.append("因子构造高度相似")
        if ic_corr is not None and pd.notna(ic_corr) and abs(ic_corr) >= IC_CORR_THRESHOLD:
            parts.append("预测信息重叠")
        return "，".join(parts)

    @staticmethod
    def _build_summary(redundant_pairs: list[PairDetail]) -> str:
        if not redundant_pairs:
            return ""
        names = [p.lib_factor for p in redundant_pairs[:3]]
        suffix = f" 等{len(redundant_pairs)}个" if len(redundant_pairs) > 3 else ""
        return f"与库中 {', '.join(names)}{suffix} 高度相关，建议谨慎入库"
