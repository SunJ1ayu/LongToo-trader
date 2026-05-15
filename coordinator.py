"""Coordinator - 交易编排器

替代原 ModularTrader，协调各 Agent 执行流程。
职责：只负责 Agent 协调，不涉及账户获取和结果处理
"""

import logging
from typing import Dict, List, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

from .agents.base import BaseAgent
from .agents.analyst import AnalystAgent
from .agents.risk import RiskAgent
from .agents.execution import ExecutionAgent
from .agents.position_monitor import PositionMonitorAgent
from .portfolio_manager import PortfolioManager
from .result_processor import ResultProcessor


class Coordinator:
    """交易流程编排器

    核心原则：买入逻辑和卖出逻辑完全独立

    执行顺序：
    0. 数据同步（可选）
    1. 持仓管理（独立卖出层）
       - manage_existing_positions(): 风控卖出、趋势破坏、持仓超时
       - normalize_position_count(): 结构收缩（强制减仓）
    2. 分析阶段（寻找买入机会）
    3. 实时行情过滤
    4. 风控阶段
    5. 执行阶段（买入新机会）
    6. 持仓监控（盘中预警）

    与旧架构的区别：
    - 旧: if buy_signal then maybe_sell()（买入驱动卖出）
    - 新: 先卖出管理，再买入分析（独立卖出层）
    """

    def __init__(
        self,
        analyst_agent: AnalystAgent,
        risk_agent: RiskAgent,
        execution_agent: ExecutionAgent,
        position_monitor_agent: Optional[PositionMonitorAgent] = None,
        memory_path: str = "~/.openclaw/workspace"
    ):
        """
        Args:
            analyst_agent: 分析 Agent
            risk_agent: 风控 Agent
            execution_agent: 执行 Agent
            position_monitor_agent: 持仓监控 Agent（可选）
            memory_path: 内存存储路径
        """
        self.analyst = analyst_agent
        self.risk = risk_agent
        self.execution = execution_agent
        self.position_monitor = position_monitor_agent
        self.memory_path = memory_path

        # 持仓管理 Agent（新增）
        from .agents.position_manager import PositionManagerAgent
        self.position_manager = PositionManagerAgent(execution_agent)

        # 委托职责
        self.portfolio_manager = PortfolioManager(execution_agent)
        self.result_processor = ResultProcessor()

        # 执行结果
        self.results = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "steps": {}
        }
    
    def _is_rebalance_day(self) -> bool:
        """判断今天是否是调仓日（每月第一个交易日）

        通过检查 DB 中本月是否已有**买入**交易来判断。
        卖出不影响调仓日判断 — 防止"调仓日只卖出不买入"导致次日不触发补仓。
        """
        import sqlite3
        from pathlib import Path

        db_dir = Path(self.memory_path) / "skills" / "LongToo-trader" / "data"
        paper_db = db_dir / "paper_trading.db"
        if not paper_db.exists():
            return True  # 首次运行，视为调仓日

        conn = sqlite3.connect(str(paper_db))
        try:
            today = datetime.now()
            month_start = today.strftime("%Y-%m-01")
            row = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE timestamp >= ? AND action = 'BUY'",
                (month_start,)
            ).fetchone()
            # 本月无买入记录 = 需要调仓
            return row[0] == 0
        except Exception as e:
            logger.warning(f"调仓日判断失败: {e}，默认视为调仓日")
            return True
        finally:
            conn.close()

    def run(self, sync_data: bool = False) -> Dict:
        """执行完整交易流程（ROE 月频调仓版）

        流程：
        0. 数据同步（可选，sync_data=True）
        1. 持仓管理（仅极端止损，非调仓日跳过趋势类止损）
        2. 判断是否调仓日
           - 调仓日：分析 + 替换 + 风控 + 执行（完整流程）
           - 非调仓日：仅监控 + 极端止损
        3. 持仓监控

        Args:
            sync_data: 是否在分析前同步全市场数据

        Returns:
            Dict: 完整执行结果
        """
        print("🚀 启动 Coordinator 交易流程（ROE 月频调仓）")

        # 检查各 Agent 健康状态
        self._health_check()

        # Step 0: 数据同步（可选）
        if sync_data:
            self._run_data_sync()

        # 判断是否调仓日，或持仓不足需要补仓
        rebalance = self._is_rebalance_day()
        positions = self.portfolio_manager.get_positions()
        active_count = len([p for p in positions if p.get("shares", 0) > 0])
        TARGET_POSITIONS = 5

        if not rebalance and active_count < TARGET_POSITIONS:
            print(f"\n📅 非调仓日，但持仓 {active_count} 只 < 目标 {TARGET_POSITIONS} 只 — 补仓模式")
            rebalance = True

        if rebalance:
            print("\n📅 调仓日 — 执行完整流程")

            # 调仓日临时提高交易上限（卖出 + 买入可能共 10 笔）
            original_max_trades = None
            if hasattr(self.execution, 'risk_engine') and self.execution.risk_engine:
                original_max_trades = self.execution.risk_engine.config.max_daily_trades
                self.execution.risk_engine.config.max_daily_trades = 10
                print(f"   ⚡ 调仓日交易上限临时提高: {original_max_trades} → 10")

            # Step 1: 持仓管理（独立卖出层）
            self._run_position_management()
            if not self._is_step_success("position_management"):
                return self._finalize(False)

            # Step 1.5: 加仓赢家 — 已禁用（ROE 月频策略不适用加仓逻辑）
            # self._run_add_to_winners_phase()

            # Step 2: 分析阶段（ROE 排名选股）
            self._run_analysis_phase()
            if not self._is_step_success("analysis"):
                return self._finalize(False)

            # Step 2.5: 替换弱持仓 — 跳过
            # ROE 策略月频调仓，替换逻辑的评分量纲（ROE归一化 vs exit_score）不可比，
            # 且调仓日已买入 Top5，无需月内替换。
            # self._run_replacement_phase()

            # Step 3: 实时行情过滤
            self._run_realtime_filter_phase()

            # Step 4: 风控阶段
            self._run_risk_phase()
            if not self._is_step_success("risk"):
                return self._finalize(False)

            # Step 5: 执行阶段（买入新机会）
            executed = self._run_execution_phase()

            # 恢复日常交易上限
            if original_max_trades is not None and self.execution.risk_engine:
                self.execution.risk_engine.config.max_daily_trades = original_max_trades
        else:
            print("\n📅 非调仓日 — 仅监控 + 极端止损")
            executed = 0

            # 仅检查极端止损（不检查趋势破坏、不替换、不加仓）
            self._run_extreme_stop_check()

        # Step 6: 持仓监控（每天运行）
        self._run_monitoring_phase()

        # 完成
        self.results["has_trades"] = executed > 0
        return self._finalize(True)
    
    def _run_extreme_stop_check(self):
        """非调仓日的极端止损检查

        只检查大幅亏损（-15%），不检查趋势破坏、不替换、不加仓。
        ROE 策略持有期长，日常波动不需要干预。
        """
        print("\n🛑 极端止损检查")

        positions = self.portfolio_manager.get_positions()
        active_positions = [p for p in positions if p.get("shares", 0) > 0]

        if not active_positions:
            print("   无持仓，跳过")
            return

        EXTREME_STOP_LOSS = -15.0  # 极端止损线

        sell_signals = []
        for pos in active_positions:
            pnl_pct = pos.get("pnl_pct", 0)
            if pnl_pct <= EXTREME_STOP_LOSS:
                sell_signals.append({
                    "symbol": pos["symbol"],
                    "signal": "sell",
                    "price": pos.get("current_price", 0),
                    "action_shares": pos.get("shares", 0),
                    "avg_cost": pos.get("avg_cost", 0),
                    "pnl_pct": pnl_pct,
                    "reason": "extreme_stop_loss",
                    "message": f"🛑 极端止损: {pos['symbol']} (亏损{pnl_pct:.1f}% <= {EXTREME_STOP_LOSS}%)"
                })

        if sell_signals:
            print(f"   触发 {len(sell_signals)} 笔极端止损")
            for sig in sell_signals:
                print(f"      {sig['message']}")
            self._execute_management_sells(sell_signals)
        else:
            print("   ✅ 无极端止损触发")

        # 更新持仓价格（每天需要）
        self._update_position_prices()

    def _run_data_sync(self):
        """Step 0: 同步全市场数据并更新持仓价格"""
        print("\n📥 Step 0: 数据同步")

        try:
            from scripts.data.market_sync import sync_incremental, get_db_stats, should_sync_today, mark_sync_done

            # 检查今天是否已同步
            if not should_sync_today():
                print("   ⏭️ 今天已同步过，跳过数据同步")
                # 即使跳过同步，也要更新持仓价格（防止价格过期）
                self._update_position_prices()
                return

            # 检查DB状态
            stats = get_db_stats()
            if not stats.get("exists"):
                print("   ⚠️ 数据库不存在，需要先全量同步 (--sync)")
                return

            # 增量更新
            result = sync_incremental(days=1, max_workers=8)

            if result.get("success"):
                mark_sync_done()
                print(f"   ✅ 增量同步完成: 更新 {result['stocks_updated']} 只")

                # 同步完成后更新持仓价格
                self._update_position_prices()
            else:
                print(f"   ⚠️ 同步失败: {result.get('error', '未知错误')}")

        except Exception as e:
            print(f"   ⚠️ 数据同步失败: {e}")

    def _update_position_prices(self):
        """从市场数据更新持仓价格 + peak_pnl 追踪

        v2.5.0: 使用 PositionEngine 中央化 peak_pnl 更新
        """
        print("   💹 更新持仓价格...")

        try:
            import sqlite3
            from pathlib import Path
            from .core.position_engine import PositionEngine

            # 数据库路径
            db_dir = Path(self.memory_path) / "skills" / "LongToo-trader" / "data"
            market_db = db_dir / "market_kline.db"
            paper_db = db_dir / "paper_trading.db"

            if not market_db.exists() or not paper_db.exists():
                print("   ⚠️ 数据库不存在，跳过价格更新")
                return

            market_conn = sqlite3.connect(str(market_db))
            paper_conn = sqlite3.connect(str(paper_db))

            # 获取持仓（包含 peak_pnl 和 peak_price）
            positions = paper_conn.execute(
                '''SELECT symbol, shares, avg_cost, peak_pnl, peak_price
                   FROM positions WHERE shares > 0'''
            ).fetchall()

            if not positions:
                print("   无持仓，跳过")
                market_conn.close()
                paper_conn.close()
                return

            # v2.5.0: 获取 PositionEngine
            position_engine = None
            if hasattr(self.execution, 'executor') and hasattr(self.execution.executor, 'storage'):
                position_engine = PositionEngine(self.execution.executor.storage)

            updated_count = 0
            total_positions_value = 0
            peak_updates = []
            peak_update_symbols = []  # 需要通过 PositionEngine 更新的 symbol

            for symbol, shares, avg_cost, old_peak_pnl, old_peak_price in positions:
                # 标准化symbol（去掉sh/sz前缀）
                pure = symbol.replace('sh', '').replace('sz', '')

                # 从K线获取最新收盘价
                row = market_conn.execute(
                    'SELECT close FROM daily_kline WHERE symbol=? ORDER BY trade_date DESC LIMIT 1',
                    (pure,)
                ).fetchone()

                if row:
                    new_price = row[0]
                    market_value = shares * new_price
                    pnl = market_value - (shares * avg_cost)
                    pnl_pct = (pnl / (shares * avg_cost) * 100) if avg_cost > 0 else 0

                    # === peak_pnl 追踪逻辑 ===
                    # 首次或数据不完整时初始化（写回DB）
                    if old_peak_pnl is None or old_peak_price is None or old_peak_price == 0:
                        old_peak_pnl = max(0, pnl_pct)
                        old_peak_price = new_price
                        peak_update_symbols.append((symbol, old_peak_pnl, old_peak_price))
                        peak_updates.append(f"{symbol}: peak_pnl 初始化 {old_peak_pnl:+.2f}%")

                    # 更新 peak_pnl：只有创新高才更新
                    if pnl_pct > old_peak_pnl:
                        new_peak_pnl = pnl_pct
                        new_peak_price = new_price
                        peak_updates.append(f"{symbol}: peak_pnl {old_peak_pnl:+.2f}% → {new_peak_pnl:+.2f}%")
                        peak_update_symbols.append((symbol, new_peak_pnl, new_peak_price))
                    else:
                        new_peak_pnl = old_peak_pnl
                        new_peak_price = old_peak_price

                    # 计算 drawdown（用于风控）
                    drawdown = new_peak_pnl - pnl_pct

                    # v2.5.0: 价格字段直接SQL更新（效率），peak_pnl 通过 PositionEngine
                    paper_conn.execute(
                        '''UPDATE positions SET
                            current_price=?,
                            market_value=?,
                            pnl=?,
                            pnl_pct=?,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE symbol=?''',
                        (new_price, market_value, pnl, pnl_pct, symbol)
                    )
                    updated_count += 1
                    total_positions_value += market_value

            # 更新账户总资产
            cash_row = paper_conn.execute('SELECT cash FROM account WHERE id=1').fetchone()
            if cash_row:
                total_cash = cash_row[0]
                new_total = total_cash + total_positions_value
                paper_conn.execute(
                    'UPDATE account SET total_assets=?, updated_at=CURRENT_TIMESTAMP WHERE id=1',
                    (new_total,)
                )

            paper_conn.commit()
            market_conn.close()
            paper_conn.close()

            # v2.5.0: 通过 PositionEngine 更新 peak_pnl（中央化）
            if position_engine and peak_update_symbols:
                for symbol, new_peak_pnl, new_peak_price in peak_update_symbols:
                    position_engine.update_peak_pnl(symbol, new_peak_pnl, new_peak_price)

            print(f"   ✅ 价格已更新: {updated_count} 只持仓")
            if peak_updates:
                print(f"   📈 peak_pnl 新高: {len(peak_updates)} 只")
                for msg in peak_updates[:3]:  # 最多显示3条
                    print(f"      {msg}")

        except Exception as e:
            print(f"   ⚠️ 价格更新失败: {e}")
    
    def _health_check(self) -> bool:
        """检查所有 Agent 健康状态"""
        agents = [
            ("Analyst", self.analyst),
            ("Risk", self.risk),
            ("Execution", self.execution)
        ]
        
        all_healthy = True
        for name, agent in agents:
            healthy = agent.health_check()
            status = "✅" if healthy else "❌"
            print(f"   {status} {name}Agent")
            if not healthy:
                all_healthy = False
        
        return all_healthy

    def _run_position_management(self):
        """持仓管理阶段（独立卖出层）

        核心原则：卖出逻辑和买入逻辑完全独立

        执行顺序：
        1. 独立风控卖出（止损、趋势破坏、持仓超时）
        2. 结构收缩（强制减仓到 MAX_POSITIONS）
        """
        print("\n🧹 Step 1: 持仓管理（独立卖出层）")

        # 获取当前持仓
        positions = self.portfolio_manager.get_positions()
        active_positions = [p for p in positions if p.get("shares", 0) > 0]
        current_count = len(active_positions)

        print(f"   当前持仓: {current_count} 只")

        if not active_positions:
            print("   ✅ 无持仓，跳过持仓管理")
            self.results["steps"]["position_management"] = {
                "success": True,
                "data": {"sell_signals": [], "managed": 0}
            }
            return

        # 1. 独立风控卖出（止损、趋势破坏、持仓超时）
        print("   📋 执行独立风控卖出...")
        pm_input = {"positions": active_positions}
        pm_result = self.position_manager.process(pm_input)
        risk_sells = pm_result.data.get("sell_signals", [])
        breakdown = pm_result.data.get("breakdown", {})

        if risk_sells:
            print(f"      风控卖出: {len(risk_sells)} 只")
            print(f"         - 止损: {breakdown.get('risk', 0)} 只")
            print(f"         - 趋势破坏: {breakdown.get('trend', 0)} 只")
            print(f"         - 持仓超时: {breakdown.get('timeout', 0)} 只")

        # 2. 结构收缩（强制减仓）
        print("   📐 执行结构收缩...")
        overflow_sells = self.position_manager.normalize_positions(
            active_positions,
            target_count=self.position_manager.config.get("max_positions", 5)
        )

        if overflow_sells:
            print(f"      结构收缩: {len(overflow_sells)} 只")

        # 合并所有卖出信号
        all_sells = self.position_manager.merge_sell_signals(risk_sells, overflow_sells)

        # 执行卖出
        if all_sells:
            self._execute_management_sells(all_sells)
        else:
            print("   ✅ 无需卖出")

        # 记录结果
        self.results["steps"]["position_management"] = {
            "success": True,
            "data": {
                "sell_signals": all_sells,
                "managed": current_count,
                "breakdown": breakdown
            }
        }

    def _execute_management_sells(self, sell_signals: List[Dict]):
        """执行持仓管理的卖出信号"""
        print(f"\n   💰 执行卖出: {len(sell_signals)} 笔")

        # 获取账户信息
        portfolio = self.portfolio_manager.get_portfolio()

        execution_input = {
            "signals": sell_signals,
            "total_assets": portfolio.get("total_value", 1000000),
            "initial_assets": portfolio.get("initial_capital", 1000000)
        }

        execution_result = self.execution.process(execution_input)

        if execution_result.success:
            executed = len([t for t in execution_result.data.get("executed_trades", []) if t.get("success")])
            print(f"   ✅ 卖出完成: {executed} 笔成功")
        else:
            print(f"   ❌ 卖出失败: {execution_result.error}")

        # 记录到结果
        self.results["steps"]["management_execution"] = execution_result

    def _run_add_to_winners_phase(self):
        """Step 1.5: 加仓赢家

        核心原则：先试错，再扩张
        - 只对盈利超过阈值的持仓加仓
        - 每只股票最多加仓1次
        - 加仓后仓位不超过20%
        """
        print("\n📈 Step 1.5: 加仓赢家")

        # 获取当前持仓
        positions = self.portfolio_manager.get_positions()
        active_positions = [p for p in positions if p.get("shares", 0) > 0]

        if not active_positions:
            print("   ✅ 无持仓，跳过加仓")
            self.results["steps"]["add_to_winners"] = {
                "success": True,
                "data": {"add_signals": [], "added": 0}
            }
            return

        # 获取账户信息用于计算仓位占比
        portfolio = self.portfolio_manager.get_portfolio()
        total_assets = portfolio.get("total_value", 1000000)

        # 计算各持仓的仓位占比
        for pos in active_positions:
            market_value = pos.get("market_value", 0)
            pos["position_pct"] = (market_value / total_assets * 100) if total_assets > 0 else 0

        # 检测加仓候选
        add_signals = self.position_manager.check_add_to_winners(active_positions)

        if not add_signals:
            print("   ✅ 无符合条件的加仓候选")
            self.results["steps"]["add_to_winners"] = {
                "success": True,
                "data": {"add_signals": [], "added": 0}
            }
            return

        print(f"   📊 发现 {len(add_signals)} 只加仓候选")
        for sig in add_signals:
            print(f"      {sig['symbol']}: 盈利{sig['pnl_pct']:+.1f}%，加仓{sig['add_position_pct']:.1f}%")

        # 执行加仓
        self._execute_add_to_winners(add_signals, total_assets)

    def _execute_add_to_winners(self, add_signals: List[Dict], total_assets: float):
        """执行加仓操作"""
        print(f"\n   💰 执行加仓: {len(add_signals)} 笔")

        executed_count = 0
        for sig in add_signals:
            symbol = sig["symbol"]
            add_position_pct = sig["add_position_pct"]
            current_price = sig["current_price"]

            # 计算加仓股数
            target_value = total_assets * (add_position_pct / 100)
            add_shares = int(target_value / current_price / 100) * 100  # 向下取整到100股倍数

            if add_shares < 100:
                print(f"      ⚠️ {symbol}: 加仓金额不足100股，跳过")
                continue

            # 构建买入信号
            buy_signal = {
                "symbol": symbol,
                "signal": "buy",
                "action_shares": add_shares,
                "price": current_price,
                "reason": "add_to_winner",
                "add_count_before": sig["add_count"]
            }

            # 执行买入
            execution_input = {
                "signals": [buy_signal],
                "total_assets": total_assets,
                "initial_assets": total_assets
            }

            execution_result = self.execution.process(execution_input)

            if execution_result.success:
                trades = execution_result.data.get("executed_trades", [])
                if trades and trades[0].get("success"):
                    executed_count += 1
                    # 更新 add_count 标记
                    self._update_add_count(symbol, sig["add_count"] + 1)
                    print(f"      ✅ {symbol}: 加仓 {add_shares}股 @ ¥{current_price:.2f}")
                else:
                    print(f"      ❌ {symbol}: 加仓失败")
            else:
                print(f"      ❌ {symbol}: 加仓执行失败")

        self.results["steps"]["add_to_winners"] = {
            "success": True,
            "data": {
                "add_signals": add_signals,
                "added": executed_count
            }
        }
        print(f"   ✅ 加仓完成: {executed_count} 笔")

    def _update_add_count(self, symbol: str, new_count: int):
        """更新加仓次数标记

        v2.5.0: 使用 PositionEngine 中央化状态管理
        """
        try:
            from .core.position_engine import PositionEngine

            # v2.5.0: 优先使用 PositionEngine
            if hasattr(self.execution, 'executor') and hasattr(self.execution.executor, 'storage'):
                position_engine = PositionEngine(self.execution.executor.storage)
                # increment_add_count 会自动加1，所以我们需要先减1
                # 或者直接用 storage 更新到指定值
                position = self.execution.executor.storage.get_position(symbol)
                if position:
                    position.add_count = new_count
                    self.execution.executor.storage.update_position(position)
                    print(f"      ✅ add_count 更新: {symbol} → {new_count}")
            else:
                # 回退：直接SQL（仅用于非模拟盘或特殊场景）
                import sqlite3
                from pathlib import Path
                db_dir = Path(self.memory_path) / "skills" / "LongToo-trader" / "data"
                paper_db = db_dir / "paper_trading.db"
                if paper_db.exists():
                    conn = sqlite3.connect(str(paper_db))
                    conn.execute("UPDATE positions SET add_count=? WHERE symbol=?", (new_count, symbol))
                    conn.commit()
                    conn.close()
                    print(f"      ✅ add_count 更新(SQL): {symbol} → {new_count}")
        except Exception as e:
            print(f"      ⚠️ 更新 add_count 失败: {e}")

    def _run_replacement_phase(self):
        """Step 2.5: 替换弱持仓

        当候选股评分明显优于最差持仓评分（差值 > 阈值）时，
        将替换卖信号注入分析结果，统一经过 Step 4 风控 + Step 5 执行。

        不直接执行卖出，保证所有卖出操作都经过风控检查。
        """
        print("\n🔄 Step 2.5: 替换评估")

        # 获取分析结果中的候选股
        analysis_result = self.results["steps"].get("analysis")
        if not analysis_result or not analysis_result.success:
            print("   无有效分析结果，跳过替换")
            self.results["steps"]["replacement"] = {"success": True, "data": {"sell_signals": [], "replaced": 0}}
            return

        signals = analysis_result.data.get("signals", [])
        candidates = [s for s in signals if s.get("signal") == "buy" and s.get("score", 0) > 0]

        if not candidates:
            print("   无买入候选，跳过替换")
            self.results["steps"]["replacement"] = {"success": True, "data": {"sell_signals": [], "replaced": 0}}
            return

        # 获取当前持仓
        positions = self.portfolio_manager.get_positions()
        active_positions = [p for p in positions if p.get("shares", 0) > 0]

        if not active_positions:
            print("   无持仓，跳过替换")
            self.results["steps"]["replacement"] = {"success": True, "data": {"sell_signals": [], "replaced": 0}}
            return

        # 获取冷却记录
        replaced_today = self._get_replaced_today()
        replacement_cooldown = self._get_replacement_cooldown()

        # 评估替换
        replacement_sells = self.position_manager.evaluate_replacements(
            candidates, active_positions,
            replaced_today=replaced_today,
            replacement_cooldown=replacement_cooldown
        )

        if replacement_sells:
            print(f"   🔀 触发 {len(replacement_sells)} 笔替换")
            for sig in replacement_sells:
                print(f"      {sig['message']}")

            # 注入替换卖信号到分析结果，统一经过风控 + 执行
            signals.extend(replacement_sells)
            analysis_result.data["signals"] = signals

            # 记录替换（用于冷却）
            for sig in replacement_sells:
                self._record_replacement(sig["symbol"])
        else:
            print("   ✅ 无需替换")

        self.results["steps"]["replacement"] = {
            "success": True,
            "data": {"sell_signals": replacement_sells, "replaced": len(replacement_sells)}
        }

    def _get_replaced_today(self) -> List[str]:
        """获取今日已替换的 symbol 列表"""
        try:
            from pathlib import Path
            import json
            state_file = Path(self.memory_path) / "skills" / "LongToo-trader" / "data" / "replacement_state.json"
            if state_file.exists():
                state = json.loads(state_file.read_text())
                today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
                if state.get("last_date") == today:
                    return state.get("replaced_today", [])
        except Exception:
            pass
        return []

    def _get_replacement_cooldown(self) -> Dict[str, str]:
        """获取替换冷却记录 {symbol: 替换日期}"""
        try:
            from pathlib import Path
            import json
            state_file = Path(self.memory_path) / "skills" / "LongToo-trader" / "data" / "replacement_state.json"
            if state_file.exists():
                state = json.loads(state_file.read_text())
                return state.get("cooldown", {})
        except Exception:
            pass
        return {}

    def _record_replacement(self, symbol: str):
        """记录替换操作（用于冷却和频率限制）"""
        try:
            from pathlib import Path
            import json
            from datetime import datetime

            state_file = Path(self.memory_path) / "skills" / "LongToo-trader" / "data" / "replacement_state.json"
            today = datetime.now().strftime("%Y-%m-%d")

            if state_file.exists():
                state = json.loads(state_file.read_text())
            else:
                state = {}

            # 更新今日替换记录
            if state.get("last_date") == today:
                state.setdefault("replaced_today", []).append(symbol)
            else:
                state["last_date"] = today
                state["replaced_today"] = [symbol]

            # 更新冷却记录
            cooldown = state.get("cooldown", {})
            # 清理过期冷却（超过 7 天的）
            cutoff = datetime.now().timestamp() - 7 * 86400
            cooldown = {s: d for s, d in cooldown.items()
                       if datetime.strptime(d[:10], "%Y-%m-%d").timestamp() > cutoff}
            cooldown[symbol] = today
            state["cooldown"] = cooldown

            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.warning(f"记录替换状态失败: {e}")

    def _run_analysis_phase(self):
        """执行分析阶段"""
        print("\n📊 Step 2: 分析阶段（寻找买入机会）")

        analysis_input = {}
        analysis_result = self.analyst.process(analysis_input)
        self.results["steps"]["analysis"] = analysis_result

        if analysis_result.success:
            signals = analysis_result.data.get("signals", [])
            print(f"   生成 {len(signals)} 个信号")
        else:
            print(f"   ❌ 分析阶段失败: {analysis_result.error}")

    def _run_realtime_filter_phase(self):
        """实时行情过滤阶段（两阶段选股 - 风控层）

        核心原则：日线指标只用完成K线计算
        - 不重新计算 MACD/MA/RSI 等
        - 只用实时行情过滤高开/低开异常的股票
        """
        print("\n💹 Step 3: 实时行情过滤")

        # 获取分析阶段的买入信号
        analysis_result = self.results["steps"].get("analysis")
        if not analysis_result or not analysis_result.success:
            print("   无有效分析结果，跳过实时过滤")
            return

        signals = analysis_result.data.get("signals", [])
        buy_signals = [s for s in signals if s.get("signal") == "buy"]

        if not buy_signals:
            print("   无买入信号，跳过实时过滤")
            return

        # 执行实时过滤
        from scripts.data.realtime_filter import realtime_filter

        filtered_signals = realtime_filter(buy_signals)

        # 更新分析结果
        sell_signals = [s for s in signals if s.get("signal") == "sell"]
        all_signals = filtered_signals + sell_signals
        analysis_result.data["signals"] = all_signals

        print(f"   过滤后剩余 {len(filtered_signals)} 个买入信号")

    def _run_risk_phase(self):
        """执行风控阶段"""
        print("\n🛡️ Step 4: 风控阶段")
        
        # 获取账户信息
        portfolio = self.portfolio_manager.get_portfolio()
        
        # 获取分析阶段的信号
        analysis_result = self.results["steps"].get("analysis")
        if not analysis_result or not analysis_result.success:
            print("   ❌ 无有效分析结果，跳过风控")
            return
        
        signals = analysis_result.data.get("signals", [])
        
        risk_input = {
            "signals": signals,
            "total_assets": portfolio.get("total_value", 1000000),
            "initial_assets": portfolio.get("initial_capital", 1000000)
        }
        
        risk_result = self.risk.process(risk_input)
        self.results["steps"]["risk"] = risk_result
        
        if risk_result.success:
            passed = len(risk_result.data.get("passed_signals", []))
            blocked = len(risk_result.data.get("blocked_signals", []))
            print(f"   通过: {passed}, 拦截: {blocked}")
        else:
            print(f"   ❌ 风控阶段失败: {risk_result.error}")
    
    def _run_execution_phase(self) -> int:
        """执行交易阶段（只处理买入）

        注意：卖出已在 Step 1 持仓管理阶段完成
        这里只处理买入新机会

        Returns:
            int: 执行的交易数量
        """
        print("\n⚡ Step 5: 执行阶段（买入新机会）")

        # 获取风控阶段通过的信号
        risk_result = self.results["steps"].get("risk")
        if not risk_result or not risk_result.success:
            print("   无有效风控结果，跳过执行")
            return 0

        passed_signals = risk_result.data.get("passed_signals", [])

        # 只处理买入信号（卖出已在 Step 1 完成）
        buy_signals = [s for s in passed_signals if s.get("signal") == "buy"]

        if not buy_signals:
            print("   无买入信号，跳过执行")
            return 0

        # 按评分排名，只取 TOP N（ROE 策略月频调仓，调仓日需要买满 Top5）
        MAX_BUY_PER_DAY = 5
        buy_signals.sort(key=lambda x: x.get("score", 0), reverse=True)
        top_n = buy_signals[:MAX_BUY_PER_DAY]
        skipped = len(buy_signals) - len(top_n)

        if skipped > 0:
            print(f"   📊 买入信号 {len(buy_signals)} 个，取 TOP {MAX_BUY_PER_DAY}，跳过 {skipped} 个")
            for i, s in enumerate(buy_signals[MAX_BUY_PER_DAY:], 1):
                print(f"      跳过 #{i+MAX_BUY_PER_DAY}: {s.get('name','')} ({s.get('symbol','')}) 评分{s.get('score',0)}")

        # 获取账户信息
        portfolio = self.portfolio_manager.get_portfolio()

        execution_input = {
            "signals": top_n,
            "total_assets": portfolio.get("total_value", 1000000),
            "initial_assets": portfolio.get("initial_capital", 1000000)
        }

        execution_result = self.execution.process(execution_input)
        self.results["steps"]["execution"] = execution_result

        if execution_result.success:
            executed = execution_result.data.get("summary", {}).get("executed", 0)
            print(f"   ✅ 买入完成: {executed} 笔")
            return executed
        else:
            print(f"   ❌ 执行阶段失败: {execution_result.error}")
            return 0

    def _run_monitoring_phase(self):
        """执行持仓监控阶段

        检测止损/止盈触发后，自动生成卖出信号并执行。
        """
        if not self.position_monitor:
            return

        print("\n👁️ Step 6: 持仓监控")
        
        # 获取持仓
        positions = self.portfolio_manager.get_positions()
        
        if not positions:
            print("   无持仓，跳过监控")
            return
        
        monitor_input = {"positions": positions}
        monitor_result = self.position_monitor.process(monitor_input)
        self.results["steps"]["position_monitor"] = monitor_result
        
        if monitor_result.success:
            alerts = monitor_result.data.get("alerts", [])
            if alerts:
                print(f"   ⚠️ 发现 {len(alerts)} 条预警")
                # 将预警转为卖出信号，交给执行Agent执行
                sell_signals = self._alerts_to_sell_signals(alerts, positions)
                if sell_signals:
                    self._execute_monitor_sells(sell_signals)
            else:
                print("   ✅ 持仓正常")
        else:
            print(f"   ⚠️ 监控失败: {monitor_result.error}")
    
    def _alerts_to_sell_signals(self, alerts: List[Dict], positions: List[Dict]) -> List[Dict]:
        """将监控预警转为卖出信号"""
        sell_signals = []
        
        # 建立 symbol → position 的快速查找
        pos_map = {}
        for pos in positions:
            sym = pos.get("symbol", "")
            if sym:
                pos_map[sym] = pos
        
        for alert in alerts:
            symbol = alert.get("symbol", "")
            position = pos_map.get(symbol)
            if not position:
                print(f"   ⚠️ 预警 {symbol} 无对应持仓，跳过")
                continue
            
            shares = position.get("shares", 0)
            if shares <= 0:
                print(f"   ⚠️ 预警 {symbol} 持仓为0，跳过")
                continue
            
            sell_signals.append({
                "symbol": symbol,
                "signal": "sell",
                "price": alert.get("current_price", 0),
                "action_shares": shares,
                "avg_cost": alert.get("avg_cost", 0),
                "reason": alert.get("type", "unknown"),
                "pnl_pct": alert.get("pnl_pct", 0),
                "message": alert.get("message", "")
            })
        
        return sell_signals
    
    def _execute_monitor_sells(self, sell_signals: List[Dict]):
        """执行监控触发的卖出信号"""
        print(f"\n   🔔 执行监控卖出: {len(sell_signals)} 笔")
        
        for sig in sell_signals:
            print(f"      {sig['message']}")
        
        # 获取账户信息
        portfolio = self.portfolio_manager.get_portfolio()
        
        execution_input = {
            "signals": sell_signals,
            "total_assets": portfolio.get("total_value", 1000000),
            "initial_assets": portfolio.get("initial_capital", 1000000)
        }
        
        execution_result = self.execution.process(execution_input)
        
        # 合并到执行阶段结果
        if "execution" in self.results["steps"]:
            existing = self.results["steps"]["execution"]
            if existing and existing.success and execution_result.success:
                # 合并交易汇总
                existing_data = existing.data
                new_data = execution_result.data
                existing_data["executed_trades"].extend(new_data["executed_trades"])
                existing_data["failed_trades"].extend(new_data["failed_trades"])
                existing_data["summary"]["total"] += new_data["summary"]["total"]
                existing_data["summary"]["executed"] += new_data["summary"]["executed"]
                existing_data["summary"]["failed"] += new_data["summary"]["failed"]
        else:
            self.results["steps"]["monitor_execution"] = execution_result
        
        if execution_result.success:
            n = len([t for t in execution_result.data.get("executed_trades", []) if t.get("success")])
            print(f"   ✅ 监控卖出执行完成: {n} 笔成功")
        else:
            print(f"   ❌ 监控卖出执行失败: {execution_result.error}")
    
    def _is_step_success(self, step_name: str) -> bool:
        """检查某阶段是否成功"""
        step = self.results["steps"].get(step_name)
        if step is None:
            return False
        # 兼容 dict 和 Result 对象两种格式
        if isinstance(step, dict):
            return step.get("success", False)
        return step.success
    
    def _finalize(self, success: bool) -> Dict:
        """完成流程"""
        # 记录每日盈亏快照
        self._record_daily_snapshot()

        # 使用 ResultProcessor 处理结果
        final_results = self.result_processor.finalize_results(self.results, success)

        status = "成功" if success else "失败"
        print(f"\n✅ Coordinator 流程结束: {status}")

        if "output_file" in final_results:
            print(f"   结果保存: {final_results['output_file']}")

        return final_results

    def _record_daily_snapshot(self):
        """记录每日资产快照到 daily_pnl 表"""
        import sqlite3
        from pathlib import Path

        db_path = Path(self.memory_path) / "skills" / "LongToo-trader" / "data" / "paper_trading.db"
        if not db_path.exists():
            return

        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(str(db_path))
        try:
            # 检查今天是否已记录
            existing = conn.execute(
                "SELECT COUNT(*) FROM daily_pnl WHERE date = ?", (today,)
            ).fetchone()[0]
            if existing > 0:
                return

            # 获取账户数据
            row = conn.execute(
                "SELECT cash, total_assets FROM account WHERE id = 1"
            ).fetchone()
            if not row:
                return

            cash, total_assets = row
            positions_value = total_assets - cash

            # 获取昨日总资产（计算当日盈亏）
            yesterday_row = conn.execute(
                "SELECT total_assets FROM daily_pnl ORDER BY date DESC LIMIT 1"
            ).fetchone()
            yesterday_assets = yesterday_row[0] if yesterday_row else 100000.0

            daily_pnl = total_assets - yesterday_assets
            daily_pnl_pct = (daily_pnl / yesterday_assets * 100) if yesterday_assets > 0 else 0

            conn.execute("""
                INSERT OR REPLACE INTO daily_pnl
                (date, cash, positions_value, total_assets, daily_pnl, daily_pnl_pct, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (today, cash, positions_value, total_assets, daily_pnl, daily_pnl_pct))
            conn.commit()
            print(f"📊 每日快照已记录: {today} 总资产 ¥{total_assets:,.2f} ({daily_pnl_pct:+.2f}%)")
        except Exception as e:
            logger.warning(f"记录每日快照失败: {e}")
        finally:
            conn.close()
    
    def generate_report(self) -> str:
        """生成交易报告"""
        return self.result_processor.generate_report(self.results)
