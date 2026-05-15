#!/usr/bin/env python3
"""组合级风险引擎 - Portfolio Engine

提供组合层面的风险指标，而非单股票风控：
- 暴露度（exposure）= 持仓市值 / 总资产
- 盈亏分布（盈/亏/持平）
- 组合健康度评分
- 单只股票仓位占比

架构原则：
- 组合风险 > 单票风险
- 分散投资降低非系统性风险
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class RiskLevel(Enum):
    """风险等级"""
    LOW = "low"        # 低风险（仓位<30%）
    MEDIUM = "medium"  # 中风险（仓位30-70%）
    HIGH = "high"      # 高风险（仓位>70%）


@dataclass
class PositionRisk:
    """单只股票风险"""
    symbol: str
    position_pct: float  # 仓位占比
    pnl_pct: float       # 盈亏百分比
    market_value: float  # 市值
    risk_contribution: float = 0  # 风险贡献度


@dataclass
class PortfolioSnapshot:
    """组合快照"""
    timestamp: str
    total_assets: float
    cash: float
    market_value: float
    exposure: float           # 暴露度 = market_value / total_assets
    cash_ratio: float         # 现金比例 = cash / total_assets
    positions_count: int
    risk_level: RiskLevel
    max_position_pct: float   # 最大单票仓位
    profit_count: int         # 盈利持仓数
    loss_count: int           # 亏损持仓数
    health_score: float       # 组合健康度（0-100）
    position_risks: List[PositionRisk] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "total_assets": self.total_assets,
            "cash": self.cash,
            "market_value": self.market_value,
            "exposure": self.exposure,
            "cash_ratio": self.cash_ratio,
            "positions_count": self.positions_count,
            "risk_level": self.risk_level.value,
            "max_position_pct": self.max_position_pct,
            "profit_count": self.profit_count,
            "loss_count": self.loss_count,
            "health_score": self.health_score,
            "position_risks": [pr.__dict__ for pr in self.position_risks]
        }


class PortfolioEngine:
    """组合级风险引擎

    提供组合层面的风险指标，用于：
    1. 风控决策（是否允许新开仓）
    2. 报告展示（组合健康度）
    3. 仓位管理（分散度检查）

    Usage:
        engine = PortfolioEngine(storage)
        snapshot = engine.get_snapshot()
        if snapshot.exposure > 0.8:
            print("仓位过重，建议减仓")
    """

    def __init__(self, storage=None):
        """
        Args:
            storage: 存储层实例（用于查询持仓和账户）
        """
        self.storage = storage
        self._last_snapshot: Optional[PortfolioSnapshot] = None

    def get_snapshot(self, positions: List[Dict] = None, account: Dict = None) -> PortfolioSnapshot:
        """获取组合快照

        Args:
            positions: 持仓列表（可选，不传则从 storage 读取）
            account: 账户信息（可选，不传则从 storage 读取）

        Returns:
            PortfolioSnapshot: 组合快照
        """
        # 获取数据
        if positions is None and self.storage:
            positions = self._get_positions_from_storage()
        positions = positions or []

        if account is None and self.storage:
            account = self._get_account_from_storage()
        account = account or {}

        # 计算基础指标
        total_assets = account.get("total_assets", 100000)
        cash = account.get("cash", 0)
        market_value = sum(p.get("market_value", 0) for p in positions)

        # 暴露度
        exposure = market_value / total_assets if total_assets > 0 else 0
        cash_ratio = cash / total_assets if total_assets > 0 else 0

        # 风险等级
        if exposure < 0.3:
            risk_level = RiskLevel.LOW
        elif exposure < 0.7:
            risk_level = RiskLevel.MEDIUM
        else:
            risk_level = RiskLevel.HIGH

        # 计算各股票仓位占比和风险贡献
        position_risks = []
        max_position_pct = 0
        profit_count = 0
        loss_count = 0

        for pos in positions:
            mv = pos.get("market_value", 0)
            position_pct = mv / total_assets * 100 if total_assets > 0 else 0
            pnl_pct = pos.get("pnl_pct", 0)

            # 更新最大仓位
            if position_pct > max_position_pct:
                max_position_pct = position_pct

            # 盈亏统计
            if pnl_pct > 0:
                profit_count += 1
            elif pnl_pct < 0:
                loss_count += 1

            # 风险贡献度（简化：仓位占比 × 绝对盈亏）
            risk_contribution = position_pct * abs(pnl_pct) / 100

            position_risks.append(PositionRisk(
                symbol=pos.get("symbol", ""),
                position_pct=position_pct,
                pnl_pct=pnl_pct,
                market_value=mv,
                risk_contribution=risk_contribution
            ))

        # 计算健康度
        health_score = self._calculate_health_score(
            exposure=exposure,
            profit_count=profit_count,
            loss_count=loss_count,
            positions_count=len(positions),
            max_position_pct=max_position_pct
        )

        # 创建快照
        snapshot = PortfolioSnapshot(
            timestamp=datetime.now().isoformat(),
            total_assets=total_assets,
            cash=cash,
            market_value=market_value,
            exposure=exposure,
            cash_ratio=cash_ratio,
            positions_count=len(positions),
            risk_level=risk_level,
            max_position_pct=max_position_pct,
            profit_count=profit_count,
            loss_count=loss_count,
            health_score=health_score,
            position_risks=position_risks
        )

        self._last_snapshot = snapshot
        return snapshot

    def _calculate_health_score(
        self,
        exposure: float,
        profit_count: int,
        loss_count: int,
        positions_count: int,
        max_position_pct: float
    ) -> float:
        """计算组合健康度

        评分维度：
        1. 仓位合理度（30-70%为最佳）
        2. 盈亏分布（盈利多加分，亏损多扣分）
        3. 分散度（单票仓位不超过20%）

        Returns:
            float: 0-100 分
        """
        score = 50.0  # 基础分

        # 仓位合理度
        if 0.3 <= exposure <= 0.7:
            score += 15  # 最佳仓位区间
        elif exposure < 0.3:
            score -= 5   # 过于保守
        else:
            score -= 10  # 过于激进

        # 盈亏分布
        if positions_count > 0:
            win_rate = profit_count / positions_count
            score += win_rate * 20  # 胜率加分
            score -= (loss_count / positions_count) * 15  # 亏损扣分

        # 分散度
        if max_position_pct > 25:
            score -= 10  # 单票仓位过重
        elif max_position_pct < 10 and positions_count > 1:
            score += 5   # 分散良好

        # 持仓数量
        if positions_count == 0:
            score = 70  # 空仓视为保守，给中等分
        elif positions_count > 8:
            score -= 5  # 过度分散

        return max(0, min(100, score))

    def _get_positions_from_storage(self) -> List[Dict]:
        """从存储层获取持仓"""
        if not self.storage:
            return []
        try:
            if hasattr(self.storage, 'get_positions'):
                positions = self.storage.get_positions()
                return [
                    {
                        "symbol": p.symbol,
                        "market_value": p.market_value,
                        "pnl_pct": p.pnl_pct
                    }
                    for p in positions if p.shares > 0
                ]
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
        return []

    def _get_account_from_storage(self) -> Dict:
        """从存储层获取账户信息"""
        if not self.storage:
            return {}
        try:
            if hasattr(self.storage, 'get_account'):
                account = self.storage.get_account()
                return {
                    "total_assets": account.total_assets,
                    "cash": account.cash
                }
        except Exception as e:
            logger.error(f"获取账户失败: {e}")
        return {}

    def get_risk_summary(self) -> Dict:
        """获取风险摘要（用于报告）"""
        if self._last_snapshot is None:
            self.get_snapshot()

        if self._last_snapshot is None:
            return {"error": "无法获取组合快照"}

        snap = self._last_snapshot
        return {
            "exposure": f"{snap.exposure * 100:.1f}%",
            "cash_ratio": f"{snap.cash_ratio * 100:.1f}%",
            "positions_count": snap.positions_count,
            "risk_level": snap.risk_level.value,
            "health_score": f"{snap.health_score:.0f}分",
            "profit_loss": f"{snap.profit_count}盈/{snap.loss_count}亏",
            "max_position": f"{snap.max_position_pct:.1f}%"
        }

    def check_rebalance_needed(self) -> Optional[str]:
        """检查是否需要再平衡

        Returns:
            str: 建议操作，None 表示不需要
        """
        if self._last_snapshot is None:
            self.get_snapshot()

        if self._last_snapshot is None:
            return None

        snap = self._last_snapshot

        # 单票仓位过重
        if snap.max_position_pct > 25:
            return f"⚠️ 单票仓位过重（{snap.max_position_pct:.1f}%），建议减仓"

        # 仓位过重
        if snap.exposure > 0.85:
            return f"⚠️ 仓位过重（{snap.exposure*100:.0f}%），建议整体减仓"

        # 过于分散
        if snap.positions_count > 8:
            return f"ℹ️ 持仓过多（{snap.positions_count}只），考虑集中优质标的"

        return None
