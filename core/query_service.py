#!/usr/bin/env python3
"""查询服务 - Query Service (CQRS 读模型)

CQRS 原则：
1. Command（写）：通过 PositionEngine、OrderManager 修改状态
2. Query（读）：通过 QueryService 查询，只读，可缓存

架构收益：
- 读写分离：查询不影响交易性能
- 可缓存：读模型可以加缓存层
- 可优化：读模型可以有专门的索引和物化视图

Usage:
    query = QueryService(storage, event_store)
    snapshot = query.get_portfolio_snapshot()
    positions = query.get_active_positions()
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any
from functools import lru_cache
import json

logger = logging.getLogger(__name__)


@dataclass
class PositionSummary:
    """持仓摘要（读模型）"""
    symbol: str
    name: str = ""
    shares: int = 0
    avg_cost: float = 0
    current_price: float = 0
    market_value: float = 0
    pnl_pct: float = 0
    peak_pnl: float = 0
    drawdown: float = 0
    position_pct: float = 0
    state: str = "UNKNOWN"
    holding_days: int = 0
    add_count: int = 0
    reduced_from_peak: bool = False


@dataclass
class PortfolioSummary:
    """组合摘要（读模型）"""
    total_assets: float = 0
    cash: float = 0
    market_value: float = 0
    initial_capital: float = 100000
    positions_count: int = 0
    profit_count: int = 0
    loss_count: int = 0
    exposure: float = 0
    cash_ratio: float = 0
    total_pnl: float = 0
    total_pnl_pct: float = 0
    daily_pnl_pct: float = 0
    health_score: float = 50
    max_position_pct: float = 0
    last_updated: str = ""

    def to_dict(self) -> Dict:
        return {
            "total_assets": self.total_assets,
            "cash": self.cash,
            "market_value": self.market_value,
            "initial_capital": self.initial_capital,
            "positions_count": self.positions_count,
            "profit_count": self.profit_count,
            "loss_count": self.loss_count,
            "exposure": f"{self.exposure * 100:.1f}%",
            "cash_ratio": f"{self.cash_ratio * 100:.1f}%",
            "total_pnl": self.total_pnl,
            "total_pnl_pct": f"{self.total_pnl_pct:.2f}%",
            "daily_pnl_pct": f"{self.daily_pnl_pct:.2f}%",
            "health_score": f"{self.health_score:.0f}分",
            "max_position_pct": f"{self.max_position_pct:.1f}%",
            "last_updated": self.last_updated
        }


class QueryService:
    """查询服务 - CQRS 读模型

    职责：
    1. 提供只读查询接口
    2. 可选缓存层
    3. 查询优化（预聚合）

    不负责：
    - 修改状态（由 Command 模型负责）
    - 下单、更新持仓等
    """

    def __init__(self, storage=None, event_store=None, cache_enabled: bool = True):
        """
        Args:
            storage: 存储层实例
            event_store: EventStore 实例（用于事件溯源查询）
            cache_enabled: 是否启用缓存
        """
        self.storage = storage
        self.event_store = event_store
        self.cache_enabled = cache_enabled
        self._name_cache: Dict[str, str] = {}  # symbol -> name 缓存

    def get_portfolio_summary(self) -> PortfolioSummary:
        """获取组合摘要（主查询接口）

        Returns:
            PortfolioSummary: 组合摘要
        """
        if not self.storage:
            return PortfolioSummary()

        try:
            # 获取账户
            account = self._get_account()
            # 获取持仓
            positions = self._get_positions()

            # 计算汇总
            market_value = sum(p.get("market_value", 0) for p in positions)
            total_assets = account.get("total_assets", 0)
            cash = account.get("cash", 0)

            # 盈亏统计
            profit_count = 0
            loss_count = 0
            max_position_pct = 0

            for p in positions:
                pnl_pct = p.get("pnl_pct", 0)
                if pnl_pct > 0:
                    profit_count += 1
                elif pnl_pct < 0:
                    loss_count += 1

                pos_pct = (p.get("market_value", 0) / total_assets * 100) if total_assets > 0 else 0
                if pos_pct > max_position_pct:
                    max_position_pct = pos_pct

            # 暴露度
            exposure = market_value / total_assets if total_assets > 0 else 0
            cash_ratio = cash / total_assets if total_assets > 0 else 0

            # 总盈亏
            initial_capital = account.get("initial_capital", 100000)
            total_pnl = total_assets - initial_capital
            total_pnl_pct = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0

            return PortfolioSummary(
                total_assets=total_assets,
                cash=cash,
                market_value=market_value,
                initial_capital=initial_capital,
                positions_count=len(positions),
                profit_count=profit_count,
                loss_count=loss_count,
                exposure=exposure,
                cash_ratio=cash_ratio,
                total_pnl=total_pnl,
                total_pnl_pct=total_pnl_pct,
                daily_pnl_pct=0,  # 需要额外数据
                max_position_pct=max_position_pct,
                last_updated=datetime.now().isoformat()
            )

        except Exception as e:
            logger.error(f"获取组合摘要失败: {e}")
            return PortfolioSummary()

    def get_active_positions(self) -> List[PositionSummary]:
        """获取活跃持仓列表

        Returns:
            List[PositionSummary]: 持仓摘要列表
        """
        if not self.storage:
            return []

        try:
            positions = self._get_positions()
            account = self._get_account()
            total_assets = account.get("total_assets", 1)

            summaries = []
            for p in positions:
                if p.get("shares", 0) <= 0:
                    continue

                market_value = p.get("market_value", 0)
                peak_pnl = p.get("peak_pnl", 0)
                pnl_pct = p.get("pnl_pct", 0)

                summaries.append(PositionSummary(
                    symbol=p.get("symbol", ""),
                    name=self._name_cache.get(p.get("symbol", ""), ""),
                    shares=p.get("shares", 0),
                    avg_cost=p.get("avg_cost", 0),
                    current_price=p.get("current_price", 0),
                    market_value=market_value,
                    pnl_pct=pnl_pct,
                    peak_pnl=peak_pnl,
                    drawdown=peak_pnl - pnl_pct,
                    position_pct=(market_value / total_assets * 100) if total_assets > 0 else 0,
                    state=p.get("state", "UNKNOWN"),
                    holding_days=p.get("holding_days", 0),
                    add_count=p.get("add_count", 0),
                    reduced_from_peak=p.get("reduced_from_peak", False)
                ))

            return summaries

        except Exception as e:
            logger.error(f"获取活跃持仓失败: {e}")
            return []

    def get_position(self, symbol: str) -> Optional[PositionSummary]:
        """获取单只股票持仓

        Args:
            symbol: 股票代码

        Returns:
            PositionSummary: 持仓摘要
        """
        positions = self.get_active_positions()
        for p in positions:
            if p.symbol == symbol:
                return p
        return None

    def get_event_history(self, symbol: str = None, limit: int = 100) -> List[Dict]:
        """获取事件历史（审计追踪）

        Args:
            symbol: 股票代码（可选）
            limit: 最大数量

        Returns:
            List[Dict]: 事件列表
        """
        if not self.event_store:
            return []

        events = self.event_store.get_events(aggregate_id=symbol, limit=limit)
        return [e.to_dict() for e in events]

    def get_trade_history(self, limit: int = 50) -> List[Dict]:
        """获取交易历史（从事件日志）

        Args:
            limit: 最大数量

        Returns:
            List[Dict]: 交易列表
        """
        if not self.event_store:
            return []

        # 查询成交事件
        events = self.event_store.get_events(event_type=None, limit=limit)
        trades = []

        for e in events:
            if "filled" in e.event_type.value or "closed" in e.event_type.value:
                trades.append({
                    "symbol": e.aggregate_id,
                    "action": e.payload.get("action", "unknown"),
                    "shares": e.payload.get("shares", 0),
                    "price": e.payload.get("price", 0),
                    "timestamp": e.timestamp
                })

        return trades[:limit]

    # ========== 缓存相关 ==========

    def set_name_cache(self, name_map: Dict[str, str]):
        """设置名称缓存

        Args:
            name_map: symbol -> name 映射
        """
        self._name_cache = name_map

    def clear_cache(self):
        """清空缓存"""
        self._name_cache.clear()

    # ========== 私有方法 ==========

    def _get_account(self) -> Dict:
        """获取账户信息"""
        if not self.storage:
            return {}
        try:
            if hasattr(self.storage, 'get_account'):
                account = self.storage.get_account()
                return {
                    "total_assets": account.total_assets,
                    "cash": account.cash,
                    "initial_capital": account.initial_capital
                }
        except Exception:
            pass
        return {}

    def _get_positions(self) -> List[Dict]:
        """获取持仓列表"""
        if not self.storage:
            return []
        try:
            if hasattr(self.storage, 'get_positions'):
                positions = self.storage.get_positions()
                return [
                    {
                        "symbol": p.symbol,
                        "shares": p.shares,
                        "avg_cost": p.avg_cost,
                        "current_price": p.current_price,
                        "market_value": p.market_value,
                        "pnl_pct": p.pnl_pct,
                        "peak_pnl": p.peak_pnl,
                        "peak_price": p.peak_price,
                        "add_count": p.add_count,
                        "reduced_from_peak": p.reduced_from_peak,
                        "state": getattr(p, 'state', 'UNKNOWN'),
                        "holding_days": 0  # 需要计算
                    }
                    for p in positions if p.shares > 0
                ]
        except Exception:
            pass
        return []

    def get_query_stats(self) -> Dict:
        """获取查询统计"""
        return {
            "cache_enabled": self.cache_enabled,
            "name_cache_size": len(self._name_cache),
            "storage_connected": self.storage is not None,
            "event_store_connected": self.event_store is not None
        }
