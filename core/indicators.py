#!/usr/bin/env python3
"""技术指标计算模块"""

from typing import List, Optional, Tuple, Dict

class TechnicalIndicators:
    """技术指标计算器"""
    
    @staticmethod
    def calculate_ma(prices: List[float], period: int) -> Optional[float]:
        """计算移动平均线"""
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period
    
    @staticmethod
    def calculate_atr(klines: List[Dict], period: int = 14) -> Optional[float]:
        """计算平均真实波幅(ATR)"""
        if len(klines) < period + 1:
            return None
        
        tr_values = []
        for i in range(1, len(klines)):
            high = float(klines[i]["high"])
            low = float(klines[i]["low"])
            close_prev = float(klines[i-1]["close"])
            
            tr1 = high - low
            tr2 = abs(high - close_prev)
            tr3 = abs(low - close_prev)
            tr = max(tr1, tr2, tr3)
            tr_values.append(tr)
        
        if len(tr_values) >= period:
            return sum(tr_values[-period:]) / period
        return None
    
    @staticmethod
    def calculate_momentum_score(current_price: float, prev_close: float) -> int:
        """计算动量评分：基于当日涨跌幅
        * 当日涨幅 > +1% → +2
        * 当日涨幅 0% ~ +1% → +1
        * 当日涨幅 -1% ~ 0% → 0
        * 当日涨幅 < -1% → -2
        """
        if prev_close <= 0:
            return 0
        
        change_pct = (current_price - prev_close) / prev_close * 100
        
        # 修复#4: 统一阈值与 trader.py 一致 (>2% 得2分, >=-2% 得0分)
        if change_pct > 2:
            return 2
        elif change_pct > 0:
            return 1
        elif change_pct >= -2:
            return 0
        else:
            return -2
    
    @staticmethod
    def calculate_macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """计算MACD指标, 返回 (macd_line, signal_line, histogram)"""
        if len(prices) < slow:
            return None, None, None
        
        def ema(data, period):
            multiplier = 2 / (period + 1)
            ema_values = [data[0]]
            for price in data[1:]:
                ema_values.append((price - ema_values[-1]) * multiplier + ema_values[-1])
            return ema_values
        
        ema_fast = ema(prices, fast)
        ema_slow = ema(prices, slow)
        
        macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(ema_fast))]
        signal_line = ema(macd_line, signal)
        histogram = [macd_line[i] - signal_line[i] for i in range(len(signal_line))]
        
        return macd_line[-1], signal_line[-1], histogram[-1]
    
    @staticmethod
    def get_trend_strength(klines: List[Dict]) -> float:
        """计算趋势强度 (价格变化率)"""
        if len(klines) < 20:
            return 0
        recent_close = float(klines[-1]["close"])
        old_close = float(klines[-20]["close"])
        return abs(recent_close - old_close) / old_close
    
    @staticmethod
    def calculate_stop_loss(current_price: float, atr: Optional[float]) -> float:
        """计算止损线 (2.5×ATR)"""
        if atr:
            return current_price - (2.5 * atr)
        return current_price * 0.95

    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> Optional[float]:
        """计算RSI指标（相对强弱指数）

        Args:
            prices: 收盘价列表（按时间正序）
            period: RSI周期（默认14）

        Returns:
            RSI值（0-100），数据不足返回None
        """
        if len(prices) < period + 1:
            return None

        gains = []
        losses = []

        for i in range(1, min(period + 1, len(prices))):
            diff = prices[-i] - prices[-i - 1]
            if diff > 0:
                gains.append(diff)
            else:
                losses.append(abs(diff))

        avg_gain = sum(gains) / period if gains else 0
        avg_loss = sum(losses) / period if losses else 0.001

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))


class SignalGenerator:
    """交易信号生成器"""
    
    @staticmethod
    def generate_signal(
        current_price: float,
        ma10: Optional[float],
        ma30: Optional[float],
        momentum_score: int,
        macd: Optional[float],
        atr: Optional[float],
        trend_strength: float,
        holding_shares: int,
        consecutive_losses: int = 0,
        max_consecutive_losses: int = 3,
        cash: float = 150000
    ) -> Dict:
        """生成交易信号"""
        signal_result = {
            "signal": "hold",
            "reason": "无明确交易信号",
            "action_shares": 0
        }
        
        # 风控检查：连续亏损
        if consecutive_losses >= max_consecutive_losses:
            signal_result["signal"] = "hold"
            signal_result["reason"] = f"连续亏损{consecutive_losses}次,冷却期中"
            return signal_result
        
        # 计算基本条件
        ma_bullish = ma10 > ma30 if ma10 and ma30 else False
        strong_trend = trend_strength > 0.003
        high_volatility = (atr / current_price) > 0.002 if atr else False
        stop_loss = TechnicalIndicators.calculate_stop_loss(current_price, atr)
        
        # 记录条件状态
        signal_result["ma_bullish"] = ma_bullish
        signal_result["strong_trend"] = strong_trend
        signal_result["high_volatility"] = high_volatility
        signal_result["stop_loss"] = stop_loss
        
        # 不满足基本条件
        if not (ma_bullish and strong_trend and high_volatility):
            if not ma_bullish:
                signal_result["reason"] = "MA10未上穿MA30"
            elif not strong_trend:
                signal_result["reason"] = f"趋势强度不足({trend_strength*100:.2f}%<0.3%)"
            elif not high_volatility:
                signal_result["reason"] = f"波动率不足({atr/current_price*100:.2f}%<0.2%)"
            return signal_result
        
        # 止损检查
        if current_price < stop_loss:
            signal_result["signal"] = "sell"
            signal_result["reason"] = f"价格{current_price:.2f}跌破止损线{stop_loss:.2f}"
            signal_result["action_shares"] = holding_shares
            return signal_result
        
        # 买入条件
        if current_price > ma10 and momentum_score >= 1 and macd and macd > 0:
            max_invest = cash * 0.2
            buy_shares = int(max_invest / current_price / 100) * 100
            if buy_shares >= 100:
                signal_result["signal"] = "buy"
                signal_result["reason"] = f"MA多头+动量评分({momentum_score})积极+MACD正向"
                signal_result["action_shares"] = buy_shares
                return signal_result
        
        # 卖出条件
        if momentum_score <= -2 or (macd and macd < 0):
            signal_result["signal"] = "sell"
            if momentum_score <= -2:
                signal_result["reason"] = f"动量弱势(评分{momentum_score})"
            else:
                signal_result["reason"] = f"MACD转负({macd:.4f}<0)"
            signal_result["action_shares"] = holding_shares
            return signal_result
        
        return signal_result


def calculate_indicators(klines: List[Dict]) -> Dict:
    """计算完整的技术指标集合
    
    Args:
        klines: K线数据列表
        
    Returns:
        Dict: 包含MA10, MA30, RSI, MACD, ATR等指标的字典
    """
    if not klines or len(klines) < 30:
        return {}
    
    prices = [float(k["close"]) for k in klines]
    
    return {
        "ma10": TechnicalIndicators.calculate_ma(prices, 10),
        "ma30": TechnicalIndicators.calculate_ma(prices, 30),
        "atr": TechnicalIndicators.calculate_atr(klines, 14),
        "current_price": prices[-1] if prices else None,
    }
