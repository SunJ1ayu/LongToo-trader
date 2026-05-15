#!/usr/bin/env python3
"""
Intraday Risk Monitor - 盘中异常风险监控

核心原则：
1. 只减仓，不加仓（不污染日线逻辑）
2. 轻量检查，不重新分析
3. 极端情况触发，不是常规决策

检查内容：
1. 跌停（pct_change < -9.5%）
2. 放量暴跌（量比5倍 + 跌幅-6%）
3. 大盘系统性风险（沪深300跌幅 > 3%）
4. 持仓弱于板块（板块涨，个股暴跌）

运行时间：10:30, 14:00
"""

import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class IntradayRiskMonitor:
    """盘中异常风险监控

    只做"异常风险逃生"，不做"盘中重新决策"
    """

    def __init__(self, config: Dict = None):
        self.config = {
            # 极端风险阈值
            "limit_down_pct": -9.5,           # 跌停阈值
            "volume_spike_ratio": 5.0,        # 放量倍数
            "panic_drop_pct": -6.0,           # 恐慌跌幅
            "market_crash_pct": -3.0,         # 大盘暴跌阈值
            "sector_underperform": -4.0,      # 跑输板块阈值

            # 系统性风险
            "index_symbol": "000300",         # 沪深300
            "reduce_on_market_crash": True,   # 大盘暴跌时整体减仓

            # 开关
            "enabled": True,
        }
        if config:
            self.config.update(config)

    def check_positions(self, positions: List[Dict], market_data: Dict = None) -> List[Dict]:
        """检查持仓异常

        Args:
            positions: 持仓列表，包含实时行情
            market_data: 市场数据（大盘、板块）

        Returns:
            异常卖出信号列表
        """
        if not self.config["enabled"]:
            return []

        sell_signals = []

        # 1. 大盘系统性风险检查
        market_crash = False
        if market_data and self.config["reduce_on_market_crash"]:
            market_crash = self._check_market_crash(market_data)
            if market_crash:
                # 大盘暴跌，所有持仓触发减仓
                for pos in positions:
                    if pos.get("shares", 0) > 0:
                        sell_signals.append(self._create_signal(
                            pos, "market_crash",
                            f"🚨 大盘暴跌{market_data.get('index_pct', 0):.1f}%，紧急减仓"
                        ))
                return sell_signals  # 大盘风险优先级最高

        # 2. 逐个检查持仓异常
        for pos in positions:
            if pos.get("shares", 0) <= 0:
                continue

            symbol = pos["symbol"]
            pct_change = pos.get("pct_change", 0)  # 今日涨跌幅
            volume_ratio = pos.get("volume_ratio", 1)  # 量比
            current_price = pos.get("current_price", pos.get("price", 0))

            # 2.1 跌停检测
            if pct_change <= self.config["limit_down_pct"]:
                sell_signals.append(self._create_signal(
                    pos, "limit_down",
                    f"🔴 跌停逃逸: {symbol} 跌幅{pct_change:.1f}%"
                ))
                continue

            # 2.2 放量暴跌检测
            if (volume_ratio >= self.config["volume_spike_ratio"] and
                pct_change <= self.config["panic_drop_pct"]):
                sell_signals.append(self._create_signal(
                    pos, "panic_drop",
                    f"⚠️ 放量暴跌: {symbol} 量比{volume_ratio:.1f}x, 跌幅{pct_change:.1f}%"
                ))
                continue

            # 2.3 板块弱于检测（如果有板块数据）
            if market_data and "sector" in pos:
                sector_change = market_data.get("sectors", {}).get(pos["sector"], 0)
                relative_weakness = pct_change - sector_change

                if (sector_change > 0 and  # 板块涨
                    relative_weakness <= self.config["sector_underperform"]):
                    sell_signals.append(self._create_signal(
                        pos, "sector_underperform",
                        f"📉 弱于板块: {symbol} 板块{sector_change:+.1f}%, 个股{pct_change:+.1f}%"
                    ))
                    continue

        return sell_signals

    def _check_market_crash(self, market_data: Dict) -> bool:
        """检查大盘是否暴跌"""
        index_pct = market_data.get("index_pct", 0)
        return index_pct <= self.config["market_crash_pct"]

    def _create_signal(self, pos: Dict, reason: str, message: str) -> Dict:
        """创建卖出信号"""
        return {
            "symbol": pos["symbol"],
            "signal": "sell",
            "price": pos.get("current_price", pos.get("price", 0)),
            "action_shares": pos.get("shares", 0),
            "avg_cost": pos.get("avg_cost", 0),
            "pnl_pct": pos.get("pnl_pct", 0),
            "reason": reason,
            "message": message,
            "timestamp": datetime.now().isoformat(),
        }


def run_intraday_check(storage=None) -> Dict:
    """执行盘中异常检查

    Args:
        storage: 数据存储对象（用于获取实时行情）

    Returns:
        {
            'has_risk': bool,
            'signals': List[Dict],
            'summary': str,
        }
    """
    from pathlib import Path

    monitor = IntradayRiskMonitor()

    # 获取持仓
    if storage is None:
        from scripts.execution.paper_executor import PaperTradingStorage
        db_path = Path.home() / ".openclaw/workspace/skills/LongToo-trader/data/paper_trading.db"
        storage = PaperTradingStorage(str(db_path))

    # 获取持仓
    positions = storage.get_positions()

    # 转换为字典格式（Position 对象 -> dict）
    positions_dict = []
    for pos in positions:
        if hasattr(pos, '__dict__'):
            positions_dict.append({
                'symbol': pos.symbol,
                'shares': pos.shares,
                'avg_cost': pos.avg_cost,
                'current_price': pos.current_price,
                'pnl_pct': pos.pnl_pct,
                'buy_date': pos.buy_date,
            })
        else:
            positions_dict.append(pos)

    if not positions_dict:
        return {
            'has_risk': False,
            'signals': [],
            'summary': '无持仓，跳过盘中检查',
        }

    # 获取实时行情数据（简化：从数据库或API）
    market_data = _get_market_data(storage)

    # 检查异常
    signals = monitor.check_positions(positions_dict, market_data)

    # 生成摘要
    if signals:
        summary = f"发现 {len(signals)} 个异常风险信号"
        for s in signals[:3]:
            summary += f"\n  - {s['message']}"
    else:
        summary = "持仓正常，无异常风险"

    return {
        'has_risk': len(signals) > 0,
        'signals': signals,
        'summary': summary,
    }


def _get_market_data(storage) -> Dict:
    """获取市场数据（大盘、板块）"""
    import sqlite3
    from pathlib import Path

    try:
        db_path = Path.home() / ".openclaw/workspace/skills/LongToo-trader/data/market_kline.db"
        conn = sqlite3.connect(str(db_path))

        # 获取沪深300最新涨跌
        row = conn.execute('''
            SELECT close FROM daily_kline
            WHERE symbol = '000300'
            ORDER BY trade_date DESC LIMIT 1
        ''').fetchone()

        index_price = row[0] if row else 0

        # 简化：假设大盘数据
        # 实际应该从实时API获取
        market_data = {
            "index_symbol": "000300",
            "index_price": index_price,
            "index_pct": 0,  # 需要实时计算
            "sectors": {},   # 板块数据
        }

        conn.close()
        return market_data

    except Exception as e:
        logger.warning(f"获取市场数据失败: {e}")
        return {}


# 命令行入口
if __name__ == "__main__":
    result = run_intraday_check()
    print(result['summary'])

    if result['has_risk']:
        print("\n⚠️ 发现异常风险！")
        for s in result['signals']:
            print(f"  {s['message']}")
