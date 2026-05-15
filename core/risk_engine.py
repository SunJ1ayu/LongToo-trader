#!/usr/bin/env python3
"""中央化风控引擎 - Risk Engine

单一真相来源：所有风控检查只在这里实现
三层调用同一逻辑，不同阶段不同严格度

架构原则：
- 风控必须读取权威数据源（数据库）
- state 是展示层，DB 是交易真相
- Defense in Depth：多层冗余
"""

from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, Any
from enum import Enum
from datetime import datetime, timedelta
import logging

try:
    from .event_store import EventStore, EventType
except ImportError:
    EventStore = None
    EventType = None

logger = logging.getLogger(__name__)


class Stage(Enum):
    """风控检查阶段"""
    PRE_TRADE = "pre_trade"           # 风控层，完整检查
    PRE_EXECUTION = "pre_execution"   # 执行层，快速检查
    HARD_BLOCK = "hard_block"         # 最终阻断，必须通过


@dataclass
class RiskResult:
    """风控检查结果"""
    allowed: bool
    reason: str = ""
    adjusted_shares: Optional[int] = None  # 截断后的股数（仓位超限时）
    priority: str = ""  # stop_loss 等


@dataclass
class RiskConfig:
    """风控配置"""
    max_positions: int = 5
    max_position_pct: float = 0.20  # 单只股票最大仓位比例
    max_daily_loss_pct: float = -0.05  # 单日最大亏损
    max_daily_trades: int = 5  # 每日最大交易次数（ROE 月频调仓需买满 Top5）
    cooldown_hours: int = 12  # 冷却期小时数
    consecutive_loss_limit: int = 3  # 连续亏损次数限制


class RiskEngine:
    """中央化风控引擎

    统一管理所有风控规则，避免逻辑漂移。
    三层调用同一 validate() 方法，通过 stage 参数区分严格度。

    Usage:
        engine = RiskEngine(storage, state_provider, config)
        result = engine.validate(symbol, action, shares, price, stage="pre_trade")
        if not result.allowed:
            reject()
        if result.adjusted_shares:
            shares = result.adjusted_shares
    """

    def __init__(
        self,
        storage,  # PaperTradingStorage 实例
        state_provider: Callable[[], Dict],  # 获取 state 的回调
        config: Optional[Dict] = None,
        event_store=None
    ):
        self.storage = storage
        self.state_provider = state_provider
        self.config = RiskConfig(**(config or {}))
        self.event_store = event_store

    def validate(
        self,
        symbol: str,
        action: str,
        shares: int,
        price: float,
        stage: str = Stage.HARD_BLOCK.value,
        priority: str = ""
    ) -> RiskResult:
        """统一风控检查入口

        Args:
            symbol: 股票代码
            action: "buy" 或 "sell"
            shares: 股数
            price: 价格
            stage: 检查阶段 (pre_trade / pre_execution / hard_block)
            priority: 优先级标记 (stop_loss 等)

        Returns:
            RiskResult: 检查结果
        """
        # 0. 止损卖出最高优先级，直接放行
        if action == "sell" and priority == "stop_loss":
            return RiskResult(allowed=True, priority="stop_loss")

        result = RiskResult(allowed=True)

        try:
            state = self.state_provider()
        except Exception as e:
            logger.error(f"获取状态失败: {e}")
            self._emit_rejected_event(symbol, action, shares, price, stage, f"⚠️ 无法获取风控状态: {e}")
            return RiskResult(allowed=False, reason=f"⚠️ 无法获取风控状态: {e}")

        # === 全阶段必查项 ===

        # 1. 紧急停止（所有阶段都检查）
        result = self._check_emergency_stop(state, result)
        if not result.allowed:
            self._emit_rejected_event(symbol, action, shares, price, stage, result.reason)
            return result

        # === pre_trade 专属检查（完整风控）===
        if stage == Stage.PRE_TRADE.value:
            result = self._check_cooldown(state, result)
            if not result.allowed:
                self._emit_rejected_event(symbol, action, shares, price, stage, result.reason)
                return result

            result = self._check_daily_loss(state, result)
            if not result.allowed:
                self._emit_rejected_event(symbol, action, shares, price, stage, result.reason)
                return result

            result = self._check_daily_trades(state, result)
            if not result.allowed:
                self._emit_rejected_event(symbol, action, shares, price, stage, result.reason)
                return result

        # === 买入专属检查 ===
        if action == "buy":
            # 2. 持仓数量限制（所有阶段都检查）
            result = self._check_position_limit(symbol, result)
            if not result.allowed:
                self._emit_rejected_event(symbol, action, shares, price, stage, result.reason)
                return result

            # 3. 单只股票仓位限制
            result = self._check_single_position(symbol, shares, price, stage, result)
            if not result.allowed:
                self._emit_rejected_event(symbol, action, shares, price, stage, result.reason)
                return result

            # 如果截断了股数，更新 shares 用于后续检查
            if result.adjusted_shares:
                shares = result.adjusted_shares

            # 4. 资金充足（hard_block 阶段才检查）
            if stage == Stage.HARD_BLOCK.value:
                result = self._check_cash(shares, price, result)

        if not result.allowed:
            self._emit_rejected_event(symbol, action, shares, price, stage, result.reason)
        return result

    def _emit_rejected_event(self, symbol: str, action: str, shares: int, price: float, stage: str, reason: str):
        """发布风控拦截事件"""
        if not self.event_store:
            return
        try:
            self.event_store.emit(
                event_type=EventType.RISK_REJECTED,
                aggregate_id=symbol,
                aggregate_type="risk",
                payload={
                    "symbol": symbol,
                    "action": action,
                    "shares": shares,
                    "price": price,
                    "stage": stage,
                    "reason": reason
                }
            )
        except Exception as e:
            logger.warning(f"风控拦截事件发布失败: {e}")

    # ==================== 检查方法 ====================

    def _check_emergency_stop(self, state: Dict, result: RiskResult) -> RiskResult:
        """紧急停止检查"""
        if state.get('emergency_stop', False):
            reason = state.get('emergency_stop_reason', '未知原因')
            result.allowed = False
            result.reason = f"🚨 紧急停止已激活: {reason}"
        return result

    def _check_cooldown(self, state: Dict, result: RiskResult) -> RiskResult:
        """连续亏损冷却期检查"""
        consecutive_losses = state.get('consecutive_losses', 0)

        if consecutive_losses < self.config.consecutive_loss_limit:
            return result

        pause_start = state.get('pause_start_time')
        if not pause_start:
            # 首次触发冷却期
            return result  # 让 RiskManager 处理冷却期启动

        try:
            pause_start_dt = datetime.fromisoformat(pause_start)
            cooldown_end = pause_start_dt + timedelta(hours=self.config.cooldown_hours)

            if datetime.now() < cooldown_end:
                remaining = cooldown_end - datetime.now()
                hours = remaining.seconds // 3600
                minutes = (remaining.seconds % 3600) // 60
                result.allowed = False
                result.reason = f"⏸️ 连续亏损{consecutive_losses}次，冷却期中（还剩{hours}小时{minutes}分钟）"
        except Exception as e:
            logger.warning(f"冷却期计算失败: {e}")

        return result

    def _check_daily_loss(self, state: Dict, result: RiskResult) -> RiskResult:
        """单日最大亏损检查"""
        daily_pnl_pct = state.get('daily_pnl_pct', 0)

        if daily_pnl_pct <= self.config.max_daily_loss_pct:
            result.allowed = False
            result.reason = f"📉 当日亏损已达{daily_pnl_pct*100:.2f}%，超过阈值{self.config.max_daily_loss_pct*100:.1f}%"

        return result

    def _check_daily_trades(self, state: Dict, result: RiskResult) -> RiskResult:
        """每日交易次数检查"""
        daily_trade_count = state.get('daily_trade_count', 0)

        if daily_trade_count >= self.config.max_daily_trades:
            result.allowed = False
            result.reason = f"📊 今日交易次数已达上限{self.config.max_daily_trades}次"

        return result

    def _check_position_limit(self, symbol: str, result: RiskResult) -> RiskResult:
        """持仓数量限制检查（直接查数据库）"""
        try:
            positions = self.storage.get_positions()
            current_count = len([p for p in positions if p.shares > 0])
            existing = self.storage.get_position(symbol)

            if existing is None and current_count >= self.config.max_positions:
                result.allowed = False
                result.reason = f"📦 持仓已达上限 {current_count}/{self.config.max_positions}，无法买入新股票"
        except Exception as e:
            logger.error(f"持仓查询失败: {e}")
            result.allowed = False
            result.reason = f"⚠️ 无法查询持仓状态: {e}"

        return result

    def _check_single_position(
        self,
        symbol: str,
        shares: int,
        price: float,
        stage: str,
        result: RiskResult
    ) -> RiskResult:
        """单只股票仓位限制检查"""
        try:
            account = self.storage.get_account()
            max_value = account.total_assets * self.config.max_position_pct

            existing = self.storage.get_position(symbol)
            current_value = (existing.shares * price) if existing else 0
            new_value = current_value + shares * price

            if new_value > max_value:
                if stage == Stage.HARD_BLOCK.value:
                    # 硬阻断模式：拒绝
                    result.allowed = False
                    result.reason = f"单只仓位超限: 当前{current_value:,.0f} + 新增{shares*price:,.0f} > 上限{max_value:,.0f}"
                else:
                    # 软拦截模式：截断到上限
                    allowed_value = max_value - current_value
                    if allowed_value > 0:
                        adjusted = int(allowed_value / price / 100) * 100
                        if adjusted >= 100:
                            result.adjusted_shares = adjusted
                            logger.info(f"仓位截断: {symbol} {shares}股 → {adjusted}股")
                        else:
                            result.allowed = False
                            result.reason = f"单只仓位已满，无法买入"
                    else:
                        result.allowed = False
                        result.reason = f"单只仓位已满，无法加仓"
        except Exception as e:
            logger.error(f"仓位检查失败: {e}")
            # 仓位检查失败，保守处理：允许通过（因为还有 hard_block 层把关）

        return result

    def _check_cash(self, shares: int, price: float, result: RiskResult) -> RiskResult:
        """资金充足检查"""
        try:
            account = self.storage.get_account()
            # 粗略估算总成本（股价 + 手续费 + 滑点）
            estimated_cost = shares * price * 1.002

            if account.cash < estimated_cost:
                result.allowed = False
                result.reason = f"资金不足: 需要约¥{estimated_cost:,.2f}，可用¥{account.cash:,.2f}"
        except Exception as e:
            logger.error(f"资金查询失败: {e}")
            result.allowed = False
            result.reason = f"⚠️ 无法查询资金状态: {e}"

        return result

    # ==================== 辅助方法 ====================

    def get_current_positions_count(self) -> int:
        """获取当前持仓数量（直接查数据库）"""
        try:
            positions = self.storage.get_positions()
            return len([p for p in positions if p.shares > 0])
        except Exception:
            return 0

    def get_risk_status(self) -> Dict[str, Any]:
        """获取当前风控状态快照"""
        try:
            state = self.state_provider()
            positions_count = self.get_current_positions_count()

            return {
                "positions_count": positions_count,
                "max_positions": self.config.max_positions,
                "emergency_stop": state.get('emergency_stop', False),
                "consecutive_losses": state.get('consecutive_losses', 0),
                "daily_pnl_pct": state.get('daily_pnl_pct', 0),
                "daily_trade_count": state.get('daily_trade_count', 0),
                "cooldown_active": state.get('cooldown_active', False),
            }
        except Exception as e:
            return {"error": str(e)}


# v2.6.0: EventStore事件扩展完成
