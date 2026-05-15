#!/usr/bin/env python3
"""
Position State Machine - 持仓状态机

核心理念：不同持仓状态对应不同的风控策略

状态定义：
- NEW_POSITION: 新建仓位（0-3天，观察期）
- PROFIT_RUNNING: 盈利奔跑（盈利>1.5%，趋势健康）
- WEAKENING: 走弱预警（盈利回撤、趋势破坏）
- EXIT_PENDING: 待退出（触发卖出条件，等待执行）
- PROTECTED: 强保护（盈利>5%，peak_pnl锁定）

状态转换：
  NEW_POSITION ──盈利→ PROFIT_RUNNING
               └─亏损/持平→ WEAKENING (3天后)

  PROFIT_RUNNING ──回撤>3%→ WEAKENING
                └─盈利>5%→ PROTECTED

  WEAKENING ──回撤>5%/止损→ EXIT_PENDING
            └─反弹→ PROFIT_RUNNING

  PROTECTED ──回撤>5%→ WEAKENING
            └─保持→ PROTECTED

不同状态的风控参数：
- 止损阈值
- 盈利保护线
- 持仓容忍度
- 仓位调整建议
"""

import logging
from enum import Enum
from typing import Dict, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class PositionState(Enum):
    """持仓状态枚举"""
    NEW_POSITION = "NEW_POSITION"      # 新建仓位（观察期）
    PROFIT_RUNNING = "PROFIT_RUNNING"  # 盈利奔跑
    WEAKENING = "WEAKENING"             # 走弱预警
    EXIT_PENDING = "EXIT_PENDING"       # 待退出
    PROTECTED = "PROTECTED"             # 强保护


# 各状态的默认配置
STATE_CONFIG = {
    PositionState.NEW_POSITION: {
        "stop_loss_pct": -5.0,          # 新仓止损较宽（给观察空间）
        "profit_protect_line": 0.0,     # 无保护线
        "max_holding_days": 3,          # 观察期
        "drawdown_threshold": 8.0,      # 回撤容忍度较大
        "weight_in_portfolio": 0.8,     # 建议仓位系数
        "description": "新建仓位，观察期，止损较宽",
        # 新仓不参与分层减仓
        "drawdown_tiers": [
            {"threshold": 8.0, "reduce_pct": 100, "action": "清仓"},
        ],
        "reduced_drawdown_tiers": [
            {"threshold": 5.0, "reduce_pct": 100, "action": "清仓"},
        ],
    },
    PositionState.PROFIT_RUNNING: {
        "stop_loss_pct": -2.0,          # 盈利后止损收紧（保护利润）
        "profit_protect_line": 1.5,     # 盈利保护线
        "max_holding_days": 30,         # 允许长期持有
        "drawdown_threshold": 3.0,      # 回撤敏感
        "weight_in_portfolio": 1.0,     # 标准仓位
        "description": "盈利奔跑，保护利润，回撤敏感",
        # 分层减仓策略
        "drawdown_tiers": [
            {"threshold": 3.0, "reduce_pct": 30, "action": "减仓30%"},
            {"threshold": 5.0, "reduce_pct": 50, "action": "减仓50%"},
            {"threshold": 8.0, "reduce_pct": 100, "action": "清仓"},
        ],
        # 已减仓后的收紧阈值
        "reduced_drawdown_tiers": [
            {"threshold": 2.0, "reduce_pct": 50, "action": "减仓50%"},
            {"threshold": 4.0, "reduce_pct": 100, "action": "清仓"},
        ],
    },
    PositionState.WEAKENING: {
        "stop_loss_pct": -1.5,          # 最严格止损
        "profit_protect_line": 0.0,     # 无保护
        "max_holding_days": 5,          # 限时退出
        "drawdown_threshold": 2.0,      # 极低容忍
        "weight_in_portfolio": 0.5,     # 建议减仓
        "description": "走弱预警，严格止损，建议减仓",
        # 走弱状态直接清仓
        "drawdown_tiers": [
            {"threshold": 2.0, "reduce_pct": 100, "action": "清仓"},
        ],
        "reduced_drawdown_tiers": [
            {"threshold": 1.5, "reduce_pct": 100, "action": "清仓"},
        ],
    },
    PositionState.EXIT_PENDING: {
        "stop_loss_pct": 0.0,           # 无条件退出
        "profit_protect_line": 0.0,
        "max_holding_days": 1,          # 立即执行
        "drawdown_threshold": 0.0,
        "weight_in_portfolio": 0.0,     # 清仓
        "description": "待退出，已触发卖出条件",
        "drawdown_tiers": [
            {"threshold": 0.0, "reduce_pct": 100, "action": "清仓"},
        ],
        "reduced_drawdown_tiers": [
            {"threshold": 0.0, "reduce_pct": 100, "action": "清仓"},
        ],
    },
    PositionState.PROTECTED: {
        "stop_loss_pct": -3.0,          # 允许一定波动
        "profit_protect_line": 5.0,     # 强保护线
        "max_holding_days": 60,         # 长期持有
        "drawdown_threshold": 5.0,      # 回撤容忍
        "weight_in_portfolio": 1.2,     # 可加仓
        "description": "强保护，盈利>5%，允许波动",
        # PROTECTED 状态更宽松
        "drawdown_tiers": [
            {"threshold": 5.0, "reduce_pct": 30, "action": "减仓30%"},
            {"threshold": 8.0, "reduce_pct": 50, "action": "减仓50%"},
            {"threshold": 12.0, "reduce_pct": 100, "action": "清仓"},
        ],
        "reduced_drawdown_tiers": [
            {"threshold": 3.0, "reduce_pct": 50, "action": "减仓50%"},
            {"threshold": 6.0, "reduce_pct": 100, "action": "清仓"},
        ],
    },
}


class PositionStateMachine:
    """持仓状态机

    职责：
    1. 根据持仓数据判断当前状态
    2. 提供状态对应的风控参数
    3. 记录状态转换历史
    """

    def __init__(self):
        self.state_history: Dict[str, List[Dict]] = {}  # symbol -> 历史记录

    def get_state(self, position: Dict) -> PositionState:
        """判断持仓当前状态

        Args:
            position: 持仓数据，包含 pnl_pct, peak_pnl, holding_days, drawdown 等

        Returns:
            当前状态
        """
        pnl_pct = position.get("pnl_pct", 0)
        peak_pnl = position.get("peak_pnl", max(0, pnl_pct))
        drawdown = peak_pnl - pnl_pct
        holding_days = position.get("holding_days", 0) or self._calc_days(position.get("buy_date"))

        # 状态判断逻辑（按优先级）

        # 1. EXIT_PENDING: 已触发卖出条件
        if position.get("exit_triggered"):
            return PositionState.EXIT_PENDING

        # 2. PROTECTED: 强盈利保护（盈利>5%）
        if pnl_pct >= 5.0 and peak_pnl >= 5.0:
            return PositionState.PROTECTED

        # 3. WEAKENING: 走弱预警
        # - 盈利回撤 > 3%
        # - 或亏损 > 1%
        # - 或观察期后仍无盈利
        if drawdown >= 3.0 or pnl_pct < -1.0:
            return PositionState.WEAKENING
        if holding_days > 3 and pnl_pct <= 0:
            return PositionState.WEAKENING

        # 4. PROFIT_RUNNING: 盈利奔跑
        # - 盈利 > 1.5%
        # - 回撤 < 3%
        if pnl_pct >= 1.5 and drawdown < 3.0:
            return PositionState.PROFIT_RUNNING

        # 5. NEW_POSITION: 新建仓位
        # - 持仓 <= 3 天
        # - 或盈利 < 1.5%
        if holding_days <= 3 or pnl_pct < 1.5:
            return PositionState.NEW_POSITION

        # 默认：PROFIT_RUNNING
        return PositionState.PROFIT_RUNNING

    def get_config(self, state: PositionState) -> Dict:
        """获取状态对应的风控配置

        Args:
            state: 持仓状态

        Returns:
            配置字典
        """
        return STATE_CONFIG.get(state, STATE_CONFIG[PositionState.NEW_POSITION])

    def get_state_info(self, position: Dict) -> Dict:
        """获取持仓状态完整信息

        Args:
            position: 持仓数据

        Returns:
            包含状态、配置、建议的字典
        """
        state = self.get_state(position)
        config = self.get_config(state)

        return {
            "symbol": position.get("symbol"),
            "state": state.value,
            "pnl_pct": position.get("pnl_pct", 0),
            "peak_pnl": position.get("peak_pnl", 0),
            "drawdown": position.get("peak_pnl", 0) - position.get("pnl_pct", 0),
            "holding_days": position.get("holding_days", 0) or self._calc_days(position.get("buy_date")),
            "config": config,
            "suggestion": self._get_suggestion(state, position),
        }

    def check_transition(self, position: Dict) -> Optional[Dict]:
        """检查状态转换

        Returns:
            如果发生状态转换，返回转换信息；否则返回 None
        """
        symbol = position.get("symbol")
        current_state = self.get_state(position)

        # 获取上次状态
        history = self.state_history.get(symbol, [])
        if history:
            last_record = history[-1]
            last_state = PositionState(last_record["state"])

            if last_state != current_state:
                # 状态发生变化
                transition = {
                    "symbol": symbol,
                    "from_state": last_state.value,
                    "to_state": current_state.value,
                    "pnl_pct": position.get("pnl_pct", 0),
                    "timestamp": datetime.now().isoformat(),
                }

                # 记录历史
                self.state_history.setdefault(symbol, []).append({
                    "state": current_state.value,
                    "pnl_pct": position.get("pnl_pct", 0),
                    "timestamp": datetime.now().isoformat(),
                })

                return transition

        # 记录当前状态
        self.state_history.setdefault(symbol, []).append({
            "state": current_state.value,
            "pnl_pct": position.get("pnl_pct", 0),
            "timestamp": datetime.now().isoformat(),
        })

        return None

    def get_dynamic_stop_loss(self, position: Dict) -> float:
        """获取动态止损阈值

        根据状态动态调整止损：
        - 新仓：-5%（给观察空间）
        - 盈利奔跑：-2%（保护利润）
        - 走弱：-1.5%（严格止损）
        - 强保护：-3%（允许波动）
        """
        state = self.get_state(position)
        config = self.get_config(state)
        return config["stop_loss_pct"]

    def should_exit(self, position: Dict) -> Tuple[bool, str]:
        """判断是否应该退出

        Returns:
            (should_exit, reason)
        """
        state = self.get_state(position)
        config = self.get_config(state)
        pnl_pct = position.get("pnl_pct", 0)
        peak_pnl = position.get("peak_pnl", max(0, pnl_pct))
        drawdown = peak_pnl - pnl_pct
        holding_days = position.get("holding_days", 0) or self._calc_days(position.get("buy_date"))

        # 1. 止损触发
        if pnl_pct <= config["stop_loss_pct"]:
            return True, f"止损触发: {pnl_pct:.1f}% <= {config['stop_loss_pct']:.1f}%"

        # 2. 盈利回撤超阈值
        if peak_pnl > 0 and drawdown >= config["drawdown_threshold"]:
            return True, f"盈利回撤: {drawdown:.1f}% >= {config['drawdown_threshold']:.1f}%"

        # 3. 状态为 EXIT_PENDING
        if state == PositionState.EXIT_PENDING:
            return True, "状态为待退出"

        # 4. WEAKENING 状态超时
        if state == PositionState.WEAKENING and holding_days > config["max_holding_days"]:
            return True, f"走弱状态超时: {holding_days}天 > {config['max_holding_days']}天"

        return False, ""

    def _calc_days(self, buy_date: str) -> int:
        """计算持仓天数"""
        if not buy_date:
            return 0
        try:
            if 'T' in buy_date:
                entry = datetime.fromisoformat(buy_date.replace('Z', '+00:00'))
            else:
                entry = datetime.strptime(buy_date[:10], "%Y-%m-%d")
            return (datetime.now() - entry.replace(tzinfo=None)).days
        except Exception:
            return 0

    def _get_suggestion(self, state: PositionState, position: Dict) -> str:
        """获取操作建议"""
        suggestions = {
            PositionState.NEW_POSITION: "观察期，给足空间",
            PositionState.PROFIT_RUNNING: "持有，跟踪止损",
            PositionState.WEAKENING: "⚠️ 走弱，建议减仓或退出",
            PositionState.EXIT_PENDING: "🔴 待执行卖出",
            PositionState.PROTECTED: "强保护，可持有或加仓",
        }
        return suggestions.get(state, "持有")


def analyze_portfolio_states(positions: List[Dict]) -> Dict:
    """分析投资组合状态分布

    Args:
        positions: 持仓列表

    Returns:
        状态分布统计
    """
    sm = PositionStateMachine()

    state_counts = {s.value: 0 for s in PositionState}
    state_positions = {s.value: [] for s in PositionState}

    for pos in positions:
        info = sm.get_state_info(pos)
        state = info["state"]
        state_counts[state] += 1
        state_positions[state].append(info)

    return {
        "total": len(positions),
        "state_counts": state_counts,
        "state_positions": state_positions,
        "health_score": _calc_health_score(state_counts, len(positions)),
    }


def _calc_health_score(state_counts: Dict[str, int], total: int) -> float:
    """计算组合健康分

    健康分计算：
    - PROFIT_RUNNING + PROTECTED: 每只 +20 分
    - NEW_POSITION: 每只 +10 分
    - WEAKENING: 每只 -15 分
    - EXIT_PENDING: 每只 -30 分

    最高 100 分，最低 0 分
    """
    if total == 0:
        return 100.0

    score = 50.0  # 基础分

    score += state_counts.get(PositionState.PROFIT_RUNNING.value, 0) * 15
    score += state_counts.get(PositionState.PROTECTED.value, 0) * 20
    score += state_counts.get(PositionState.NEW_POSITION.value, 0) * 5
    score -= state_counts.get(PositionState.WEAKENING.value, 0) * 15
    score -= state_counts.get(PositionState.EXIT_PENDING.value, 0) * 30

    return max(0, min(100, score))
