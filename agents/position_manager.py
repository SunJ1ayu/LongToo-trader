#!/usr/bin/env python3
"""
Position Manager Agent - 独立持仓管理 Agent

核心理念：
1. 买入逻辑和卖出逻辑完全独立
2. 盈利持仓和亏损持仓是不同的资产状态，必须区别对待
3. 结构收缩优先处理亏损股/弱势股/无效持仓

职责：
1. manage_existing_positions() - 独立风控卖出（不依赖买点）
2. normalize_position_count() - 结构收缩（分层减仓）
3. calculate_exit_score() - 组内卖出优先级评分

卖出规则（分层）：
Layer A - 强制卖出（最高优先级）：
  - 止损触发：亏损超过阈值
  - 盈利回撤：从峰值回撤超过阈值
  - 持仓超时无效：持有N天仍无盈利

Layer B - 结构收缩（次优先级）：
  - 亏损股/持平股：按 exit_score 排序
  - 弱盈利股：盈利<保护线，趋势恶化

Layer C - 盈利保护（最后才动）：
  - 强盈利股：盈利>保护线，不参与结构收缩
"""

import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from .base import BaseAgent, AgentResult
from .position_state_machine import PositionStateMachine, PositionState
from .trend_analyzer import TrendAnalyzer

logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_CONFIG = {
    "max_positions": 5,
    # Layer A - 强制卖出
    "stop_loss_pct": -8.0,           # 止损阈值（ROE 月频策略，放宽至 -8%）
    "profit_drawdown_pct": 5.0,      # 盈利回撤阈值（从峰值）
    "max_holding_days_no_profit": 10, # 持仓超时（无盈利）
    # Layer B - 结构收缩
    "profit_protect_line": 1.5,      # 盈利保护线（%）
    # exit_score 权重（仅用于组内排序）
    "pnl_weight": 0.5,               # 盈亏权重（提高）
    "trend_weight": 0.3,             # 趋势权重
    "holding_days_weight": 0.15,     # 持仓时间权重（降低）
    "volume_weight": 0.05,           # 成交量权重
    # 加仓配置（单次加仓）
    "add_to_winner_threshold": 5.0,  # 加仓门槛：盈利超过5%
    "add_position_pct": 5.0,         # 加仓仓位：增加5%（从10%→15%）
    "max_add_count": 1,              # 最大加仓次数：只允许1次
    # 替换配置
    "replacement_difference_threshold": 15,  # 差值阈值：候选评分 - 持仓评分 > 此值才替换
    "replacement_cooldown_days": 3,          # 被替换股票冷却天数
}


class PositionManagerAgent(BaseAgent):
    """独立持仓管理 Agent

    与买入逻辑完全解耦，每天独立运行。
    集成状态机，不同状态对应不同的风控策略。
    """

    def __init__(self, data_provider, config: Dict = None):
        """
        Args:
            data_provider: 数据提供者（用于获取K线）
            config: 配置字典
        """
        super().__init__()
        self.data_provider = data_provider
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.state_machine = PositionStateMachine()  # 状态机
        self.trend_analyzer = TrendAnalyzer()  # 趋势分析器

    def health_check(self) -> bool:
        return self.data_provider is not None

    def handle_message(self, message):
        """处理消息（消息总线用）"""
        pass

    def process(self, input_data: Dict) -> AgentResult:
        """执行持仓管理（分层架构）

        Layer A: 强制卖出（止损、盈利回撤、超时无效）
        Layer B: 趋势破坏卖出
        Layer C: 结构收缩卖出（分层减仓）

        Args:
            input_data: 包含
                - positions: 当前持仓列表

        Returns:
            AgentResult: data 包含
                - sell_signals: 需要卖出的信号列表
                - breakdown: 各层卖出统计
        """
        positions = input_data.get("positions", [])

        if not positions:
            return AgentResult(success=True, data={"sell_signals": [], "managed": 0})

        # Layer A: 强制卖出（最高优先级）
        layer_a_sells = self._manage_risk_sells(positions)

        # Layer B: 趋势破坏卖出
        layer_b_sells = self._manage_trend_sells(positions)

        # 合并 Layer A + Layer B
        forced_sells = self.merge_sell_signals(layer_a_sells, layer_b_sells)

        logger.info(f"持仓管理: LayerA={len(layer_a_sells)}只(强制), LayerB={len(layer_b_sells)}只(趋势)")

        return AgentResult(
            success=True,
            data={
                "sell_signals": forced_sells,
                "managed": len(positions),
                "breakdown": {
                    "stop_loss": len([s for s in layer_a_sells if s["reason"] == "stop_loss"]),
                    "profit_drawdown": len([s for s in layer_a_sells if s["reason"] == "profit_drawdown"]),
                    "timeout_no_profit": len([s for s in layer_a_sells if s["reason"] == "timeout_no_profit"]),
                    "trend_break": len(layer_b_sells)
                }
            }
        )

    def normalize_positions(self, positions: List[Dict], target_count: int = None) -> List[Dict]:
        """结构收缩 - 分层减仓到目标数量

        核心原则：
        1. 盈利持仓和亏损持仓是不同的资产状态
        2. 结构收缩优先处理亏损股/弱势股/无效持仓
        3. 盈利股默认保护，除非趋势恶化或盈利回撤

        分层逻辑：
        - Group A（优先卖）：亏损股 + 持平股 (pnl <= 0)
        - Group B（次优先）：弱盈利股 (pnl > 0 but < profit_protect_line)
        - Group C（保护）：强盈利股 (pnl >= profit_protect_line)，不参与收缩

        Args:
            positions: 当前持仓
            target_count: 目标持仓数（默认从配置读取）

        Returns:
            需要卖出的信号列表
        """
        if target_count is None:
            target_count = self.config["max_positions"]

        current_count = len([p for p in positions if p.get("shares", 0) > 0])

        if current_count <= target_count:
            logger.info(f"持仓数 {current_count} 未超限，无需收缩")
            return []

        sell_count = current_count - target_count
        logger.info(f"⚠️ 持仓超限: {current_count}/{target_count}，需卖出 {sell_count} 只")

        # 分层分组
        profit_protect_line = self.config["profit_protect_line"]

        group_a = []  # 亏损/持平股
        group_b = []  # 弱盈利股
        group_c = []  # 强盈利股（保护）

        for pos in positions:
            if pos.get("shares", 0) <= 0:
                continue

            pnl_pct = pos.get("pnl_pct", 0)
            score = self.calculate_exit_score(pos)

            if pnl_pct <= 0:
                group_a.append((pos, score))
            elif pnl_pct < profit_protect_line:
                group_b.append((pos, score))
            else:
                group_c.append((pos, score))

        logger.info(f"分层: A组{len(group_a)}只(亏损/持平), B组{len(group_b)}只(弱盈利), C组{len(group_c)}只(强盈利)")

        # 按组内 exit_score 排序（最低分优先）
        group_a.sort(key=lambda x: x[1])
        group_b.sort(key=lambda x: x[1])

        # 生成卖出信号：优先从 A组，不够再从 B组
        sell_signals = []

        # Layer 1: 从 A组（亏损/持平）卖出
        for pos, score in group_a[:sell_count]:
            sell_signals.append({
                "symbol": pos["symbol"],
                "signal": "sell",
                "price": pos.get("current_price", pos.get("price", 0)),
                "action_shares": pos.get("shares", 0),
                "avg_cost": pos.get("avg_cost", 0),
                "pnl_pct": pos.get("pnl_pct", 0),
                "exit_score": score,
                "reason": "position_overflow_loss",
                "message": f"🔧 收缩亏损仓位: {pos['symbol']} (盈亏{pos['pnl_pct']:+.1f}%, 评分{score:.1f})"
            })

        # Layer 2: 如果 A组不够，从 B组（弱盈利）补充
        remaining = sell_count - len(sell_signals)
        if remaining > 0 and group_b:
            for pos, score in group_b[:remaining]:
                sell_signals.append({
                    "symbol": pos["symbol"],
                    "signal": "sell",
                    "price": pos.get("current_price", pos.get("price", 0)),
                    "action_shares": pos.get("shares", 0),
                    "avg_cost": pos.get("avg_cost", 0),
                    "pnl_pct": pos.get("pnl_pct", 0),
                    "exit_score": score,
                    "reason": "position_overflow_weak_profit",
                    "message": f"🔧 收缩弱盈利: {pos['symbol']} (盈亏{pos['pnl_pct']:+.1f}%, 评分{score:.1f})"
                })

        # Layer 3: C组（强盈利）保护，不参与收缩
        if len(sell_signals) < sell_count:
            logger.warning(f"⚠️ 强盈利股 {len(group_c)} 只受保护，不参与收缩")

        return sell_signals

    def evaluate_replacements(self, candidates: List[Dict], holdings: List[Dict],
                              replaced_today: List[str] = None,
                              replacement_cooldown: Dict[str, int] = None) -> List[Dict]:
        """评估是否应该用候选股替换最差持仓

        替换条件（差值判断）：
        1. best_buy_score - worst_exit_score > difference_threshold（明显优于）
        2. 冷却期检查通过（被替换股票 N 天内不能重新买入）

        Args:
            candidates: 候选股列表，每只含 symbol, score (buy_score)
            holdings: 当前持仓列表，每只含 symbol, pnl_pct, shares 等
            replaced_today: 今日已替换的 symbol 列表（频率限制）
            replacement_cooldown: {symbol: 替换日期} 冷却记录

        Returns:
            需要卖出的替换信号列表（仅卖信号，买信号由常规流程处理）
        """
        if not candidates or not holdings:
            return []

        # 每天最多替换 1 次
        if replaced_today and len(replaced_today) >= 1:
            logger.debug("替换跳过: 今日已执行过替换")
            return []

        # 差值阈值：候选股必须明显优于持仓
        difference_threshold = self.config.get("replacement_difference_threshold", 15)
        # 冷却天数
        cooldown_days = self.config.get("replacement_cooldown_days", 3)

        # 计算每只持仓的 exit_score（越低越该卖）
        scored_holdings = []
        for pos in holdings:
            if pos.get("shares", 0) <= 0:
                continue
            symbol = pos["symbol"]

            # 冷却期检查：被替换卖出的股票 N 天内不能再被替换买入
            if replacement_cooldown and symbol in replacement_cooldown:
                from datetime import datetime, timedelta
                replaced_date = replacement_cooldown[symbol]
                if isinstance(replaced_date, str):
                    replaced_date = datetime.strptime(replaced_date[:10], "%Y-%m-%d").date()
                cooldown_end = replaced_date + timedelta(days=cooldown_days)
                if datetime.now().date() < cooldown_end:
                    logger.debug(f"替换跳过: {symbol} 在冷却期内（至 {cooldown_end}）")
                    continue

            exit_score = self.calculate_exit_score(pos)
            scored_holdings.append((pos, exit_score))

        if not scored_holdings:
            return []

        # 按 exit_score 升序（最该卖的排前面）
        scored_holdings.sort(key=lambda x: x[1])

        # 取最差持仓
        worst_holding, worst_exit_score = scored_holdings[0]

        # 取最佳候选
        best_candidate = max(candidates, key=lambda c: c.get("score", 0))
        best_buy_score = best_candidate.get("score", 0)

        # 核心判断：差值是否足够大
        score_diff = best_buy_score - worst_exit_score
        if score_diff < difference_threshold:
            logger.debug(f"替换跳过: 差值{score_diff:.1f} < 阈值{difference_threshold} "
                        f"(候选{best_buy_score:.0f} vs 持仓{worst_exit_score:.0f})")
            return []

        symbol = worst_holding["symbol"]
        pnl_pct = worst_holding.get("pnl_pct", 0)

        sell_signals = [{
            "symbol": symbol,
            "signal": "sell",
            "price": worst_holding.get("current_price", worst_holding.get("price", 0)),
            "action_shares": worst_holding.get("shares", 0),
            "avg_cost": worst_holding.get("avg_cost", 0),
            "pnl_pct": pnl_pct,
            "exit_score": worst_exit_score,
            "reason": "replacement",
            "replacement_candidate": best_candidate.get("symbol"),
            "replacement_score": best_buy_score,
            "score_diff": score_diff,
            "message": f"🔄 替换: {symbol} (评分{worst_exit_score:.0f}, 盈亏{pnl_pct:+.1f}%) → "
                       f"{best_candidate.get('symbol')}(评分{best_buy_score:.0f}, 差值{score_diff:.0f})"
        }]

        logger.info(f"触发替换: {sell_signals[0]['message']}")
        return sell_signals

    def check_add_to_winners(self, positions: List[Dict]) -> List[Dict]:
        """检测符合加仓条件的持仓

        加仓条件（全部满足）：
        1. pnl_pct > add_to_winner_threshold（默认5%）
        2. state == PROFIT_RUNNING 或 PROTECTED
        3. add_count < max_add_count（默认只允许1次）
        4. 当前仓位 < 最大仓位限制（20%）

        Args:
            positions: 当前持仓列表

        Returns:
            需要加仓的信号列表
        """
        add_signals = []

        threshold = self.config.get("add_to_winner_threshold", 5.0)
        add_position_pct = self.config.get("add_position_pct", 5.0)
        max_add_count = self.config.get("max_add_count", 1)
        max_position_pct = 20.0  # 单只股票最大仓位

        for pos in positions:
            if pos.get("shares", 0) <= 0:
                continue

            symbol = pos["symbol"]
            pnl_pct = pos.get("pnl_pct", 0)
            add_count = pos.get("add_count", 0)

            # 计算当前仓位占比（需要总资产信息）
            # 这里简化处理，用 market_value 和总资产比例
            # 实际调用时传入 total_assets
            current_position_pct = pos.get("position_pct", 10)  # 默认10%

            # 获取状态机状态
            # 条件1：盈利超过阈值（便宜检查放前面）
            if pnl_pct < threshold:
                continue

            # 获取状态机状态（昂贵操作放在阈值检查之后）
            state_info = self.state_machine.get_state_info(pos)
            state = state_info["state"]

            # 条件2：状态为 PROFIT_RUNNING 或 PROTECTED
            if state not in [PositionState.PROFIT_RUNNING.value, PositionState.PROTECTED.value]:
                continue

            # 条件3：加仓次数未超限
            if add_count >= max_add_count:
                continue

            # 条件4：仓位未超限
            if current_position_pct >= max_position_pct:
                continue

            # 计算加仓后仓位
            target_position_pct = min(current_position_pct + add_position_pct, max_position_pct)
            actual_add_pct = target_position_pct - current_position_pct

            add_signals.append({
                "symbol": symbol,
                "signal": "add_to_winner",
                "current_shares": pos.get("shares", 0),
                "current_position_pct": current_position_pct,
                "add_position_pct": actual_add_pct,
                "target_position_pct": target_position_pct,
                "pnl_pct": pnl_pct,
                "state": state,
                "add_count": add_count,
                "avg_cost": pos.get("avg_cost", 0),
                "current_price": pos.get("current_price", pos.get("price", 0)),
                "reason": f"盈利{pnl_pct:.1f}%超过{threshold}%阈值，状态{state}"
            })

            logger.info(f"📈 加仓候选: {symbol} 盈利{pnl_pct:.1f}%，当前仓位{current_position_pct:.1f}%，加仓{actual_add_pct:.1f}%")

        return add_signals

    def calculate_exit_score(self, position: Dict) -> float:
        """计算卖出优先级评分

        评分越低 = 越该卖

        因子：
        - pnl_weight: 盈亏（亏损越大分数越低）
        - trend_weight: 趋势（趋势越弱分数越低）
        - holding_days_weight: 持仓时间（持仓越久分数越低）
        - volume_weight: 成交量（放量下跌分数低）

        Args:
            position: 持仓信息

        Returns:
            exit_score (0-100，越低越该卖)
        """
        score = 50.0  # 基准分

        # 1. 盈亏因子 (权重40%)
        pnl_pct = position.get("pnl_pct", 0)
        pnl_score = 50 + pnl_pct * 2  # 亏损3% = 44分，盈利3% = 56分
        pnl_score = max(0, min(100, pnl_score))

        # 2. 趋势因子 (权重30%) - 使用技术指标
        trend_score = self._get_trend_score(position)

        # 3. 持仓时间因子 (权重20%)
        entry_date = position.get("entry_date") or position.get("created_at")
        holding_days = self._calculate_holding_days(entry_date)
        # 持仓越久分数越低（鼓励快速脱离成本）
        days_score = max(0, 100 - holding_days * 3)  # 10天 = 70分, 20天 = 40分

        # 4. 成交量因子 (权重10%) - 暂用均价偏离替代
        current_price = position.get("current_price", position.get("price", 0))
        avg_price = position.get("avg_price_5d", current_price)  # 如果有5日均价
        if avg_price > 0:
            price_deviation = (current_price - avg_price) / avg_price
            volume_score = 50 - price_deviation * 100  # 低于均价 = 分数低
        else:
            volume_score = 50

        # 加权计算
        weights = self.config
        final_score = (
            pnl_score * weights["pnl_weight"] +
            trend_score * weights["trend_weight"] +
            days_score * weights["holding_days_weight"] +
            volume_score * weights["volume_weight"]
        )

        return round(final_score, 1)

    # ========== 私有方法 ==========

    def _calculate_reduce_ratio(self, drawdown: float, state_config: Dict,
                                 reduced_from_peak: bool) -> Tuple[int, str]:
        """根据回撤程度计算减仓比例

        Args:
            drawdown: 从 peak_pnl 回撤的幅度
            state_config: 状态配置
            reduced_from_peak: 是否已触发过减仓

        Returns:
            (reduce_pct, action_message)
        """
        # 已减仓用收紧的阈值
        tiers_key = "reduced_drawdown_tiers" if reduced_from_peak else "drawdown_tiers"
        tiers = state_config.get(tiers_key, [])

        # 从大到小遍历，找到最高阈值满足条件的
        result = (0, "")
        for tier in tiers:
            if drawdown >= tier["threshold"]:
                result = (tier["reduce_pct"], tier["action"])
                # 继续遍历，找更高的阈值

        return result

    def _manage_risk_sells(self, positions: List[Dict]) -> List[Dict]:
        """Layer A - 强制卖出（最高优先级）

        使用状态机动态风控：
        - NEW_POSITION: 止损 -5%（观察期给空间）
        - PROFIT_RUNNING: 止损 -2%（保护利润）
        - WEAKENING: 止损 -1.5%（严格止损）
        - PROTECTED: 止损 -3%（允许波动）

        规则：
        1. 状态机判定 should_exit()
        2. 止损触发：亏损超过状态阈值 → 清仓
        3. 盈利回撤：分层减仓（30%→50%→清仓）
        4. 持仓超时无效：WEAKENING 状态超时
        """
        sell_signals = []

        for pos in positions:
            if pos.get("shares", 0) <= 0:
                continue

            symbol = pos["symbol"]
            pnl_pct = pos.get("pnl_pct", 0)
            peak_pnl = pos.get("peak_pnl", max(0, pnl_pct))
            drawdown = peak_pnl - pnl_pct

            # 计算持仓天数
            entry_date = pos.get("entry_date") or pos.get("buy_date")
            holding_days = self._calculate_holding_days(entry_date)
            pos["holding_days"] = holding_days

            # 状态机判定
            state_info = self.state_machine.get_state_info(pos)
            state = state_info["state"]
            state_config = state_info["config"]

            # 动态止损阈值（根据状态）
            dynamic_stop_loss = state_config["stop_loss_pct"]

            # 减仓标记
            reduced_from_peak = pos.get("reduced_from_peak", False)

            # 检查是否应该退出
            should_exit, reason = self.state_machine.should_exit(pos)

            if should_exit:
                sell_signals.append({
                    "symbol": symbol,
                    "signal": "sell",
                    "price": pos.get("current_price", 0),
                    "action_shares": pos.get("shares", 0),
                    "avg_cost": pos.get("avg_cost", 0),
                    "pnl_pct": pnl_pct,
                    "peak_pnl": peak_pnl,
                    "drawdown": drawdown,
                    "state": state,
                    "reason": "state_machine_exit",
                    "message": f"🚨 状态退出: {symbol} ({state}, {reason})"
                })
                continue

            # Rule 1: 动态止损触发 → 清仓
            if pnl_pct <= dynamic_stop_loss:
                sell_signals.append({
                    "symbol": symbol,
                    "signal": "sell",
                    "price": pos.get("current_price", 0),
                    "action_shares": pos.get("shares", 0),
                    "avg_cost": pos.get("avg_cost", 0),
                    "pnl_pct": pnl_pct,
                    "state": state,
                    "reason": "dynamic_stop_loss",
                    "message": f"🛑 动态止损: {symbol} ({state}, 亏损{pnl_pct:.1f}% <= {dynamic_stop_loss:.1f}%)"
                })
                # v2.5.0: 发布 STOP_TRIGGERED 事件
                self._emit_stop_event(symbol, state, pnl_pct, "dynamic_stop_loss")
                continue

            # Rule 2: 盈利回撤 → 分层减仓（核心逻辑）
            if peak_pnl > 0 and drawdown > 0:
                reduce_pct, action = self._calculate_reduce_ratio(
                    drawdown, state_config, reduced_from_peak
                )
                if reduce_pct > 0:
                    reduce_shares = int(pos.get("shares", 0) * reduce_pct / 100)
                    # 确保减仓数量有效（至少1股）
                    if reduce_shares > 0:
                        sell_signals.append({
                            "symbol": symbol,
                            "signal": "sell",
                            "price": pos.get("current_price", 0),
                            "action_shares": reduce_shares,  # 部分卖出
                            "avg_cost": pos.get("avg_cost", 0),
                            "pnl_pct": pnl_pct,
                            "peak_pnl": peak_pnl,
                            "drawdown": drawdown,
                            "state": state,
                            "reason": "profit_protection",
                            "reduce_pct": reduce_pct,
                            "reduce_type": action,
                            "reduced_from_peak": True,  # 标记已减仓
                            # peak_pnl 保持不变！
                            "message": f"🛡️ 盈利保护: {symbol} ({action}, 回撤{drawdown:.1f}%)"
                        })
                        continue

        return sell_signals

    def _manage_trend_sells(self, positions: List[Dict]) -> List[Dict]:
        """趋势破坏卖出（Layer B）

        检测信号：
        1. 跌破均线（MA5/MA10/MA20）
        2. 放量阴线（成交量放大1.5倍 + 收跌）
        3. 连续下跌（连续3天以上收跌）
        4. MACD 死叉

        只有趋势破坏程度达到 'broken' 或 'critical' 才触发卖出
        """
        sell_signals = []

        for pos in positions:
            if pos.get("shares", 0) <= 0:
                continue

            symbol = pos["symbol"]
            current_price = pos.get("current_price", pos.get("price", 0))

            # 使用趋势分析器
            trend = self.trend_analyzer.analyze_trend(symbol, current_price)
            break_level = trend['break_level']

            # 只有严重破坏才立即卖出
            if break_level == 'critical':
                reasons = ', '.join([s['name'] for s in trend['signals'] if s['triggered']])
                sell_signals.append({
                    "symbol": symbol,
                    "signal": "sell",
                    "price": current_price,
                    "action_shares": pos.get("shares", 0),
                    "avg_cost": pos.get("avg_cost", 0),
                    "pnl_pct": pos.get("pnl_pct", 0),
                    "trend_score": trend['trend_score'],
                    "reason": "trend_critical",
                    "message": f"🔴 趋势严重破坏: {symbol} (评分{trend['trend_score']}, {reasons})"
                })

            # 趋势破坏（中等程度）+ WEAKENING 状态 → 卖出
            elif break_level == 'broken':
                state_info = self.state_machine.get_state_info(pos)
                state = state_info['state']

                # 只有 WEAKENING 状态下的趋势破坏才卖出
                if state == 'WEAKENING':
                    reasons = ', '.join([s['name'] for s in trend['signals'] if s['triggered']])
                    sell_signals.append({
                        "symbol": symbol,
                        "signal": "sell",
                        "price": current_price,
                        "action_shares": pos.get("shares", 0),
                        "avg_cost": pos.get("avg_cost", 0),
                        "pnl_pct": pos.get("pnl_pct", 0),
                        "trend_score": trend['trend_score'],
                        "state": state,
                        "reason": "trend_broken_with_weakening",
                        "message": f"📉 趋势破坏+走弱: {symbol} ({state}, 评分{trend['trend_score']})"
                    })

        return sell_signals

    def _manage_timeout_sells(self, positions: List[Dict]) -> List[Dict]:
        """持仓超时卖出 - 已合并到 _manage_risk_sells 的 Rule 3

        保留此方法以兼容现有架构，但逻辑已移至 Layer A
        """
        # 已在 _manage_risk_sells 中处理
        return []

    def _emit_stop_event(self, symbol: str, state: str, pnl_pct: float, reason: str):
        """发布 TRADE_STOP_TRIGGERED 事件（v2.5.0）

        Args:
            symbol: 股票代码
            state: 当前状态机状态
            pnl_pct: 当前盈亏百分比
            reason: 触发原因
        """
        try:
            from scripts.messaging.local_bus import Message, Channel

            message = Message(
                channel=Channel.TRADE_STOP_TRIGGERED,
                sender=self.name,
                msg_type="event",
                payload={
                    "symbol": symbol,
                    "state": state,
                    "pnl_pct": pnl_pct,
                    "reason": reason,
                    "timestamp": datetime.now().isoformat()
                }
            )

            if self._message_bus:
                self._message_bus.publish_sync(message)
                logger.info(f"事件发布: TRADE_STOP_TRIGGERED {symbol} ({reason})")
            else:
                logger.debug(f"无消息总线，跳过事件发布")

        except Exception as e:
            logger.warning(f"发布止损事件失败: {e}")

    @staticmethod
    def merge_sell_signals(*signal_lists) -> List[Dict]:
        """合并卖出信号（按symbol去重，保留优先级最高的原因）

        可被外部调用，无需实例化。
        """
        priority = {
            "stop_loss": 1, "dynamic_stop_loss": 1, "profit_protection": 1,
            "state_machine_exit": 2, "trend_critical": 2, "trend_broken_with_weakening": 2,
            "timeout_no_profit": 3,
            "position_overflow_loss": 4, "position_overflow_weak_profit": 4,
        }

        merged = {}
        for signals in signal_lists:
            for sig in signals:
                symbol = sig["symbol"]
                if symbol not in merged:
                    merged[symbol] = sig
                else:
                    if priority.get(sig["reason"], 99) < priority.get(merged[symbol]["reason"], 99):
                        merged[symbol] = sig

        return list(merged.values())

    def _get_trend_score(self, position: Dict) -> float:
        """计算趋势得分"""
        # 尝试从持仓数据中获取趋势指标
        # 如果没有，默认50分（中性）
        trend_score = 50.0

        # 使用 current_price vs avg_cost 的关系作为简单趋势判断
        current_price = position.get("current_price", position.get("price", 0))
        avg_cost = position.get("avg_cost", 0)

        if avg_cost > 0:
            # 价格在成本之上 = 趋势向好
            price_ratio = current_price / avg_cost
            if price_ratio > 1.05:
                trend_score = 70
            elif price_ratio > 1.02:
                trend_score = 60
            elif price_ratio > 1.0:
                trend_score = 55
            elif price_ratio > 0.97:
                trend_score = 45
            else:
                trend_score = 30

        return trend_score

    def _get_ma_value(self, symbol: str, period: int) -> Optional[float]:
        """获取MA值"""
        try:
            # 尝试使用 data_provider 获取K线
            if hasattr(self.data_provider, 'get_klines'):
                klines = self.data_provider.get_klines(symbol, limit=period)
                if klines and len(klines) >= period:
                    closes = [k['close'] for k in klines[-period:]]
                    return sum(closes) / len(closes)
        except Exception as e:
            logger.debug(f"获取MA失败 {symbol}: {e}")

        return None

    def _calculate_holding_days(self, entry_date: str) -> int:
        """计算持仓天数"""
        if not entry_date:
            return 0

        try:
            # 尝试解析日期
            if 'T' in entry_date:
                entry = datetime.fromisoformat(entry_date.replace('Z', '+00:00'))
            else:
                entry = datetime.strptime(entry_date[:10], "%Y-%m-%d")

            return (datetime.now() - entry.replace(tzinfo=None)).days
        except Exception:
            return 0
