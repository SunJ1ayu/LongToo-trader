#!/usr/bin/env python3
"""Filter Pipeline — 主从制过滤器框架

设计原则：
1. Filter 返回 delta（multiplier + additive + sizing），不直接改 score
2. 统一聚合：final = base_score × all_multipliers + all_additives
3. Sizing 用 max 约束（min(sizing_adj)），不用乘法，防止 collapse
4. Multiplier 最多2个，防止 alpha 被压扁
5. 纯函数，无实例状态
6. FilterResult 只允许 score + sizing，不允许控制 stoploss/execution/portfolio
"""

from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional
import math
import logging

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    """单个 filter 的调整结果"""
    name: str
    multiplier: float = 1.0    # 乘法因子
    additive: float = 0.0      # 加法调整（分）
    sizing_adj: float = 1.0    # 仓位约束（1.0=不限制，<1.0=缩小仓位）
    reason: str = ""


class ScoreFilter:
    """过滤器基类"""
    name: str = "base"
    is_multiplier: bool = False  # True=乘法filter，最多2个

    def apply(self, score: float, data: Dict, market_state: Optional[str]) -> FilterResult:
        raise NotImplementedError


class FilterPipeline:
    """过滤器管道 — 统一聚合"""

    def __init__(self, filters: List[ScoreFilter]):
        mult_count = sum(1 for f in filters if f.is_multiplier)
        if mult_count > 2:
            raise ValueError(f"最多2个乘法filter，当前{mult_count}个")
        self.filters = filters

    def run(self, base_score: float, data: Dict, market_state: Optional[str] = None
            ) -> Tuple[float, float, List[FilterResult]]:
        """执行所有 filter，统一聚合。

        Returns:
            (final_score, sizing_constraint, attributions)
        """
        total_mult = 1.0
        total_add = 0.0
        sizing_constraint = 1.0  # max 约束，不用乘法
        attributions = []

        for f in self.filters:
            r = f.apply(base_score, data, market_state)
            total_mult *= r.multiplier
            total_add += r.additive
            sizing_constraint = min(sizing_constraint, r.sizing_adj)
            attributions.append(r)

        final = base_score * total_mult + total_add
        return max(0, min(100, final)), sizing_constraint, attributions


# ============================================================
# 具体过滤器实现
# ============================================================

class RegimeFilter(ScoreFilter):
    """市场状态过滤 — 乘法因子

    基于 MarketRegime 的 strong/neutral/weak 判断。
    弱市降低整体分数，强市略微提升。
    """
    name = "regime"
    is_multiplier = True

    MULTS = {
        'strong': 1.05,
        'neutral': 1.0,
        'weak': 0.85,
    }

    def apply(self, score: float, data: Dict, market_state: Optional[str]) -> FilterResult:
        if not market_state:
            return FilterResult(name=self.name, reason="no market_state")

        mult = self.MULTS.get(market_state, 1.0)
        return FilterResult(
            name=self.name,
            multiplier=mult,
            reason=f"market={market_state}, mult={mult}",
        )


class VolatilityFilter(ScoreFilter):
    """波动率过滤 — 乘法因子

    高波动率（年化>25%）降低置信度。
    用 ATR/price 近似年化波动率。
    """
    name = "volatility"
    is_multiplier = True

    def apply(self, score: float, data: Dict, market_state: Optional[str]) -> FilterResult:
        atr = data.get('atr', 0)
        price = data.get('price', 0)
        if not price or not atr:
            return FilterResult(name=self.name, reason="no price/atr")

        # ATR/price 日波动率，年化 ≈ × sqrt(252) ≈ × 15.87
        daily_vol = atr / price
        annual_vol = daily_vol * 15.87

        if annual_vol > 0.25:
            return FilterResult(
                name=self.name,
                multiplier=0.92,
                reason=f"high_vol={annual_vol:.1%}, mult=0.92",
            )
        return FilterResult(name=self.name, reason=f"vol={annual_vol:.1%}, ok")


class DefensiveFilter(ScoreFilter):
    """防御性过滤 — 只改 sizing，不改 score

    极端超买/连涨/暴涨时降低仓位，但不扣分。
    注意：RSI 70-80 不惩罚，这是动量策略正常区间。
    """
    name = "defensive"
    is_multiplier = False

    def apply(self, score: float, data: Dict, market_state: Optional[str]) -> FilterResult:
        rsi = data.get('rsi', 50)
        consec_up = data.get('consecutive_up_days', 0)
        change_pct = data.get('change_pct', 0)

        reasons = []
        sizing = 1.0

        # RSI > 80: 极端超买
        if rsi > 80:
            sizing = min(sizing, 0.7)
            reasons.append(f"RSI={rsi:.0f}>80")

        # 连涨 5+ 天
        if consec_up >= 5:
            sizing = min(sizing, 0.8)
            reasons.append(f"连涨{consec_up}天")

        # 今日涨幅 > 8%
        if change_pct > 8:
            sizing = min(sizing, 0.7)
            reasons.append(f"涨幅{change_pct:.1f}%>8%")

        if reasons:
            return FilterResult(
                name=self.name,
                sizing_adj=sizing,
                reason=", ".join(reasons),
            )
        return FilterResult(name=self.name, reason="ok")


# ============================================================
# FilterFactory — 从配置创建 Pipeline
# ============================================================

class FilterFactory:
    """从配置创建 FilterPipeline"""

    REGISTRY = {
        'regime': RegimeFilter,
        'volatility': VolatilityFilter,
        'defensive': DefensiveFilter,
    }

    @classmethod
    def from_config(cls, filter_names: Optional[List[str]]) -> Optional[FilterPipeline]:
        """从配置名列表创建 Pipeline。

        Args:
            filter_names: ['regime', 'volatility', 'defensive'] 或 None

        Returns:
            FilterPipeline 或 None（无 filter 时）
        """
        if not filter_names:
            return None

        filters = []
        for name in filter_names:
            if name in cls.REGISTRY:
                filters.append(cls.REGISTRY[name]())
            else:
                logger.warning(f"未知filter: {name}")

        if not filters:
            return None
        return FilterPipeline(filters)
