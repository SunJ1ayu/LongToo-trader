#!/usr/bin/env python3
"""
Position Monitor Agent - 持仓监控 Agent

盘中监控持仓，触发止损止盈时生成预警
"""

import logging
import yaml
from pathlib import Path
from typing import Dict, List, Optional
from .base import BaseAgent, AgentResult

logger = logging.getLogger(__name__)


def load_position_monitor_config():
    """加载持仓监控配置"""
    config_path = Path(__file__).parent.parent.parent / "config" / "paper_trading.yaml"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
                risk = config.get("risk_management", {})
                return {
                    "stop_loss_pct": -risk.get("stop_loss_pct", 8.0),
                    "take_profit_pct": risk.get("take_profit_pct", 15.0)
                }
        except Exception as e:
            logger.warning(f"加载配置文件失败: {e}，使用默认配置")
    return {"stop_loss_pct": -8.0, "take_profit_pct": 15.0}


class PositionMonitorAgent(BaseAgent):
    """持仓监控 Agent
    
    功能：
    1. 监控持仓股票实时价格
    2. 检查是否触发止损（配置值）
    3. 检查是否触发止盈（配置值）
    4. 生成预警信号
    """
    
    def __init__(self, data_provider):
        """
        Args:
            data_provider: 数据提供者实例
        """
        super().__init__()
        self.data_provider = data_provider
        config = load_position_monitor_config()
        self.stop_loss_pct = config["stop_loss_pct"]
        self.take_profit_pct = config["take_profit_pct"]
        logger.info(f"持仓监控配置: 止损{self.stop_loss_pct}%, 止盈+{self.take_profit_pct}%")
    
    def handle_message(self, message):
        """处理消息（消息总线用）"""
        # PositionMonitorAgent 不参与消息总线
        pass
    
    def health_check(self) -> bool:
        """健康检查"""
        return self.data_provider is not None
    
    def process(self, input_data: Dict) -> AgentResult:
        """监控持仓并检查止损止盈
        
        Args:
            input_data: 包含以下字段
                - positions: 持仓列表
                
        Returns:
            AgentResult: data 包含
                - alerts: 预警列表
                - positions_checked: 检查的股票数
        """
        try:
            positions = input_data.get("positions", [])
            
            if not positions:
                return AgentResult(
                    success=True,
                    data={"alerts": [], "positions_checked": 0}
                )
            
            alerts = []
            
            for position in positions:
                symbol = position.get("symbol")
                current_price = position.get("current_price", 0)
                avg_cost = position.get("avg_cost", 0)
                pnl_pct = position.get("pnl_pct", 0)
                
                if avg_cost <= 0:
                    continue
                
                # 检查止损
                if pnl_pct <= self.stop_loss_pct:
                    alerts.append({
                        "type": "stop_loss",
                        "symbol": symbol,
                        "current_price": current_price,
                        "avg_cost": avg_cost,
                        "pnl_pct": pnl_pct,
                        "message": f"🔴 {symbol} 触发止损：亏损 {pnl_pct:.2f}%"
                    })
                    logger.warning(f"止损触发: {symbol} 亏损 {pnl_pct:.2f}%")
                
                # 检查止盈
                elif pnl_pct >= self.take_profit_pct:
                    alerts.append({
                        "type": "take_profit",
                        "symbol": symbol,
                        "current_price": current_price,
                        "avg_cost": avg_cost,
                        "pnl_pct": pnl_pct,
                        "message": f"🟢 {symbol} 触发止盈：盈利 {pnl_pct:.2f}%"
                    })
                    logger.info(f"止盈触发: {symbol} 盈利 {pnl_pct:.2f}%")
            
            return AgentResult(
                success=True,
                data={
                    "alerts": alerts,
                    "positions_checked": len(positions)
                }
            )
            
        except Exception as e:
            logger.error(f"持仓监控失败: {e}", exc_info=True)
            return AgentResult(
                success=False,
                data={},
                error=f"持仓监控失败: {str(e)}"
            )
