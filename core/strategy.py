#!/usr/bin/env python3
"""策略模块 - 定义交易规则和决策逻辑 (V2.0 增强版)

改进点：
1. 买入改为评分制（65分触发），解决"无信号"问题
2. 增加止盈逻辑（15%目标止盈 + 5%保本止盈）
3. 动态止损（根据持仓时间调整：ATR×3 → 追踪止损）
4. 大盘指数判断市场环境（而非个股趋势）
"""

from typing import Dict, List, Optional, Callable
from abc import ABC, abstractmethod
import math
import logging

try:
    from .event_store import EventStore, EventType
except ImportError:
    EventStore = None
    EventType = None

logger = logging.getLogger(__name__)


class TradingStrategy(ABC):
    """交易策略基类"""
    
    @abstractmethod
    def analyze(self, data: Dict) -> Dict:
        """分析数据并返回信号"""
        pass
    
    @abstractmethod
    def get_name(self) -> str:
        """返回策略名称"""
        pass


class MomentumTrendStrategy(TradingStrategy):
    """
    动量趋势策略 (V2.0 增强版)
    
    改进点：
    1. 买入评分制（65分触发），解决"一票否决"导致的无信号问题
    2. 止盈逻辑（15%目标止盈 + 5%保本止盈），及时锁定利润
    3. 动态止损（新开仓ATR×3 → 长期追踪止损），更合理的风控
    """
    
    def __init__(self, config: Optional[Dict] = None, event_store=None, market_regime=None, filters=None):
        self.config = config or {}
        self.event_store = event_store
        self.market_regime = market_regime
        self.filters = filters  # FilterPipeline

        # P0: 向后兼容处理 - 支持旧版int配置和新版dict配置
        threshold_config = self.config.get('buy_score_threshold', 70)
        if isinstance(threshold_config, dict):
            # 新版动态阈值配置，使用neutral作为默认阈值
            self.buy_score_threshold = threshold_config.get('neutral', 60)
            self.dynamic_thresholds = threshold_config
        else:
            # 旧版固定阈值配置
            self.buy_score_threshold = threshold_config
            self.dynamic_thresholds = {'strong': 70, 'neutral': 65, 'weak': 55}
        
        # P0: 向后兼容处理 - 支持旧版float配置和新版dict配置
        position_config = self.config.get('max_position_pct', 0.2)
        if isinstance(position_config, dict):
            # 新版动态仓位配置
            self.max_position_pct = position_config.get('neutral', 0.15)
            self.dynamic_position_sizes = position_config
        else:
            # 旧版固定仓位配置
            self.max_position_pct = position_config
            self.dynamic_position_sizes = {'strong': 0.20, 'neutral': 0.15, 'weak': 0.10}
        
        # 仓位管理（保留原注释）
        
        # 止盈设置
        self.take_profit_pct = self.config.get('take_profit_pct', 0.15)  # 15%目标止盈
        self.take_profit_momentum_threshold = self.config.get('take_profit_momentum_threshold', 0)
        self.break_even_profit_pct = self.config.get('break_even_profit_pct', 0.05)  # 5%保本止盈
        self.break_even_momentum_threshold = self.config.get('break_even_momentum_threshold', -1)
        
        # 止损设置
        self.stop_loss_atr_mult_initial = self.config.get('stop_loss_atr_mult_initial', 3.0)
        self.stop_loss_atr_mult_trailing = self.config.get('stop_loss_atr_mult_trailing', 2.0)
        self.stop_loss_max_drawdown = self.config.get('stop_loss_max_drawdown', 0.05)
    
    def _emit_signal_event(self, symbol: str, result: Dict):
        """发布策略信号事件（如果 event_store 可用）"""
        if not self.event_store:
            return
        try:
            self.event_store.emit(
                event_type=EventType.STRATEGY_SIGNAL_GENERATED,
                aggregate_id=symbol,
                aggregate_type="strategy",
                payload={
                    "symbol": symbol,
                    "signal": result.get("signal"),
                    "score": result.get("score", 0),
                    "reason": result.get("reason", ""),
                    "conditions": result.get("conditions", {}),
                    "priority": result.get("priority", "")
                }
            )
        except Exception as e:
            logger.warning(f"策略信号事件发布失败: {e}")

    def get_name(self) -> str:
        return "动量趋势策略V2"

    def detect_market_state(self, data: Dict, index_data: Dict = None) -> str:
        """
        P0: 检测市场环境状态（基于大盘指数）
        
        Args:
            data: 个股数据（向后兼容，未提供大盘数据时使用）
            index_data: 大盘指数数据（如上证指数），包含 ma10, ma30, price
        
        Returns:
            str: 'strong' | 'neutral' | 'weak'
        """
        # 优先使用大盘指数数据
        if index_data:
            ma10 = index_data.get('ma10')
            ma30 = index_data.get('ma30')
            price = index_data.get('price', 0)
            source = "大盘指数"
        else:
            # 回退到个股数据（向后兼容）
            ma10 = data.get('ma10')
            ma30 = data.get('ma30')
            price = data.get('price', 0)
            source = "个股趋势"
        
        if ma10 and ma30 and price:
            if ma10 > ma30 and price > ma10:
                logger.debug(f"市场环境判断: 强势 ({source})")
                return 'strong'  # 强势：MA多头排列，价格在MA10之上
            elif ma10 > ma30 or price > ma10:
                logger.debug(f"市场环境判断: 震荡 ({source})")
                return 'neutral'  # 震荡：部分多头排列
            else:
                logger.debug(f"市场环境判断: 弱势 ({source})")
                return 'weak'  # 弱势：MA空头排列
        
        logger.debug("市场环境判断: 默认震荡 (数据不足)")
        return 'neutral'  # 默认震荡
    
    def get_dynamic_threshold(self, market_state: str) -> float:
        """
        P0: 根据市场环境获取动态买入阈值
        """
        return self.dynamic_thresholds.get(market_state, self.buy_score_threshold)
    
    def get_dynamic_position_size(self, market_state: str) -> float:
        """
        P0: 根据市场环境获取动态仓位大小
        """
        return self.dynamic_position_sizes.get(market_state, self.max_position_pct)
    
    def calculate_buy_score(self, data: Dict, market_state: str = 'neutral') -> float:
        """
        买入评分 v3 — A股优化版
        
        权重分配（v3）：
        - 趋势条件：20分（降低，避免MA金叉=高分地板）
        - 动量条件：25分（大幅降低，非线性压缩）
        - 技术指标：30分（提高，MACD 15 + RSI 15）
        - 波动率：10分
        - 成交量：10分（提高）
        - A股特色：5分（新增）
        - 负向扣分：追高/超买/涨停
        """
        score = 0.0
        
        current_price = data.get('price', 0)
        ma10 = data.get('ma10')
        ma30 = data.get('ma30')
        momentum_score = data.get('momentum_score', 0)
        macd = data.get('macd', 0)
        macd_signal = data.get('macd_signal', 0)
        trend_strength = data.get('trend_strength', 0)
        atr = data.get('atr', 0)
        rsi = data.get('rsi', 50)
        volume_ratio = data.get('volume_ratio', 1.0)
        
        # ===== 正向因子 =====
        
        # 1. 趋势条件（20分）— 分段连续打分
        if ma10 and ma30:
            ma_gap_pct = (ma10 - ma30) / ma30 * 100 if ma30 > 0 else 0
            if ma10 > ma30:
                # 金叉强度分档：差距越大越强，最高15分
                score += min(ma_gap_pct * 7.5, 15)
            else:
                # 空头排列扣分
                score -= min(abs(ma_gap_pct) * 5, 10)
            # 价格与MA10的距离（连续打分）
            if current_price > 0 and ma10 > 0:
                price_vs_ma10 = (current_price - ma10) / ma10 * 100
                if 0 < price_vs_ma10 <= 5:
                    score += min(price_vs_ma10 * 1, 5)  # 在MA10上方，最佳
                elif price_vs_ma10 > 5:
                    score += max(5 - (price_vs_ma10 - 5) * 0.5, 0)  # 离太远，追高风险
        
        # 2. 动量条件（25分）— 非线性压缩
        if momentum_score >= 0:
            # 0→5, 1→12, 2→18, 3→22, 4→25（非线性，避免动量2直接30分）
            score += 5 + min(momentum_score * 6 + momentum_score ** 1.5 * 2, 20)
        else:
            score += momentum_score * 8  # -1→-8, -2→-16（负动量重扣）
        
        # 3. 技术指标（30分）
        # MACD（15分）— 分段打分，避免×100太极端
        if macd > 0:
            macd_hist = macd - macd_signal
            if macd_hist > 0:
                # 红柱增长：分段递增，最高15分
                if macd < 0.05:
                    score += macd * 100  # 0-5分：温和
                elif macd < 0.15:
                    score += 5 + (macd - 0.05) * 50  # 5-10分：中等
                else:
                    score += 10 + min((macd - 0.15) * 20, 5)  # 10-15分：强势
            else:
                # 红柱缩短：有但减弱
                score += min(macd * 50, 8)
        else:
            score += max(macd * 30, -8)  # 负MACD扣分
        
        # RSI（15分）— 大幅提高权重，中性区间最佳
        if rsi is not None:
            if 40 <= rsi <= 60:
                score += 15  # 中性最佳
            elif 30 <= rsi < 40:
                score += 10 + (rsi - 30) * 0.5  # 30→10, 40→15
            elif 60 < rsi <= 70:
                score += 15 - (rsi - 60) * 0.5  # 60→15, 70→10
            elif rsi < 30:
                score += rsi * 0.3  # 超卖，谨慎加分
            else:  # rsi > 70
                score -= (rsi - 70) * 1.5  # 超买扣分！70→0, 80→-15
        
        # 趋势强度
        if trend_strength > 0:
            score += min(trend_strength * 500, 2)
        
        # 4. 波动率（10分）— 连续打分
        if current_price > 0 and atr and math.isfinite(current_price):
            volatility = atr / current_price
            if 0.015 <= volatility <= 0.025:
                score += 10  # 理想波动
            elif 0.01 <= volatility < 0.015:
                score += 5 + (volatility - 0.01) * 1000  # 偏低，可接受
            elif 0.025 < volatility <= 0.03:
                score += 10 - (volatility - 0.025) * 1000  # 偏高
            elif volatility > 0.05:
                score -= 5  # 过高波动扣分
        
        # 5. 成交量（10分）— 连续打分
        if volume_ratio > 1.0:
            score += min((volume_ratio - 1.0) * 8, 10)  # 量比越高分越高
        elif volume_ratio < 0.5:
            score -= 2  # 缩量扣分

        # 6. 技术形态（5分）— 替代原A股特色因子
        # 6.1 超跌反弹机会（连续下跌后首次上涨）
        consecutive_down = data.get('consecutive_down_days', 0)
        if consecutive_down >= 3 and momentum_score > 0:
            score += 3  # 连续下跌后反弹，有机会
        # 6.2 放量突破MA10
        if volume_ratio > 1.5 and current_price > ma10 > 0 and ma30 > 0 and ma10 > ma30:
            score += 2  # 放量突破均线，强势

        # ===== 负向惩罚 =====
        
        # 7. 追高风险（今日涨幅）
        today_change = data.get('change_pct', 0)
        if today_change > 5:
            score -= (today_change - 5) * 2  # 涨5%以上开始扣分
        if today_change >= 9.9:
            score -= 20  # 涨停板，基本买不进
        
        # 8. 连续上涨惩罚
        consecutive_up = data.get('consecutive_up_days', 0)
        if consecutive_up >= 3:
            score -= (consecutive_up - 2) * 3  # 连涨3天-3，4天-6
        
        # 9. 市场环境（已移至 FilterPipeline 处理，避免硬编码）
        # RegimeFilter 会根据 market_state 做乘法调整

        return min(max(score, 0), 100)
    
    def calculate_trading_days(self, data: Dict) -> int:
        """
        计算持仓交易日天数（排除周末）
        
        优先使用 trading_days 字段，如果没有则使用 holding_days
        """
        # 如果数据已提供交易日天数，直接使用
        if 'trading_days' in data:
            return data['trading_days']
        
        # 否则使用 holding_days 作为备选（假设传入的已经是交易日）
        return data.get('holding_days', 0)
    
    def calculate_stop_loss(self, data: Dict) -> float:
        """
        动态止损计算（基于交易日天数）
        
        - 新开仓（<=3个交易日）：ATR×3，最大亏损8%
        - 中期（4-10个交易日）：成本价-5% 或 ATR×2.5
        - 长期（>10个交易日）：追踪止损，保护利润
        """
        current_price = data.get('price', 0)
        avg_cost = data.get('avg_cost', current_price)
        atr = data.get('atr', current_price * 0.02)
        trading_days = self.calculate_trading_days(data)
        highest_price = data.get('highest_price_since_entry') or current_price
        
        profit_pct = (current_price - avg_cost) / avg_cost if avg_cost > 0 else 0
        
        if trading_days <= 3:
            # 新开仓：ATR×3 止损，但不能超过成本价-8%
            atr_stop = avg_cost - atr * self.stop_loss_atr_mult_initial
            max_loss_stop = avg_cost * 0.92  # 最大亏损8%
            stop_loss = max(atr_stop, max_loss_stop)
        elif trading_days <= 10:
            # 中期：取更严格的止损
            atr_stop = avg_cost - atr * 2.5
            pct_stop = avg_cost * (1 - self.stop_loss_max_drawdown)  # 成本价-5%
            stop_loss = max(atr_stop, pct_stop)
        else:
            # 长期：追踪止损，保护利润
            trailing_stop = highest_price - atr * self.stop_loss_atr_mult_trailing
            if profit_pct > 0.10:  # 盈利10%以上，锁定利润
                min_stop = avg_cost * 1.03  # 至少锁定3%利润
                stop_loss = max(trailing_stop, min_stop)
            else:
                stop_loss = max(trailing_stop, avg_cost * 0.95)
        
        return stop_loss
    
    def check_take_profit(self, data: Dict) -> Optional[Dict]:
        """检查止盈条件"""
        current_price = data.get('price', 0)
        avg_cost = data.get('avg_cost', 0)
        momentum_score = data.get('momentum_score', 0)
        holding_shares = data.get('holding_shares', 0)
        
        if avg_cost <= 0 or holding_shares <= 0:
            return None
        
        profit_pct = (current_price - avg_cost) / avg_cost
        
        # 梯度止盈逻辑：盈利越高，对动量要求越宽松
        if profit_pct >= 0.20:
            # 盈利20%以上：动量稍微减弱即可止盈（允许部分利润回撤）
            if momentum_score <= 1:
                return {
                    'signal': 'sell',
                    'reason': f'目标止盈: 盈利{profit_pct*100:.1f}%, 动量轻微减弱({momentum_score})',
                    'action_shares': holding_shares,
                    'priority': 'take_profit'
                }
        elif profit_pct >= self.take_profit_pct:
            # 盈利15-20%：动量转弱止盈
            if momentum_score <= self.take_profit_momentum_threshold:
                return {
                    'signal': 'sell',
                    'reason': f'目标止盈: 盈利{profit_pct*100:.1f}%, 动量转弱({momentum_score})',
                    'action_shares': holding_shares,
                    'priority': 'take_profit'
                }
        
        # 保本止盈（5%盈利 + 动量转弱）
        if profit_pct >= self.break_even_profit_pct:
            if momentum_score <= self.break_even_momentum_threshold:
                return {
                    'signal': 'sell',
                    'reason': f'获利保护: 盈利{profit_pct*100:.1f}%, 动量转弱({momentum_score})',
                    'action_shares': holding_shares,
                    'priority': 'break_even'
                }
        
        # 紧急止盈：盈利20%以上后动量严重转弱（快速下跌保护）
        if profit_pct >= 0.20 and momentum_score <= -2:
            return {
                'signal': 'sell',
                'reason': f'紧急止盈: 盈利{profit_pct*100:.1f}%但动量严重转弱({momentum_score})',
                'action_shares': holding_shares,
                'priority': 'emergency_take_profit'
            }
        
        return None
    
    def analyze(self, data: Dict, market_state: str = None, index_data: Dict = None) -> Dict:
        """
        分析单只股票并生成交易信号
        
        优先级：止损 > 止盈 > 买入
        
        Args:
            data: 股票数据
            market_state: 市场环境状态 ('strong'|'neutral'|'weak')，
                         如果为None则自动判断（优先使用index_data）
            index_data: 大盘指数数据（如上证指数），用于判断市场环境
        """
        current_price = data.get('price', 0)
        holding_shares = data.get('holding_shares', 0)
        cash = data.get('cash', 150000)
        avg_cost = data.get('avg_cost', 0)
        symbol = data.get('symbol', '')

        # 数据异常检查
        if current_price <= 0:
            result = {
                'signal': 'hold',
                'reason': '价格数据异常',
                'action_shares': 0,
                'conditions': {},
                'score': 0
            }
            self._emit_signal_event(symbol, result)
            return result

        result = {
            'signal': 'hold',
            'reason': '无明确交易信号',
            'action_shares': 0,
            'conditions': {},
            'score': 0
        }
        
        # ========== 1. 止损检查（最高优先级）==========
        stored_stop_loss = data.get('stored_stop_loss')
        if stored_stop_loss and stored_stop_loss > 0:
            stop_loss = stored_stop_loss
        else:
            stop_loss = self.calculate_stop_loss(data)
        
        # 止损价合理性校验
        trading_days = self.calculate_trading_days(data)
        if avg_cost > 0 and stop_loss >= avg_cost and trading_days <= 10 and current_price <= avg_cost * 1.05:
            stop_loss = avg_cost * 0.95  # 强制设为成本价下方5%
        
        if current_price < stop_loss and holding_shares > 0:
            result['signal'] = 'sell'
            result['reason'] = f'止损触发: 价格{current_price:.2f} < 止损线{stop_loss:.2f}'
            result['action_shares'] = holding_shares
            result['priority'] = 'stop_loss'
            result['conditions'] = {'stop_loss': stop_loss, 'current_price': current_price}
            self._emit_signal_event(symbol, result)
            return result
        
        # ========== 2. 止盈检查 ==========
        if holding_shares > 0:
            take_profit_signal = self.check_take_profit(data)
            if take_profit_signal:
                self._emit_signal_event(symbol, take_profit_signal)
                return take_profit_signal
        
        # ========== 3. 买入信号检查 ==========
        # 🔧 修复：已持仓的股票不重复发买入信号，避免每天推同样的票
        if cash > 10000 and holding_shares == 0:
            # P0: 动态阈值 - 根据市场环境调整
            # 优先使用传入的大盘状态，否则根据大盘数据自动判断（优先使用index_data）
            if market_state is None:
                if self.market_regime and index_data:
                    # 使用 MarketRegime 模块
                    regime_result = self.market_regime.detect(index_data)
                    market_state = regime_result['regime']
                else:
                    market_state = self.detect_market_state(data, index_data)
            
            buy_score = self.calculate_buy_score(data, market_state=market_state)

            # Filter Pipeline（纯函数，无实例状态）
            final_score = buy_score
            sizing_constraint = 1.0
            filter_attrs = []
            if self.filters:
                final_score, sizing_constraint, filter_attrs = self.filters.run(
                    buy_score, data, market_state
                )

            result['score'] = final_score

            dynamic_threshold = self.get_dynamic_threshold(market_state)
            dynamic_position = self.get_dynamic_position_size(market_state)

            if final_score >= dynamic_threshold:
                # P0: 动态仓位 - 根据市场环境和评分调整
                # 基础仓位 = 动态仓位配置
                # 评分加成：score=阈值 -> 50%基础仓位, score=100 -> 100%基础仓位
                score_bonus = (final_score - dynamic_threshold) / (100 - dynamic_threshold) * 0.5
                position_pct = dynamic_position * (0.5 + score_bonus)
                position_pct *= sizing_constraint  # Filter sizing 约束（max 约束，防 collapse）
                position_pct = max(0, min(position_pct, dynamic_position))  # 限制在0-动态仓位上限之间
                
                max_invest = cash * position_pct
                buy_shares = int(max_invest / current_price / 100) * 100
                
                if buy_shares >= 100:
                    # 计算止损价并加入conditions
                    stop_loss = self.calculate_stop_loss(data)
                    
                    result['signal'] = 'buy'
                    result['reason'] = f'买入评分{final_score:.0f}分(阈值{dynamic_threshold}, 市场{market_state}), MA多头+动量积极'
                    result['action_shares'] = buy_shares
                    result['priority'] = 'buy'
                    result['price'] = current_price  # 执行层需要的价格
                    result['position_size'] = position_pct * 100  # 执行层需要的仓位比例(%)
                    result['conditions'] = {
                        'score': final_score,
                        'base_score': buy_score,
                        'threshold': dynamic_threshold,
                        'market_state': market_state,
                        'position_pct': position_pct,
                        'ma10': data.get('ma10'),
                        'ma30': data.get('ma30'),
                        'momentum_score': data.get('momentum_score'),
                        'macd': data.get('macd'),
                        'macd_signal': data.get('macd_signal', 0),
                        'rsi': data.get('rsi'),
                        'atr': data.get('atr'),
                        'trend_strength': data.get('trend_strength'),
                        'volume_ratio': data.get('volume_ratio'),
                        'stop_loss': stop_loss,
                        'filter_attribution': [
                            {'filter': r.name, 'mult': r.multiplier, 'add': r.additive,
                             'sizing': r.sizing_adj, 'reason': r.reason}
                            for r in filter_attrs
                        ] if filter_attrs else [],
                    }
                    self._emit_signal_event(symbol, result)
                    return result
            else:
                result['reason'] = f'买入评分{final_score:.0f}分，未达到阈值{dynamic_threshold}(市场{market_state})'
        elif cash > 10000 and holding_shares > 0:
            # 已持仓：只展示评分，不发买入信号
            buy_score = self.calculate_buy_score(data)
            result['score'] = buy_score
            result['reason'] = f'已持仓，当前评分{buy_score:.0f}分（不发重复买入）'
        
        # ========== 4. 普通卖出信号 ==========
        if holding_shares > 0:
            momentum_score = data.get('momentum_score', 0)
            macd = data.get('macd', 0)
            profit_pct = (current_price - avg_cost) / avg_cost if avg_cost > 0 else 0
            
            if momentum_score <= -3 and profit_pct < self.break_even_profit_pct:
                result['signal'] = 'sell'
                result['reason'] = f'动量严重转弱({momentum_score})且盈利有限({profit_pct*100:.1f}%)'
                result['action_shares'] = holding_shares
                result['priority'] = 'momentum_weak'
                self._emit_signal_event(symbol, result)
                return result

        self._emit_signal_event(symbol, result)
        return result


class ConservativeStrategy(TradingStrategy):
    """
    保守策略

    提高评分阈值，减少交易频率
    """

    def __init__(self, config: Optional[Dict] = None, event_store=None, market_regime=None, filters=None):
        self.config = config or {}
        # 使用增强版策略但提高门槛
        self.base_strategy = MomentumTrendStrategy({
            **self.config,
            'buy_score_threshold': 75,  # 75分才买
            'take_profit_pct': 0.10,     # 10%止盈
        }, event_store=event_store, market_regime=market_regime, filters=filters)
    
    def get_name(self) -> str:
        return "保守策略"
    
    def analyze(self, data: Dict) -> Dict:
        return self.base_strategy.analyze(data)


class StrategyFactory:
    """策略工厂"""

    STRATEGIES = {
        'momentum_trend': MomentumTrendStrategy,
        'conservative': ConservativeStrategy,
    }

    @classmethod
    def create(cls, strategy_name: str, config: Optional[Dict] = None,
               event_store=None, market_regime=None, filters=None) -> TradingStrategy:
        """创建策略实例"""
        if strategy_name not in cls.STRATEGIES:
            raise ValueError(f"未知策略: {strategy_name}")

        return cls.STRATEGIES[strategy_name](config, event_store=event_store, market_regime=market_regime, filters=filters)
    
    @classmethod
    def list_strategies(cls) -> List[str]:
        """列出可用策略"""
        return list(cls.STRATEGIES.keys())


# v2.6.0: EventStore事件扩展完成
