"""Execution Agent - 交易执行逻辑封装（阶段2：支持消息总线 + OrderManager）"""

from typing import Dict, List, Any
from datetime import datetime
from .base import BaseAgent, AgentResult
from ..messaging import Channel, Message
from ..core.order_manager import OrderManager, OrderStatus


class ExecutionAgent(BaseAgent):
    """交易执行 Agent

    负责执行买入/卖出交易，更新止损价，记录交易日志。
    阶段2：支持消息总线，订阅 C3_RISK_RESULT，执行后发布到 SYS_LOG
    v2.5.0: 使用 OrderManager 管理订单生命周期
    """

    def __init__(self, api_client, memory_manager, risk_manager, dry_run: bool = True,
                 order_manager: OrderManager = None):
        """
        Args:
            api_client: API 客户端实例
            memory_manager: MemoryManager 实例
            risk_manager: RiskManager 实例
            dry_run: 是否为模拟模式
            order_manager: OrderManager 实例（v2.5.0）
        """
        super().__init__()
        self.api_client = api_client
        self.memory_manager = memory_manager
        self.risk_manager = risk_manager
        self.dry_run = dry_run
        self._executed_ids = set()  # 信号去重缓存（防止重复交易）
        self.order_manager = order_manager  # v2.5.0
    
    def setup_subscriptions(self):
        """设置消息订阅（阶段2新增）"""
        self.subscribe(Channel.C3_RISK_RESULT, self.handle_message)
        print(f"🔔 {self.name} 已订阅 {Channel.C3_RISK_RESULT.value}")
    
    def handle_message(self, message: Message):
        """处理收到的消息（阶段2新增）
        
        收到 C3_RISK_RESULT 后执行交易
        """
        if message.channel == Channel.C3_RISK_RESULT:
            print(f"📥 {self.name} 收到风控结果: {message.msg_id}")
            
            payload = message.payload
            if "error" in payload:
                print(f"⚠️ 风控阶段出错，跳过执行: {payload['error']}")
                return
            
            # 执行交易
            input_data = {
                "signals": payload.get("passed_signals", []),
                "total_assets": payload.get("risk_status", {}).get("total_assets", 1000000),
                "initial_assets": 1000000
            }
            result = self.process(input_data)
            
            # 发布执行日志
            log_msg = Message(
                channel=Channel.SYS_LOG,
                sender=self.name,
                msg_type="result" if result.success else "error",
                payload={
                    "action": "trade_execution",
                    "result": result.data if result.success else {"error": result.error}
                },
                correlation_id=message.msg_id
            )
            self.publish(log_msg)
            print(f"📤 {self.name} 发布执行日志")
    
    def process(self, input_data: Dict) -> AgentResult:
        """执行交易
        
        Args:
            input_data: 包含以下字段
                - signals: 通过风控的交易信号列表
                - total_assets: 当前总资产
                - initial_assets: 初始资金
                
        Returns:
            AgentResult: data 包含
                - executed_trades: 成功执行的交易列表
                - failed_trades: 失败的交易列表
                - summary: 执行汇总
        """
        try:
            signals = input_data.get("signals", [])
            total_assets = input_data.get("total_assets", 1000000)
            initial_assets = input_data.get("initial_assets", 1000000)

            executed_trades = []
            failed_trades = []

            for signal in signals:
                try:
                    symbol = signal['symbol']
                    action = signal['signal']

                    # v2.5.0: 使用 OrderManager 创建订单（含去重）
                    if self.order_manager:
                        order = self.order_manager.create_order(signal)
                        if order is None:
                            print(f"  ⚠️ 重复信号跳过: {symbol} {action}")
                            continue
                        order_id = order.order_id
                    else:
                        # 回退：手动去重（兼容旧调用）
                        sig_id = f"{symbol}_{signal.get('timestamp', datetime.now().isoformat())}_{action}"
                        if sig_id in self._executed_ids:
                            print(f"  ⚠️ 重复信号跳过: {symbol} {action}")
                            continue
                        self._executed_ids.add(sig_id)
                        order_id = sig_id

                    # 每日开仓限制检查（买入才检查）
                    if action == 'buy' and self.memory_manager:
                        state = self.memory_manager.get_strategy_state() or {}
                        daily_count = state.get('daily_trade_count', 0)
                        if daily_count >= 3:
                            print(f"  ⚠️ 每日开仓限制: 已开仓{daily_count}次，跳过 {symbol} 买入")
                            if self.order_manager:
                                self.order_manager.mark_failed(order_id, "每日开仓限制")
                            continue

                    # T+1 检查（卖出才检查）
                    if action == 'sell' and self.memory_manager:
                        buy_date = self.memory_manager.get_buy_date(symbol)
                        if buy_date and datetime.now().date() <= buy_date:
                            print(f"  ⚠️ T+1限制: {symbol} 今日买入，无法卖出")
                            if self.order_manager:
                                self.order_manager.mark_failed(order_id, "T+1限制")
                            continue

                    # 涨跌停检查
                    price = signal.get('price', 0)
                    change_pct = signal.get('change_pct', 0)
                    if action == 'buy' and change_pct >= 9.8:
                        print(f"  ⚠️ 涨停板: {symbol} 涨幅{change_pct:.1f}%，无法买入")
                        if self.order_manager:
                            self.order_manager.mark_failed(order_id, "涨停板")
                        continue
                    if action == 'sell' and change_pct <= -9.8:
                        print(f"  ⚠️ 跌停板: {symbol} 跌幅{change_pct:.1f}%，无法卖出")
                        if self.order_manager:
                            self.order_manager.mark_failed(order_id, "跌停板")
                        continue

                    result = self._execute_single_trade(signal)
                    if result.get("success"):
                        executed_trades.append(result)
                        self._post_trade_update(signal, result)
                        # v2.5.0: 标记订单成交
                        if self.order_manager:
                            self.order_manager.mark_filled(order_id, result)
                        else:
                            sig_id = f"{symbol}_{signal.get('timestamp', '')}_{action}"
                            self._executed_ids.add(sig_id)
                    else:
                        failed_trades.append(result)
                        if self.order_manager:
                            self.order_manager.mark_failed(order_id, result.get("error", "未知错误"))
                except Exception as e:
                    failed_trades.append({
                        "signal": signal,
                        "error": str(e),
                        "success": False
                    })
            
            # 更新当日盈亏统计
            updated = self.risk_manager.update_daily_pnl(total_assets, initial_assets)
            if not updated:
                # 盈亏统计失败，风控数据异常，触发报警但不阻断交易（避免资金损失）
                print("🚨 警告: 盈亏统计更新失败！风控可能无法准确计算当日亏损！")
                try:
                    from scripts.utils.alerts import get_alert_manager
                    get_alert_manager().send(
                        title="盈亏统计更新失败",
                        content="当日盈亏统计更新失败，风控数据可能不准确，请立即检查！",
                        level="critical"
                    )
                except Exception as e:
                    print(f"⚠️ 发送报警失败: {e}")
            
            return AgentResult(
                success=True,
                data={
                    "executed_trades": executed_trades,
                    "failed_trades": failed_trades,
                    "summary": {
                        "total": len(signals),
                        "executed": len(executed_trades),
                        "failed": len(failed_trades)
                    }
                }
            )
            
        except Exception as e:
            return AgentResult(
                success=False,
                data={},
                error=f"交易执行失败: {str(e)}"
            )
    
    def _execute_single_trade(self, signal: Dict) -> Dict:
        """执行单笔交易"""
        symbol = signal["symbol"]
        action = signal["signal"]
        shares = signal.get("action_shares", 0)
        
        if self.dry_run:
            return {
                "success": True,
                "mode": "dryrun",
                "symbol": symbol,
                "action": action,
                "quantity": shares,
                "price": signal.get("price", 0),
                "amount": round(shares * signal.get("price", 0), 2),
                "order_id": f"dryrun_{datetime.now().strftime('%Y%m%d%H%M%S')}_{symbol}"
            }
        else:
            return self.api_client.execute_order(symbol, action, shares)
    
    def _post_trade_update(self, signal: Dict, result: Dict):
        """交易后更新"""
        symbol = signal["symbol"]
        action = signal["signal"]
        
        # 计算实际盈亏率
        profit_rate = 0
        if action == "sell":
            avg_cost = signal.get("avg_cost", 0)
            sell_price = result.get("price", signal.get("price", 0))
            if avg_cost > 0:
                profit_rate = (sell_price - avg_cost) / avg_cost
        
        self.memory_manager.update_after_trade(signal, profit_rate)
        self.memory_manager.log_trade(signal)
        
        # 更新持仓数量（风控用）
        state = self.memory_manager.load_strategy_state()
        if action == 'buy':
            state['current_holdings_count'] = state.get('current_holdings_count', 0) + 1
            state['daily_trade_count'] = state.get('daily_trade_count', 0) + 1
        elif action == 'sell':
            state['current_holdings_count'] = max(0, state.get('current_holdings_count', 0) - 1)
        self.memory_manager.save_strategy_state(state)
        
        # 发送交易执行通知
        if result.get("success"):
            try:
                from scripts.utils.alerts import get_alert_manager
                get_alert_manager().trade_executed(
                    symbol=symbol,
                    action=action,
                    quantity=result.get("quantity", signal.get("action_shares", 0)),
                    price=result.get("price", signal.get("price", 0)),
                    amount=result.get("amount", 0)
                )
            except Exception as e:
                print(f"⚠️ 发送交易通知失败: {e}")
        
        if action == "buy":
            # 安全获取止损价
            conditions = signal.get("conditions") or {}
            stop_loss = conditions.get("stop_loss")
            
            if stop_loss and stop_loss > 0:
                saved = self.memory_manager.save_stop_loss(symbol, stop_loss)
                if saved:
                    print(f"  💾 已保存 {symbol} 止损价: ¥{stop_loss:.2f}")
                else:
                    # 严重：止损价保存失败，持仓无保护
                    error_msg = f"🚨 严重错误: {symbol} 止损价保存失败！持仓无保护，建议立即手动检查！"
                    print(f"  {error_msg}")
                    # 发送紧急报警
                    try:
                        from scripts.utils.alerts import get_alert_manager
                        get_alert_manager().send(
                            title=f"止损价保存失败 - {symbol}",
                            content=error_msg,
                            level="critical"
                        )
                    except Exception as e:
                        print(f"⚠️ 发送止损报警失败: {e}")
            else:
                print(f"  ⚠️ 警告: {symbol} 买入时未能设置止损价！")
        
        if action == "sell":
            self.memory_manager.clear_stop_loss(symbol)
    
    def health_check(self) -> bool:
        """检查执行组件健康状态"""
        try:
            _ = self.api_client.get_portfolio()
            return True
        except Exception:
            return False
