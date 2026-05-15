#!/usr/bin/env python3
"""回测系统 - 历史数据验证策略有效性

核心设计：
- 复用 PaperTradingExecutor + RiskEngine + Strategy，不重写
- BacktestStorage: 内存 SQLite，隔离回测和实盘
- BacktestExecutor: 子类化 executor，注入模拟日期解决 T+1
- 严格逐日截断数据，杜绝未来函数
"""

import json
import csv
import math
import random
import logging
import sqlite3
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from scripts.execution.paper_executor import (
    PaperTradingStorage, PaperTradingExecutor, Position,
    TradeRecord, VirtualAccount, SlippageModel, TransactionCostCalculator
)
from scripts.core.risk_engine import RiskEngine, RiskConfig, Stage, RiskResult
from scripts.core.strategy import StrategyFactory
from scripts.core.indicators import TechnicalIndicators

logger = logging.getLogger(__name__)


# ============================================================
# Storage: 内存 SQLite，与 PaperTradingStorage 同 schema
# ============================================================

class BacktestStorage(PaperTradingStorage):
    """临时文件 SQLite storage，回测结束后自动清理。"""

    def __init__(self):
        # 用临时文件，避免内存数据库连接隔离问题
        self._tmpfile = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self._tmpfile.close()
        # 直接设置 db_path 后调 _init_db（PaperTradingStorage.__init__ 的逻辑）
        self.db_path = Path(self._tmpfile.name)
        self._init_db()

    def cleanup(self):
        """回测结束后清理临时文件。"""
        try:
            import os
            os.unlink(self._tmpfile.name)
        except OSError:
            pass


# ============================================================
# Executor: 子类化，注入模拟日期
# ============================================================

class BacktestExecutor(PaperTradingExecutor):
    """回测专用执行器，解决 datetime.now() 问题。"""

    def __init__(self, config: dict, storage: PaperTradingStorage,
                 risk_engine=None, simulated_date: str = "",
                 bt_state_provider=None):
        super().__init__(config, storage, risk_engine)
        self.simulated_date = simulated_date
        self._bt_state_provider = bt_state_provider

    def can_sell(self, symbol: str, shares: int) -> Tuple[bool, str]:
        """Override: 用 simulated_date 替代 datetime.now()"""
        position = self.storage.get_position(symbol)
        if position is None or position.shares == 0:
            return False, f"未持有股票: {symbol}"
        if position.shares < shares:
            return False, f"持仓不足: 持有{position.shares}股, 想卖{shares}股"

        buy_date = datetime.strptime(position.buy_date, "%Y-%m-%d").date()
        today = datetime.strptime(self.simulated_date, "%Y-%m-%d").date()
        if (today - buy_date).days < 1:
            can_sell_date = buy_date + timedelta(days=1)
            return False, f"T+1限制: 买入日期{position.buy_date}, 最早可卖日期{can_sell_date}"
        return True, "OK"

    def get_positions(self) -> List[Position]:
        """Override: 用 simulated_date 做 T+1 判断"""
        positions = self.storage.get_positions()
        today = datetime.strptime(self.simulated_date, "%Y-%m-%d").date()
        for pos in positions:
            buy_date = datetime.strptime(pos.buy_date, "%Y-%m-%d").date()
            pos.can_sell = (today - buy_date).days >= 1
        return positions

    def execute_sell(self, symbol: str, shares: int, signal_price: float,
                     volatility: float = 0.02) -> Tuple[bool, str, Optional[TradeRecord]]:
        """Override: 卖出后用模拟时间设置冷却期"""
        success, msg, trade = super().execute_sell(symbol, shares, signal_price, volatility)
        if success and trade and self.risk_engine:
            # 更新冷却期为模拟时间（而非 datetime.now()）
            state = self.risk_engine.state_provider()
            if state.get('consecutive_losses', 0) >= self.risk_engine.config.consecutive_loss_limit:
                # 用模拟日期设置 pause_start_time
                self._bt_state_provider.pause_start_time = (
                    datetime.strptime(self.simulated_date, "%Y-%m-%d").isoformat()
                )
        return success, msg, trade


# ============================================================
# RiskEngine: 子类化，用模拟时间做冷却期检查
# ============================================================

class BacktestRiskEngine(RiskEngine):
    """回测专用 RiskEngine，冷却期检查用模拟时间。"""

    def __init__(self, storage, state_provider, config=None, event_store=None,
                 simulated_date_provider=None):
        super().__init__(storage, state_provider, config, event_store)
        self.simulated_date_provider = simulated_date_provider  # 返回当前模拟日期的回调

    def _check_cooldown(self, state: Dict, result: RiskResult) -> RiskResult:
        """Override: 用模拟时间替代 datetime.now()"""
        consecutive_losses = state.get('consecutive_losses', 0)
        if consecutive_losses < self.config.consecutive_loss_limit:
            return result

        pause_start = state.get('pause_start_time')
        if not pause_start:
            return result

        try:
            pause_start_dt = datetime.fromisoformat(pause_start)
            cooldown_end = pause_start_dt + timedelta(hours=self.config.cooldown_hours)

            # 用模拟时间替代 datetime.now()
            if self.simulated_date_provider:
                sim_now = datetime.strptime(self.simulated_date_provider(), "%Y-%m-%d")
            else:
                sim_now = datetime.now()

            if sim_now < cooldown_end:
                remaining = cooldown_end - sim_now
                hours = remaining.seconds // 3600
                minutes = (remaining.seconds % 3600) // 60
                result.allowed = False
                result.reason = f"⏸️ 连续亏损{consecutive_losses}次，冷却期中（还剩{hours}小时{minutes}分钟）"
        except Exception as e:
            logger.warning(f"冷却期计算失败: {e}")

        return result


# ============================================================
# State Provider: 给 RiskEngine 用
# ============================================================

class BacktestStateProvider:
    """RiskEngine 的 state_provider 回调。"""

    def __init__(self):
        self.emergency_stop = False
        self.consecutive_losses = 0
        self.pause_start_time = None
        self.daily_pnl_pct = 0.0
        self.daily_trade_count = 0

    def __call__(self) -> Dict:
        return {
            'emergency_stop': self.emergency_stop,
            'consecutive_losses': self.consecutive_losses,
            'pause_start_time': self.pause_start_time,
            'daily_pnl_pct': self.daily_pnl_pct,
            'daily_trade_count': self.daily_trade_count,
        }

    def reset_daily(self):
        self.daily_pnl_pct = 0.0
        self.daily_trade_count = 0


# ============================================================
# BacktestResult: 绩效指标 + 输出
# ============================================================

@dataclass
class BacktestResult:
    """回测结果。"""

    # 配置
    strategy_name: str
    period: str
    initial_capital: float

    # 核心指标
    final_value: float = 0.0
    total_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_duration_days: int = 0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    avg_holding_days: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0

    # 交易统计
    total_trades: int = 0
    buy_trades: int = 0
    sell_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0

    # 明细
    trades: List[Dict] = field(default_factory=list)
    daily_values: List[Dict] = field(default_factory=list)
    daily_returns: List[float] = field(default_factory=list)
    candidate_snapshots: List[Dict] = field(default_factory=list)

    def print_summary(self):
        """打印回测摘要。"""
        print("\n" + "=" * 60)
        print(f"📊 回测报告 | {self.strategy_name}")
        print(f"📅 区间: {self.period}")
        print("=" * 60)

        print(f"\n💰 资金概况")
        print(f"  初始资金:    ¥{self.initial_capital:>12,.2f}")
        print(f"  最终资产:    ¥{self.final_value:>12,.2f}")
        print(f"  总收益率:      {self.total_return_pct:>+10.2f}%")
        print(f"  年化收益:      {self.annualized_return_pct:>+10.2f}%")

        print(f"\n📉 风险指标")
        print(f"  最大回撤:      {self.max_drawdown_pct:>10.2f}%")
        print(f"  回撤持续:      {self.max_drawdown_duration_days:>10}天")
        print(f"  Sharpe比率:    {self.sharpe_ratio:>10.2f}")
        print(f"  Sortino比率:   {self.sortino_ratio:>10.2f}")

        print(f"\n🎯 交易统计")
        print(f"  总交易次数:    {self.total_trades:>10}")
        print(f"  买入/卖出:     {self.buy_trades}/{self.sell_trades}")
        print(f"  胜率:          {self.win_rate_pct:>10.1f}%")
        print(f"  盈亏比:        {self.profit_factor:>10.2f}")
        print(f"  平均持仓:      {self.avg_holding_days:>10.1f}天")
        print(f"  平均盈利:      {self.avg_win_pct:>+10.2f}%")
        print(f"  平均亏损:      {self.avg_loss_pct:>+10.2f}%")
        print("=" * 60)

    def to_csv(self, path: str):
        """导出交易明细到 CSV。"""
        if not self.trades:
            print("无交易记录可导出")
            return
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=self.trades[0].keys())
            writer.writeheader()
            writer.writerows(self.trades)
        print(f"交易明细已导出: {path}")

    def to_json(self, path: str):
        """导出完整结果到 JSON。"""
        data = {
            'strategy': self.strategy_name,
            'period': self.period,
            'initial_capital': self.initial_capital,
            'final_value': self.final_value,
            'total_return_pct': self.total_return_pct,
            'annualized_return_pct': self.annualized_return_pct,
            'max_drawdown_pct': self.max_drawdown_pct,
            'sharpe_ratio': self.sharpe_ratio,
            'sortino_ratio': self.sortino_ratio,
            'win_rate_pct': self.win_rate_pct,
            'profit_factor': self.profit_factor,
            'avg_holding_days': self.avg_holding_days,
            'total_trades': self.total_trades,
            'trades': self.trades,
            'daily_values': self.daily_values,
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"完整结果已导出: {path}")

    def print_trade_distribution(self):
        """Trade Distribution Analysis — 统计什么因子区分赢家和亏家。"""
        sell_trades = [t for t in self.trades if t['action'] == 'SELL' and 'entry' in t]
        if not sell_trades:
            print("\n无带入场特征的卖出记录，无法分析")
            return

        wins = [t for t in sell_trades if t['pnl'] > 0]
        losses = [t for t in sell_trades if t['pnl'] <= 0]

        if not wins or not losses:
            print(f"\n赢家={len(wins)}, 亏家={len(losses)}，至少需要各1笔才能对比")
            return

        # 要分析的因子
        factors = ['score', 'base_score', 'rsi', 'atr_pct', 'volume_ratio',
                    'momentum_score', 'trend_strength', 'macd', 'change_pct',
                    'consecutive_up_days']

        print("\n" + "=" * 70)
        print("📊 Trade Distribution Analysis — 因子区分度")
        print("=" * 70)
        print(f"  赢家: {len(wins)}笔, 平均盈利: +{sum(t['pnl_pct'] for t in wins)/len(wins):.2f}%")
        print(f"  亏家: {len(losses)}笔, 平均亏损: {sum(t['pnl_pct'] for t in losses)/len(losses):.2f}%")
        print("-" * 70)
        print(f"  {'因子':<20} {'赢家均值':>10} {'亏家均值':>10} {'差值':>10} {'方向':>6}")
        print("-" * 70)

        for factor in factors:
            win_vals = [t['entry'].get(factor, 0) for t in wins if factor in t.get('entry', {})]
            loss_vals = [t['entry'].get(factor, 0) for t in losses if factor in t.get('entry', {})]
            if not win_vals or not loss_vals:
                continue
            win_avg = sum(win_vals) / len(win_vals)
            loss_avg = sum(loss_vals) / len(loss_vals)
            diff = win_avg - loss_avg
            # 判断因子方向：正值=赢家更高=好因子
            direction = "✅" if diff > 0 else "❌"
            print(f"  {factor:<20} {win_avg:>10.2f} {loss_avg:>10.2f} {diff:>+10.2f} {direction:>6}")

        print("=" * 70)
        print("  ✅ = 赢家更高（正向因子）  ❌ = 亏家更高（反向因子）")
        print("  差值越大 = 因子区分度越强")

    def print_score_bucket_analysis(self):
        """Score Bucket Analysis — 全候选池分层收益分析。"""
        snapshots = [s for s in self.candidate_snapshots if s.get('fwd_5d_return') is not None]
        if not snapshots:
            print("\n无候选池快照数据，无法分析")
            return

        # 等距分桶
        buckets = {}
        for s in snapshots:
            score = s['score']
            bucket = int(score // 10) * 10
            label = f"{bucket}-{bucket+10}"
            if label not in buckets:
                buckets[label] = []
            buckets[label].append(s)

        print("\n" + "=" * 70)
        print("📊 Score Bucket Analysis — 全候选池分层收益")
        print("=" * 70)
        print(f"  总候选数: {len(snapshots)}, 已成交: {sum(1 for s in snapshots if s.get('executed'))}")
        print("-" * 70)
        print(f"  {'Score区间':<12} {'数量':>6} {'5日收益':>10} {'10日收益':>10} {'成交率':>8}")
        print("-" * 70)

        for label in sorted(buckets.keys()):
            items = buckets[label]
            n = len(items)
            fwd5 = [s['fwd_5d_return'] for s in items if s['fwd_5d_return'] is not None]
            fwd10 = [s.get('fwd_10d_return') for s in items if s.get('fwd_10d_return') is not None]
            executed = sum(1 for s in items if s.get('executed'))

            avg5 = sum(fwd5) / len(fwd5) if fwd5 else 0
            avg10 = sum(fwd10) / len(fwd10) if fwd10 else 0
            exec_rate = executed / n * 100 if n > 0 else 0

            marker = "  ← top" if label.startswith("8") or label.startswith("9") else ""
            print(f"  {label:<12} {n:>6} {avg5:>+9.2f}% {avg10:>+9.2f}% {exec_rate:>7.1f}%{marker}")

        print("=" * 70)

        # 相关性：score 与 5日收益
        scores = [s['score'] for s in snapshots]
        returns = [s['fwd_5d_return'] for s in snapshots]
        if len(scores) > 10:
            mean_s = sum(scores) / len(scores)
            mean_r = sum(returns) / len(returns)
            cov = sum((s - mean_s) * (r - mean_r) for s, r in zip(scores, returns)) / len(scores)
            std_s = (sum((s - mean_s) ** 2 for s in scores) / len(scores)) ** 0.5
            std_r = (sum((r - mean_r) ** 2 for r in returns) / len(returns)) ** 0.5
            if std_s > 0 and std_r > 0:
                corr = cov / (std_s * std_r)
                print(f"  Score vs 5日收益 相关系数: {corr:.3f}", end="")
                if abs(corr) < 0.05:
                    print(" (几乎无关)")
                elif corr > 0.1:
                    print(" (正向 — score 有排序能力)")
                elif corr < -0.1:
                    print(" (反向 — score 可能反向)")
                else:
                    print(" (弱相关)")


# ============================================================
# BacktestEngine: 核心回测引擎
# ============================================================

class BacktestEngine:
    """回测引擎，复用现有策略+执行器+风控。"""

    def __init__(
        self,
        strategy_name: str = "momentum_trend",
        initial_capital: float = 100000,
        strategy_config: Optional[Dict] = None,
        risk_config: Optional[Dict] = None,
        seed: Optional[int] = None,
        max_positions: int = 5,
        index_symbol: str = "000300",
        filter_names: Optional[List[str]] = None,
    ):
        self.strategy_name = strategy_name
        self.initial_capital = initial_capital
        self.strategy_config = strategy_config or {}
        self.risk_config = risk_config or {}
        self.seed = seed
        self.max_positions = max_positions
        self.index_symbol = index_symbol
        self.filter_names = filter_names

        # 初始化 Filter Pipeline
        from scripts.core.filters import FilterFactory
        filters = FilterFactory.from_config(filter_names)

        # 初始化策略（不传 event_store，回测不需要）
        self.strategy = StrategyFactory.create(
            strategy_name, config=self.strategy_config, filters=filters
        )

        # 数据
        self._kline_data: Dict[str, List[Dict]] = {}   # symbol -> klines
        self._trading_days: List[str] = []              # 排序后的交易日
        self._index_klines: List[Dict] = []             # 指数 K 线

        # 运行时状态
        self._storage: Optional[BacktestStorage] = None
        self._executor: Optional[BacktestExecutor] = None
        self._state_provider: Optional[BacktestStateProvider] = None
        self._trades: List[Dict] = []
        self._daily_values: List[Dict] = []
        self._buy_dates: Dict[str, str] = {}  # symbol -> buy_date
        self._buy_prices: Dict[str, float] = {}  # symbol -> 实际买入均价
        self._entry_chars: Dict[str, Dict] = {}  # symbol -> 入场时的技术指标快照
        self._candidate_snapshots: List[Dict] = []  # 全候选池快照（含未成交）

    def load_data(
        self,
        start_date: str,
        end_date: Optional[str] = None,
        symbols: Optional[List[str]] = None,
        min_history_days: int = 60,
    ):
        """从 market_kline.db 加载数据。

        Args:
            start_date: 回测开始日期 YYYY-MM-DD
            end_date: 回测结束日期，默认最新
            symbols: 股票列表，None 表示全部
            min_history_days: 指标预热所需天数
        """
        db_path = Path.home() / ".openclaw/workspace/skills/LongToo-trader/data/market_kline.db"
        if not db_path.exists():
            raise FileNotFoundError(f"市场数据不存在: {db_path}，请先运行 --sync")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # 计算预热起始日（往前多取一些数据用于指标计算）
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        warmup_start = (start_dt - timedelta(days=min_history_days + 30)).strftime("%Y-%m-%d")
        if end_date is None:
            end_date = conn.execute(
                "SELECT MAX(trade_date) FROM daily_kline"
            ).fetchone()[0] or start_date

        # 加载股票列表
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            stock_rows = conn.execute(
                f"SELECT DISTINCT symbol FROM daily_kline WHERE symbol IN ({placeholders})",
                symbols
            ).fetchall()
        else:
            stock_rows = conn.execute(
                "SELECT DISTINCT symbol FROM daily_kline"
            ).fetchall()

        all_symbols = [r['symbol'] for r in stock_rows]
        logger.info(f"加载 {len(all_symbols)} 只股票数据 ({warmup_start} ~ {end_date})")

        # 批量加载 K 线
        for sym in all_symbols:
            rows = conn.execute(
                "SELECT trade_date, open, high, low, close, volume "
                "FROM daily_kline WHERE symbol = ? AND trade_date >= ? AND trade_date <= ? "
                "ORDER BY trade_date ASC",
                (sym, warmup_start, end_date)
            ).fetchall()
            if rows:
                self._kline_data[sym] = [dict(r) for r in rows]

        # 加载指数数据
        idx_rows = conn.execute(
            "SELECT trade_date, open, high, low, close, volume "
            "FROM daily_kline WHERE symbol = ? AND trade_date >= ? AND trade_date <= ? "
            "ORDER BY trade_date ASC",
            (self.index_symbol, warmup_start, end_date)
        ).fetchall()
        self._index_klines = [dict(r) for r in idx_rows]

        conn.close()

        # 构建交易日列表（从 start_date 开始）
        all_dates = set()
        for klines in self._kline_data.values():
            for k in klines:
                if k['trade_date'] >= start_date:
                    all_dates.add(k['trade_date'])
        self._trading_days = sorted(all_dates)

        # 过滤掉预热期不足的股票（至少30天历史数据）
        valid = {}
        for sym, klines in self._kline_data.items():
            warmup_count = sum(1 for k in klines if k['trade_date'] < start_date)
            if warmup_count >= 30:
                valid[sym] = klines
            elif len(klines) >= 30:
                # 预热不足但总数据够30天，保留但会在 _build_strategy_input 中跳过
                valid[sym] = klines
        self._kline_data = valid

        logger.info(
            f"数据加载完成: {len(self._kline_data)} 只股票, "
            f"{len(self._trading_days)} 个交易日"
        )

    def run(self) -> BacktestResult:
        """执行回测。"""
        if not self._trading_days:
            print("无交易日数据，请检查日期范围和数据是否已同步")
            return BacktestResult(
                strategy_name=self.strategy_name,
                period="N/A",
                initial_capital=self.initial_capital,
            )

        if self.seed is not None:
            random.seed(self.seed)

        # 初始化组件
        self._storage = BacktestStorage()
        self._storage.init_account(self.initial_capital)

        self._state_provider = BacktestStateProvider()

        risk_engine = BacktestRiskEngine(
            storage=self._storage,
            state_provider=self._state_provider,
            config={**self.risk_config, 'max_positions': self.max_positions},
            simulated_date_provider=lambda: self._executor.simulated_date if self._executor else self._trading_days[0],
        )

        config = {
            'initial_capital': self.initial_capital,
            'slippage': 0.001,
            'volatility_factor': 0.5,
            'max_positions': self.max_positions,
            'max_position_pct': 0.20,
        }
        self._executor = BacktestExecutor(
            config=config,
            storage=self._storage,
            risk_engine=risk_engine,
            simulated_date=self._trading_days[0],
            bt_state_provider=self._state_provider,
        )

        self._trades = []
        self._daily_values = []
        self._buy_dates = {}

        # 主循环
        total_days = len(self._trading_days)
        for day_idx, date in enumerate(self._trading_days):
            self._executor.simulated_date = date
            self._state_provider.reset_daily()

            if self.seed is not None:
                random.seed(self.seed + day_idx)

            self._simulate_day(date, day_idx)

            # 进度
            if (day_idx + 1) % 20 == 0 or day_idx == total_days - 1:
                acct = self._executor.get_account_status()
                logger.info(
                    f"  [{day_idx+1}/{total_days}] {date} "
                    f"资产=¥{acct.total_assets:,.2f} "
                    f"持仓={len(self._storage.get_positions())}"
                )

        # 计算前瞻收益（5日、10日）
        self._compute_forward_returns()

        result = self._build_result()

        # 清理临时文件
        if self._storage:
            self._storage.cleanup()

        return result

    # ----------------------------------------------------------
    # 每日模拟
    # ----------------------------------------------------------

    def _simulate_day(self, date: str, day_idx: int):
        """模拟一个交易日。"""
        # 1. 更新持仓价格
        prices = {}
        positions = self._storage.get_positions()
        for pos in positions:
            kline = self._get_kline(pos.symbol, date)
            if kline:
                prices[pos.symbol] = float(kline['close'])
        if prices:
            self._executor.update_market_prices(prices)

        # 2. 卖出检查（先卖后买）
        positions = self._storage.get_positions()
        for pos in positions:
            if pos.shares <= 0:
                continue
            kline = self._get_kline(pos.symbol, date)
            if not kline:
                continue
            # T+1: 当天买入的跳过
            if pos.buy_date == date:
                continue
            # 涨停不能卖（跌停 = change_pct <= -9.9%）
            # 实际跌停时很难卖出，但简化处理允许卖出

            strategy_input = self._build_strategy_input(
                pos.symbol, date, day_idx, pos
            )
            if not strategy_input:
                continue

            result = self.strategy.analyze(strategy_input)
            if result.get('signal') == 'sell':
                shares = result.get('action_shares', pos.shares)
                success, msg, trade = self._executor.execute_sell(
                    pos.symbol, shares, float(kline['close'])
                )
                if success and trade:
                    self._record_trade(trade, date, result.get('reason', ''))
                    # 连续亏损计数
                    if trade.tax > 0:  # 卖出有税 = 已实现
                        pnl = trade.amount - trade.total_cost
                        if pnl < 0:
                            self._state_provider.consecutive_losses += 1
                        else:
                            self._state_provider.consecutive_losses = 0

        # 3. 买入检查
        current_positions = len([p for p in self._storage.get_positions() if p.shares > 0])
        if current_positions < self.max_positions:
            market_state = self._get_market_state(date)

            # 排序保证可复现
            candidates = sorted(
                s for s in self._kline_data.keys()
                if s not in {p.symbol for p in self._storage.get_positions() if p.shares > 0}
            )

            for symbol in candidates:
                if current_positions >= self.max_positions:
                    break

                kline = self._get_kline(symbol, date)
                if not kline:
                    continue
                # 停牌跳过
                if int(kline.get('volume', 0) or 0) == 0:
                    continue

                # 涨停不能买
                prev_close = self._get_prev_close(symbol, date)
                if prev_close and prev_close > 0:
                    change_pct = (float(kline['close']) - prev_close) / prev_close * 100
                    if change_pct >= 9.9:
                        continue

                strategy_input = self._build_strategy_input(symbol, date, day_idx)
                if not strategy_input:
                    continue

                result = self.strategy.analyze(strategy_input, market_state=market_state)
                if result.get('signal') == 'buy':
                    # 记录全候选池快照（含未成交的）
                    self._candidate_snapshots.append({
                        'date': date,
                        'symbol': symbol,
                        'score': result.get('score', 0),
                        'base_score': result.get('conditions', {}).get('base_score', 0),
                        'price': float(kline['close']),
                        'rsi': strategy_input.get('rsi', 50),
                        'atr_pct': strategy_input.get('atr', 0) / strategy_input.get('price', 1) * 100,
                        'volume_ratio': strategy_input.get('volume_ratio', 1),
                        'momentum_score': strategy_input.get('momentum_score', 0),
                        'trend_strength': strategy_input.get('trend_strength', 0),
                        'macd': strategy_input.get('macd', 0),
                        'macd_signal': strategy_input.get('macd_signal', 0),
                        'change_pct': strategy_input.get('change_pct', 0),
                        'market_state': market_state or 'unknown',
                        'ma10': strategy_input.get('ma10'),
                        'ma30': strategy_input.get('ma30'),
                        'consecutive_up_days': strategy_input.get('consecutive_up_days', 0),
                        'consecutive_down_days': strategy_input.get('consecutive_down_days', 0),
                        'executed': False,  # 后续标记是否成交
                    })

                    stop_loss = result.get('conditions', {}).get('stop_loss', 0)
                    success, msg, trade = self._executor.execute_buy(
                        symbol, result['action_shares'], float(kline['close']),
                        stop_loss=stop_loss
                    )
                    if success and trade:
                        # 标记快照为已成交
                        for snap in reversed(self._candidate_snapshots):
                            if snap['symbol'] == symbol and snap['date'] == date:
                                snap['executed'] = True
                                break
                        # 修复 buy_date 为模拟日期
                        pos = self._storage.get_position(symbol)
                        if pos:
                            pos.buy_date = date
                            self._storage.update_position(pos)
                        # 保存入场特征（用于 Trade Distribution Analysis）
                        self._entry_chars[symbol] = {
                            'score': result.get('score', 0),
                            'base_score': result.get('conditions', {}).get('base_score', 0),
                            'rsi': strategy_input.get('rsi', 50),
                            'atr': strategy_input.get('atr', 0),
                            'atr_pct': strategy_input.get('atr', 0) / strategy_input.get('price', 1) * 100,
                            'volume_ratio': strategy_input.get('volume_ratio', 1),
                            'momentum_score': strategy_input.get('momentum_score', 0),
                            'trend_strength': strategy_input.get('trend_strength', 0),
                            'macd': strategy_input.get('macd', 0),
                            'change_pct': strategy_input.get('change_pct', 0),
                            'consecutive_up_days': strategy_input.get('consecutive_up_days', 0),
                            'consecutive_down_days': strategy_input.get('consecutive_down_days', 0),
                            'market_state': market_state or 'unknown',
                        }
                        self._record_trade(trade, date, result.get('reason', ''))
                        self._buy_dates[symbol] = date
                        # 记录实际买入均价（含滑点）
                        if symbol not in self._buy_prices:
                            self._buy_prices[symbol] = trade.price
                        else:
                            # 加仓：加权平均
                            old_shares = pos.shares - trade.shares if pos else 0
                            old_cost = self._buy_prices[symbol] * old_shares
                            new_cost = trade.price * trade.shares
                            total = old_shares + trade.shares
                            self._buy_prices[symbol] = (old_cost + new_cost) / total if total > 0 else trade.price
                        current_positions += 1
                        self._state_provider.daily_trade_count += 1

        # 4. 记录每日快照
        account = self._executor.get_account_status()
        positions_value = sum(
            p.market_value for p in self._storage.get_positions() if p.shares > 0
        )
        self._daily_values.append({
            'date': date,
            'cash': account.cash,
            'positions_value': positions_value,
            'total_value': account.total_assets,
            'positions_count': len([p for p in self._storage.get_positions() if p.shares > 0]),
        })

    # ----------------------------------------------------------
    # 前瞻收益计算
    # ----------------------------------------------------------

    def _compute_forward_returns(self):
        """为每个 candidate snapshot 计算 5日/10日 前瞻收益。"""
        for snap in self._candidate_snapshots:
            symbol = snap['symbol']
            date = snap['date']
            klines = self._kline_data.get(symbol, [])

            # 找到信号日的索引
            idx = None
            for i, k in enumerate(klines):
                if k['trade_date'] == date:
                    idx = i
                    break
            if idx is None:
                continue

            entry_price = float(klines[idx]['close'])

            # 5日前瞻收益
            if idx + 5 < len(klines):
                fwd5_price = float(klines[idx + 5]['close'])
                snap['fwd_5d_return'] = round((fwd5_price / entry_price - 1) * 100, 2)
            else:
                snap['fwd_5d_return'] = None

            # 10日前瞻收益
            if idx + 10 < len(klines):
                fwd10_price = float(klines[idx + 10]['close'])
                snap['fwd_10d_return'] = round((fwd10_price / entry_price - 1) * 100, 2)
            else:
                snap['fwd_10d_return'] = None

    # ----------------------------------------------------------
    # 数据辅助
    # ----------------------------------------------------------

    def _get_kline(self, symbol: str, date: str) -> Optional[Dict]:
        """获取某只股票某天的 K 线。"""
        klines = self._kline_data.get(symbol, [])
        # 二分查找优化
        lo, hi = 0, len(klines) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if klines[mid]['trade_date'] == date:
                return klines[mid]
            elif klines[mid]['trade_date'] < date:
                lo = mid + 1
            else:
                hi = mid - 1
        return None

    def _get_prev_close(self, symbol: str, date: str) -> Optional[float]:
        """获取前一交易日收盘价。"""
        klines = self._kline_data.get(symbol, [])
        for i, k in enumerate(klines):
            if k['trade_date'] == date and i > 0:
                return float(klines[i - 1]['close'])
        return None

    def _get_kline_history(self, symbol: str, date: str, include_current: bool = True) -> List[Dict]:
        """获取截止到 date 的全部 K 线（严格不包含未来数据）。"""
        klines = self._kline_data.get(symbol, [])
        result = []
        for k in klines:
            if k['trade_date'] < date:
                result.append(k)
            elif k['trade_date'] == date and include_current:
                result.append(k)
            else:
                break
        return result

    def _get_index_history(self, date: str) -> List[Dict]:
        """获取指数截止到 date 的 K 线。"""
        result = []
        for k in self._index_klines:
            if k['trade_date'] <= date:
                result.append(k)
            else:
                break
        return result

    def _get_market_state(self, date: str) -> Optional[str]:
        """获取市场状态（strong/neutral/weak）。"""
        idx_history = self._get_index_history(date)
        if len(idx_history) < 60:
            return None
        try:
            from scripts.core.market_regime import MarketRegime
            regime = MarketRegime()
            # 构造 regime.detect() 需要的数据
            prices = [float(k['close']) for k in idx_history]
            ma20 = TechnicalIndicators.calculate_ma(prices, 20)
            ma60 = TechnicalIndicators.calculate_ma(prices, 60)
            current = prices[-1]
            if ma20 and ma60:
                if current > ma60 and ma20 > ma60:
                    return 'strong'
                elif current < ma60 and ma20 < ma60:
                    return 'weak'
            return 'neutral'
        except Exception:
            return None

    def _calc_trading_days(self, buy_date: str, current_date: str) -> int:
        """计算两个日期之间的交易日天数。"""
        buy_dt = datetime.strptime(buy_date, "%Y-%m-%d")
        cur_dt = datetime.strptime(current_date, "%Y-%m-%d")
        # 简化：用自然日 * 5/7 近似
        delta = (cur_dt - buy_dt).days
        return max(0, int(delta * 5 / 7))

    # ----------------------------------------------------------
    # 构造策略输入
    # ----------------------------------------------------------

    def _build_strategy_input(
        self,
        symbol: str,
        date: str,
        day_idx: int,
        position: Optional[Position] = None,
    ) -> Optional[Dict]:
        """构造 strategy.analyze() 需要的 data dict。

        严格使用截止到 date 的历史数据，杜绝未来函数。
        """
        history = self._get_kline_history(symbol, date, include_current=True)
        if len(history) < 30:
            return None

        prices = [float(k['close']) for k in history]
        current = history[-1]
        prev = history[-2] if len(history) > 1 else current

        current_price = float(current['close'])

        # 技术指标
        ma10 = TechnicalIndicators.calculate_ma(prices, 10)
        ma30 = TechnicalIndicators.calculate_ma(prices, 30)
        atr = TechnicalIndicators.calculate_atr(history, 14)
        rsi = TechnicalIndicators.calculate_rsi(prices, 14)
        macd, macd_signal, _ = TechnicalIndicators.calculate_macd(prices)
        momentum_score = TechnicalIndicators.calculate_momentum_score(
            current_price, float(prev['close'])
        )
        trend_strength = TechnicalIndicators.get_trend_strength(history)

        # 成交量比
        vol_20 = [int(k['volume'] or 0) for k in history[-20:]]
        avg_vol = sum(vol_20) / len(vol_20) if vol_20 else 1
        volume_ratio = int(current['volume'] or 0) / avg_vol if avg_vol > 0 else 1.0

        # 涨跌幅
        prev_close = float(prev['close'])
        change_pct = (current_price - prev_close) / prev_close * 100 if prev_close > 0 else 0

        # 连涨/连跌天数
        consec_up, consec_down = 0, 0
        for i in range(len(history) - 1, max(0, len(history) - 10), -1):
            c = float(history[i]['close'])
            p = float(history[i - 1]['close'])
            if c > p:
                if consec_down > 0:
                    break
                consec_up += 1
            elif c < p:
                if consec_up > 0:
                    break
                consec_down += 1
            else:
                break

        data = {
            'symbol': symbol,
            'price': current_price,
            'ma10': ma10,
            'ma30': ma30,
            'atr': atr or current_price * 0.02,
            'rsi': rsi if rsi is not None else 50,
            'macd': macd or 0,
            'macd_signal': macd_signal or 0,
            'momentum_score': momentum_score,
            'trend_strength': trend_strength,
            'volume_ratio': volume_ratio,
            'change_pct': change_pct,
            'consecutive_up_days': consec_up,
            'consecutive_down_days': consec_down,
            'cash': self._executor.get_account_status().cash,
            'holding_shares': 0,
            'avg_cost': 0,
            'highest_price_since_entry': current_price,
            'trading_days': 0,
        }

        if position and position.shares > 0:
            data['holding_shares'] = position.shares
            data['avg_cost'] = position.avg_cost
            data['highest_price_since_entry'] = position.peak_price or current_price
            data['trading_days'] = self._calc_trading_days(position.buy_date, date)
            data['stored_stop_loss'] = position.stop_loss if position.stop_loss > 0 else None

        return data

    # ----------------------------------------------------------
    # 记录 + 指标计算
    # ----------------------------------------------------------

    def _record_trade(self, trade: TradeRecord, date: str, reason: str):
        """记录交易到明细列表。"""
        pnl = 0.0
        pnl_pct = 0.0
        entry_chars = {}
        if trade.action == 'SELL':
            # 用实际买入均价计算盈亏
            buy_price = self._buy_prices.get(trade.symbol, 0)
            if buy_price > 0:
                gross_pnl = (trade.price - buy_price) * trade.shares
                pnl = gross_pnl - trade.commission - trade.tax
                pnl_pct = (trade.price / buy_price - 1) * 100
            # 带入入场特征
            entry_chars = self._entry_chars.pop(trade.symbol, {})
            # 清仓后清除买入价格记录
            if trade.shares > 0:
                pos = self._storage.get_position(trade.symbol)
                if not pos or pos.shares == 0:
                    self._buy_prices.pop(trade.symbol, None)

        record = {
            'date': date,
            'symbol': trade.symbol,
            'action': trade.action,
            'shares': trade.shares,
            'price': round(trade.price, 2),
            'amount': round(trade.amount, 2),
            'commission': round(trade.commission, 2),
            'tax': round(trade.tax, 2),
            'slippage': round(trade.slippage, 4),
            'reason': reason,
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct, 2),
        }
        # 卖出时带入入场特征（用于 Trade Distribution Analysis）
        if entry_chars:
            record['entry'] = entry_chars
        self._trades.append(record)

    def _build_result(self) -> BacktestResult:
        """计算绩效指标，构建回测结果。"""
        period = f"{self._trading_days[0]} ~ {self._trading_days[-1]}"
        final_value = self._daily_values[-1]['total_value'] if self._daily_values else self.initial_capital

        # 日收益率
        daily_returns = []
        for i in range(1, len(self._daily_values)):
            prev_val = self._daily_values[i - 1]['total_value']
            curr_val = self._daily_values[i]['total_value']
            if prev_val > 0:
                daily_returns.append((curr_val - prev_val) / prev_val)

        # 总收益
        total_return = (final_value - self.initial_capital) / self.initial_capital
        trading_days = len(self._daily_values)
        annualized = (1 + total_return) ** (252 / max(trading_days, 1)) - 1

        # 最大回撤
        peak = self.initial_capital
        max_dd = 0.0
        dd_start = 0
        max_dd_duration = 0
        current_dd_start = 0
        for i, dv in enumerate(self._daily_values):
            val = dv['total_value']
            if val > peak:
                peak = val
                current_dd_start = i
            dd = (peak - val) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
                max_dd_duration = i - current_dd_start

        # Sharpe
        sharpe = 0.0
        sortino = 0.0
        if daily_returns:
            mean_ret = sum(daily_returns) / len(daily_returns)
            std_ret = (sum((r - mean_ret) ** 2 for r in daily_returns) / len(daily_returns)) ** 0.5
            if std_ret > 0:
                sharpe = (mean_ret * 252 - 0.02) / (std_ret * (252 ** 0.5))

            # Sortino
            downside = [r for r in daily_returns if r < 0]
            if downside:
                down_std = (sum(r ** 2 for r in downside) / len(downside)) ** 0.5
                if down_std > 0:
                    sortino = (mean_ret * 252 - 0.02) / (down_std * (252 ** 0.5))

        # 交易统计
        sell_trades = [t for t in self._trades if t['action'] == 'SELL']
        buy_trades = [t for t in self._trades if t['action'] == 'BUY']
        wins = [t for t in sell_trades if t.get('pnl', 0) > 0]
        losses = [t for t in sell_trades if t.get('pnl', 0) <= 0]
        win_rate = len(wins) / len(sell_trades) * 100 if sell_trades else 0

        total_win = sum(t['pnl'] for t in wins)
        total_loss = abs(sum(t['pnl'] for t in losses))
        profit_factor = total_win / total_loss if total_loss > 0 else float('inf')

        avg_win_pct = sum(t['pnl_pct'] for t in wins) / len(wins) if wins else 0
        avg_loss_pct = sum(t['pnl_pct'] for t in losses) / len(losses) if losses else 0

        # 平均持仓天数
        holding_periods = []
        for t in sell_trades:
            sym = t['symbol']
            if sym in self._buy_dates:
                bd = datetime.strptime(self._buy_dates[sym], "%Y-%m-%d")
                sd = datetime.strptime(t['date'], "%Y-%m-%d")
                holding_periods.append((sd - bd).days)
        avg_holding = sum(holding_periods) / len(holding_periods) if holding_periods else 0

        return BacktestResult(
            strategy_name=self.strategy_name,
            period=period,
            initial_capital=self.initial_capital,
            final_value=round(final_value, 2),
            total_return_pct=round(total_return * 100, 2),
            annualized_return_pct=round(annualized * 100, 2),
            max_drawdown_pct=round(max_dd * 100, 2),
            max_drawdown_duration_days=max_dd_duration,
            sharpe_ratio=round(sharpe, 2),
            sortino_ratio=round(sortino, 2),
            win_rate_pct=round(win_rate, 1),
            profit_factor=round(profit_factor, 2),
            avg_holding_days=round(avg_holding, 1),
            avg_win_pct=round(avg_win_pct, 2),
            avg_loss_pct=round(avg_loss_pct, 2),
            total_trades=len(self._trades),
            buy_trades=len(buy_trades),
            sell_trades=len(sell_trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            trades=self._trades,
            daily_values=self._daily_values,
            daily_returns=daily_returns,
            candidate_snapshots=self._candidate_snapshots,
        )
