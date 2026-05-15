#!/usr/bin/env python3
"""
Report Agent - 盘后报告 Agent

每日收盘后生成交易统计报告
"""

from typing import Dict, List
from datetime import datetime, timedelta
from .base import BaseAgent, AgentResult


class ReportAgent(BaseAgent):
    """盘后报告 Agent
    
    功能：
    1. 统计当日交易
    2. 计算胜率、盈亏比
    3. 分析持仓表现
    4. 生成日报
    """
    
    def __init__(self, storage):
        """
        Args:
            storage: 数据存储实例
        """
        super().__init__()
        self.storage = storage
    
    def handle_message(self, message):
        """处理消息（消息总线用）"""
        # ReportAgent 不参与消息总线
        pass
    
    def health_check(self) -> bool:
        """健康检查"""
        return self.storage is not None
    
    def process(self, input_data: Dict) -> AgentResult:
        """生成盘后统计报告

        Args:
            input_data: 包含以下字段
                - account: 当前账户信息
                - positions: 当前持仓

        Returns:
            AgentResult: data 包含完整统计报告
        """
        try:
            # 获取今日交易
            today_trades = self._get_today_trades()

            # 获取历史交易（最近30天）
            recent_trades = self._get_recent_trades(days=30)

            # 计算统计数据
            stats = {
                "today": self._calculate_stats(today_trades),
                "recent": self._calculate_stats(recent_trades),
                "current_positions": len(input_data.get("positions", [])),
                "account_summary": input_data.get("account", {}),
                "today_trades": today_trades  # 新增：今日交易明细
            }

            return AgentResult(
                success=True,
                data=stats
            )

        except Exception as e:
            return AgentResult(
                success=False,
                data={},
                error=f"生成报告失败: {str(e)}"
            )
    
    def _get_today_trades(self) -> List[Dict]:
        """获取今日交易"""
        today = datetime.now().strftime("%Y-%m-%d")
        all_trades = self.storage.get_trades(limit=1000)
        # 处理 TradeRecord 对象
        result = []
        for t in all_trades:
            # TradeRecord 使用 timestamp 字段
            if hasattr(t, 'timestamp'):
                trade_timestamp = t.timestamp
            elif hasattr(t, 'date'):
                trade_timestamp = t.date
            else:
                trade_timestamp = t.get("timestamp", t.get("date", ""))
            if trade_timestamp.startswith(today):
                result.append(self._trade_to_dict(t))
        return result

    def _get_recent_trades(self, days: int = 30) -> List[Dict]:
        """获取最近N天交易"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        all_trades = self.storage.get_trades(limit=1000)
        # 处理 TradeRecord 对象
        result = []
        for t in all_trades:
            # TradeRecord 使用 timestamp 字段
            if hasattr(t, 'timestamp'):
                trade_timestamp = t.timestamp
            elif hasattr(t, 'date'):
                trade_timestamp = t.date
            else:
                trade_timestamp = t.get("timestamp", t.get("date", ""))
            if trade_timestamp >= cutoff:
                result.append(self._trade_to_dict(t))
        return result

    def _trade_to_dict(self, trade) -> Dict:
        """将 TradeRecord 转换为 dict"""
        if hasattr(trade, 'symbol'):
            return {
                "symbol": trade.symbol,
                "action": trade.action.lower() if hasattr(trade.action, 'lower') else trade.action,
                "date": getattr(trade, 'timestamp', getattr(trade, 'date', '')),
                "price": trade.price,
                "shares": trade.shares,
                "pnl": getattr(trade, 'pnl', 0)
            }
        return trade  # 已经是 dict
    
    def _calculate_stats(self, trades: List[Dict]) -> Dict:
        """计算交易统计"""
        if not trades:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "avg_profit": 0.0,
                "total_pnl": 0.0
            }
        
        buy_count = len([t for t in trades if t.get("action") == "buy"])
        sell_count = len([t for t in trades if t.get("action") == "sell"])
        
        # 计算卖出交易的盈亏
        sell_trades = [t for t in trades if t.get("action") == "sell"]
        if sell_trades:
            profits = [t.get("pnl", 0) for t in sell_trades]
            wins = len([p for p in profits if p > 0])
            win_rate = (wins / len(sell_trades)) * 100 if sell_trades else 0.0
            avg_profit = sum(profits) / len(profits) if profits else 0.0
            total_pnl = sum(profits)
        else:
            win_rate = 0.0
            avg_profit = 0.0
            total_pnl = 0.0
        
        return {
            "total_trades": len(trades),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "win_rate": win_rate,
            "avg_profit": avg_profit,
            "total_pnl": total_pnl
        }
