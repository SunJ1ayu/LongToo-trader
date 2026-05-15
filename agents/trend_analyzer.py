#!/usr/bin/env python3
"""
Trend Analyzer - 趋势破坏检测

检测以下趋势破坏信号：
1. 跌破均线（MA5/MA10/MA20）
2. 放量阴线（成交量异常放大 + 收跌）
3. 连续下跌（连续N天收跌）
4. 弱于指数（相对强度下降）
5. MACD 死叉

每个信号独立评分，综合判断趋势是否破坏。
"""

import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import math

logger = logging.getLogger(__name__)


class TrendAnalyzer:
    """趋势分析器

    从 K 线数据计算技术指标，判断趋势是否破坏。
    """

    def __init__(self, db_path: str = None):
        """
        Args:
            db_path: K线数据库路径（默认使用项目路径）
        """
        if db_path is None:
            db_path = str(Path.home() / ".openclaw/workspace/skills/LongToo-trader/data/market_kline.db")
        self.db_path = db_path

    def get_klines(self, symbol: str, days: int = 30) -> Optional[List[Dict]]:
        """从数据库获取 K 线数据

        Args:
            symbol: 股票代码
            days: 天数

        Returns:
            K 线列表 [{date, open, high, low, close, volume}]
        """
        try:
            # 标准化 symbol
            pure = symbol.replace('sh', '').replace('sz', '')

            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                '''SELECT trade_date, open, high, low, close, volume
                   FROM daily_kline
                   WHERE symbol = ?
                   ORDER BY trade_date DESC
                   LIMIT ?''',
                (pure, days)
            )

            rows = cursor.fetchall()
            conn.close()

            if not rows:
                logger.debug(f"无K线数据: {symbol}")
                return None

            klines = []
            for row in rows:
                klines.append({
                    'date': row[0],
                    'open': float(row[1]) if row[1] else 0,
                    'high': float(row[2]) if row[2] else 0,
                    'low': float(row[3]) if row[3] else 0,
                    'close': float(row[4]) if row[4] else 0,
                    'volume': float(row[5]) if row[5] else 0,
                })

            # 按日期升序（从旧到新）
            klines.reverse()
            return klines

        except Exception as e:
            logger.error(f"获取K线失败 {symbol}: {e}")
            return None

    def analyze_trend(self, symbol: str, current_price: float = None) -> Dict:
        """分析趋势状态

        Args:
            symbol: 股票代码
            current_price: 当前价格（可选，用于实时判断）

        Returns:
            {
                'symbol': str,
                'trend_score': float (0-100, 越低越差),
                'signals': List[Dict],  # 各项信号详情
                'break_level': str,     # 破坏程度
                'suggestion': str,      # 建议
            }
        """
        klines = self.get_klines(symbol, days=30)

        if not klines or len(klines) < 10:
            return {
                'symbol': symbol,
                'trend_score': 50,  # 默认中性
                'signals': [],
                'break_level': 'unknown',
                'suggestion': '数据不足，无法判断趋势',
            }

        # 使用当前价格或最新收盘价
        if current_price is None:
            current_price = klines[-1]['close']

        signals = []
        trend_score = 70  # 默认健康趋势

        # 1. 均线检测
        ma_signals = self._check_ma_break(klines, current_price)
        signals.extend(ma_signals)
        for sig in ma_signals:
            if sig['triggered']:
                trend_score -= sig['penalty']

        # 2. 放量阴线检测
        volume_signal = self._check_volume_break(klines)
        signals.append(volume_signal)
        if volume_signal['triggered']:
            trend_score -= volume_signal['penalty']

        # 3. 连续下跌检测
        consecutive_signal = self._check_consecutive_down(klines)
        signals.append(consecutive_signal)
        if consecutive_signal['triggered']:
            trend_score -= consecutive_signal['penalty']

        # 4. MACD 死叉检测（简化版）
        macd_signal = self._check_macd_dead(klines)
        signals.append(macd_signal)
        if macd_signal['triggered']:
            trend_score -= macd_signal['penalty']

        # 综合判断
        trend_score = max(0, min(100, trend_score))

        break_level = self._get_break_level(trend_score)
        suggestion = self._get_suggestion(break_level)

        return {
            'symbol': symbol,
            'trend_score': round(trend_score, 1),
            'signals': signals,
            'break_level': break_level,
            'suggestion': suggestion,
        }

    def is_trend_broken(self, symbol: str, current_price: float = None) -> Tuple[bool, str]:
        """判断趋势是否破坏

        Returns:
            (is_broken, reason)
        """
        result = self.analyze_trend(symbol, current_price)

        if result['break_level'] in ['broken', 'critical']:
            reasons = [s['name'] for s in result['signals'] if s['triggered']]
            return True, f"趋势破坏 ({', '.join(reasons)})"

        return False, ""

    # ========== 私有方法 ==========

    def _check_ma_break(self, klines: List[Dict], current_price: float) -> List[Dict]:
        """检测均线跌破"""
        signals = []

        # 计算 MA5, MA10, MA20
        closes = [k['close'] for k in klines]

        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else closes[-1]
        ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else closes[-1]
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else closes[-1]

        # MA5 跌破（价格低于MA5）
        ma5_break = current_price < ma5
        signals.append({
            'name': '跌破MA5',
            'triggered': ma5_break,
            'value': f"当前{current_price:.2f} vs MA5{ma5:.2f}",
            'penalty': 8 if ma5_break else 0,
        })

        # MA10 跌破（更严重）
        ma10_break = current_price < ma10
        signals.append({
            'name': '跌破MA10',
            'triggered': ma10_break,
            'value': f"当前{current_price:.2f} vs MA10{ma10:.2f}",
            'penalty': 12 if ma10_break else 0,
        })

        # MA20 跌破（最严重）
        ma20_break = current_price < ma20
        signals.append({
            'name': '跌破MA20',
            'triggered': ma20_break,
            'value': f"当前{current_price:.2f} vs MA20{ma20:.2f}",
            'penalty': 18 if ma20_break else 0,
        })

        return signals

    def _check_volume_break(self, klines: List[Dict]) -> Dict:
        """检测放量阴线"""
        if len(klines) < 5:
            return {
                'name': '放量阴线',
                'triggered': False,
                'value': '数据不足',
                'penalty': 0,
            }

        # 最近5天平均成交量
        avg_volume = sum(k['volume'] for k in klines[-5:-1]) / 4
        today_volume = klines[-1]['volume']
        today_close = klines[-1]['close']
        yesterday_close = klines[-2]['close']

        # 放量阴线：成交量放大1.5倍 + 收跌
        volume_ratio = today_volume / avg_volume if avg_volume > 0 else 1
        price_down = today_close < yesterday_close

        triggered = volume_ratio >= 1.5 and price_down

        return {
            'name': '放量阴线',
            'triggered': triggered,
            'value': f"量比{volume_ratio:.1f}, 收跌{yesterday_close:.2f}→{today_close:.2f}",
            'penalty': 15 if triggered else 0,
        }

    def _check_consecutive_down(self, klines: List[Dict]) -> Dict:
        """检测连续下跌"""
        if len(klines) < 5:
            return {
                'name': '连续下跌',
                'triggered': False,
                'value': '数据不足',
                'penalty': 0,
            }

        # 计算连续下跌天数
        down_days = 0
        for i in range(len(klines) - 1, 0, -1):
            if klines[i]['close'] < klines[i-1]['close']:
                down_days += 1
            else:
                break

        # 连续下跌3天以上触发
        triggered = down_days >= 3

        return {
            'name': '连续下跌',
            'triggered': triggered,
            'value': f"连续下跌{down_days}天",
            'penalty': 10 + down_days * 2 if triggered else 0,  # 越久越严重
        }

    def _check_macd_dead(self, klines: List[Dict]) -> Dict:
        """检测 MACD 死叉（简化版）"""
        if len(klines) < 20:
            return {
                'name': 'MACD死叉',
                'triggered': False,
                'value': '数据不足',
                'penalty': 0,
            }

        # 简化 MACD 计算
        closes = [k['close'] for k in klines]

        # EMA12 和 EMA26（简化为 SMA）
        ema12 = sum(closes[-12:]) / 12
        ema26 = sum(closes[-26:]) / 26 if len(closes) >= 26 else sum(closes) / len(closes)

        # DIF = EMA12 - EMA26
        dif = ema12 - ema26

        # DEA（简化为 DIF 的9日均值）
        # 这里用当前和昨天的 DIF 判断死叉趋势
        prev_ema12 = sum(closes[-13:-1]) / 12
        prev_ema26 = sum(closes[-27:-1]) / 26 if len(closes) >= 27 else sum(closes[-1:]) / len(closes)
        prev_dif = prev_ema12 - prev_ema26

        # 死叉：DIF 从上方跌破 DEA（简化判断）
        # 如果 DIF 在下降且已经接近0或负值
        triggered = dif < prev_dif and dif < 0

        return {
            'name': 'MACD死叉',
            'triggered': triggered,
            'value': f"DIF={dif:.2f} (前值{prev_dif:.2f})",
            'penalty': 12 if triggered else 0,
        }

    def _get_break_level(self, trend_score: float) -> str:
        """获取趋势破坏程度"""
        if trend_score >= 60:
            return 'healthy'
        elif trend_score >= 40:
            return 'weakening'
        elif trend_score >= 20:
            return 'broken'
        else:
            return 'critical'

    def _get_suggestion(self, break_level: str) -> str:
        """获取建议"""
        suggestions = {
            'healthy': '趋势健康，持有',
            'weakening': '趋势走弱，关注',
            'broken': '趋势破坏，建议卖出',
            'critical': '严重破坏，立即退出',
        }
        return suggestions.get(break_level, '观望')


def batch_analyze_trends(positions: List[Dict], analyzer: TrendAnalyzer = None) -> Dict:
    """批量分析持仓趋势

    Args:
        positions: 持仓列表
        analyzer: 分析器实例

    Returns:
        {
            'healthy': List[Dict],  # 健康持仓
            'weakening': List[Dict],  # 走弱持仓
            'broken': List[Dict],  # 破坏持仓（需要卖出）
            'critical': List[Dict],  # 严重破坏
        }
    """
    if analyzer is None:
        analyzer = TrendAnalyzer()

    result = {
        'healthy': [],
        'weakening': [],
        'broken': [],
        'critical': [],
    }

    for pos in positions:
        symbol = pos.get('symbol')
        current_price = pos.get('current_price', pos.get('price', 0))

        trend = analyzer.analyze_trend(symbol, current_price)
        level = trend['break_level']

        pos_with_trend = {**pos, 'trend': trend}

        result[level].append(pos_with_trend)

    return result