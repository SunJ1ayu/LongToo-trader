#!/usr/bin/env python3
"""ROE 单因子回测脚本

回测逻辑：
1. 每季度末，从季报数据中筛选 ROE 符合条件的股票
2. 按 ROE 排序，选取 top N 只股票
3. 等权买入，持有到下个季度末
4. 计算收益、夏普、最大回撤等指标

用法：
    python3 scripts/backtest/roe_backtest.py --roe_min 15 --hold_num 30 --rebalance quarterly
"""

import sqlite3
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
import argparse
import json

# 数据路径
DB_DIR = Path.home() / ".openclaw/workspace/skills/LongToo-trader/data"
QUARTERLY_DB = DB_DIR / "quarterly_fundamentals.db"
MARKET_DB = DB_DIR / "market_kline.db"

# 回测参数
BACKTEST_START = "2020-06-30"  # 从2020年下半年开始（确保有足够历史数据）
BACKTEST_END = "2026-03-31"


def load_quarterly_data():
    """加载季报数据"""
    conn = sqlite3.connect(str(QUARTERLY_DB))
    df = pd.read_sql_query("""
        SELECT symbol, year, quarter, stat_date, pub_date, roe, eps,
               net_profit, total_shares, np_margin, gp_margin
        FROM quarterly_fundamentals
        WHERE roe IS NOT NULL
        ORDER BY symbol, year, quarter
    """, conn)
    conn.close()
    return df


def get_trading_dates():
    """仅加载交易日列表"""
    conn = sqlite3.connect(str(MARKET_DB))
    dates = pd.read_sql_query(
        "SELECT DISTINCT trade_date FROM daily_kline ORDER BY trade_date", conn
    )
    conn.close()
    dates['date'] = pd.to_datetime(dates['trade_date'])
    return dates['date'].tolist()


def get_stock_returns(symbols, start_date, end_date):
    """查询指定股票在指定区间的收益率（不加载全量数据）"""
    conn = sqlite3.connect(str(MARKET_DB))
    rets = {}
    for symbol in symbols:
        rows = conn.execute("""
            SELECT close FROM daily_kline
            WHERE symbol=? AND trade_date >= ? AND trade_date <= ?
            ORDER BY trade_date ASC
        """, (symbol, str(start_date.date()), str(end_date.date()))).fetchall()
        if len(rows) >= 2:
            start_price = rows[0][0]
            end_price = rows[-1][0]
            if start_price and start_price > 0:
                rets[symbol] = (end_price - start_price) / start_price
    conn.close()
    return rets


def get_rebalance_dates(trading_dates):
    """获取调仓日期（每季度末后第一个交易日）"""
    quarter_ends = []
    for year in range(2020, 2027):
        for quarter_end in ['03-31', '06-30', '09-30', '12-31']:
            date = pd.Timestamp(f"{year}-{quarter_end}")
            if date >= pd.Timestamp(BACKTEST_START) and date <= pd.Timestamp(BACKTEST_END):
                quarter_ends.append(date)

    rebalance_dates = []
    for qe in quarter_ends:
        for td in trading_dates:
            if td >= qe:
                rebalance_dates.append(td)
                break

    return rebalance_dates


def filter_roe_stocks(quarterly_df, year, quarter, roe_min, continuous_growth=False):
    """筛选 ROE 符合条件的股票"""
    # 获取指定年份和季度的数据
    mask = (quarterly_df['year'] == year) & (quarterly_df['quarter'] == quarter)
    df = quarterly_df[mask].copy()

    # ROE 筛选
    df = df[df['roe'] >= roe_min]

    if continuous_growth and quarter > 1:
        # 获取上一季度的 ROE
        prev_mask = (quarterly_df['year'] == year) & (quarterly_df['quarter'] == quarter - 1)
        prev_df = quarterly_df[prev_mask][['symbol', 'roe']].rename(columns={'roe': 'roe_prev'})

        if quarter == 1:
            # 如果是 Q1，需要上一年的 Q4
            prev_mask = (quarterly_df['year'] == year - 1) & (quarterly_df['quarter'] == 4)
            prev_df = quarterly_df[prev_mask][['symbol', 'roe']].rename(columns={'roe': 'roe_prev'})

        df = df.merge(prev_df, on='symbol', how='inner')
        df = df[df['roe'] > df['roe_prev']]

    return df


def run_backtest(roe_min=15, hold_num=30, rebalance='quarterly', continuous_growth=False):
    """运行回测"""
    print(f"📊 开始回测: ROE > {roe_min}%, 持仓 {hold_num} 只, 调仓频率: {rebalance}")

    # 加载数据
    print("   加载季报数据...")
    quarterly_df = load_quarterly_data()
    print("   加载交易日列表...")
    trading_dates = get_trading_dates()
    print(f"   交易日: {len(trading_dates)} 天")

    # 获取调仓日期
    rebalance_dates = get_rebalance_dates(trading_dates)
    print(f"   调仓日期: {len(rebalance_dates)} 个")

    # 初始化
    portfolio = {}  # {symbol: weight}
    cash = 1000000  # 初始资金 100 万
    total_value = cash
    nav_history = []  # 净值历史
    trade_log = []  # 交易记录
    candidate_stats = []  # 候选数量统计

    # 按月统计收益
    monthly_returns = []

    # 行业集中度统计
    industry_concentration = []

    for i, rebalance_date in enumerate(rebalance_dates[:-1]):  # 最后一个日期不调仓
        # 确定当前季度
        month = rebalance_date.month
        if month <= 3:
            year, quarter = rebalance_date.year - 1, 4
        elif month <= 6:
            year, quarter = rebalance_date.year, 1
        elif month <= 9:
            year, quarter = rebalance_date.year, 2
        else:
            year, quarter = rebalance_date.year, 3

        # 筛选股票
        candidates = filter_roe_stocks(quarterly_df, year, quarter, roe_min, continuous_growth)
        candidate_count = len(candidates)

        # 记录候选数量
        candidate_stats.append({
            'date': rebalance_date,
            'year': year,
            'quarter': quarter,
            'candidate_count': candidate_count
        })

        if candidate_count == 0:
            print(f"   ⚠️ {rebalance_date.strftime('%Y-%m-%d')}: 无候选股票，保持现金")
            continue

        # 按 ROE 排序，选取 top N
        candidates = candidates.nlargest(min(hold_num, candidate_count), 'roe')
        selected_symbols = candidates['symbol'].tolist()

        # 获取下一个调仓日期
        next_rebalance = rebalance_dates[i + 1]

        # 计算持仓期间收益（按需查询，不加载全量数据）
        rets = get_stock_returns(selected_symbols, rebalance_date, next_rebalance)
        period_returns = list(rets.values())

        if period_returns:
            # 等权计算组合收益
            portfolio_return = np.mean(period_returns)
            total_value *= (1 + portfolio_return)

            # 记录净值
            nav_history.append({
                'date': next_rebalance,
                'nav': total_value / 1000000,
                'return': portfolio_return,
                'stock_count': len(selected_symbols),
                'candidate_count': candidate_count
            })

            # 记录交易
            trade_log.append({
                'date': rebalance_date,
                'next_date': next_rebalance,
                'stocks': selected_symbols,
                'return': portfolio_return,
                'candidate_count': candidate_count
            })

    # 计算回测指标
    nav_df = pd.DataFrame(nav_history)

    if len(nav_df) == 0:
        print("❌ 回测无数据，请检查参数")
        return None

    # 年化收益
    total_return = nav_df.iloc[-1]['nav'] - 1
    years = (nav_df.iloc[-1]['date'] - nav_df.iloc[0]['date']).days / 365
    annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

    # 夏普比率（假设无风险利率 3%）
    risk_free_rate = 0.03
    excess_returns = nav_df['return'] - risk_free_rate / 4  # 季度收益
    sharpe = np.mean(excess_returns) / np.std(excess_returns) * np.sqrt(4) if np.std(excess_returns) > 0 else 0

    # 最大回撤
    nav_series = nav_df['nav']
    peak = nav_series.expanding().max()
    drawdown = (nav_series - peak) / peak
    max_drawdown = drawdown.min()

    # 胜率
    win_rate = (nav_df['return'] > 0).mean()

    # 候选数量统计
    candidate_counts = [s['candidate_count'] for s in candidate_stats]
    candidate_min = min(candidate_counts) if candidate_counts else 0
    candidate_median = np.median(candidate_counts) if candidate_counts else 0
    candidate_max = max(candidate_counts) if candidate_counts else 0

    # 输出结果
    print(f"\n📈 回测结果:")
    print(f"   年化收益: {annual_return*100:.2f}%")
    print(f"   夏普比率: {sharpe:.2f}")
    print(f"   最大回撤: {max_drawdown*100:.2f}%")
    print(f"   胜率: {win_rate*100:.1f}%")
    print(f"   总收益: {total_return*100:.2f}%")
    print(f"\n   候选数量统计:")
    print(f"     最小值: {candidate_min}")
    print(f"     中位数: {candidate_median:.0f}")
    print(f"     最大值: {candidate_max}")

    # 返回结果
    result = {
        'params': {
            'roe_min': roe_min,
            'hold_num': hold_num,
            'rebalance': rebalance,
            'continuous_growth': continuous_growth
        },
        'metrics': {
            'annual_return': annual_return,
            'sharpe': sharpe,
            'max_drawdown': max_drawdown,
            'win_rate': win_rate,
            'total_return': total_return
        },
        'candidate_stats': {
            'min': candidate_min,
            'median': candidate_median,
            'max': candidate_max
        },
        'nav_history': nav_history,
        'trade_log': trade_log
    }

    return result


def save_result(result, filename):
    """保存回测结果"""
    # 转换 Timestamp 为字符串
    def convert_timestamps(obj):
        if isinstance(obj, dict):
            return {k: convert_timestamps(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [convert_timestamps(item) for item in obj]
        elif isinstance(obj, pd.Timestamp):
            return obj.strftime('%Y-%m-%d')
        elif isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        return obj

    result_serializable = convert_timestamps(result)

    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(result_serializable, f, ensure_ascii=False, indent=2)
    print(f"   结果已保存到: {filename}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ROE 单因子回测")
    parser.add_argument("--roe_min", type=float, default=15, help="ROE 最小值（默认 15）")
    parser.add_argument("--hold_num", type=int, default=30, help="持仓股票数（默认 30）")
    parser.add_argument("--rebalance", type=str, default='quarterly', help="调仓频率（默认 quarterly）")
    parser.add_argument("--continuous_growth", action="store_true", help="要求 ROE 连续增长")
    parser.add_argument("--output", type=str, help="输出文件路径")
    args = parser.parse_args()

    result = run_backtest(
        roe_min=args.roe_min,
        hold_num=args.hold_num,
        rebalance=args.rebalance,
        continuous_growth=args.continuous_growth
    )

    if result and args.output:
        save_result(result, args.output)
