"""Risk Agent - 风控逻辑封装（阶段2：支持消息总线）"""

from typing import Dict, List, Any
from .base import BaseAgent, AgentResult
from ..messaging import Channel, Message


class RiskAgent(BaseAgent):
    """风控 Agent
    
    负责检查交易信号是否符合风控规则，拦截违规交易。
    阶段2：支持消息总线，订阅 C2_ANALYSIS_RESULT，发布到 C3_RISK_RESULT
    """
    
    def __init__(self, risk_manager):
        """
        Args:
            risk_manager: RiskManager 实例，提供风控检查能力
        """
        super().__init__()
        self.risk_manager = risk_manager
    
    def setup_subscriptions(self):
        """设置消息订阅（阶段2新增）"""
        self.subscribe(Channel.C2_ANALYSIS_RESULT, self.handle_message)
        print(f"🔔 {self.name} 已订阅 {Channel.C2_ANALYSIS_RESULT.value}")
    
    def handle_message(self, message: Message):
        """处理收到的消息（阶段2新增）
        
        收到 C2_ANALYSIS_RESULT 后执行风控检查，发布结果到 C3
        """
        if message.channel == Channel.C2_ANALYSIS_RESULT:
            print(f"📥 {self.name} 收到分析结果: {message.msg_id}")
            
            # 从消息中提取信号
            payload = message.payload
            if "error" in payload:
                # 分析阶段出错，直接转发错误
                response = Message(
                    channel=Channel.C3_RISK_RESULT,
                    sender=self.name,
                    msg_type="error",
                    payload={"error": payload["error"], "passed_signals": []},
                    correlation_id=message.msg_id
                )
            else:
                # 执行风控检查
                # 获取账户信息（这里简化，实际需要传入或通过消息传递）
                input_data = {
                    "signals": payload.get("signals", []),
                    "total_assets": payload.get("total_assets", 1000000),
                    "initial_assets": payload.get("initial_assets", 1000000)
                }
                result = self.process(input_data)
                
                # 发布结果到 C3
                response = Message(
                    channel=Channel.C3_RISK_RESULT,
                    sender=self.name,
                    msg_type="result" if result.success else "error",
                    payload=result.data if result.success else {"error": result.error},
                    correlation_id=message.msg_id
                )
            
            self.publish(response)
            print(f"📤 {self.name} 发布风控结果到 {Channel.C3_RISK_RESULT.value}")
    
    def process(self, input_data: Dict) -> AgentResult:
        """执行风控检查
        
        Args:
            input_data: 包含以下字段
                - signals: 待检查的交易信号列表
                - total_assets: 当前总资产
                - initial_assets: 初始资金
                
        Returns:
            AgentResult: data 包含
                - passed_signals: 通过风控的信号列表
                - blocked_signals: 被拦截的信号列表
                - risk_status: 当前风控状态
        """
        try:
            signals = input_data.get("signals", [])
            total_assets = input_data.get("total_assets", 1000000)
            initial_assets = input_data.get("initial_assets", 1000000)
            
            # 检查 risk_manager 是否有效
            has_risk_manager = self.risk_manager and hasattr(self.risk_manager, 'check_and_clear_cooldown')
            
            if has_risk_manager:
                # 检查冷却期是否过期
                self.risk_manager.check_and_clear_cooldown()
                
                # 更新当日盈亏
                self.risk_manager.update_daily_pnl(total_assets, initial_assets)
            
            passed_signals = []
            blocked_signals = []
            
            for signal in signals:
                # 跳过 HOLD 信号
                if signal.get("signal") == "hold":
                    continue
                
                # 执行风控检查（如果有 risk_manager）
                if has_risk_manager:
                    risk_check = self.risk_manager.check_risk_limits(signal)
                    
                    if risk_check["allowed"]:
                        passed_signals.append(signal)
                    else:
                        blocked_signal = signal.copy()
                        blocked_signal["blocked_reason"] = risk_check.get("reason", "风控拦截")
                        blocked_signals.append(blocked_signal)
                        
                        # 发送风控拦截通知
                        try:
                            from scripts.utils.alerts import get_alert_manager
                            get_alert_manager().risk_block(
                                symbol=signal.get("symbol", "unknown"),
                                reason=risk_check.get("reason", "风控拦截")
                            )
                        except Exception:
                            pass
                else:
                    # 没有 risk_manager，直接通过所有信号
                    passed_signals.append(signal)
            
            # 获取当前风控状态
            if has_risk_manager:
                risk_status = self.risk_manager.get_risk_status()
            else:
                risk_status = {"note": "风控管理器未启用"}
            
            return AgentResult(
                success=True,
                data={
                    "passed_signals": passed_signals,
                    "blocked_signals": blocked_signals,
                    "risk_status": risk_status,
                    "total_checked": len(signals),
                    "passed_count": len(passed_signals),
                    "blocked_count": len(blocked_signals)
                }
            )
            
        except Exception as e:
            return AgentResult(
                success=False,
                data={},
                error=f"风控检查失败: {str(e)}"
            )
    
    def health_check(self) -> bool:
        """检查风控组件健康状态"""
        try:
            _ = self.risk_manager.get_risk_status()
            return True
        except Exception:
            return False
