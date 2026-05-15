#!/usr/bin/env python3
"""中央化持仓状态管理 - Position Engine

单一真相来源：所有持仓状态字段修改只在这里实现
避免多处直接修改导致状态不一致

架构原则：
- peak_pnl 是 immutable，只增不减
- reduced_from_peak 标记是否已触发减仓保护
- add_count 追踪加仓次数
- 清仓后必须重置所有状态

v2.5.0: 集成 Event Sourcing，所有状态变化记录为事件
"""

import logging
from datetime import datetime
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PositionStateUpdate:
    """状态更新结果"""
    success: bool
    old_value: Optional[float] = None
    new_value: Optional[float] = None
    message: str = ""


class PositionEngine:
    """中央化持仓状态管理

    所有 Agent 只能调用 PositionEngine 方法来修改状态字段
    禁止直接修改 Position 对象或直接执行 SQL

    v2.5.0: 所有状态变化追加到 EventStore，支持崩溃恢复

    Usage:
        engine = PositionEngine(storage, event_store)
        engine.update_peak_pnl("sh600519", 5.2, 1850.0)
        engine.mark_reduced_from_peak("sh600519")
        engine.increment_add_count("sh600519")
    """

    def __init__(self, storage, event_store=None):
        """
        Args:
            storage: PaperTradingStorage 实例
            event_store: EventStore 实例（v2.5.0 可选）
        """
        self.storage = storage
        self.event_store = event_store  # v2.5.0: Event Sourcing

    def update_peak_pnl(self, symbol: str, new_pnl_pct: float, new_price: float) -> PositionStateUpdate:
        """更新 peak_pnl（只有创新高才调用）

        核心原则：peak_pnl 是 immutable，永不下降

        Args:
            symbol: 股票代码
            new_pnl_pct: 新的盈利百分比
            new_price: 新的价格（用于更新 peak_price）

        Returns:
            PositionStateUpdate: 更新结果
        """
        try:
            position = self.storage.get_position(symbol)
            if position is None:
                return PositionStateUpdate(
                    success=False,
                    message=f"未找到持仓: {symbol}"
                )

            old_peak = position.peak_pnl or 0

            # 只有创新高才更新
            if new_pnl_pct > old_peak:
                position.peak_pnl = new_pnl_pct
                position.peak_price = new_price
                self.storage.update_position(position)

                # v2.5.0: 追加事件到 EventStore
                self._append_event("peak_pnl_updated", symbol, {
                    "old_peak_pnl": old_peak,
                    "peak_pnl": new_pnl_pct,
                    "peak_price": new_price
                })

                logger.info(f"peak_pnl 更新: {symbol} {old_peak:+.2f}% → {new_pnl_pct:+.2f}%")
                return PositionStateUpdate(
                    success=True,
                    old_value=old_peak,
                    new_value=new_pnl_pct,
                    message=f"peak_pnl 更新: {old_peak:+.2f}% → {new_pnl_pct:+.2f}%"
                )
            else:
                # 未创新高，不更新
                return PositionStateUpdate(
                    success=True,
                    old_value=old_peak,
                    new_value=old_peak,
                    message=f"未创新高，保持 peak_pnl={old_peak:+.2f}%"
                )

        except Exception as e:
            logger.error(f"更新 peak_pnl 失败: {symbol} - {e}")
            return PositionStateUpdate(
                success=False,
                message=f"更新失败: {e}"
            )

    def mark_reduced_from_peak(self, symbol: str) -> PositionStateUpdate:
        """标记已触发减仓保护

        部分卖出后调用，标记 reduced_from_peak=True

        Args:
            symbol: 股票代码

        Returns:
            PositionStateUpdate: 更新结果
        """
        try:
            position = self.storage.get_position(symbol)
            if position is None:
                return PositionStateUpdate(
                    success=False,
                    message=f"未找到持仓: {symbol}"
                )

            if position.reduced_from_peak:
                return PositionStateUpdate(
                    success=True,
                    message=f"已标记过 reduced_from_peak"
                )

            position.reduced_from_peak = True
            position.last_reduce_at = datetime.now().strftime("%Y-%m-%d")
            self.storage.update_position(position)

            # v2.5.0: 追加事件到 EventStore
            self._append_event("reduce_flag_set", symbol, {
                "timestamp": position.last_reduce_at
            })

            logger.info(f"减仓保护标记: {symbol} reduced_from_peak=True")
            return PositionStateUpdate(
                success=True,
                message=f"减仓保护标记: reduced_from_peak=True"
            )

        except Exception as e:
            logger.error(f"标记 reduced_from_peak 失败: {symbol} - {e}")
            return PositionStateUpdate(
                success=False,
                message=f"标记失败: {e}"
            )

    def increment_add_count(self, symbol: str) -> PositionStateUpdate:
        """增加加仓次数

        加仓成功后调用

        Args:
            symbol: 股票代码

        Returns:
            PositionStateUpdate: 更新结果，new_value 为新的加仓次数
        """
        try:
            position = self.storage.get_position(symbol)
            if position is None:
                return PositionStateUpdate(
                    success=False,
                    message=f"未找到持仓: {symbol}"
                )

            old_count = position.add_count or 0
            new_count = old_count + 1

            position.add_count = new_count
            self.storage.update_position(position)

            # v2.5.0: 追加事件到 EventStore
            self._append_event("add_count_incremented", symbol, {
                "old_count": old_count,
                "new_count": new_count
            })

            logger.info(f"加仓次数更新: {symbol} {old_count} → {new_count}")
            return PositionStateUpdate(
                success=True,
                old_value=old_count,
                new_value=new_count,
                message=f"加仓次数: {old_count} → {new_count}"
            )

        except Exception as e:
            logger.error(f"增加加仓次数失败: {symbol} - {e}")
            return PositionStateUpdate(
                success=False,
                message=f"更新失败: {e}"
            )

    def reset_state_flags(self, symbol: str) -> PositionStateUpdate:
        """清仓后重置状态

        清仓后必须调用，重置所有状态字段：
        - peak_pnl = 0
        - peak_price = 0
        - reduced_from_peak = False
        - add_count = 0
        - last_reduce_at = None

        Args:
            symbol: 股票代码

        Returns:
            PositionStateUpdate: 更新结果
        """
        try:
            position = self.storage.get_position(symbol)
            if position is None:
                # 持仓已不存在，无需重置
                return PositionStateUpdate(
                    success=True,
                    message=f"持仓已不存在，无需重置"
                )

            # 重置所有状态字段
            position.peak_pnl = 0
            position.peak_price = 0
            position.reduced_from_peak = False
            position.add_count = 0
            position.last_reduce_at = None

            self.storage.update_position(position)

            # v2.5.0: 追加事件到 EventStore（清仓事件）
            self._append_event("position_closed", symbol, {
                "reset": True
            })

            logger.info(f"状态重置: {symbol} (peak_pnl=0, reduced_from_peak=False, add_count=0)")
            return PositionStateUpdate(
                success=True,
                message=f"状态已重置"
            )

        except Exception as e:
            logger.error(f"重置状态失败: {symbol} - {e}")
            return PositionStateUpdate(
                success=False,
                message=f"重置失败: {e}"
            )

    def get_state_summary(self, symbol: str) -> dict:
        """获取持仓状态摘要

        Args:
            symbol: 股票代码

        Returns:
            状态摘要字典
        """
        try:
            position = self.storage.get_position(symbol)
            if position is None:
                return {"error": f"未找到持仓: {symbol}"}

            return {
                "symbol": symbol,
                "peak_pnl": position.peak_pnl or 0,
                "peak_price": position.peak_price or 0,
                "reduced_from_peak": position.reduced_from_peak or False,
                "add_count": position.add_count or 0,
                "last_reduce_at": position.last_reduce_at,
                "current_pnl_pct": position.pnl_pct or 0,
            }
        except Exception as e:
            return {"error": str(e)}

    def _append_event(self, event_type: str, symbol: str, payload: dict):
        """追加事件到 EventStore（v2.5.0 内部方法）

        Args:
            event_type: 事件类型字符串
            symbol: 股票代码
            payload: 事件负载
        """
        if self.event_store is None:
            return

        try:
            from .event_store import Event, EventType

            # 映射字符串到 EventType
            event_type_map = {
                "peak_pnl_updated": EventType.PEAK_PNL_UPDATED,
                "reduce_flag_set": EventType.REDUCE_FLAG_SET,
                "add_count_incremented": EventType.ADD_COUNT_INCREMENTED,
                "position_closed": EventType.POSITION_CLOSED,
                "position_opened": EventType.POSITION_OPENED,
            }

            event = Event(
                event_type=event_type_map.get(event_type, EventType.POSITION_UPDATED),
                aggregate_id=symbol,
                aggregate_type="position",
                payload=payload
            )

            self.event_store.append(event)

        except Exception as e:
            logger.warning(f"事件追加失败: {e}")
