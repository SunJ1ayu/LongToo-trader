#!/usr/bin/env python3
"""
Paper Trading Adapter
将 PaperTradingExecutor 包装成与实盘 ExecutionAgent 兼容的接口

使得现有的 AnalystAgent 和 RiskAgent 可以无缝切换到模拟盘模式
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from scripts.execution.paper_executor import PaperTradingExecutor, PaperTradingStorage
from scripts.data.tencent_provider import TencentFinanceProvider as DataProvider
from scripts.agents.base import BaseAgent, AgentResult

logger = logging.getLogger(__name__)


class PaperTradingExecutionAgent(BaseAgent):
    """
    模拟盘执行 Agent - 适配器模式
    
    提供与实盘 ExecutionAgent 相同的接口，但使用虚拟资金执行
    继承 BaseAgent，统一 Agent 接口
    """
    
    def __init__(self, config: Dict, storage: Optional[PaperTradingStorage] = None,
                 memory_manager=None, risk_engine=None, position_engine=None):
        """
        Args:
            config: 配置字典，包含：
                - initial_capital: 初始资金（默认100000）
                - slippage: 滑点率（默认0.001）
                - max_positions: 最大持仓数（默认10）
                - max_position_pct: 单票最大仓位%（默认20）
                - data_provider: 数据源配置
            memory_manager: MemoryManager 实例（用于追踪连续亏损等风控指标）
            risk_engine: RiskEngine 实例（v2.4.0 中央化风控）
            position_engine: PositionEngine 实例（v2.5.0 中央化状态管理）
        """
        self.config = config
        self.executor = PaperTradingExecutor(config, storage, risk_engine=risk_engine)
        self.data_provider = DataProvider()
        self.memory_manager = memory_manager

        # v2.4.0: RiskEngine 中央化
        self.risk_engine = risk_engine
        # v2.5.0: PositionEngine 中央化状态管理
        self.position_engine = position_engine

        # 记录启动信息
        account = self.executor.get_account_status()
        logger.info(f"=" * 50)
        logger.info(f"【模拟盘启动】初始资金: ¥{account.initial_capital:,.2f}")
        logger.info(f"=" * 50)
    
    def health_check(self) -> bool:
        """健康检查"""
        try:
            # 检查数据库连接
            import sqlite3
            with sqlite3.connect(self.executor.storage.db_path) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return False
    
    # ==================== 账户查询接口 ====================
    
    def get_account(self) -> Dict:
        """获取账户信息（兼容实盘 API 格式）"""
        account = self.executor.get_account_status()
        positions = self.executor.get_positions()
        
        # 计算持仓市值
        positions_value = sum(p.market_value for p in positions)
        
        return {
            "cash": account.cash,
            "total_assets": account.total_assets,
            "initial_capital": account.initial_capital,
            "positions_value": positions_value,
            "available_cash": account.cash,  # 可用现金
            "total_return": account.total_assets - account.initial_capital,
            "total_return_pct": ((account.total_assets - account.initial_capital) / account.initial_capital) * 100
        }
    
    def get_portfolio(self) -> Dict:
        """获取投资组合（兼容实盘 API 格式）"""
        positions = self.get_positions()
        account = self.get_account()
        
        return {
            "holdings": positions,
            "portfolio": account
        }
    
    def get_positions(self) -> List[Dict]:
        """获取持仓列表（兼容实盘 API 格式）"""
        # 加载股票名称映射
        stock_names = {}
        try:
            import json
            from scripts.data.market_sync import STOCK_LIST_CACHE
            if STOCK_LIST_CACHE.exists():
                cache = json.loads(STOCK_LIST_CACHE.read_text(encoding='utf-8'))
                stock_names = {s['code']: s['name'] for s in cache.get('stocks', [])}
        except:
            pass

        positions = self.executor.get_positions()
        result = []
        for pos in positions:
            code = pos.symbol.replace('sh', '').replace('sz', '')
            result.append({
                "symbol": pos.symbol,
                "name": stock_names.get(code, pos.symbol),
                "shares": pos.shares,
                "avg_cost": pos.avg_cost,
                "current_price": pos.current_price,
                "market_value": pos.market_value,
                "pnl": pos.pnl,
                "pnl_pct": pos.pnl_pct,
                "can_sell": pos.can_sell,
                "buy_date": pos.buy_date,
                # 盈利保护字段
                "peak_pnl": pos.peak_pnl,
                "peak_price": pos.peak_price,
                "reduced_from_peak": pos.reduced_from_peak,
                "last_reduce_at": pos.last_reduce_at
            })
        return result

    def get_position(self, symbol: str) -> Optional[Dict]:
        """获取单只股票持仓"""
        position = self.executor.storage.get_position(symbol)
        if position and position.shares > 0:
            return {
                "symbol": position.symbol,
                "shares": position.shares,
                "avg_cost": position.avg_cost,
                "current_price": position.current_price,
                "market_value": position.market_value,
                "pnl": position.pnl,
                "pnl_pct": position.pnl_pct,
                "can_sell": position.can_sell,
                "buy_date": position.buy_date,
                # 盈利保护字段
                "peak_pnl": position.peak_pnl,
                "peak_price": position.peak_price,
                "reduced_from_peak": position.reduced_from_peak,
                "last_reduce_at": position.last_reduce_at
            }
        return None
    
    # ==================== 交易执行接口 ====================
    
    def calculate_shares_by_position_size(self, position_size_pct: float, price: float) -> int:
        """
        根据仓位比例计算买入股数
        
        Args:
            position_size_pct: 仓位比例（如15表示15%）
            price: 当前价格
        
        Returns:
            买入股数（100的倍数）
        """
        account = self.executor.get_account_status()
        target_value = account.total_assets * (position_size_pct / 100)
        shares = int(target_value / price / 100) * 100  # 向下取整到100的倍数
        return max(shares, 0)
    
    def execute_buy_by_position_size(self, symbol: str, position_size_pct: float, 
                                     signal_price: float = None) -> Tuple[bool, str, Optional[Dict]]:
        """
        根据策略仓位比例执行买入
        
        Args:
            symbol: 股票代码
            position_size_pct: 仓位比例（如15表示15%仓位）
            signal_price: 信号价格（可选）
        
        Returns:
            (success, message, trade_info)
        """
        # 如果没有提供信号价格，获取实时价格
        if signal_price is None:
            quote = self.data_provider.get_realtime_quote(symbol)
            if quote is None:
                return False, f"无法获取 {symbol} 的行情", None
            signal_price = quote["price"]
        elif signal_price <= 0:
            return False, f"无效的信号价格: {signal_price}", None
        # 传了有效 price，直接使用，不再查询实时行情
        
        # 根据仓位比例计算股数
        shares = self.calculate_shares_by_position_size(position_size_pct, signal_price)
        
        if shares < 100:
            return False, f"计算股数不足100股（仓位{position_size_pct}%，价格¥{signal_price:.2f}）", None
        
        logger.info(f"策略仓位: {position_size_pct}%，计算买入: {shares}股 @ ¥{signal_price:.2f}")
        
        # 执行买入
        return self.execute_buy(symbol, shares, signal_price)
    
    def execute_buy(self, symbol: str, shares: int, signal_price: float = None, stop_loss: float = 0.0) -> Tuple[bool, str, Optional[Dict]]:
        """
        执行买入（指定股数）
        
        Args:
            symbol: 股票代码
            shares: 买入股数（100的倍数）
            signal_price: 信号价格（可选，不填则实时获取）
            stop_loss: 止损价格（可选）
        
        Returns:
            (success, message, trade_info)
        """
        # 如果没有提供信号价格，尝试获取实时价格
        if signal_price is None:
            quote = self.data_provider.get_realtime_quote(symbol)
            if quote is None:
                return False, f"无法获取 {symbol} 的行情", None
            signal_price = quote["price"]
        # 如果传了 price 但小于等于0，属于无效价格
        elif signal_price <= 0:
            return False, f"无效的信号价格: {signal_price}", None
        # 传了有效 price，直接使用，不再查询实时行情（避免网络问题阻断交易）
        
        # 执行买入
        success, msg, trade = self.executor.execute_buy(symbol, shares, signal_price, stop_loss=stop_loss)
        
        if success and trade:
            trade_info = {
                "symbol": trade.symbol,
                "action": trade.action,
                "shares": trade.shares,
                "price": trade.price,
                "amount": trade.amount,
                "commission": trade.commission,
                "total_cost": trade.total_cost,
                "slippage": trade.slippage,
                "timestamp": trade.timestamp
            }
            # v2.5.0: 发布 ORDER_FILLED 事件
            self._emit_trade_event(trade_info)
            return True, msg, trade_info
        
        return False, msg, None

    def _emit_trade_event(self, trade_info: Dict):
        """发布 TRADE_ORDER_FILLED 事件（v2.5.0）

        Args:
            trade_info: 交易信息字典，包含 symbol, action, shares, price 等
        """
        try:
            from scripts.messaging.local_bus import Message, Channel

            # 获取持仓盈亏信息（卖出时）
            pnl_pct = trade_info.get("pnl_pct", 0)

            message = Message(
                channel=Channel.TRADE_ORDER_FILLED,
                sender=self.name,
                msg_type="event",
                payload={
                    "symbol": trade_info["symbol"],
                    "action": trade_info["action"],
                    "shares": trade_info["shares"],
                    "price": trade_info["price"],
                    "amount": trade_info["amount"],
                    "pnl_pct": pnl_pct,
                    "timestamp": trade_info["timestamp"]
                }
            )

            # 使用 publish_sync 确保事件被处理（同步发布）
            if self._message_bus:
                self._message_bus.publish_sync(message)
                logger.info(f"事件发布: TRADE_ORDER_FILLED {trade_info['symbol']} {trade_info['action']}")
            else:
                logger.debug(f"无消息总线，跳过事件发布")

        except Exception as e:
            logger.warning(f"发布事件失败: {e}")

    def _track_trade(self, signal: Dict, profit_rate: float):
        """追踪交易到风控系统（用于连续亏损熔断等）"""
        if not self.memory_manager:
            return
        try:
            self.memory_manager.update_after_trade(signal, profit_rate)
            self.memory_manager.log_trade(signal)
        except Exception as e:
            logger.warning(f"风控追踪失败: {e}")

    def _update_reduce_flag(self, symbol: str):
        """更新盈利保护减仓标记（卖出后调用）

        v2.5.0: 使用 PositionEngine 中央化状态管理
        """
        if self.position_engine:
            result = self.position_engine.mark_reduced_from_peak(symbol)
            if not result.success:
                logger.warning(f"更新减仓标记失败: {result.message}")
        else:
            # 回退：直接修改（仅用于兼容旧调用）
            try:
                position = self.executor.storage.get_position(symbol)
                if position and position.shares > 0:
                    position.reduced_from_peak = True
                    position.last_reduce_at = datetime.now().strftime("%Y-%m-%d")
                    self.executor.storage.update_position(position)
                    logger.info(f"盈利保护标记已更新: {symbol} reduced_from_peak=True")
            except Exception as e:
                logger.warning(f"更新减仓标记失败: {e}")
    
    def execute_sell(self, symbol: str, shares: int = None, signal_price: float = None) -> Tuple[bool, str, Optional[Dict]]:
        """
        执行卖出
        
        Args:
            symbol: 股票代码
            shares: 卖出股数（None表示全部卖出）
            signal_price: 信号价格（可选）
        
        Returns:
            (success, message, trade_info)
        """
        # 获取持仓
        position = self.executor.storage.get_position(symbol)
        if position is None or position.shares == 0:
            return False, f"未持有股票 {symbol}", None
        
        # 如果未指定股数，卖出全部
        if shares is None:
            shares = position.shares
        
        # 如果没有提供信号价格，获取实时价格
        if signal_price is None:
            quote = self.data_provider.get_realtime_quote(symbol)
            if quote is None:
                return False, f"无法获取 {symbol} 的行情", None
            signal_price = quote["price"]
        elif signal_price <= 0:
            return False, f"无效的信号价格: {signal_price}", None
        # 传了有效 price，直接使用，不再查询实时行情（避免网络问题阻断交易）
        
        # 执行卖出
        success, msg, trade = self.executor.execute_sell(symbol, shares, signal_price)
        
        if success and trade:
            trade_info = {
                "symbol": trade.symbol,
                "action": trade.action,
                "shares": trade.shares,
                "price": trade.price,
                "amount": trade.amount,
                "commission": trade.commission,
                "tax": trade.tax,
                "total_revenue": trade.total_cost,  # 卖出是收入
                "slippage": trade.slippage,
                "timestamp": trade.timestamp
            }
            # v2.5.0: 发布 ORDER_FILLED 事件（卖出包含盈亏信息）
            # 计算 pnl_pct（如果有持仓信息）
            if position and position.avg_cost > 0:
                trade_info["pnl_pct"] = (trade.price - position.avg_cost) / position.avg_cost * 100
            self._emit_trade_event(trade_info)
            return True, msg, trade_info
        
        return False, msg, None
    
    # ==================== 查询接口 ====================
    
    def get_quote(self, symbol: str) -> Optional[Dict]:
        """获取实时行情"""
        return self.data_provider.get_realtime_quote(symbol)
    
    def update_portfolio(self):
        """更新投资组合市值"""
        positions = self.executor.get_positions()
        if not positions:
            return
        
        # 获取所有持仓的最新价格（过滤掉None）
        # 转换为腾讯格式（sh/sz前缀）
        def to_tencent(sym):
            sym = sym.lower()
            if sym.startswith('sh') or sym.startswith('sz'):
                return sym
            if sym.startswith('6'):
                return f'sh{sym}'
            return f'sz{sym}'
        
        symbols = [to_tencent(p.symbol) for p in positions if p.symbol and p.shares > 0]
        if not symbols:
            return
        quotes = self.data_provider.get_batch_quotes(symbols)
        
        # 提取价格
        prices = {symbol: quote["price"] for symbol, quote in quotes.items()}
        
        # 更新市值
        self.executor.update_market_prices(prices)
        
        logger.info("投资组合市值已更新")
    
    def get_market_state(self) -> str:
        """
        获取当前市场环境状态（基于上证指数）
        
        Returns:
            'strong' | 'neutral' | 'weak'
        """
        from scripts.core.strategy import MomentumTrendStrategy
        
        # 获取上证指数数据
        index_data = self.data_provider.get_index_quote("000001")
        
        if index_data is None:
            logger.warning("无法获取上证指数数据，默认返回震荡市场")
            return 'neutral'
        
        # 使用策略的环境判断逻辑
        strategy = MomentumTrendStrategy({})
        market_state = strategy.detect_market_state({}, index_data)
        
        ma10_str = f"{index_data['ma10']:.2f}" if index_data.get('ma10') is not None else "N/A"
        ma30_str = f"{index_data['ma30']:.2f}" if index_data.get('ma30') is not None else "N/A"
        logger.info(f"市场环境判断: {market_state} (上证指数: {index_data['price']:.2f}, "
                   f"MA10: {ma10_str}, MA30: {ma30_str})")
        
        return market_state
    
    def get_portfolio_summary(self) -> Dict:
        """获取投资组合摘要"""
        # 先更新市值
        self.update_portfolio()
        
        summary = self.executor.get_portfolio_summary()
        positions_with_names = self.get_positions()
        
        return {
            "cash": summary["cash"],
            "positions_value": summary["positions_value"],
            "total_assets": summary["total_assets"],
            "initial_capital": summary["initial_capital"],
            "total_return": summary["total_return"],
            "total_return_pct": summary["total_return_pct"],
            "positions_count": summary["positions_count"],
            "unrealized_pnl": summary["unrealized_pnl"],
            "positions": positions_with_names
        }
    
    def get_trade_history(self, limit: int = 50) -> List[Dict]:
        """获取交易历史"""
        trades = self.executor.storage.get_trades(limit)
        return [
            {
                "id": t.id,
                "symbol": t.symbol,
                "action": t.action,
                "shares": t.shares,
                "price": t.price,
                "amount": t.amount,
                "commission": t.commission,
                "tax": t.tax,
                "total_cost": t.total_cost,
                "slippage": t.slippage,
                "timestamp": t.timestamp
            }
            for t in trades
        ]
    
    # ==================== 风控查询接口 ====================
    
    def can_trade(self, symbol: str, action: str, shares: int) -> Tuple[bool, str]:
        """检查是否可以交易（用于 RiskAgent 风控检查）"""
        if action == "BUY":
            # 获取当前价格
            quote = self.data_provider.get_realtime_quote(symbol)
            if quote is None:
                return False, f"无法获取 {symbol} 的行情"
            return self.executor.can_buy(symbol, shares, quote["price"])
        elif action == "SELL":
            return self.executor.can_sell(symbol, shares)
        else:
            return False, f"未知的交易类型: {action}"
    
    # ==================== 工具方法 ====================
    
    def print_portfolio(self):
        """打印投资组合（美观格式）"""
        summary = self.get_portfolio_summary()
        
        print("\n" + "=" * 60)
        print("📊 模拟盘投资组合")
        print("=" * 60)
        print(f"💰 现金:           ¥{summary['cash']:>12,.2f}")
        print(f"📈 持仓市值:       ¥{summary['positions_value']:>12,.2f}")
        print(f"💵 总资产:         ¥{summary['total_assets']:>12,.2f}")
        print(f"📊 初始资金:       ¥{summary['initial_capital']:>12,.2f}")
        
        return_pct = summary['total_return_pct']
        return_emoji = "🟢" if return_pct >= 0 else "🔴"
        print(f"{return_emoji} 总收益:         ¥{summary['total_return']:>12,.2f} ({return_pct:+.2f}%)")
        
        if summary['positions']:
            print(f"\n📋 持仓明细 ({summary['positions_count']}只):")
            print("-" * 60)
            print(f"{'股票':<12} {'数量':>8} {'成本':>10} {'现价':>10} {'市值':>12} {'盈亏':>10}")
            print("-" * 60)
            for p in summary['positions']:
                pnl_emoji = "🟢" if p['pnl'] >= 0 else "🔴"
                can_sell = "✓" if p['can_sell'] else "✗"
                display_name = p.get('name', p['symbol'])
                print(f"{display_name:<10}{can_sell} {p['shares']:>8} ¥{p['avg_cost']:>9.2f} ¥{p['current_price']:>9.2f} ¥{p['market_value']:>11,.2f} {pnl_emoji}¥{p['pnl']:>8.2f}")
        
        print("=" * 60)

    # ==================== BaseAgent 接口实现 ====================
    
    def process(self, input_data: Dict) -> AgentResult:
        """执行交易（BaseAgent 接口）
        
        Args:
            input_data: 包含以下字段
                - signals: 交易信号列表
                - total_assets: 总资产
                - initial_assets: 初始资产
                
        Returns:
            AgentResult: data 包含
                - executed: 执行的交易数
                - buy_signals: 买入信号列表
                - sell_signals: 卖出信号列表
        """
        try:
            signals = input_data.get("signals", [])
            total_assets = input_data.get("total_assets", 1000000)
            
            if not signals:
                return AgentResult(
                    success=True,
                    data={
                        "executed": 0,
                        "buy_signals": [],
                        "sell_signals": [],
                        "summary": {"executed": 0, "buy": 0, "sell": 0}
                    }
                )
            
            # 使用原有的执行逻辑
            from scripts.agents.execution import ExecutionAgent
            buy_signals = [s for s in signals if s.get("signal") == "buy"]
            sell_signals = [s for s in signals if s.get("signal") == "sell"]
            
            executed = 0
            for signal in signals:
                symbol = signal.get("symbol")
                action = signal.get("signal")
                
                if action == "buy":
                    # 从信号获取价格和仓位（策略已计算好）
                    price = signal.get("price", 0)
                    action_shares = signal.get("action_shares", 0)
                    stop_loss = signal.get("conditions", {}).get("stop_loss", 0)

                    if price <= 0 or action_shares < 100:
                        logger.warning(f"买入信号无效: {symbol} price={price}, shares={action_shares}")
                        continue

                    # === v2.4.0: 使用 RiskEngine 中央化 ===
                    if self.risk_engine is not None:
                        from scripts.core.risk_engine import Stage

                        result = self.risk_engine.validate(
                            symbol=symbol,
                            action="buy",
                            shares=action_shares,
                            price=price,
                            stage=Stage.PRE_EXECUTION.value
                        )

                        if not result.allowed:
                            logger.warning(f"风控拒绝: {symbol} - {result.reason}")
                            continue

                        if result.adjusted_shares:
                            logger.info(f"仓位截断: {symbol} {action_shares}股 → {result.adjusted_shares}股")
                            action_shares = result.adjusted_shares

                    else:
                        # === 降级模式：原有逻辑 ===
                        # 检查持仓数量上限
                        max_positions = self.config.get("max_positions", 5)
                        account = self.executor.get_account_status()
                        current_positions = self.executor.get_positions()
                        current_position = self.executor.storage.get_position(symbol)
                        if current_position is None and len(current_positions) >= max_positions:
                            logger.warning(f"持仓数量已达上限: {len(current_positions)}/{max_positions}，跳过 {symbol}")
                            continue

                        # 检查单只股票仓位上限（20%）
                        max_position_pct = 0.20  # 20%仓位上限

                        # 计算买入后的总仓位
                        current_shares = current_position.shares if current_position else 0
                        current_value = current_shares * price
                        new_value = action_shares * price
                        total_value_after_buy = current_value + new_value
                        position_pct_after_buy = total_value_after_buy / account.total_assets

                        if position_pct_after_buy > max_position_pct:
                            # 超过20%上限，截断到20%
                            max_value = account.total_assets * max_position_pct
                            allowed_new_value = max_value - current_value
                            allowed_shares = int(allowed_new_value / price / 100) * 100

                            if allowed_shares >= 100:
                                logger.warning(f"{symbol} 仓位限制: 原计划{action_shares}股，截断到{allowed_shares}股（超过20%上限）")
                                action_shares = allowed_shares
                            else:
                                logger.info(f"跳过买入: {symbol} 已持仓{current_shares}股，再买会超过20%上限")
                                continue

                    # 执行买入
                    success, msg, result = self.execute_buy(symbol, action_shares, price, stop_loss=stop_loss)
                    if success:
                        executed += 1
                        logger.info(f"买入成功: {symbol} {action_shares}股 @ ¥{price:.2f}, 止损: ¥{stop_loss:.2f}")
                        # 追踪风控指标（买入不计盈亏，profit_rate=0）
                        self._track_trade(signal, profit_rate=0)
                                
                elif action == "sell":
                    # 获取持仓数量
                    position = self.get_position(symbol)
                    if position and position.get("shares", 0) > 0:
                        # 使用 signal 中的 action_shares（部分卖出）
                        shares = signal.get("action_shares", position["shares"])
                        success, msg, result = self.execute_sell(symbol, shares)
                        if success:
                            executed += 1
                            # 计算盈亏率用于风控追踪
                            avg_cost = position.get("avg_cost", 0)
                            sell_price = result.get("price", signal.get("price", 0)) if result else signal.get("price", 0)
                            profit_rate = (sell_price - avg_cost) / avg_cost if avg_cost > 0 else 0
                            self._track_trade(signal, profit_rate=profit_rate)

                            # 盈利保护：更新减仓标记
                            reason = signal.get("reason", "")
                            if reason == "profit_protection":
                                self._update_reduce_flag(symbol)
            
            return AgentResult(
                success=True,
                data={
                    "executed": executed,
                    "buy_signals": buy_signals,
                    "sell_signals": sell_signals,
                    "summary": {
                        "executed": executed,
                        "buy": len(buy_signals),
                        "sell": len(sell_signals)
                    }
                }
            )
            
        except Exception as e:
            logger.error(f"执行交易失败: {e}", exc_info=True)
            return AgentResult(
                success=False,
                data={},
                error=f"执行交易失败: {str(e)}"
            )
    
    def handle_message(self, message):
        """处理消息（消息总线用）"""
        # PaperTradingExecutionAgent 不参与消息总线
        pass


# 测试代码
if __name__ == "__main__":
    import json
    
    # 配置
    config = {
        "initial_capital": 100000,
        "slippage": 0.001,
        "max_positions": 5,  # 最大持仓数量
        "max_position_pct": 15  # 单只最大仓位比例
    }
    
    # 创建 Agent
    agent = PaperTradingExecutionAgent(config)
    
    # 打印初始状态
    agent.print_portfolio()
    
    # 测试买入（使用模拟价格，实际运行时需要 AkShare）
    print("\n" + "=" * 60)
    print("📝 测试买入")
    print("=" * 60)
    
    # 假设买入 000001 1000股 @ 10.5
    success, msg, trade = agent.execute_buy("000001.SZ", 1000, 10.5)
    print(f"结果: {msg}")
    if trade:
        print(f"成交价: ¥{trade['price']:.2f}, 手续费: ¥{trade['commission']:.2f}")
    
    # 打印持仓
    agent.print_portfolio()
    
    # 打印交易历史
    print("\n📜 最近交易:")
    trades = agent.get_trade_history(10)
    for t in trades:
        print(f"  {t['timestamp']}: {t['action']} {t['symbol']} {t['shares']}股 @ ¥{t['price']:.2f}")
