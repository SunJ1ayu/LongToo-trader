#!/usr/bin/env python3
"""
Paper Trading Executor - 模拟盘执行器
使用虚拟资金进行交易模拟，独立于实盘系统

特性：
- 虚拟账户管理（现金、持仓、总资产）
- T+1 交易规则
- 真实手续费模拟（佣金、印花税）
- 滑点模型
- SQLite 持久化
"""

import sqlite3
import random
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class VirtualAccount:
    """虚拟账户状态"""
    cash: float  # 可用现金
    total_assets: float  # 总资产（现金+持仓市值）
    initial_capital: float  # 初始资金
    updated_at: str
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class Position:
    """持仓信息"""
    symbol: str  # 股票代码
    shares: int  # 持股数量
    avg_cost: float  # 平均成本
    current_price: float  # 当前价格
    market_value: float  # 市值
    pnl: float  # 盈亏金额
    pnl_pct: float  # 盈亏比例
    can_sell: bool  # 是否可卖（T+1）
    buy_date: str  # 买入日期
    stop_loss: float = 0.0  # 止损价
    # 盈利保护字段
    peak_pnl: float = 0.0  # 历史最高盈利（immutable）
    peak_price: float = 0.0  # 历史最高价格
    add_count: int = 0  # 加仓轮次（未来用）
    reduced_from_peak: bool = False  # 是否已触发过减仓保护
    last_reduce_at: str = ""  # 最后减仓日期

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class TradeRecord:
    """交易记录"""
    id: Optional[int]
    symbol: str
    action: str  # BUY/SELL
    shares: int
    price: float
    amount: float
    commission: float  # 佣金
    tax: float  # 印花税
    total_cost: float  # 总成本
    timestamp: str
    signal_price: float  # 信号价格（用于计算滑点）
    slippage: float  # 滑点金额
    
    def to_dict(self) -> Dict:
        return asdict(self)


class PaperTradingStorage:
    """模拟盘数据存储（SQLite）"""
    
    def __init__(self, db_path: str = "~/.openclaw/workspace/skills/LongToo-trader/data/paper_trading.db"):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """初始化数据库表"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # 账户表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cash REAL NOT NULL,
                    total_assets REAL NOT NULL,
                    initial_capital REAL NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 持仓表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    shares INTEGER NOT NULL,
                    avg_cost REAL NOT NULL,
                    current_price REAL NOT NULL,
                    market_value REAL NOT NULL,
                    pnl REAL DEFAULT 0,
                    pnl_pct REAL DEFAULT 0,
                    can_sell BOOLEAN DEFAULT 0,
                    buy_date DATE NOT NULL,
                    stop_loss REAL DEFAULT 0,
                    peak_pnl REAL DEFAULT 0,
                    peak_price REAL DEFAULT 0,
                    add_count INTEGER DEFAULT 0,
                    reduced_from_peak BOOLEAN DEFAULT 0,
                    last_reduce_at DATE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 交易记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
                    shares INTEGER NOT NULL,
                    price REAL NOT NULL,
                    amount REAL NOT NULL,
                    commission REAL NOT NULL,
                    tax REAL NOT NULL DEFAULT 0,
                    total_cost REAL NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    signal_price REAL,
                    slippage REAL DEFAULT 0
                )
            """)

            # 每日盈亏表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_pnl (
                    date DATE PRIMARY KEY,
                    cash REAL NOT NULL,
                    positions_value REAL NOT NULL,
                    total_assets REAL NOT NULL,
                    daily_pnl REAL NOT NULL,
                    daily_pnl_pct REAL NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 数据库迁移：逐字段检查并添加（更健壮）
            migrations = [
                ("peak_pnl", "REAL DEFAULT 0"),
                ("peak_price", "REAL DEFAULT 0"),
                ("add_count", "INTEGER DEFAULT 0"),
                ("reduced_from_peak", "BOOLEAN DEFAULT 0"),
                ("last_reduce_at", "DATE"),
            ]
            for col_name, col_type in migrations:
                try:
                    cursor.execute(f"SELECT {col_name} FROM positions LIMIT 1")
                except sqlite3.OperationalError:
                    logger.info(f"数据库迁移：添加字段 {col_name}")
                    cursor.execute(f"ALTER TABLE positions ADD COLUMN {col_name} {col_type}")

            conn.commit()
            logger.info(f"数据库初始化完成: {self.db_path}")
    
    def init_account(self, initial_capital: float):
        """初始化账户"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO account (id, cash, total_assets, initial_capital, updated_at)
                VALUES (1, ?, ?, ?, ?)
            """, (initial_capital, initial_capital, initial_capital, datetime.now().isoformat()))
            conn.commit()
            logger.info(f"账户初始化: 初始资金 ¥{initial_capital:,.2f}")
    
    def get_account(self) -> Optional[VirtualAccount]:
        """获取账户状态"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT cash, total_assets, initial_capital, updated_at FROM account WHERE id = 1")
            row = cursor.fetchone()
            if row:
                return VirtualAccount(cash=row[0], total_assets=row[1], 
                                    initial_capital=row[2], updated_at=row[3])
            return None
    
    def update_account(self, cash: float, total_assets: float):
        """更新账户状态"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE account SET cash = ?, total_assets = ?, updated_at = ? WHERE id = 1
            """, (cash, total_assets, datetime.now().isoformat()))
            conn.commit()
    
    def get_positions(self) -> List[Position]:
        """获取所有持仓"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT symbol, shares, avg_cost, current_price, market_value,
                       pnl, pnl_pct, can_sell, buy_date, stop_loss,
                       peak_pnl, peak_price, add_count, reduced_from_peak, last_reduce_at
                FROM positions WHERE shares > 0
            """)
            rows = cursor.fetchall()
            positions = []
            for row in rows:
                positions.append(Position(
                    symbol=row[0], shares=row[1], avg_cost=row[2], current_price=row[3],
                    market_value=row[4], pnl=row[5], pnl_pct=row[6],
                    can_sell=bool(row[7]), buy_date=row[8], stop_loss=row[9] if row[9] else 0.0,
                    peak_pnl=row[10] if row[10] else 0.0, peak_price=row[11] if row[11] else 0.0,
                    add_count=row[12] if row[12] else 0, reduced_from_peak=bool(row[13]) if row[13] is not None else False,
                    last_reduce_at=row[14] if row[14] else ""
                ))
            return positions
    
    def get_position(self, symbol: str) -> Optional[Position]:
        """获取单只股票持仓"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT symbol, shares, avg_cost, current_price, market_value,
                       pnl, pnl_pct, can_sell, buy_date, stop_loss,
                       peak_pnl, peak_price, add_count, reduced_from_peak, last_reduce_at
                FROM positions WHERE symbol = ?
            """, (symbol,))
            row = cursor.fetchone()
            if row:
                return Position(
                    symbol=row[0], shares=row[1], avg_cost=row[2], current_price=row[3],
                    market_value=row[4], pnl=row[5], pnl_pct=row[6],
                    can_sell=bool(row[7]), buy_date=row[8], stop_loss=row[9] if row[9] else 0.0,
                    peak_pnl=row[10] if row[10] else 0.0, peak_price=row[11] if row[11] else 0.0,
                    add_count=row[12] if row[12] else 0, reduced_from_peak=bool(row[13]) if row[13] is not None else False,
                    last_reduce_at=row[14] if row[14] else ""
                )
            return None

    def update_position(self, position: Position):
        """更新持仓"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO positions
                (symbol, shares, avg_cost, current_price, market_value, pnl, pnl_pct,
                 can_sell, buy_date, stop_loss, peak_pnl, peak_price, add_count,
                 reduced_from_peak, last_reduce_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (position.symbol, position.shares, position.avg_cost, position.current_price,
                  position.market_value, position.pnl, position.pnl_pct,
                  position.can_sell, position.buy_date, position.stop_loss,
                  position.peak_pnl, position.peak_price, position.add_count,
                  position.reduced_from_peak, position.last_reduce_at, datetime.now().isoformat()))
            conn.commit()
    
    def record_trade(self, trade: TradeRecord):
        """记录交易"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trades (symbol, action, shares, price, amount, commission, tax, total_cost, signal_price, slippage)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (trade.symbol, trade.action, trade.shares, trade.price, trade.amount,
                  trade.commission, trade.tax, trade.total_cost, trade.signal_price, trade.slippage))
            conn.commit()
            logger.info(f"交易记录已保存: {trade.action} {trade.symbol} {trade.shares}股")
    
    def get_trades(self, limit: int = 100) -> List[TradeRecord]:
        """获取交易记录"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, symbol, action, shares, price, amount, commission, tax, total_cost, timestamp, signal_price, slippage
                FROM trades ORDER BY timestamp DESC LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            trades = []
            for row in rows:
                trades.append(TradeRecord(
                    id=row[0], symbol=row[1], action=row[2], shares=row[3], price=row[4],
                    amount=row[5], commission=row[6], tax=row[7], total_cost=row[8],
                    timestamp=row[9], signal_price=row[10], slippage=row[11]
                ))
            return trades


class SlippageModel:
    """滑点模型 - 模拟真实成交偏差"""
    
    def __init__(self, base_slippage: float = 0.001, volatility_factor: float = 0.5):
        """
        Args:
            base_slippage: 基础滑点（0.001 = 0.1%）
            volatility_factor: 波动率因子
        """
        self.base_slippage = base_slippage
        self.volatility_factor = volatility_factor
    
    def calculate(self, signal_price: float, action: str, volatility: float = 0.02) -> Tuple[float, float]:
        """
        计算实际成交价和滑点
        
        Returns:
            (actual_price, slippage_amount)
        """
        # 根据波动率调整滑点
        adjusted_slippage = self.base_slippage * (1 + volatility * self.volatility_factor)
        
        # 添加随机性（正态分布）
        random_factor = random.gauss(0, 0.3)  # 均值0，标准差0.3
        final_slippage = adjusted_slippage * (1 + random_factor)
        
        # 买入滑点：成交价高于信号价
        # 卖出滑点：成交价低于信号价
        if action == "BUY":
            actual_price = signal_price * (1 + final_slippage)
        else:  # SELL
            actual_price = signal_price * (1 - final_slippage)
        
        slippage_amount = abs(actual_price - signal_price)
        
        return round(actual_price, 2), round(slippage_amount, 2)


class TransactionCostCalculator:
    """交易成本计算器"""
    
    # A股交易费用标准
    BUY_COMMISSION_RATE = 0.0003  # 买入佣金：万3
    SELL_COMMISSION_RATE = 0.0003  # 卖出佣金：万3
    STAMP_TAX_RATE = 0.001  # 印花税：千1（仅卖出）
    MIN_COMMISSION = 5  # 最低佣金5元
    
    @classmethod
    def calculate_buy_cost(cls, amount: float) -> Tuple[float, float]:
        """计算买入成本
        Returns: (commission, total_cost)
        """
        commission = max(amount * cls.BUY_COMMISSION_RATE, cls.MIN_COMMISSION)
        total_cost = amount + commission
        return round(commission, 2), round(total_cost, 2)
    
    @classmethod
    def calculate_sell_cost(cls, amount: float) -> Tuple[float, float, float]:
        """计算卖出成本
        Returns: (commission, tax, total_revenue)
        """
        commission = max(amount * cls.SELL_COMMISSION_RATE, cls.MIN_COMMISSION)
        tax = amount * cls.STAMP_TAX_RATE  # 印花税
        total_revenue = amount - commission - tax
        return round(commission, 2), round(tax, 2), round(total_revenue, 2)


class PaperTradingExecutor:
    """模拟盘执行器 - 核心类"""

    def __init__(self, config: Dict, storage: Optional[PaperTradingStorage] = None, risk_engine=None):
        """
        Args:
            config: 配置字典
            storage: 数据存储实例（可选，默认创建新实例）
            risk_engine: RiskEngine 实例（v2.4.0 中央化风控）
        """
        self.config = config
        self.storage = storage or PaperTradingStorage()
        self.slippage_model = SlippageModel(
            base_slippage=config.get("slippage", 0.001),
            volatility_factor=config.get("volatility_factor", 0.5)
        )

        # v2.4.0: RiskEngine 中央化（最终硬阻断）
        self.risk_engine = risk_engine

        # 初始化账户（如果是新数据库）
        self._init_account_if_needed()
    
    def _init_account_if_needed(self):
        """如果账户不存在，初始化"""
        account = self.storage.get_account()
        if account is None:
            initial_capital = self.config.get("initial_capital", 100000)
            self.storage.init_account(initial_capital)
            logger.info(f"模拟盘账户已初始化: ¥{initial_capital:,.2f}")
    
    def get_account_status(self) -> VirtualAccount:
        """获取账户状态"""
        account = self.storage.get_account()
        if account is None:
            raise RuntimeError("账户未初始化")
        return account
    
    def get_positions(self) -> List[Position]:
        """获取当前持仓"""
        positions = self.storage.get_positions()
        # 更新T+1状态（检查是否已可卖）
        today = datetime.now().date()
        for pos in positions:
            buy_date = datetime.strptime(pos.buy_date, "%Y-%m-%d").date()
            pos.can_sell = (today - buy_date).days >= 1
        return positions
    
    def can_buy(self, symbol: str, shares: int, price: float) -> Tuple[bool, str]:
        """检查是否可以买入（最终硬阻断）

        v2.4.0: 优先使用 RiskEngine 中央化，避免规则漂移
        """
        # === 优先使用 RiskEngine ===
        if self.risk_engine is not None:
            from scripts.core.risk_engine import Stage

            result = self.risk_engine.validate(
                symbol=symbol,
                action="buy",
                shares=shares,
                price=price,
                stage=Stage.HARD_BLOCK.value
            )

            if not result.allowed:
                return False, result.reason

            # 如果截断了股数，返回调整后的值（但实际上 execute_buy 会重新计算）
            if result.adjusted_shares:
                # 这里只做检查，不改变 shares（execute_buy 会处理截断）
                pass

            return True, "OK"

        # === 降级模式：原有逻辑 ===
        account = self.get_account_status()
        amount = shares * price
        commission, total_cost = TransactionCostCalculator.calculate_buy_cost(amount)

        if account.cash < total_cost:
            return False, f"资金不足: 需要¥{total_cost:,.2f}, 可用¥{account.cash:,.2f}"

        # 检查持仓数量限制
        max_positions = self.config.get("max_positions", 5)  # 默认最多5只
        current_positions = len(self.get_positions())
        position = self.storage.get_position(symbol)
        if position is None and current_positions >= max_positions:
            return False, f"持仓数量已达上限: {max_positions}"

        # 检查单只股票仓位限制
        max_position_pct = self.config.get("max_position_pct", 20)
        max_position_value = account.total_assets * (max_position_pct / 100)
        if position:
            new_value = (position.shares + shares) * price
        else:
            new_value = amount
        if new_value > max_position_value:
            return False, f"单只股票仓位超限: 最大¥{max_position_value:,.2f}"

        return True, "OK"
    
    def can_sell(self, symbol: str, shares: int) -> Tuple[bool, str]:
        """检查是否可以卖出（T+1检查）"""
        position = self.storage.get_position(symbol)
        if position is None or position.shares == 0:
            return False, f"未持有股票: {symbol}"
        
        if position.shares < shares:
            return False, f"持仓不足: 持有{position.shares}股, 想卖{shares}股"
        
        # T+1检查 - 动态计算（不依赖数据库中的 can_sell 字段）
        buy_date = datetime.strptime(position.buy_date, "%Y-%m-%d").date()
        today = datetime.now().date()
        if (today - buy_date).days < 1:
            can_sell_date = buy_date + timedelta(days=1)
            return False, f"T+1限制: 买入日期{position.buy_date}, 最早可卖日期{can_sell_date}"
        
        return True, "OK"
    
    def execute_buy(self, symbol: str, shares: int, signal_price: float, 
                    volatility: float = 0.02, stop_loss: float = 0.0) -> Tuple[bool, str, Optional[TradeRecord]]:
        """执行买入"""
        # 检查是否可买
        can_buy, reason = self.can_buy(symbol, shares, signal_price)
        if not can_buy:
            logger.warning(f"买入被拒绝: {reason}")
            return False, reason, None
        
        # 计算滑点
        actual_price, slippage = self.slippage_model.calculate(signal_price, "BUY", volatility)
        amount = shares * actual_price
        commission, total_cost = TransactionCostCalculator.calculate_buy_cost(amount)
        
        # 更新账户
        account = self.get_account_status()
        new_cash = account.cash - total_cost
        
        # 更新持仓
        position = self.storage.get_position(symbol)
        today = datetime.now().strftime("%Y-%m-%d")
        if position:
            # 加仓，更新平均成本
            total_shares = position.shares + shares
            total_cost_basis = (position.shares * position.avg_cost) + (shares * actual_price)
            new_avg_cost = total_cost_basis / total_shares
            position.shares = total_shares
            position.avg_cost = round(new_avg_cost, 2)
            position.current_price = actual_price
            position.market_value = total_shares * actual_price
            # 加仓时更新止损价（如果提供了新的止损价）
            if stop_loss > 0:
                position.stop_loss = stop_loss
        else:
            # 新建持仓
            position = Position(
                symbol=symbol, shares=shares, avg_cost=actual_price,
                current_price=actual_price, market_value=amount,
                pnl=0, pnl_pct=0, can_sell=False, buy_date=today,
                stop_loss=stop_loss
            )
        
        # 计算总资产（重新读取所有持仓，含刚更新的）
        all_positions = self.storage.get_positions()
        # 更新刚买入的持仓市值
        for p in all_positions:
            if p.symbol == symbol:
                p.market_value = shares * actual_price
                break
        positions_value = sum(p.market_value for p in all_positions)
        new_total_assets = new_cash + positions_value
        
        # 保存到数据库
        self.storage.update_account(new_cash, new_total_assets)
        self.storage.update_position(position)
        
        trade = TradeRecord(
            id=None, symbol=symbol, action="BUY", shares=shares,
            price=actual_price, amount=amount, commission=commission,
            tax=0, total_cost=total_cost, timestamp=datetime.now().isoformat(),
            signal_price=signal_price, slippage=slippage
        )
        self.storage.record_trade(trade)
        
        logger.info(f"买入成功: {symbol} {shares}股 @ ¥{actual_price:.2f}, 滑点: ¥{slippage:.2f}")
        return True, f"买入成功: {symbol} {shares}股 @ ¥{actual_price:.2f}", trade
    
    def execute_sell(self, symbol: str, shares: int, signal_price: float,
                     volatility: float = 0.02) -> Tuple[bool, str, Optional[TradeRecord]]:
        """执行卖出"""
        # 检查是否可卖
        can_sell, reason = self.can_sell(symbol, shares)
        if not can_sell:
            logger.warning(f"卖出被拒绝: {reason}")
            return False, reason, None
        
        # 计算滑点
        actual_price, slippage = self.slippage_model.calculate(signal_price, "SELL", volatility)
        amount = shares * actual_price
        commission, tax, total_revenue = TransactionCostCalculator.calculate_sell_cost(amount)
        
        # 获取持仓
        position = self.storage.get_position(symbol)
        
        # 计算盈亏
        sell_cost_basis = shares * position.avg_cost
        realized_pnl = total_revenue - sell_cost_basis
        
        # 更新账户
        account = self.get_account_status()
        new_cash = account.cash + total_revenue
        
        # 更新持仓
        remaining_shares = position.shares - shares
        if remaining_shares == 0:
            # 清仓，删除持仓
            position.shares = 0
            position.market_value = 0
        else:
            # 部分卖出
            position.shares = remaining_shares
            position.market_value = remaining_shares * actual_price
        
        # 计算总资产
        all_positions = self.storage.get_positions()
        positions_value = sum(p.market_value for p in all_positions)
        new_total_assets = new_cash + positions_value
        
        # 保存到数据库
        self.storage.update_account(new_cash, new_total_assets)
        self.storage.update_position(position)
        
        trade = TradeRecord(
            id=None, symbol=symbol, action="SELL", shares=shares,
            price=actual_price, amount=amount, commission=commission,
            tax=tax, total_cost=total_revenue, timestamp=datetime.now().isoformat(),
            signal_price=signal_price, slippage=slippage
        )
        self.storage.record_trade(trade)
        
        logger.info(f"卖出成功: {symbol} {shares}股 @ ¥{actual_price:.2f}, "
                   f" realized_pnl: ¥{realized_pnl:.2f}, 滑点: ¥{slippage:.2f}")
        return True, f"卖出成功: {symbol} {shares}股 @ ¥{actual_price:.2f}, 盈亏: ¥{realized_pnl:.2f}", trade
    
    def update_market_prices(self, prices: Dict[str, float]):
        """更新持仓市值（调用行情数据后）"""
        positions = self.storage.get_positions()
        total_positions_value = 0
        
        # 构建标准化的价格映射（兼容 '000042' 和 'sz000042' 两种格式）
        normalized_prices = {}
        for sym, price in prices.items():
            normalized_prices[sym] = price
            # 同时存储纯数字版本
            pure = sym.replace('sh', '').replace('sz', '')
            normalized_prices[pure] = price
        
        for position in positions:
            price = normalized_prices.get(position.symbol)
            if price:
                position.current_price = price
                position.market_value = position.shares * position.current_price
                position.pnl = position.market_value - (position.shares * position.avg_cost)
                position.pnl_pct = (position.pnl / (position.shares * position.avg_cost)) * 100
                self.storage.update_position(position)
                total_positions_value += position.market_value
        
        # 更新总资产
        account = self.get_account_status()
        new_total_assets = account.cash + total_positions_value
        self.storage.update_account(account.cash, new_total_assets)
        
        logger.info(f"市值已更新: 总资产 ¥{new_total_assets:,.2f}")
    
    def get_portfolio_summary(self) -> Dict:
        """获取投资组合摘要"""
        account = self.get_account_status()
        positions = self.get_positions()
        
        total_positions_value = sum(p.market_value for p in positions)
        total_pnl = sum(p.pnl for p in positions)
        total_cost = sum(p.shares * p.avg_cost for p in positions)
        total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
        
        return {
            "cash": account.cash,
            "positions_value": total_positions_value,
            "total_assets": account.total_assets,
            "initial_capital": account.initial_capital,
            "total_return": account.total_assets - account.initial_capital,
            "total_return_pct": ((account.total_assets - account.initial_capital) / account.initial_capital) * 100,
            "positions_count": len(positions),
            "unrealized_pnl": total_pnl,
            "unrealized_pnl_pct": total_pnl_pct
        }


if __name__ == "__main__":
    # 测试代码
    config = {
        "initial_capital": 100000,
        "slippage": 0.001,
        "volatility_factor": 0.5,
        "max_positions": 5,  # 最大持仓数量
        "max_position_pct": 15  # 单只最大仓位比例
    }
    
    executor = PaperTradingExecutor(config)
    
    # 打印账户状态
    account = executor.get_account_status()
    print(f"账户状态: 现金¥{account.cash:,.2f}, 总资产¥{account.total_assets:,.2f}")
    
    # 测试买入
    success, msg, trade = executor.execute_buy("000001.SZ", 100, 10.0)
    print(f"买入结果: {msg}")
    
    # 打印持仓
    positions = executor.get_positions()
    for pos in positions:
        print(f"持仓: {pos.symbol} {pos.shares}股 成本¥{pos.avg_cost:.2f}")
    
    # 打印投资组合
    summary = executor.get_portfolio_summary()
    print(f"\n投资组合摘要:")
    print(f"  现金: ¥{summary['cash']:,.2f}")
    print(f"  持仓市值: ¥{summary['positions_value']:,.2f}")
    print(f"  总资产: ¥{summary['total_assets']:,.2f}")
    print(f"  总收益: ¥{summary['total_return']:,.2f} ({summary['total_return_pct']:.2f}%)")
