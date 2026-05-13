"""
factor_scorer.py
因子综合评分 + 入库建议

评分维度（满分100）：
  - IC 质量   35分：IC均值(15) + ICIR(10) + t统计量(10)
  - 预测稳定性 25分：IC胜率(10) + IC稳定性(10) + 偏度惩罚(5)
  - 分组区分度 25分：单调性(15) + L/S夏普(10)
  - 独立性     15分：与库因子低相关度
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class FactorScoreDetail:
    """单项评分明细"""
    dimension: str
    raw_value: float
    score: float           # 该维度得分
    max_score: float       # 该维度满分
    comment: str = ""


@dataclass
class FactorScore:
    """单因子综合评分结果"""
    factor_name: str
    total_score: float = 0.0
    grade: str = "D"               # A+ / A / B / C / D
    recommendation: str = ""       # 入库建议
    details: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "factor_name": self.factor_name,
            "total_score": round(self.total_score, 1),
            "grade": self.grade,
            "recommendation": self.recommendation,
            "details": [
                {
                    "dimension": d.dimension,
                    "raw_value": round(d.raw_value, 4),
                    "score": round(d.score, 1),
                    "max_score": d.max_score,
                    "comment": d.comment,
                }
                for d in self.details
            ],
        }


class FactorScorer:
    """
    因子综合评分器

    使用示例：
        scorer = FactorScorer()
        score = scorer.score(
            factor_name="ep_ttm",
            ic_stats=analyzer.calculate_ic_statistics(),
            group_stats=engine_result["group_stats"],
            factor_summary=engine_result["factor_summary"],
            max_lib_corr=0.35,   # 与库因子的最高相关系数
        )
        print(score.total_score, score.grade, score.recommendation)
    """

    def score(
        self,
        factor_name: str,
        ic_stats: dict,
        group_stats: list,
        factor_summary: dict,
        max_lib_corr: float = 0.0,   # 与因子库最高相关系数（绝对值）
    ) -> FactorScore:

        result = FactorScore(factor_name=factor_name)
        details = []
        total = 0.0

        # ── 维度1：IC 质量（35分）────────────────────────
        ic_mean = abs(ic_stats.get("ic_mean", 0))
        icir = abs(ic_stats.get("icir", 0))
        t_stat = abs(ic_stats.get("t_stat", 0))

        # IC 均值打分（满15）：0.03 以下 0 分，0.06+ 满分
        ic_mean_score = self._linear_score(ic_mean, 0.03, 0.06, 0, 15)
        details.append(FactorScoreDetail(
            "IC均值", ic_mean, ic_mean_score, 15,
            self._label(ic_mean, [(0.03, "弱"), (0.05, "中"), (0.08, "强"), (99, "很强")])
        ))

        # ICIR（满10）：0.3 以下 0 分，0.8+ 满分
        icir_score = self._linear_score(icir, 0.3, 0.8, 0, 10)
        details.append(FactorScoreDetail(
            "ICIR", icir, icir_score, 10,
            self._label(icir, [(0.3, "不稳定"), (0.5, "一般"), (0.7, "稳定"), (99, "很稳定")])
        ))

        # t统计量（满10）：1.65 以下 0 分，3+ 满分
        t_score = self._linear_score(t_stat, 1.65, 3.0, 0, 10)
        details.append(FactorScoreDetail(
            "t统计量", t_stat, t_score, 10,
            "显著" if t_stat >= 2 else "不显著"
        ))

        total += ic_mean_score + icir_score + t_score

        # ── 维度2：预测稳定性（25分）─────────────────────
        ic_win_rate = ic_stats.get("ic_win_rate", 0.5)
        ic_stability = ic_stats.get("ic_stability", 0)
        ic_skewness = abs(ic_stats.get("ic_skewness", 0))

        # IC 胜率（满10）
        win_score = self._linear_score(ic_win_rate, 0.5, 0.65, 0, 10)
        details.append(FactorScoreDetail(
            "IC胜率", ic_win_rate, win_score, 10,
            f"{ic_win_rate:.1%}"
        ))

        # IC 稳定性（满10）：|IC|>0.02 的天数占比
        stability_score = self._linear_score(ic_stability, 0.3, 0.65, 0, 10)
        details.append(FactorScoreDetail(
            "IC稳定性", ic_stability, stability_score, 10,
            f"{ic_stability:.1%}"
        ))

        # 偏度惩罚（满5）：偏度越小越好
        skew_score = max(0, 5 - ic_skewness * 2)
        details.append(FactorScoreDetail(
            "IC偏度", ic_skewness, skew_score, 5,
            "对称" if ic_skewness < 0.5 else "分布偏斜"
        ))

        total += win_score + stability_score + skew_score

        # ── 维度3：分组区分度（25分）─────────────────────
        # 单调性检验（满15）
        monotonicity_score, mono_comment = self._check_monotonicity(group_stats, 15)
        details.append(FactorScoreDetail(
            "分组单调性", 0, monotonicity_score, 15, mono_comment
        ))

        # L/S 夏普比率（满10）
        ls_sharpe = abs(factor_summary.get("sharpe_ratio", 0))
        sharpe_score = self._linear_score(ls_sharpe, 0.5, 2.0, 0, 10)
        details.append(FactorScoreDetail(
            "L/S夏普", ls_sharpe, sharpe_score, 10,
            self._label(ls_sharpe, [(0.5, "弱"), (1.0, "一般"), (1.5, "好"), (99, "优秀")])
        ))

        total += monotonicity_score + sharpe_score

        # ── 维度4：独立性（15分）──────────────────────────
        # 与库因子最高相关系数越低越好
        independence_score = self._linear_score(
            1 - max_lib_corr, 0.3, 1.0, 0, 15
        )
        details.append(FactorScoreDetail(
            "因子独立性", 1 - max_lib_corr, independence_score, 15,
            f"与库最高相关={max_lib_corr:.2f}" + (
                "（高度冗余）" if max_lib_corr >= 0.7
                else "（中度相关）" if max_lib_corr >= 0.5
                else "（独立性好）"
            )
        ))

        total += independence_score

        # ── 汇总 ─────────────────────────────────────────
        result.total_score = round(total, 1)
        result.details = details
        result.grade = self._grade(total)
        result.recommendation = self._recommend(total, max_lib_corr)

        return result

    def score_batch(
        self,
        factors_data: list[dict],
    ) -> list[FactorScore]:
        """
        批量评分

        Parameters
        ----------
        factors_data : list of dict
            每项包含 score() 所需的参数：
            {"factor_name": ..., "ic_stats": ..., "group_stats": ...,
             "factor_summary": ..., "max_lib_corr": ...}
        """
        return [self.score(**item) for item in factors_data]

    # ── 辅助方法 ──────────────────────────────

    @staticmethod
    def _linear_score(
        value: float,
        low: float, high: float,
        score_low: float, score_high: float,
    ) -> float:
        """线性插值打分"""
        if value <= low:
            return score_low
        if value >= high:
            return score_high
        ratio = (value - low) / (high - low)
        return score_low + ratio * (score_high - score_low)

    @staticmethod
    def _label(value: float, thresholds: list[tuple]) -> str:
        for threshold, label in thresholds:
            if value < threshold:
                return label
        return thresholds[-1][1]

    @staticmethod
    def _check_monotonicity(group_stats: list, max_score: float) -> tuple[float, str]:
        """
        检验分组收益单调性，返回（得分，说明）

        group_stats 格式：[{"group": "G1", "annual_return": ...}, ...]
        """
        if not group_stats or len(group_stats) < 3:
            return 0.0, "数据不足"

        try:
            returns = [g.get("annual_return", 0) for g in group_stats]
            n = len(returns)
            # 计算单调性得分：相邻组正序比例
            ascending = sum(1 for i in range(n - 1) if returns[i] < returns[i + 1])
            descending = sum(1 for i in range(n - 1) if returns[i] > returns[i + 1])

            mono_ratio = max(ascending, descending) / (n - 1)
            score = mono_ratio * max_score

            if mono_ratio >= 1.0:
                comment = "完全单调（优秀）"
            elif mono_ratio >= 0.8:
                comment = f"基本单调（{mono_ratio:.0%}）"
            else:
                comment = f"单调性差（{mono_ratio:.0%}）"

            return score, comment

        except Exception as e:
            logger.warning("单调性检验失败: %s", e)
            return 0.0, "计算失败"

    @staticmethod
    def _grade(score: float) -> str:
        if score >= 85:
            return "A+"
        if score >= 75:
            return "A"
        if score >= 60:
            return "B"
        if score >= 45:
            return "C"
        return "D"

    @staticmethod
    def _recommend(score: float, max_lib_corr: float) -> str:
        if max_lib_corr >= 0.7:
            return "不建议入库：与现有因子高度相关，增量信息有限"
        if score >= 75:
            return "建议入库：各维度表现优秀，可纳入因子库"
        if score >= 60:
            return "可考虑入库：表现中等，建议结合业务逻辑判断"
        if score >= 45:
            return "暂缓入库：因子效果偏弱，建议优化后再评估"
        return "不建议入库：因子预测能力不足"
