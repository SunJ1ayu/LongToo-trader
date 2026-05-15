#!/usr/bin/env python3
"""订单管理器 - OrderManager

统一管理订单生命周期：
- 订单去重（内存 + 数据库）
- 订单状态跟踪（pending → filled → failed）
- 订单历史记录

架构原则：
- 订单是交易的原子单位，必须可追踪
- 去重是风控的第一道防线
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set
from enum import Enum

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "pending"      # 待执行
    FILLED = "filled"        # 已成交
    FAILED = "failed"        # 失败
    CANCELLED = "cancelled"  # 已取消


@dataclass
class Order:
    """订单数据结构"""
    order_id: str
    symbol: str
    action: str  # "buy" or "sell"
    shares: int
    price: float
    signal_id: str  # 关联的信号ID
    status: OrderStatus = OrderStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    filled_at: Optional[str] = None
    result: Optional[Dict] = None  # 执行结果
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "action": self.action,
            "shares": self.shares,
            "price": self.price,
            "signal_id": self.signal_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "filled_at": self.filled_at,
            "result": self.result,
            "error": self.error
        }


class OrderManager:
    """订单管理器

    职责：
    1. 订单去重（防止重复下单）
    2. 订单状态跟踪（pending → filled → failed）
    3. 订单历史记录（审计追踪）

    Usage:
        om = OrderManager(storage)
        order = om.create_order(signal)
        if order:
            result = execute_trade(order)
            om.mark_filled(order.order_id, result)
    """

    def __init__(self, storage=None, dedup_seconds: int = 60):
        """
        Args:
            storage: 存储层实例（用于数据库去重检查）
            dedup_seconds: 去重时间窗口（秒）
        """
        self.storage = storage
        self.dedup_seconds = dedup_seconds
        self._pending_orders: Dict[str, Order] = {}
        self._executed_ids: Set[str] = set()
        self._order_history: List[Order] = []

    def create_order(self, signal: Dict) -> Optional[Order]:
        """创建订单（含去重检查）

        Args:
            signal: 信号字典，包含 symbol, signal, action_shares, price 等

        Returns:
            Order: 订单对象（如果通过去重检查），否则返回 None
        """
        symbol = signal.get("symbol")
        action = signal.get("signal", signal.get("action"))
        shares = signal.get("action_shares", 0)
        price = signal.get("price", 0)

        # 生成订单ID和信号ID
        timestamp = signal.get("timestamp", datetime.now().isoformat())
        signal_id = f"{symbol}_{timestamp}_{action}"
        order_id = f"ord_{datetime.now().strftime('%Y%m%d%H%M%S')}_{symbol}"

        # 去重检查1：内存缓存
        if signal_id in self._executed_ids:
            logger.debug(f"内存去重跳过: {symbol} {action}")
            return None

        # 去重检查2：数据库（如果有storage）
        if self.storage and hasattr(self.storage, 'check_recent_trade'):
            if self.storage.check_recent_trade(symbol, action, seconds=self.dedup_seconds):
                logger.debug(f"数据库去重跳过: {symbol} {action}")
                self._executed_ids.add(signal_id)
                return None

        # 创建订单
        order = Order(
            order_id=order_id,
            symbol=symbol,
            action=action,
            shares=shares,
            price=price,
            signal_id=signal_id,
            status=OrderStatus.PENDING
        )

        # 加入待执行列表
        self._pending_orders[order_id] = order
        logger.info(f"订单创建: {order_id} {symbol} {action} {shares}股@¥{price:.2f}")

        return order

    def mark_filled(self, order_id: str, result: Dict) -> bool:
        """标记订单成交

        Args:
            order_id: 订单ID
            result: 执行结果字典

        Returns:
            bool: 是否成功标记
        """
        order = self._pending_orders.get(order_id)
        if order is None:
            logger.warning(f"订单不存在: {order_id}")
            return False

        order.status = OrderStatus.FILLED
        order.filled_at = datetime.now().isoformat()
        order.result = result

        # 移动到历史记录
        self._order_history.append(order)
        self._pending_orders.pop(order_id, None)
        self._executed_ids.add(order.signal_id)

        logger.info(f"订单成交: {order_id} {order.symbol} {order.action}")
        return True

    def mark_failed(self, order_id: str, reason: str) -> bool:
        """标记订单失败

        Args:
            order_id: 订单ID
            reason: 失败原因

        Returns:
            bool: 是否成功标记
        """
        order = self._pending_orders.get(order_id)
        if order is None:
            logger.warning(f"订单不存在: {order_id}")
            return False

        order.status = OrderStatus.FAILED
        order.error = reason

        # 移动到历史记录
        self._order_history.append(order)
        self._pending_orders.pop(order_id, None)

        logger.warning(f"订单失败: {order_id} {order.symbol} - {reason}")
        return True

    def get_pending_orders(self) -> List[Order]:
        """获取待执行订单列表"""
        return list(self._pending_orders.values())

    def get_order_history(self, symbol: str = None, limit: int = 100) -> List[Order]:
        """获取订单历史

        Args:
            symbol: 筛选特定股票（可选）
            limit: 返回的最大数量

        Returns:
            List[Order]: 订单历史列表
        """
        history = self._order_history
        if symbol:
            history = [o for o in history if o.symbol == symbol]
        return history[-limit:]

    def check_duplicate(self, symbol: str, action: str) -> bool:
        """检查是否重复订单

        Args:
            symbol: 股票代码
            action: 交易方向

        Returns:
            bool: True 表示是重复订单
        """
        # 检查内存缓存
        signal_id_pattern = f"{symbol}_.*_{action}"
        for sig_id in self._executed_ids:
            if sig_id.startswith(f"{symbol}_") and sig_id.endswith(f"_{action}"):
                return True

        # 检查数据库（如果有storage）
        if self.storage and hasattr(self.storage, 'check_recent_trade'):
            return self.storage.check_recent_trade(symbol, action, seconds=self.dedup_seconds)

        return False

    def get_stats(self) -> Dict:
        """获取订单统计"""
        filled = len([o for o in self._order_history if o.status == OrderStatus.FILLED])
        failed = len([o for o in self._order_history if o.status == OrderStatus.FAILED])
        pending = len(self._pending_orders)

        return {
            "pending": pending,
            "filled": filled,
            "failed": failed,
            "total_history": len(self._order_history),
            "dedup_seconds": self.dedup_seconds
        }

    def clear_cache(self):
        """清空内存缓存（用于进程重启后重置）"""
        self._pending_orders.clear()
        self._executed_ids.clear()
        self._order_history.clear()
        logger.info("订单缓存已清空")