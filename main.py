#!/usr/bin/env python3
"""
LongToo 龙兔量化交易执行器 - 统一入口
支持两种模式：
1. 实盘模式：连接真实券商 API
2. 模拟模式：自建模拟盘，虚拟资金交易

用法:
    python3 main.py --mode live [--dry-run]
    python3 main.py --mode paper [--init]

示例:
    python3 main.py --mode live                        # 实盘交易
    python3 main.py --mode live --dry-run              # 实盘模拟（不下单）
    python3 main.py --mode paper --init                # 初始化模拟盘账户
    python3 main.py --mode paper --status              # 查看模拟盘账户
    python3 main.py --mode paper --buy 000001.SZ 1000  # 模拟盘买入
"""

import argparse
import sys
import os
import time
import atexit
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "scripts"))

# ============================================================
# 常量定义 - 避免inline dict重复
# ============================================================

# 仓位状态 -> 短名称
POSITION_STATE_SHORT = {
    'NEW_POSITION': '新仓',
    'PROFIT_RUNNING': '运行',
    'WEAKENING': '走弱',
    'EXIT_PENDING': '待退',
    'PROTECTED': '保护'
}

# 仓位状态 -> Emoji
POSITION_STATE_EMOJI = {
    'NEW_POSITION': '🔵',
    'PROFIT_RUNNING': '🟢',
    'WEAKENING': '🟡',
    'EXIT_PENDING': '🔴',
    'PROTECTED': '🟢'
}

# 市场状态 -> Emoji
MARKET_STATE_EMOJI = {
    '强势': '🟢',
    '弱势': '🔴',
    '震荡': '🟡',
    '偏强': '🟢',
    '偏弱': '🔴'
}

# 内部市场状态 -> 显示名称
MARKET_STATE_NAMES = {
    'bull': '强势',
    'bullish': '强势',
    'neutral': '震荡',
    'bearish': '弱势',
    'bear': '弱势'
}

# 卖出原因 -> 人类可读
SELL_REASON_NAMES = {
    'stop_loss': '止损触发',
    'dynamic_stop_loss': '动态止损',
    'state_machine_exit': '状态机退出',
    'timeout_no_profit': '持仓超时未盈利',
    'trend_critical': '趋势严重破坏',
    'trend_broken_with_weakening': '趋势破坏+走弱',
    'position_overflow_loss': '结构收缩｜亏损仓位',
    'position_overflow_weak_profit': '结构收缩｜弱盈利'
}

# 股票名称缓存（避免每次报告都加载文件）
_stock_name_cache: Dict[str, str] = None


def _load_stock_names() -> Dict[str, str]:
    """加载股票名称映射（带缓存）"""
    global _stock_name_cache
    if _stock_name_cache is not None:
        return _stock_name_cache

    try:
        watchlist_path = Path(__file__).parent / "watchlist.json"
        with open(watchlist_path, 'r', encoding='utf-8') as f:
            watchlist = json.load(f)
        _stock_name_cache = {}
        for s in watchlist.get('stocks', []):
            symbol = s['symbol']
            _stock_name_cache[symbol] = s['name']
            if symbol.startswith('6'):
                _stock_name_cache['sh' + symbol] = s['name']
            else:
                _stock_name_cache['sz' + symbol] = s['name']
    except (FileNotFoundError, json.JSONDecodeError):
        _stock_name_cache = {}

    return _stock_name_cache


def _sign_prefix(value: float) -> str:
    """返回正负号前缀（正数返回+，负数返回空字符串）"""
    return "+" if value >= 0 else ""


def _calc_holding_days(buy_date: str, now: datetime = None) -> int:
    """计算持仓天数"""
    if not buy_date:
        return 0
    if now is None:
        now = datetime.now()
    try:
        entry = datetime.strptime(buy_date[:10], "%Y-%m-%d")
        return (now - entry).days
    except ValueError:
        return 0


# ============================================================
# PID 锁 - 防止并发执行导致重复报告
# ============================================================
LOCK_FILE = Path(__file__).parent / "data" / ".trading.lock"
LOCK_TIMEOUT = 300  # 锁超时 5 分钟（进程卡死后自动释放）

def acquire_lock() -> bool:
    """获取交易锁，防止并发执行
    Returns: True 表示可以继续，False 表示已有进程在运行"""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    if LOCK_FILE.exists():
        try:
            data = LOCK_FILE.read_text().strip()
            pid, ts = data.split(",")
            pid, ts = int(pid), float(ts)
            
            # 检查进程是否还活着
            try:
                os.kill(pid, 0)  # 信号0只是检查进程存在
                # 进程还活着，检查是否超时
                if time.time() - ts < LOCK_TIMEOUT:
                    print(f"⚠️ 交易进程已在运行 (PID: {pid})，跳过本次执行")
                    return False
                else:
                    print(f"⚠️ 锁已超时 ({time.time() - ts:.0f}s)，强制释放")
            except (OSError, ProcessLookupError):
                # 进程已死，清理旧锁
                print(f"⚠️ 发现残留锁 (PID {pid} 已不存在)，清理后继续")
        except (ValueError, FileNotFoundError):
            pass
    
    # 写入新锁
    LOCK_FILE.write_text(f"{os.getpid()},{time.time()}")
    return True

def release_lock():
    """释放交易锁"""
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass

# 注册退出清理
atexit.register(release_lock)


def run_live_mode(args):
    """运行实盘模式"""
    from scripts.agents import AnalystAgent, RiskAgent, ExecutionAgent
    from scripts.coordinator import Coordinator
    from scripts.core.config import get_config
    from scripts.core.api_client import LiveAPIClient
    from scripts.core.strategy import StrategyFactory
    from memory_manager import MemoryManager, RiskManager
    
    config = get_config()
    strategy_name = args.strategy or config.trading.strategy_name
    dry_run = args.dry_run if args.dry_run is not None else config.trading.dry_run
    
    print("=" * 60)
    print("🤖 LongToo 龙兔量化交易 - Agent 架构版")
    print("=" * 60)
    print(f"策略: {strategy_name}")
    print(f"模式: {'🟡 模拟' if dry_run else '🔴 实盘'}")
    print("=" * 60)
    
    # 风控状态查询
    if args.risk_status:
        memory_manager = MemoryManager(config.memory_path)
        risk_manager = RiskManager(memory_manager)
        print("\n🛡️ 风控状态:")
        status = risk_manager.get_risk_status()
        print(f"   紧急停止: {'🚨 激活' if status['emergency_stop'] else '✅ 正常'}")
        print(f"   连续亏损: {status['consecutive_losses']} 次")
        print(f"   当日盈亏: {status['daily_pnl_pct']*100:.2f}%")
        print(f"   冷却状态: {'⏸️ 冷却中' if status['cooldown_active'] else '✅ 正常'}")
        return 0
    
    # 紧急停止/解除
    if args.emergency_stop:
        memory_manager = MemoryManager(config.memory_path)
        risk_manager = RiskManager(memory_manager)
        print("\n🚨 激活紧急停止...")
        risk_manager.activate_emergency_stop("手动触发")
        return 0
    
    if args.resume:
        memory_manager = MemoryManager(config.memory_path)
        risk_manager = RiskManager(memory_manager)
        print("\n✅ 解除紧急停止...")
        risk_manager.deactivate_emergency_stop()
        return 0
    
    # 创建 Agent
    print("\n🔧 初始化 Agent...")
    api_client = LiveAPIClient(config.api.base_url, config.api.auth_token)
    memory_manager = MemoryManager(config.memory_path)
    risk_manager = RiskManager(memory_manager)
    strategy = StrategyFactory.create(strategy_name)
    
    analyst = AnalystAgent(api_client, strategy, memory_manager)
    risk = RiskAgent(risk_manager)
    execution = ExecutionAgent(api_client, memory_manager, risk_manager, dry_run)
    
    print(f"   ✅ AnalystAgent")
    print(f"   ✅ RiskAgent")
    print(f"   ✅ ExecutionAgent")
    
    # 运行
    print("\n🚀 启动 Coordinator...")
    coordinator = Coordinator(analyst, risk, execution)
    
    try:
        result = coordinator.run()
        print("\n" + coordinator.generate_report())
        
        # 发送报告
        try:
            from scripts.utils.alerts import get_alert_manager
            risk_status = risk_manager.get_risk_status()
            get_alert_manager().daily_report(
                total_assets=result.get("total_assets", 0),
                cash=result.get("cash", 0),
                pnl_pct=risk_status.get("daily_pnl_pct", 0),
                trade_count=risk_status.get("daily_trade_count", 0)
            )
        except Exception as e:
            print(f"⚠️ 发送每日报告失败: {e}")
        
        return 0 if result.get("success") else 1
        
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断")
        return 130
    except Exception as e:
        print(f"\n💥 异常: {e}")
        import traceback
        traceback.print_exc()
        return 1


def run_paper_mode(args):
    """运行 Paper Trading 模式（自建模拟盘）"""
    from scripts.execution.paper_adapter import PaperTradingExecutionAgent
    from scripts.execution.paper_executor import PaperTradingStorage
    import yaml

    # 加载配置
    config_path = Path(__file__).parent / "config" / "paper_trading.yaml"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    else:
        config = {"account": {"initial_capital": 100000}}

    # 合并 account 和 risk_management 配置
    paper_config = config.get("account", {})
    risk_config = config.get("risk_management", {})
    paper_config.update(risk_config)  # 合入 max_positions 等配置

    # 初始化账户
    if args.init:
        import os
        db_path = Path("~/.openclaw/workspace/skills/LongToo-trader/data/paper_trading.db").expanduser()
        if db_path.exists():
            os.remove(db_path)
            print(f"已清除旧数据库: {db_path}")

        storage = PaperTradingStorage()
        capital = args.capital or paper_config.get("initial_capital", 100000)
        storage.init_account(capital)
        
        print("=" * 60)
        print("🎉 模拟盘账户初始化成功！")
        print("=" * 60)
        print(f"💰 初始资金: ¥{capital:,.2f}")
        print(f"📁 数据文件: {db_path}")
        print("=" * 60)
        return 0
    
    # 创建 Agent
    agent = PaperTradingExecutionAgent(paper_config)
    
    # 自动交易模式（定时任务用）- 优先检查
    if args.daily:
        return run_paper_daily_trading(agent, args)

    # 收盘快照（更新价格 + 记录每日盈亏）
    if args.snapshot:
        return run_paper_snapshot(agent)

    # 查看状态
    if args.status or (not args.buy and not args.sell and not args.daily):
        agent.print_portfolio()
        return 0
    
    # 买入
    if args.buy:
        symbol = args.buy[0]
        shares = int(args.buy[1])
        success, msg, trade = agent.execute_buy(symbol, shares)
        print(f"\n{'='*60}")
        print(f"🛒 买入 {symbol} {shares}股")
        print(f"{'='*60}")
        print(f"{'✅' if success else '❌'} {msg}")
        if trade:
            print(f"   成交价: ¥{trade['price']:.2f}")
            print(f"   手续费: ¥{trade['commission']:.2f}")
            print(f"   总成本: ¥{trade['total_cost']:.2f}")
        agent.print_portfolio()
        return 0 if success else 1
    
    # 卖出
    if args.sell:
        symbol = args.sell[0]
        shares = int(args.sell[1]) if len(args.sell) > 1 else None
        success, msg, trade = agent.execute_sell(symbol, shares)
        print(f"\n{'='*60}")
        print(f"💰 卖出 {symbol} {shares if shares else '全部'}股")
        print(f"{'='*60}")
        print(f"{'✅' if success else '❌'} {msg}")
        if trade:
            print(f"   成交价: ¥{trade['price']:.2f}")
            print(f"   手续费: ¥{trade['commission']:.2f}")
            print(f"   印花税: ¥{trade['tax']:.2f}")
            print(f"   净收入: ¥{trade['total_revenue']:.2f}")
        agent.print_portfolio()
        return 0 if success else 1
    
    # 查看交易历史
    if args.history:
        trades = agent.get_trade_history(20)
        print(f"\n{'='*80}")
        print("📜 最近交易记录")
        print(f"{'='*80}")
        print(f"{'时间':<20} {'操作':<6} {'股票':<12} {'数量':>8} {'价格':>10} {'金额':>12} {'费用':>8}")
        print(f"{'-'*80}")
        for t in trades:
            ts = t['timestamp'][:19] if len(t['timestamp']) > 19 else t['timestamp']
            print(f"{ts:<20} {t['action']:<6} {t['symbol']:<12} {t['shares']:>8} "
                  f"¥{t['price']:>9.2f} ¥{t['amount']:>11,.2f} ¥{t['commission']:>7.2f}")
        print(f"{'='*80}")
        return 0
    
    return 0


def run_paper_snapshot(agent):
    """收盘快照：更新持仓现价 + 记录每日盈亏"""
    import sqlite3
    from datetime import datetime

    storage = agent.executor.storage

    # 更新持仓现价
    positions = agent.executor.get_positions()
    updated = 0
    for pos in positions:
        if pos.shares <= 0:
            continue
        try:
            price = agent._get_current_price(pos.symbol)
            if price and price > 0:
                pos.current_price = price
                storage.update_position(pos)
                updated += 1
        except Exception:
            pass

    # 记录快照
    db_path = storage.db_path
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(str(db_path))
    try:
        existing = conn.execute("SELECT COUNT(*) FROM daily_pnl WHERE date=?", (today,)).fetchone()[0]
        if existing > 0:
            print(f"📊 {today} 快照已存在，跳过")
            return 0

        row = conn.execute("SELECT cash, total_assets FROM account WHERE id=1").fetchone()
        if not row:
            print("❌ 账户未初始化")
            return 1

        cash, total_assets = row
        positions_value = total_assets - cash

        yesterday = conn.execute("SELECT total_assets FROM daily_pnl ORDER BY date DESC LIMIT 1").fetchone()
        yesterday_assets = yesterday[0] if yesterday else 100000.0
        daily_pnl = total_assets - yesterday_assets
        daily_pnl_pct = (daily_pnl / yesterday_assets * 100) if yesterday_assets > 0 else 0

        conn.execute("""INSERT OR REPLACE INTO daily_pnl
            (date, cash, positions_value, total_assets, daily_pnl, daily_pnl_pct, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (today, cash, positions_value, total_assets, daily_pnl, daily_pnl_pct))
        conn.commit()
        print(f"📊 收盘快照已记录: {today}")
        print(f"   总资产: ¥{total_assets:,.2f}")
        print(f"   现金: ¥{cash:,.2f}")
        print(f"   持仓市值: ¥{positions_value:,.2f}")
        emoji = "🟢" if daily_pnl >= 0 else "🔴"
        print(f"   当日盈亏: {emoji}¥{daily_pnl:+,.2f} ({daily_pnl_pct:+.2f}%)")
        print(f"   更新价格: {updated}/{len(positions)} 只")
    finally:
        conn.close()
    return 0


def run_paper_daily_trading(agent, args):
    """模拟盘日度自动交易 - 使用 Coordinator 多 Agent 架构"""
    from scripts.coordinator import Coordinator
    from scripts.agents.analyst import AnalystAgent
    from scripts.agents.risk import RiskAgent
    from scripts.agents.position_monitor import PositionMonitorAgent
    from scripts.agents.report import ReportAgent
    from scripts.core.strategy import MomentumTrendStrategy
    from scripts.execution.paper_executor import PaperTradingStorage
    from scripts.memory_manager import MemoryManager, RiskManager
    import yaml
    from pathlib import Path
    
    print("=" * 60)
    print("🤖 Paper Trading - 每日自动交易 (Coordinator)")
    print("=" * 60)
    
    # 只创建一个 MemoryManager 实例（修复 Bug 10）
    memory_manager = MemoryManager()
    
    # 🔧 注入 memory_manager 到执行 Agent（用于连续亏损熔断追踪）
    agent.memory_manager = memory_manager
    
    # 每日首次交易前重置计数器（解决周末/节假日未重置问题）
    if memory_manager.reset_daily_stats_if_new_day():
        print("✅ 已重置每日交易统计")
    
    # 加载策略配置
    config_path = Path(__file__).parent / "config" / "paper_trading.yaml"
    strategy_config = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                full_config = yaml.safe_load(f)
                strategy_config = full_config.get("strategies", {}).get("momentum_trend", {})
                risk_config = full_config.get("risk_management", {})
                strategy_config.update(risk_config)
                print(f"✅ 已加载策略配置")
        except Exception as e:
            print(f"⚠️ 加载配置失败: {e}，使用默认配置")
    
    # 创建策略和 AnalystAgent
    from scripts.core.filters import FilterFactory
    filter_names = strategy_config.get("filters", ["regime", "volatility", "defensive"])
    filters = FilterFactory.from_config(filter_names)
    strategy = MomentumTrendStrategy(strategy_config, filters=filters)
    analyst = AnalystAgent(
        api_client=agent,
        strategy=strategy
    )
    
    # 创建 RiskEngine（v2.4.0 中央化风控）
    # 🔧 架构原则：风控不信缓存，直接查数据库
    from scripts.core.risk_engine import RiskEngine

    risk_storage = agent.executor.storage if hasattr(agent, 'executor') and hasattr(agent.executor, 'storage') else None

    if risk_storage is not None:
        risk_engine = RiskEngine(
            storage=risk_storage,
            state_provider=memory_manager.load_strategy_state,
            config={
                "max_positions": 5,
                "max_position_pct": 0.20,
                "max_daily_loss_pct": -0.05,
                "max_daily_trades": 5,
                "cooldown_hours": 12
            }
        )
        print("✅ RiskEngine 已初始化（中央化风控）")
    else:
        risk_engine = None
        print("⚠️ RiskEngine 未初始化，使用降级模式")

    # 创建 RiskManager 和 RiskAgent（使用同一个 memory_manager 实例）
    risk_manager = RiskManager(memory_manager, storage=risk_storage, risk_engine=risk_engine)
    risk = RiskAgent(risk_manager)

    # 注入 RiskEngine 到执行 Agent
    if risk_engine is not None:
        agent.risk_engine = risk_engine
        agent.executor.risk_engine = risk_engine

    # v2.5.0: 创建 EventStore（Event Sourcing）
    from scripts.core.event_store import EventStore

    event_store = EventStore()
    print("✅ EventStore 已初始化（Event Sourcing）")

    # v2.5.0: 创建 PositionEngine（中央化状态管理，集成 EventStore）
    from scripts.core.position_engine import PositionEngine

    position_engine = None
    if risk_storage is not None:
        position_engine = PositionEngine(risk_storage, event_store=event_store)
        agent.position_engine = position_engine
        print("✅ PositionEngine 已初始化（中央化状态管理 + Event Sourcing）")

    # v2.5.0: 创建 PortfolioEngine（组合级风险）
    from scripts.core.portfolio_engine import PortfolioEngine

    portfolio_engine = None
    if risk_storage is not None:
        portfolio_engine = PortfolioEngine(risk_storage)
        print("✅ PortfolioEngine 已初始化（组合级风险）")

    # v2.5.0: 创建 QueryService（CQRS 读模型）
    from scripts.core.query_service import QueryService

    query_service = QueryService(storage=risk_storage, event_store=event_store)
    print("✅ QueryService 已初始化（CQRS 读模型）")

    # 创建 PositionMonitorAgent
    position_monitor = PositionMonitorAgent(agent.data_provider)
    
    # 创建 Coordinator
    coordinator = Coordinator(analyst, risk, agent, position_monitor)
    
    # 执行交易流程（daily 模式自动同步数据）
    result = coordinator.run(sync_data=True)
    
    # 输出结果
    print("\n" + "=" * 60)
    print("📊 交易完成")
    print("=" * 60)
    agent.print_portfolio()
    
    # 处理 AgentResult 对象或 dict
    def get_data(r, key, default=None):
        if hasattr(r, 'data'):
            return r.data.get(key, default)
        return r.get(key, default)
    
    steps = get_data(result, 'steps', {})
    
    # 解析 analysis 结果
    analysis_step = steps.get('analysis', {})
    if hasattr(analysis_step, 'data'):
        analysis_data = analysis_step.data
    else:
        analysis_data = analysis_step.get('data', {})
    market_state = analysis_data.get('market_state', 'neutral')
    
    # 解析 execution 结果
    execution_step = steps.get('execution', {})
    if hasattr(execution_step, 'data'):
        execution_data = execution_step.data
    else:
        execution_data = execution_step.get('data', {})
    buy_signals = execution_data.get('buy_signals', [])
    sell_signals = execution_data.get('sell_signals', [])
    
    # 解析持仓监控结果
    monitor_step = steps.get('position_monitor', {})
    if hasattr(monitor_step, 'data'):
        monitor_data = monitor_step.data
    else:
        monitor_data = monitor_step.get('data', {})
    position_alerts = monitor_data.get('alerts', [])
    
    # 生成盘后报告（ReportAgent）
    print("\n📊 生成盘后统计报告...")
    storage = PaperTradingStorage()
    report_agent = ReportAgent(storage)
    report_input = {
        "account": agent.get_account(),
        "positions": agent.get_positions()
    }
    report_result = report_agent.process(report_input)
    
    if report_result.success:
        stats = report_result.data
        today_stats = stats.get("today", {})
        print(f"   今日交易: {today_stats.get('total_trades', 0)} 笔")
        print(f"   胜率: {today_stats.get('win_rate', 0):.1f}%")
    
    # 智能推送：有交易、有预警、或每日收盘推送
    has_trades = buy_signals or sell_signals
    has_alerts = bool(position_alerts)
    is_end_of_day = datetime.now().hour >= 14  # 下午2点后算收盘

    if has_trades or has_alerts or is_end_of_day:
        # 获取今日交易记录
        today_trades = []
        if report_result.success:
            today_trades = report_result.data.get("today_trades", [])
            stats = report_result.data

        # 生成详细日报（使用新函数）
        report = generate_daily_report(
            agent, market_state, buy_signals, sell_signals,
            today_trades=today_trades,
            position_alerts=position_alerts,
            stats=stats if report_result.success else None
        )

        # 显示报告内容
        print("\n" + report + "\n")

        # v2.5.0: 打印组合快照摘要
        if portfolio_engine:
            snap = portfolio_engine.get_snapshot(
                positions=agent.get_positions(),
                account=agent.get_account()
            )
            print("📊 组合快照:")
            print(f"   暴露度: {snap.exposure*100:.1f}%  现金比例: {snap.cash_ratio*100:.1f}%")
            print(f"   风险等级: {snap.risk_level.value}  健康度: {snap.health_score:.0f}分")
            print(f"   盈亏分布: {snap.profit_count}盈/{snap.loss_count}亏  最大仓位: {snap.max_position_pct:.1f}%")
            rebalance_msg = portfolio_engine.check_rebalance_needed()
            if rebalance_msg:
                print(f"   {rebalance_msg}")

        # v2.5.0: 使用 QueryService 查询（CQRS 读模型）
        if query_service:
            portfolio_summary = query_service.get_portfolio_summary()
            print("📋 QueryService 查询结果:")
            print(f"   总资产: ¥{portfolio_summary.total_assets:,.0f}  现金: ¥{portfolio_summary.cash:,.0f}")
            print(f"   持仓数: {portfolio_summary.positions_count}  盈亏分布: {portfolio_summary.profit_count}盈/{portfolio_summary.loss_count}亏")

            # 显示事件历史（审计追踪）
            event_stats = event_store.get_stats() if event_store else {}
            if event_stats.get("total_events", 0) > 0:
                print(f"   📜 事件总数: {event_stats['total_events']} 条")

        send_notification(report)
        print("✅ 报告已推送")
    else:
        print("ℹ️ 无交易无预警，跳过推送")
    
    success = result.success if hasattr(result, 'success') else result.get('success')
    return 0 if success else 1


def analyze_market(agent, market_state):
    """分析市场环境"""
    try:
        # 获取上证指数数据
        index_data = agent.data_provider.get_index_quote('000001')
        if index_data:
            change_pct = index_data.get('change_pct', 0)
            index_value = index_data.get('close', 0) or index_data.get('price', 0)

            if change_pct > 1:
                state = '强势'
            elif change_pct > 0:
                state = '偏强'
            elif change_pct > -1:
                state = '震荡'
            else:
                state = '弱势'

            return {
                'state': state,
                'sh_index': index_value,
                'sh_change_pct': change_pct,
                'comment': f'大盘{state}，涨跌{change_pct:+.2f}%'
            }
    except (KeyError, TypeError, AttributeError):
        pass

    # 默认返回（使用常量）
    return {
        'state': MARKET_STATE_NAMES.get(market_state, '震荡'),
        'sh_index': 0,
        'sh_change_pct': 0,
        'comment': '市场数据暂缺'
    }


def generate_daily_report(agent, market_state, buy_signals, sell_signals, today_trades=None, position_alerts=None, stats=None):
    """生成每日交易报告 - 策略驾驶舱版

    七大模块：
    1. 今日摘要（最关键，放最前）
    2. 风控/异常事件
    3. 今日操作（含原因）
    4. 持仓明细（含状态和峰值）
    5. 组合健康度
    6. 市场环境（增强版）
    7. 明日计划

    Args:
        agent: 模拟盘代理
        market_state: 市场状态
        buy_signals: 买入信号
        sell_signals: 卖出信号
        today_trades: 今日实际交易记录
        position_alerts: 持仓预警
        stats: 统计数据（today/recent）
    """
    from scripts.agents.position_state_machine import PositionStateMachine, analyze_portfolio_states

    account = agent.get_account()
    positions = agent.get_positions()
    state_machine = PositionStateMachine()

    # 加载股票名称映射（使用缓存）
    name_map = _load_stock_names()

    # 计算基础数据
    total_assets = account['total_assets']
    initial_capital = account['initial_capital']
    cash = account['cash']
    cash_pct = (cash / total_assets * 100) if total_assets else 0
    pos_pct = 100 - cash_pct
    total_profit = total_assets - initial_capital
    profit_pct = (total_profit / initial_capital * 100) if initial_capital else 0

    # 计算今日盈亏（从stats获取）
    today_pnl_pct = 0
    if stats and 'today' in stats:
        today_pnl_pct = stats['today'].get('daily_pnl_pct', 0)

    # 单次遍历处理持仓信息（合并多次迭代）
    now = datetime.now()
    position_states = {}
    stop_loss_triggered = []
    drawdown_alerts = []
    profit_positions = 0
    loss_positions = 0

    for pos in positions:
        pnl_pct = pos.get('pnl_pct', 0)
        peak_pnl = pos.get('peak_pnl', max(0, pnl_pct))
        pos['peak_pnl'] = peak_pnl
        pos['holding_days'] = _calc_holding_days(pos.get('buy_date', ''), now)

        # 盈亏统计
        if pnl_pct >= 0:
            profit_positions += 1
        else:
            loss_positions += 1

        # 止损检测
        if pnl_pct <= -8:
            stop_loss_triggered.append(pos)

        # 回撤检测
        drawdown = peak_pnl - pnl_pct
        if drawdown >= 3 and peak_pnl > 0:
            drawdown_alerts.append((pos, drawdown))

        # 状态机状态
        state_info = state_machine.get_state_info(pos)
        position_states[pos['symbol']] = state_info

    # 分析组合状态
    portfolio_analysis = analyze_portfolio_states(positions) if positions else {'state_counts': {}, 'health_score': 100}

    # 获取大盘数据
    market_analysis = analyze_market(agent, market_state)

    date_str = now.strftime('%Y-%m-%d %H:%M')

    lines = []

    # ========== 标题 ==========
    lines.append(f"📊 LongToo量化日报 #{date_str}")
    lines.append("━" * 28)

    trade_count = len(today_trades) if today_trades else 0
    sell_count = len([t for t in (today_trades or []) if t.get('action', '').lower() == 'sell'])
    buy_count = trade_count - sell_count
    max_trades = 3

    # ========== 第一屏：今天发生了什么 ==========

    # 1. 今日摘要
    lines.append("")
    lines.append("【今日摘要】")

    summary_events = []
    if sell_count > 0 and sell_count > buy_count:
        summary_events.append(f"结构收缩，卖出{sell_count}只")
    elif trade_count > 0:
        summary_events.append(f"交易{trade_count}笔")
    else:
        summary_events.append("无交易")

    if trade_count > max_trades:
        summary_events.append(f"超限{trade_count}/{max_trades}")

    lines.append(f"⚠ {'｜'.join(summary_events)}")

    # 仓位情绪
    if pos_pct < 30:
        pos_emoji = "🛡"
        pos_desc = "防守"
    elif pos_pct > 70:
        pos_emoji = "🔥"
        pos_desc = "进攻"
    else:
        pos_emoji = "⚖"
        pos_desc = "均衡"
    lines.append(f"{pos_emoji} {pos_desc}仓位 {pos_pct:.0f}%｜现金 {cash_pct:.0f}%")

    # 盈亏
    if len(positions) == 0:
        pos_status = "空仓"
    elif loss_positions == 0:
        pos_status = "全盈"
    elif profit_positions == 0:
        pos_status = "全亏"
    else:
        pos_status = f"盈{profit_positions}亏{loss_positions}"
    lines.append(f"今日 {_sign_prefix(today_pnl_pct)}{today_pnl_pct:.2f}%｜累计 {_sign_prefix(total_profit)}{profit_pct:.2f}%｜{pos_status}")

    # ========== 2. 系统状态 ==========
    lines.append("")
    lines.append("【系统状态】")

    # 判断系统模式
    if sell_count > buy_count and sell_count > 0:
        lines.append("⚠ 风险模式：结构收缩中")
    elif buy_count > 0 and sell_count == 0:
        lines.append("🟢 进攻模式：积极建仓")
    elif trade_count == 0:
        lines.append("🟡 观望模式：无操作")
    else:
        lines.append("⚖ 调仓模式：结构优化")

    if trade_count > max_trades:
        lines.append(f"⚠ 今日交易超限 ({trade_count}/{max_trades})")

    # ========== 3. 今日系统行为 ==========
    lines.append("")
    lines.append("【系统行为】")

    behaviors = []
    if buy_count == 0 and sell_count == 0:
        behaviors.append("今日无操作")
    if buy_count > 0:
        behaviors.append(f"新开仓 {buy_count} 只")
    if sell_count > 0:
        behaviors.append(f"清仓 {sell_count} 只")
    if sell_count > buy_count and sell_count > 0:
        behaviors.append("仓位收缩防御")
    if len(positions) == 0:
        behaviors.append("空仓观望")

    lines.append("｜".join(behaviors) if behaviors else "系统正常运行")

    # ========== 4. 风控事件 ==========
    lines.append("")
    lines.append("【风控事件】")

    risk_events = []

    # 超限交易
    if trade_count > max_trades:
        risk_events.append(f"交易超限 {trade_count}/{max_trades}")

    # 止损触发（使用已计算的列表）
    for p in stop_loss_triggered:
        name = name_map.get(p['symbol'], p['symbol'][:6])
        risk_events.append(f"止损预警：{name} {p['pnl_pct']:.1f}%")

    # 盈利回撤（使用已计算的列表）
    for p, drawdown in drawdown_alerts:
        name = name_map.get(p['symbol'], p['symbol'][:6])
        risk_events.append(f"回撤预警：{name} {drawdown:.1f}%")

    # 持仓预警
    if position_alerts:
        for alert in position_alerts[:2]:
            risk_events.append(alert.get('message', ''))

    if risk_events:
        for e in risk_events[:4]:
            lines.append(f"🔴 {e}")
    else:
        lines.append("🟢 无异常风控事件")

    # ========== 5. 今日操作（含原因） ==========
    lines.append("")
    lines.append("【今日操作】")

    if today_trades:
        # 构建卖出原因映射（从sell_signals）
        sell_reasons = {}
        for sig in (sell_signals or []):
            sym = sig.get('symbol', '')
            reason = sig.get('reason', '')
            state = sig.get('state', '')
            pnl = sig.get('pnl_pct', 0)
            drawdown = sig.get('drawdown', 0)
            sell_reasons[sym] = {'reason': reason, 'state': state, 'pnl': pnl, 'drawdown': drawdown}

        for t in today_trades[:6]:  # 最多显示6笔
            symbol = t.get('symbol', '')
            name = name_map.get(symbol, symbol[:6])
            action = t.get('action', '').upper()
            shares = t.get('shares', 0)
            price = t.get('price', 0)
            amount = shares * price

            if action == 'SELL':
                # 生成卖出原因
                reason_info = sell_reasons.get(symbol, {})
                reason_text = _format_sell_reason(reason_info, name)
                lines.append(f"🔴 卖出 {name} {shares}股@¥{price:.2f}")
                lines.append(f"   原因：{reason_text}")
            else:
                lines.append(f"🟢 买入 {name} {shares}股@¥{price:.2f} ¥{amount:,.0f}")

        if len(today_trades) > 6:
            lines.append(f"   ... 共{len(today_trades)}笔")
    else:
        lines.append("无交易操作")

    # ========== 5. 持仓明细 ==========
    lines.append("")
    lines.append("【持仓明细】")

    if positions:
        sorted_pos = sorted(positions, key=lambda x: x.get('pnl_pct', 0), reverse=True)
        for p in sorted_pos[:8]:
            symbol = p.get('symbol', '')
            name = name_map.get(symbol, symbol[:6])
            shares = p.get('shares', 0)
            cost = p.get('avg_cost', 0)
            price = p.get('current_price', 0)
            pnl_pct = p.get('pnl_pct', 0)
            holding_days = p.get('holding_days', 0)
            peak_pnl = p.get('peak_pnl', 0)

            state_info = position_states.get(symbol, {})
            state = state_info.get('state', 'NEW_POSITION')
            state_emoji = POSITION_STATE_EMOJI.get(state, '⚪')
            state_short = POSITION_STATE_SHORT.get(state, state[:4])

            # 第一行：核心信息
            pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
            add_count = p.get('add_count', 0)
            add_flag = f"加{add_count}" if add_count > 0 else ""
            lines.append(f"{name} {shares}股｜{pnl_emoji} {_sign_prefix(pnl_pct)}{pnl_pct:.1f}%｜{state_emoji} {state_short} {add_flag}")

            # 第二行：辅助信息
            lines.append(f"   成本 ¥{cost:.2f} → ¥{price:.2f}｜{holding_days}天｜峰值 {_sign_prefix(peak_pnl)}{peak_pnl:.1f}%")

        if len(positions) > 8:
            lines.append(f"   ... 共 {len(positions)} 只")
    else:
        lines.append("空仓")

    # ========== 6. 组合健康度（含扣分，优化4） ==========
    lines.append("")
    lines.append("【组合健康度】")

    # 计算健康分（带扣分项）
    health_deductions = []
    base_health = 100

    # 扣分：风控异常
    if trade_count > max_trades:
        base_health -= 10
        health_deductions.append("交易超限")
    if stop_loss_triggered:
        base_health -= 15
        health_deductions.append("止损预警")

    # 扣分：回撤（使用已计算的drawdown_alerts）
    if drawdown_alerts:
        base_health -= 5
        health_deductions.append("盈利回撤")

    # 扣分：持仓亏损
    if loss_positions > len(positions) * 0.5:
        base_health -= 10
        health_deductions.append("持仓亏损")

    health_score = max(0, base_health)
    health_emoji = "🟢" if health_score >= 70 else "🟡" if health_score >= 40 else "🔴"

    lines.append(f"健康分 {health_emoji} {health_score}分")
    if health_deductions:
        lines.append(f"扣分项：{'、'.join(health_deductions[:3])}")

    # 盈亏分布
    if positions:
        lines.append(f"盈亏分布：盈 {profit_positions}/{len(positions)}｜亏 {loss_positions}/{len(positions)}")
    else:
        lines.append("盈亏分布：空仓")

    # ========== 7. 市场环境 ==========
    lines.append("")
    lines.append("【市场环境】")

    sh_index = market_analysis.get('sh_index', 0)
    sh_change = market_analysis.get('sh_change_pct', 0)
    market_state_text = market_analysis.get('state', '震荡')

    # 方向显示
    sh_dir = "↑" if sh_change > 0 else "↓" if sh_change < 0 else "—"

    lines.append(f"上证  {sh_index:.2f}  {sh_dir} {_sign_prefix(sh_change)}{sh_change:.2f}%")

    # 市场状态（使用常量）
    state_emoji = MARKET_STATE_EMOJI.get(market_state_text, "🟡")
    if sh_change > 0.5:
        market_detail = "偏强"
    elif sh_change < -0.5:
        market_detail = "偏弱"
    else:
        market_detail = ""
    market_display = f"{market_state_text}{market_detail}" if market_detail else market_state_text
    lines.append(f"状态  {state_emoji} {market_display}")

    # 风险等级
    if abs(sh_change) < 0.5:
        risk_emoji = "🟢"
        risk_text = "低风险"
    elif abs(sh_change) < 1.5:
        risk_emoji = "🟡"
        risk_text = "中风险"
    else:
        risk_emoji = "🔴"
        risk_text = "高风险"
    lines.append(f"风险  {risk_emoji} {risk_text}")

    # ========== 8. 明日计划（行动导向，简化） ==========
    lines.append("")
    lines.append("【明日计划】")

    # 待执行信号
    if sell_signals:
        sell_names = [name_map.get(s['symbol'], s['symbol'][:6]) for s in sell_signals[:3]]
        lines.append(f"🔴 待卖出 {len(sell_signals)} 只：{', '.join(sell_names)}")
    if buy_signals:
        buy_names = [name_map.get(s.get('symbol', ''), s.get('symbol', '')[:6]) for s in buy_signals[:3]]
        lines.append(f"🟢 待买入 {len(buy_signals)} 只：{', '.join(buy_names)}")

    if not sell_signals and not buy_signals:
        lines.append("🟡 无待执行信号")

    # 加仓候选（盈利>5%且未加仓）
    add_candidates = []
    for p in positions:
        pnl = p.get('pnl_pct', 0)
        add_count = p.get('add_count', 0)
        state_info = position_states.get(p['symbol'], {})
        state = state_info.get('state', '')
        if pnl >= 5 and add_count == 0 and state in ['PROFIT_RUNNING', 'PROTECTED']:
            name = name_map.get(p['symbol'], p['symbol'][:6])
            add_candidates.append(f"{name}({pnl:.0f}%)")

    if add_candidates:
        lines.append(f"📈 加仓候选：{', '.join(add_candidates[:3])}")

    # 仓位建议（用仓位情绪emoji）
    if pos_pct < 30:
        lines.append(f"🛡 保持防守仓位 {pos_pct:.0f}%")
    elif pos_pct > 70:
        lines.append(f"🔥 保持进攻仓位 {pos_pct:.0f}%")
    else:
        lines.append(f"⚖ 保持均衡仓位 {pos_pct:.0f}%")

    lines.append("🔹 候选池待更新")

    return "\n".join(lines)


def _format_sell_reason(reason_info: dict, name: str) -> str:
    """格式化卖出原因 - 即便数据库没有，也从现有信息生成可解释原因"""
    reason = reason_info.get('reason', '')
    state = reason_info.get('state', '')
    pnl = reason_info.get('pnl', 0)
    drawdown = reason_info.get('drawdown', 0)

    # 有明确原因时使用常量映射（带动态参数的单独处理）
    if reason and reason in SELL_REASON_NAMES:
        reason_text = SELL_REASON_NAMES[reason]
    elif reason == 'profit_drawdown':
        reason_text = f'盈利回撤{drawdown:.1f}%'
    elif reason == 'state_drawdown':
        reason_text = f'状态回撤{drawdown:.1f}%'
    elif reason:
        reason_text = reason
    else:
        # 无明确原因时，从现有状态信息生成简短说明
        parts = []
        if pnl <= -5:
            parts.append('止损未触发' if pnl > -8 else '触发止损线')
        elif pnl < 0:
            parts.append('弱势持仓')
        elif pnl > 0 and pnl < 2:
            parts.append('弱盈利')
        elif pnl > 5:
            parts.append('止盈未触发')
        else:
            parts.append('结构收缩')

        if state:
            state_text = {'PROFIT_RUNNING': '盈利运行', 'WEAKENING': '走弱', 'EXIT_PENDING': '待退出'}.get(state, '')
            if state_text:
                parts.append(state_text)

        reason_text = '｜'.join(parts)

    # 添加盈亏信息
    if pnl != 0:
        reason_text = f"{reason_text}｜{pnl:+.1f}%"

    return reason_text


def _get_state_emoji(state: str) -> str:
    """获取状态对应的emoji"""
    return POSITION_STATE_EMOJI.get(state, '⚪')


def send_notification(message):
    """发送QQ通知 - 龙兔风格"""
    import subprocess
    try:
        # 使用 openclaw 发送消息
        result = subprocess.run(
            ["openclaw", "message", "send",
             "--target", os.getenv("ALERT_QQBOT_TARGET", "qqbot:c2c:AF08ED48AC526DD285D308875D192C50"),
             "--message", message + "\n\n🐰"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            print("推送完毕 🐰")
        else:
            print(f"推送失败: {result.stderr}")
    except Exception as e:
        print(f"推送异常: {e}")


def run_data_sync(args):
    """运行数据同步命令"""
    import logging
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )
    
    from scripts.data.market_sync import sync_full, sync_incremental, get_db_stats
    
    if args.sync_stats:
        stats = get_db_stats()
        if not stats.get("exists"):
            print("❌ 数据库不存在，请先运行同步")
            return 1
        print(f"📊 市场数据库统计:")
        print(f"   股票数量: {stats['symbol_count']}")
        print(f"   总行数:   {stats['total_rows']}")
        print(f"   日期范围: {stats['earliest_date']} ~ {stats['latest_date']}")
        print(f"   文件大小: {stats['db_size_mb']} MB")
        return 0
    
    if args.sync_incremental:
        result = sync_incremental(days=1, max_workers=args.sync_workers)
    else:
        result = sync_full(days=60, max_workers=args.sync_workers)
    
    return 0 if result.get("success") else 1


def run_preselect(args):
    """运行尾盘预选 - 两阶段选股阶段1"""
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )

    from scripts.agents.analyst import AnalystAgent
    from scripts.core.strategy import MomentumTrendStrategy
    from scripts.data.candidates import save_candidates
    from scripts.data.market_sync import get_db_path

    print("=" * 60)
    print("🔍 尾盘预选 - 两阶段选股（阶段1）")
    print("=" * 60)

    # 检查数据库
    db_path = get_db_path()
    if not db_path.exists():
        print("❌ 市场数据库不存在，请先运行数据同步 (--sync)")
        return 1

    # 获取市场环境（简单判断）
    market_state = 'neutral'
    try:
        from scripts.data.tencent_provider import TencentFinanceProvider
        provider = TencentFinanceProvider()
        index_data = provider.get_index_quote('000001')
        if index_data:
            change_pct = index_data.get('change_pct', 0)
            if change_pct > 1:
                market_state = 'strong'
            elif change_pct < -1:
                market_state = 'weak'
            print(f"📊 上证指数: {index_data.get('price', 0):.2f} ({change_pct:+.2f}%)")
            print(f"📈 市场环境: {market_state.upper()}")
    except Exception as e:
        print(f"⚠️ 获取指数数据失败: {e}，使用默认震荡市场")

    # 创建 AnalystAgent（只需要策略，不需要API客户端）
    strategy = MomentumTrendStrategy()
    analyst = AnalystAgent(api_client=None, strategy=strategy)

    # 执行粗评分
    print(f"\n🔍 开始粗评分扫描...")
    top_n = args.preselect_top or 30
    candidates = analyst._scan_market_coarse(market_state=market_state, top_n=top_n)

    if not candidates:
        print("❌ 未找到符合条件的候选")
        return 1

    # 保存候选池
    print(f"\n💾 保存候选池...")
    success = save_candidates(candidates, market_state)

    if success:
        print(f"\n✅ 预选完成: {len(candidates)} 只候选已保存")
        print(f"   交易时间运行 --daily 将从候选池精确分析")
        return 0
    else:
        return 1


def run_intraday_check(args):
    """运行盘中异常风险检查 - 只减仓不加仓"""
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )

    from scripts.agents.intraday_risk import run_intraday_check
    from scripts.execution.paper_executor import PaperTradingStorage
    from pathlib import Path

    print("=" * 60)
    print("🛡️ 盘中异常风险检查")
    print("=" * 60)
    print("检查内容：跌停、放量暴跌、大盘风险")
    print("原则：只减仓不加仓，异常逃生")
    print("=" * 60)

    db_path = Path.home() / ".openclaw/workspace/skills/LongToo-trader/data/paper_trading.db"
    storage = PaperTradingStorage(str(db_path))

    # 执行检查
    result = run_intraday_check(storage)

    print(result['summary'])

    if result['has_risk']:
        print()
        print("⚠️ 发现异常风险，需要手动执行卖出！")
        for s in result['signals']:
            print(f"  {s['message']}")

        # 如果需要自动执行（可选）
        # agent = PaperTradingExecutionAgent(storage)
        # for sig in result['signals']:
        #     agent.execute_sell(sig['symbol'], sig['action_shares'])

        return 1  # 有风险，返回非零
    else:
        print()
        print("✅ 持仓正常，无异常风险")
        return 0


def run_backtest(args):
    """运行回测模拟"""
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )

    from scripts.core.backtest import BacktestEngine

    symbols = None
    if args.backtest_symbols:
        symbols = [s.strip() for s in args.backtest_symbols.split(",")]

    filter_names = None
    if args.backtest_filters:
        filter_names = [f.strip() for f in args.backtest_filters.split(",")]

    engine = BacktestEngine(
        strategy_name=args.backtest_strategy,
        initial_capital=args.backtest_capital,
        seed=args.backtest_seed,
        filter_names=filter_names,
    )

    print(f"📊 加载数据...")
    engine.load_data(
        start_date=args.backtest_start,
        end_date=args.backtest_end,
        symbols=symbols,
    )

    print(f"🚀 开始回测...")
    result = engine.run()
    result.print_summary()
    result.print_trade_distribution()
    result.print_score_bucket_analysis()

    if args.backtest_output:
        if args.backtest_output.endswith('.csv'):
            result.to_csv(args.backtest_output)
        else:
            result.to_json(args.backtest_output)

    return 0


def run_sync_history(args):
    """下载历史数据，扩展回测窗口"""
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )

    from scripts.data.market_sync import sync_full

    days = args.sync_history
    if days < 60:
        print("⚠️ 建议至少60天，指标预热需要30天以上")

    print(f"📥 下载 {days} 天历史数据...")
    result = sync_full(days=days, max_workers=args.sync_workers)

    if result.get("success"):
        print(f"✅ 同步完成: {result.get('stocks_updated', 0)} 只股票")
        return 0
    else:
        print(f"❌ 同步失败: {result.get('error', '未知错误')}")
        return 1


def main():
    """主入口"""
    parser = argparse.ArgumentParser(
        description="LongToo 龙兔量化交易执行器 - 支持实盘和模拟盘两种模式",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 实盘模式
  %(prog)s --mode live                        # 实盘交易
  %(prog)s --mode live --dry-run              # 实盘模拟（不下单）
  %(prog)s --mode live --risk-status          # 查看风控状态

  # Paper Trading 模式（自建模拟盘）
  %(prog)s --mode paper --init                # 初始化账户（10万）
  %(prog)s --mode paper --init --capital 200000  # 初始化20万
  %(prog)s --mode paper --status              # 查看账户
  %(prog)s --mode paper --buy 000001.SZ 1000  # 买入1000股
  %(prog)s --mode paper --sell 000001.SZ 500  # 卖出500股
  %(prog)s --mode paper --sell 000001.SZ      # 清仓
  %(prog)s --mode paper --history             # 查看交易历史
  %(prog)s --mode paper --daily               # 自动交易（定时任务用）

  # 数据同步
  %(prog)s --sync                                # 全量同步全A股数据
  %(prog)s --sync-incremental                    # 增量更新（只下载今天数据）
  %(prog)s --sync-stats                          # 查看本地DB统计

  # 两阶段选股
  %(prog)s --preselect                           # 尾盘预选（14:50定时任务用）
  %(prog)s --preselect --preselect-top 50        # 预选50只候选
  %(prog)s --mode paper --daily                  # 交易时间精确分析（读取候选池）

  # 回测
  %(prog)s --backtest --backtest-start 2025-03-01           # 快速回测
  %(prog)s --backtest --backtest-start 2025-01-01 --backtest-seed 42  # 可复现回测
  %(prog)s --backtest --backtest-symbols "600519,000001"    # 测试特定股票
  %(prog)s --backtest --backtest-output results.json        # 导出结果
  %(prog)s --sync-history 180                               # 下载180天历史数据
        """
    )
    
    # 通用参数
    parser.add_argument(
        "--mode",
        choices=["live", "paper"],
        help="运行模式: live (实盘交易) / paper (模拟交易)"
    )
    
    # 实盘模式参数
    parser.add_argument(
        "--strategy",
        default="momentum_trend",
        help="策略名称 (默认: momentum_trend, 实盘模式)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="模拟模式，不实际下单 (实盘模式下使用)"
    )
    parser.add_argument(
        "--risk-status",
        action="store_true",
        help="查看风控状态"
    )
    parser.add_argument(
        "--emergency-stop",
        action="store_true",
        help="激活紧急停止"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="解除紧急停止"
    )
    
    # 数据同步参数
    parser.add_argument(
        "--sync",
        action="store_true",
        help="全量同步全A股数据到本地DB"
    )
    parser.add_argument(
        "--sync-incremental",
        action="store_true",
        help="增量更新本地DB（只下载今天数据）"
    )
    parser.add_argument(
        "--sync-workers",
        type=int,
        default=8,
        help="数据同步并发线程数（默认8）"
    )
    parser.add_argument(
        "--sync-stats",
        action="store_true",
        help="显示本地DB统计信息"
    )

    # 尾盘预选参数
    parser.add_argument(
        "--preselect",
        action="store_true",
        help="尾盘预选：粗评分扫描全A股，保存候选池（14:50定时任务用）"
    )
    parser.add_argument(
        "--preselect-top",
        type=int,
        default=30,
        help="预选候选数量（默认30）"
    )

    # Paper Trading 模式参数
    parser.add_argument(
        "--init",
        action="store_true",
        help="初始化模拟盘账户 (Paper模式)"
    )
    parser.add_argument(
        "--capital",
        type=float,
        help="初始资金 (Paper模式，默认10万)"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="查看账户状态 (Paper模式)"
    )
    parser.add_argument(
        "--buy",
        nargs=2,
        metavar=("SYMBOL", "SHARES"),
        help="买入股票 (Paper模式)"
    )
    parser.add_argument(
        "--sell",
        nargs="+",
        metavar=("SYMBOL", "SHARES"),
        help="卖出股票 (Paper模式，股数可选)"
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="查看交易历史 (Paper模式)"
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="自动交易模式 - 扫描自选股并执行买卖信号 (Paper模式，定时任务用)"
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="收盘快照 - 更新持仓现价并记录每日盈亏"
    )
    parser.add_argument(
        "--intraday",
        action="store_true",
        help="盘中异常风险检查 - 只减仓不加仓，检测跌停/放量暴跌/大盘风险"
    )

    # 回测命令
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="运行回测模拟，验证策略在历史数据上的表现"
    )
    parser.add_argument(
        "--backtest-start",
        default="2025-01-01",
        help="回测起始日期 (YYYY-MM-DD，默认2025-01-01)"
    )
    parser.add_argument(
        "--backtest-end",
        default=None,
        help="回测结束日期 (YYYY-MM-DD，默认最新数据)"
    )
    parser.add_argument(
        "--backtest-strategy",
        default="momentum_trend",
        help="回测策略 (默认momentum_trend)"
    )
    parser.add_argument(
        "--backtest-capital",
        type=float,
        default=100000,
        help="回测初始资金 (默认10万)"
    )
    parser.add_argument(
        "--backtest-symbols",
        default=None,
        help="回测股票列表，逗号分隔 (默认全部)"
    )
    parser.add_argument(
        "--backtest-output",
        default=None,
        help="导出结果到文件 (JSON/CSV路径)"
    )
    parser.add_argument(
        "--backtest-seed",
        type=int,
        default=None,
        help="随机种子，保证可复现"
    )
    parser.add_argument(
        "--backtest-filters",
        default=None,
        help="启用的过滤器，逗号分隔 (regime,volatility,defensive)，默认全部启用"
    )
    parser.add_argument(
        "--sync-history",
        type=int,
        default=0,
        metavar="DAYS",
        help="下载N天历史数据 (扩展回测数据窗口)"
    )

    args = parser.parse_args()

    # 处理数据同步命令（独立于交易模式）
    if args.sync or args.sync_incremental or args.sync_stats:
        return run_data_sync(args)

    # 处理尾盘预选命令（独立于交易模式）
    if args.preselect:
        return run_preselect(args)

    # 处理盘中风险检查命令（独立于交易模式）
    if args.intraday:
        return run_intraday_check(args)

    # 处理回测命令（独立于交易模式）
    if args.backtest:
        return run_backtest(args)

    # 处理历史数据同步命令
    if args.sync_history > 0:
        return run_sync_history(args)

    # 未指定同步命令时，必须指定 --mode
    if not args.mode:
        parser.error("请指定 --mode (live/paper) 或数据同步命令 (--sync/--sync-incremental/--sync-stats) 或尾盘预选 (--preselect)")
    
    # 交易操作加锁防并发（状态查询不加锁）
    is_trading_op = (
        args.mode == "live" or
        args.daily or
        args.buy is not None or
        args.sell is not None
    )
    if is_trading_op and not acquire_lock():
        return 0  # 已有进程在运行，静默退出
    
    # 根据模式分发
    try:
        if args.mode == "live":
            return run_live_mode(args)
        elif args.mode == "paper":
            return run_paper_mode(args)
        else:
            print(f"❌ 未知模式: {args.mode}")
            return 1
    finally:
        if is_trading_op:
            release_lock()


if __name__ == "__main__":
    sys.exit(main())
