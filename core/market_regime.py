#!/usr/bin/env python3
"""市场状态感知模块 - Market Regime Detection

基于沪深300指数判断市场状态：
- strong: 上涨趋势（价格 > MA60，且MA20 > MA60）
- neutral: 震荡（价格在MA60附近 ±1%）
- weak: 下跌趋势（价格 < MA60，且MA20 < MA60）

辅助指标：
- 20日波动率分位数（高波动 → 谨慎）

Usage:
    regime = MarketRegime()
    state = regime.detect(index_data)
    adjustments = regime.get_strategy_adjustments(state['regime'])
"""

import math
import logging
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class MarketRegime:
    """市场状态感知模块"""

    # 波动率阈值（年化）
    VOL_LOW = 0.15    # 15%以下：低波动
    VOL_HIGH = 0.25   # 25%以上：高波动

    # MA偏离阈值
    MA_DEVIATION_STRONG = 0.02   # 偏离MA60 > 2% → 强势
    MA_DEVIATION_NEUTRAL = 0.01  # 偏离MA60 ±1% → 震荡

    def detect(self, index_data: Dict) -> Dict:
        """检测当前市场状态

        Args:
            index_data: 大盘指数数据，包含：
                - price: 当前价格
                - ma20: 20日均线
                - ma60: 60日均线
                - prices_20d: 最近20天收盘价列表（用于计算波动率）

        Returns:
            Dict: {
                'regime': 'strong' | 'neutral' | 'weak',
                'confidence': float (0-1),
                'volatility': float (年化波动率),
                'volatility_percentile': float (0-100),
                'details': {
                    'trend': 'up' | 'sideways' | 'down',
                    'volatility_level': 'low' | 'normal' | 'high',
                    'price_vs_ma60': float (偏离百分比),
                    'ma20_vs_ma60': float (偏离百分比)
                }
            }
        """
        price = index_data.get('price', 0)
        ma20 = index_data.get('ma20', 0)
        ma60 = index_data.get('ma60', 0)
        prices_20d = index_data.get('prices_20d', [])

        if not all([price, ma20, ma60]):
            return {
                'regime': 'neutral',
                'confidence': 0.3,
                'volatility': 0,
                'volatility_percentile': 50,
                'details': {
                    'trend': 'sideways',
                    'volatility_level': 'normal',
                    'price_vs_ma60': 0,
                    'ma20_vs_ma60': 0
                }
            }

        # 计算偏离度
        price_vs_ma60 = (price - ma60) / ma60  # 正=在上方，负=在下方
        ma20_vs_ma60 = (ma20 - ma60) / ma60

        # 计算波动率
        volatility = self._calculate_volatility(prices_20d) if prices_20d else 0.2
        vol_level = self._get_volatility_level(volatility)

        # 判断趋势
        if price > ma60 and ma20 > ma60:
            if price_vs_ma60 > self.MA_DEVIATION_STRONG:
                regime = 'strong'
                confidence = min(0.7 + price_vs_ma60 * 5, 1.0)
                trend = 'up'
            else:
                regime = 'neutral'
                confidence = 0.5
                trend = 'sideways'
        elif price < ma60 and ma20 < ma60:
            if price_vs_ma60 < -self.MA_DEVIATION_STRONG:
                regime = 'weak'
                confidence = min(0.7 + abs(price_vs_ma60) * 5, 1.0)
                trend = 'down'
            else:
                regime = 'neutral'
                confidence = 0.5
                trend = 'sideways'
        else:
            regime = 'neutral'
            confidence = 0.4
            trend = 'sideways'

        # 高波动降低信心
        if vol_level == 'high':
            confidence *= 0.8

        return {
            'regime': regime,
            'confidence': round(confidence, 2),
            'volatility': round(volatility, 4),
            'volatility_percentile': self._vol_to_percentile(volatility),
            'details': {
                'trend': trend,
                'volatility_level': vol_level,
                'price_vs_ma60': round(price_vs_ma60 * 100, 2),
                'ma20_vs_ma60': round(ma20_vs_ma60 * 100, 2)
            }
        }

    def get_strategy_adjustments(self, regime: str) -> Dict:
        """根据市场状态返回策略调整参数"""
        adjustments = {
            'strong': {
                'buy_score_threshold': 60,
                'stop_loss_mult': 1.0,
                'max_position_pct': 0.20,
                'description': '强势市场：降低买入门槛，正常止损，满仓位'
            },
            'neutral': {
                'buy_score_threshold': 65,
                'stop_loss_mult': 0.9,
                'max_position_pct': 0.15,
                'description': '震荡市场：标准门槛，稍紧止损，减仓位'
            },
            'weak': {
                'buy_score_threshold': 75,
                'stop_loss_mult': 0.8,
                'max_position_pct': 0.10,
                'description': '弱势市场：提高门槛，收紧止损，最低仓位'
            }
        }
        return adjustments.get(regime, adjustments['neutral'])

    def _calculate_volatility(self, prices: List[float]) -> float:
        """计算20日年化波动率"""
        if len(prices) < 2:
            return 0.2  # 默认中等波动

        returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                returns.append((prices[i] - prices[i-1]) / prices[i-1])

        if not returns:
            return 0.2

        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        daily_vol = math.sqrt(variance)
        annual_vol = daily_vol * math.sqrt(252)

        return annual_vol

    def _get_volatility_level(self, volatility: float) -> str:
        """波动率分级"""
        if volatility < self.VOL_LOW:
            return 'low'
        elif volatility > self.VOL_HIGH:
            return 'high'
        return 'normal'

    def _vol_to_percentile(self, vol: float) -> float:
        """波动率转简化分位数（基于经验值）"""
        if vol < 0.10:
            return 10
        elif vol < 0.15:
            return 30
        elif vol < 0.20:
            return 50
        elif vol < 0.25:
            return 70
        elif vol < 0.30:
            return 85
        else:
            return 95


def get_index_data_from_db(symbol: str = "000300", days: int = 60) -> Optional[Dict]:
    """从本地数据库获取大盘指数数据

    Args:
        symbol: 指数代码（默认沪深300: 000300）
        days: 获取天数

    Returns:
        Dict: 包含 price, ma20, ma60, prices_20d 的字典，失败返回 None
    """
    try:
        import sqlite3
        db_path = str(Path.home() / ".openclaw/workspace/skills/LongToo-trader/data/market_kline.db")

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            '''SELECT close FROM daily_kline WHERE symbol=? ORDER BY trade_date DESC LIMIT ?''',
            (symbol, days)
        ).fetchall()
        conn.close()

        if not rows or len(rows) < 60:
            logger.warning(f"指数数据不足: {len(rows) if rows else 0} 天")
            return None

        prices = [float(r[0]) for r in reversed(rows)]  # 按时间正序

        from .indicators import TechnicalIndicators
        ma20 = TechnicalIndicators.calculate_ma(prices, 20)
        ma60 = TechnicalIndicators.calculate_ma(prices, 60)

        return {
            'price': prices[-1],
            'ma20': ma20,
            'ma60': ma60,
            'prices_20d': prices[-20:]
        }
    except Exception as e:
        logger.error(f"获取指数数据失败: {e}")
        return None
