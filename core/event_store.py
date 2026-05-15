#!/usr/bin/env python3
"""Event Sourcing - 事件存储

核心理念：
1. 事件是不可变的事实（fact），只能追加，不能修改或删除
2. 当前状态 = 所有事件的归约（fold/reduce）
3. 支持崩溃恢复：从事件日志重建状态

架构原则：
- 事件是交易真相，state 是展示层
- 所有状态变化必须记录为事件
- 事件可回放、可审计、可调试

Usage:
    store = EventStore(db_path)
    store.append(PositionOpened(symbol="sh600519", shares=1000, price=1850))
    events = store.get_events(aggregate_id="sh600519")
    position = rebuild_position(events)
"""

import logging
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable
from enum import Enum
from pathlib import Path
import json

logger = logging.getLogger(__name__)


class EventType(Enum):
    """事件类型"""
    # 账户事件
    ACCOUNT_CREATED = "account_created"

    # 持仓事件
    POSITION_OPENED = "position_opened"      # 开仓
    POSITION_UPDATED = "position_updated"    # 持仓更新（peak_pnl等）
    POSITION_REDUCED = "position_reduced"    # 减仓（部分卖出）
    POSITION_CLOSED = "position_closed"      # 清仓

    # 订单事件
    ORDER_PLACED = "order_placed"            # 下单
    ORDER_FILLED = "order_filled"            # 成交
    ORDER_CANCELLED = "order_cancelled"      # 取消

    # 风控事件
    STOP_LOSS_UPDATED = "stop_loss_updated"  # 止损更新
    PEAK_PNL_UPDATED = "peak_pnl_updated"    # 峰值盈利更新（关键！）
    REDUCE_FLAG_SET = "reduce_flag_set"      # 减仓标记设置
    ADD_COUNT_INCREMENTED = "add_count_incremented"  # 加仓次数增加

    # 策略事件
    STRATEGY_SIGNAL_GENERATED = "strategy_signal_generated"  # 策略信号生成（含评分详情）

    # 风控拦截事件
    RISK_REJECTED = "risk_rejected"          # 风控拦截（含原因）

    # 市场事件
    REGIME_CHANGED = "regime_changed"        # 市场状态切换


@dataclass
class Event:
    """事件基类"""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: EventType = EventType.POSITION_UPDATED
    aggregate_id: str = ""  # 被作用的实体ID（symbol for position, "account" for account）
    aggregate_type: str = "position"  # 实体类型
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)  # causation_id, correlation_id等

    def to_dict(self) -> Dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "aggregate_id": self.aggregate_id,
            "aggregate_type": self.aggregate_type,
            "timestamp": self.timestamp,
            "payload": self.payload,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "Event":
        return cls(
            event_id=data.get("event_id", ""),
            event_type=EventType(data.get("event_type", "position_updated")),
            aggregate_id=data.get("aggregate_id", ""),
            aggregate_type=data.get("aggregate_type", "position"),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
            payload=data.get("payload", {}),
            metadata=data.get("metadata", {})
        )


class EventStore:
    """事件存储

    Append-only 事件日志，支持：
    1. 事件追加
    2. 事件查询（按聚合根）
    3. 状态重建（从事件归约）

    存储在 SQLite，表结构：
    events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT,
        aggregate_id TEXT,
        aggregate_type TEXT,
        timestamp TEXT,
        payload JSON,
        metadata JSON,
        sequence INTEGER AUTOINCREMENT
    )
    """

    def __init__(self, db_path: str = None):
        """
        Args:
            db_path: 数据库路径（默认 ~/.openclaw/workspace/skills/LongToo-trader/data/events.db）
        """
        if db_path is None:
            db_path = str(
                Path.home() / ".openclaw/workspace/skills/LongToo-trader/data/events.db"
            )

        self.db_path = db_path
        self._conn = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取持久连接（惰性创建）"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _init_db(self):
        """初始化数据库表"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                event_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                aggregate_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                payload JSON NOT NULL,
                metadata JSON,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_aggregate_id
            ON events(aggregate_id, timestamp)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_event_type
            ON events(event_type, timestamp)
        """)
        conn.commit()

        logger.info(f"EventStore 初始化完成: {self.db_path}")

    def append(self, event: Event) -> bool:
        """追加事件（不可修改）

        Args:
            event: 事件对象

        Returns:
            bool: 是否成功
        """
        try:
            conn = self._get_conn()
            conn.execute("""
                INSERT INTO events (event_id, event_type, aggregate_id, aggregate_type,
                                    timestamp, payload, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                event.event_id,
                event.event_type.value,
                event.aggregate_id,
                event.aggregate_type,
                event.timestamp,
                json.dumps(event.payload),
                json.dumps(event.metadata)
            ))
            conn.commit()

            logger.info(f"事件追加: {event.event_type.value} {event.aggregate_id}")
            return True

        except sqlite3.IntegrityError:
            logger.warning(f"事件已存在: {event.event_id}")
            return False
        except sqlite3.OperationalError:
            # 连接可能失效，重连一次
            self._conn = None
            try:
                conn = self._get_conn()
                conn.execute("""
                    INSERT INTO events (event_id, event_type, aggregate_id, aggregate_type,
                                        timestamp, payload, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    event.event_id, event.event_type.value,
                    event.aggregate_id, event.aggregate_type,
                    event.timestamp, json.dumps(event.payload),
                    json.dumps(event.metadata)
                ))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"事件追加失败(重连后): {e}")
                return False
        except Exception as e:
            logger.error(f"事件追加失败: {e}")
            return False

    def emit(
        self,
        event_type: EventType,
        aggregate_id: str,
        aggregate_type: str,
        payload: Dict[str, Any],
        metadata: Dict[str, Any] = None
    ) -> bool:
        """发布事件的便捷方法

        Args:
            event_type: 事件类型
            aggregate_id: 聚合根ID
            aggregate_type: 聚合根类型
            payload: 事件数据
            metadata: 元数据（可选）

        Returns:
            bool: 是否成功
        """
        event = Event(
            event_type=event_type,
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            payload=payload,
            metadata=metadata or {}
        )
        return self.append(event)

    def get_events(
        self,
        aggregate_id: str = None,
        event_type: EventType = None,
        since: str = None,
        limit: int = 1000
    ) -> List[Event]:
        """查询事件

        Args:
            aggregate_id: 聚合根ID（如 symbol）
            event_type: 事件类型过滤
            since: 起始时间
            limit: 最大数量

        Returns:
            List[Event]: 事件列表（按时间顺序）
        """
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row

            query = "SELECT * FROM events WHERE 1=1"
            params = []

            if aggregate_id:
                query += " AND aggregate_id = ?"
                params.append(aggregate_id)

            if event_type:
                query += " AND event_type = ?"
                params.append(event_type.value)

            if since:
                query += " AND timestamp >= ?"
                params.append(since)

            query += " ORDER BY timestamp ASC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()

            events = []
            for row in rows:
                events.append(Event.from_dict({
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "aggregate_id": row["aggregate_id"],
                    "aggregate_type": row["aggregate_type"],
                    "timestamp": row["timestamp"],
                    "payload": json.loads(row["payload"]),
                    "metadata": json.loads(row["metadata"] or "{}")
                }))

            return events

        except Exception as e:
            logger.error(f"事件查询失败: {e}")
            return []

    def rebuild_state(
        self,
        aggregate_id: str,
        aggregate_type: str,
        reducer: Callable[[Any, Event], Any],
        initial_state: Any = None
    ) -> Any:
        """从事件重建状态

        Args:
            aggregate_id: 聚合根ID
            aggregate_type: 聚合根类型
            reducer: 归约函数 (state, event) -> new_state
            initial_state: 初始状态

        Returns:
            Any: 重建后的状态
        """
        events = self.get_events(aggregate_id=aggregate_id)

        state = initial_state
        for event in events:
            if event.aggregate_type == aggregate_type:
                state = reducer(state, event)

        return state

    def get_all_events(self, limit: int = 10000) -> List[Event]:
        """获取所有事件（用于全量回放）"""
        return self.get_events(limit=limit)

    def get_stats(self) -> Dict:
        """获取统计信息"""
        try:
            conn = self._get_conn()
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            types = conn.execute("""
                SELECT event_type, COUNT(*) as count
                FROM events GROUP BY event_type
            """).fetchall()

            return {
                "total_events": total,
                "event_types": {row[0]: row[1] for row in types},
                "db_path": self.db_path
            }
        except Exception as e:
            return {"error": str(e)}


# ========== 状态重建函数 ==========

def rebuild_position(events: List[Event]) -> Dict:
    """从事件重建持仓状态

    Args:
        events: 持仓相关事件列表

    Returns:
        Dict: 持仓状态
    """
    position = {
        "symbol": "",
        "shares": 0,
        "avg_cost": 0,
        "peak_pnl": 0,
        "peak_price": 0,
        "reduced_from_peak": False,
        "add_count": 0,
        "total_cost": 0
    }

    for event in events:
        payload = event.payload

        if event.event_type == EventType.POSITION_OPENED:
            position["symbol"] = event.aggregate_id
            position["shares"] = payload.get("shares", 0)
            position["avg_cost"] = payload.get("price", 0)
            position["peak_price"] = payload.get("price", 0)
            position["total_cost"] = position["shares"] * position["avg_cost"]

        elif event.event_type == EventType.POSITION_UPDATED:
            # 更新 shares（加减仓）
            new_shares = payload.get("shares", position["shares"])
            if new_shares > position["shares"]:
                # 加仓：更新平均成本
                add_cost = payload.get("price", 0)
                add_shares = new_shares - position["shares"]
                position["total_cost"] += add_shares * add_cost
                position["avg_cost"] = position["total_cost"] / new_shares
            position["shares"] = new_shares

        elif event.event_type == EventType.POSITION_REDUCED:
            # 减仓：不更新成本
            position["shares"] = payload.get("remaining_shares", position["shares"])

        elif event.event_type == EventType.POSITION_CLOSED:
            position["shares"] = 0
            position["total_cost"] = 0

        elif event.event_type == EventType.PEAK_PNL_UPDATED:
            # 关键：peak_pnl 只增不减
            new_peak = payload.get("peak_pnl", 0)
            if new_peak > position["peak_pnl"]:
                position["peak_pnl"] = new_peak
                position["peak_price"] = payload.get("peak_price", position["peak_price"])

        elif event.event_type == EventType.REDUCE_FLAG_SET:
            position["reduced_from_peak"] = True

        elif event.event_type == EventType.ADD_COUNT_INCREMENTED:
            position["add_count"] += 1

    return position


def rebuild_account(events: List[Event]) -> Dict:
    """从事件重建账户状态

    Args:
        events: 账户相关事件列表

    Returns:
        Dict: 账户状态
    """
    account = {
        "initial_capital": 100000,
        "cash": 100000,
        "total_assets": 100000
    }

    for event in events:
        if event.event_type == EventType.ACCOUNT_CREATED:
            account["initial_capital"] = event.payload.get("initial_capital", 100000)
            account["cash"] = account["initial_capital"]
            account["total_assets"] = account["initial_capital"]

        elif event.event_type == EventType.ORDER_FILLED:
            action = event.payload.get("action", "buy")
            amount = event.payload.get("amount", 0)

            if action == "buy":
                account["cash"] -= amount
            else:
                account["cash"] += amount

    return account


def rebuild_strategy_signals(store: "EventStore", aggregate_id: str = None) -> List[Dict]:
    """从事件重建策略信号历史（使用 SQL 过滤）

    Args:
        store: EventStore 实例
        aggregate_id: 可选，按股票代码过滤

    Returns:
        List[Dict]: 策略信号列表
    """
    events = store.get_events(
        event_type=EventType.STRATEGY_SIGNAL_GENERATED,
        aggregate_id=aggregate_id
    )
    return [event.payload for event in events]


# v2.6.0: EventStore事件扩展完成