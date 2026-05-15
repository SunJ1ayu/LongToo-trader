#!/usr/bin/env python3
"""多因子回测脚本 — Phase 4

叠加条件：ROE + PE(TTM) + 净利润同比 + 动量

PE(TTM) = close / sum(最近四个单季EPS)
  - EPS 为累计制，需减法还原：Q2_single = Q2_cum - Q1_cum
  - 分母 < 0 的股票直接排除

用法：
    python3 scripts/backtest/multifactor_backtest.py --roe_min 15 --pe_max 50 --output results/backtest_4_1.json
    python3 scripts/backtest/multifactor_backtest.py --roe_min 15 --pe_max 50 --profit_growth_min 0.1 --output results/backtest_4_2.json
    python3 scripts/backtest/multifactor_backtest.py --roe_min 15 --momentum_min 0 --output results/backtest_4_3.json
"""

import sqlite3
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
import argparse
import json

DB_DIR = Path.home() / ".openclaw/workspace/skills/LongToo-trader/data"
QUARTERLY_DB = DB_DIR / "quarterly_fundamentals.db"
MARKET_DB = DB_DIR / "market_kline.db"

BACKTEST_START = "2020-06-30"
BACKTEST_END = "2026-03-31"


def load_quarterly_data():
    conn = sqlite3.connect(str(QUARTERLY_DB))
    df = pd.read_sql_query("""
        SELECT symbol, year, quarter, stat_date, pub_date, roe, eps,
               net_profit, total_shares, np_margin, gp_margin
        FROM quarterly_fundamentals
        WHERE roe IS NOT NULL AND eps IS NOT NULL AND net_profit IS NOT NULL
        ORDER BY symbol, year, quarter
    """, conn)
    conn.close()
    return df


def preprocess_quarterly(df):
    """累计制 → 单季值。添加 eps_single, np_single 列"""
    df = df.sort_values(['symbol', 'year', 'quarter']).copy()
    df['eps_single'] = np.nan
    df['np_single'] = np.nan

    for sym, grp in df.groupby('symbol'):
        rows = grp.sort_values(['year', 'quarter'])
        prev_eps = None
        prev_np = None
        prev_key = None
        for idx, row in rows.iterrows():
            y, q = row['year'], row['quarter']
            # 判断是否连续季度（忽略年份跨度：Q1 接上年 Q4）
            is_consecutive = prev_key is not None and (
                (q == prev_key[1] + 1 and q <= 4) or
                (q == 1 and prev_key[1] == 4 and y == prev_key[0] + 1)
            )
            if is_consecutive and prev_eps is not None and row['eps'] >= prev_eps:
                df.at[idx, 'eps_single'] = row['eps'] - prev_eps
                df.at[idx, 'np_single'] = row['net_profit'] - prev_np
            elif q == 1:
                # Q1 本身就是单季值（年初累计 = 单季），也处理非连续的情况
                df.at[idx, 'eps_single'] = row['eps']
                df.at[idx, 'np_single'] = row['net_profit']
            else:
                # 非连续季度或累计值异常（Qn_cum < Qn-1_cum），无法还原，标记为 NaN
                pass
            prev_eps = row['eps']
            prev_np = row['net_profit']
            prev_key = (y, q)

    return df


def get_trading_dates():
    conn = sqlite3.connect(str(MARKET_DB))
    dates = pd.read_sql_query(
        "SELECT DISTINCT trade_date FROM daily_kline ORDER BY trade_date", conn
    )
    conn.close()
    dates['date'] = pd.to_datetime(dates['trade_date'])
    return sorted(dates['date'].tolist())


def get_rebalance_dates(trading_dates):
    quarter_ends = []
    for year in range(2020, 2027):
        for quarter_end in ['03-31', '06-30', '09-30', '12-31']:
            date = pd.Timestamp(f"{year}-{quarter_end}")
            if pd.Timestamp(BACKTEST_START) <= date <= pd.Timestamp(BACKTEST_END):
                quarter_ends.append(date)

    rebalance_dates = []
    for qe in quarter_ends:
        for td in trading_dates:
            if td >= qe:
                rebalance_dates.append(td)
                break
    return rebalance_dates


def get_prices_batch(symbols, date, trading_dates):
    """批量获取股票在指定日期的收盘价（前一天收盘价）"""
    # 找到 date 在 trading_dates 中的位置，取前一日
    try:
        idx = trading_dates.index(date)
    except ValueError:
        # 找最近的交易日
        idx = min(range(len(trading_dates)), key=lambda i: abs((trading_dates[i] - date).total_seconds()))
    if idx == 0:
        prev_date = date
    else:
        prev_date = trading_dates[idx - 1]

    conn = sqlite3.connect(str(MARKET_DB))
    prices = {}
    # 批量查询
    placeholders = ','.join(['?'] * len(symbols))
    rows = conn.execute(f"""
        SELECT symbol, close FROM daily_kline
        WHERE symbol IN ({placeholders}) AND trade_date = ?
    """, symbols + [str(prev_date.date())]).fetchall()
    for sym, close in rows:
        if close and close > 0:
            prices[sym] = close
    conn.close()
    return prices


def get_stock_returns(symbols, start_date, end_date):
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


def get_momentum_batch(symbols, date, trading_dates):
    """批量计算动量：回溯60个交易日，要求≥40个有效日"""
    try:
        end_idx = trading_dates.index(date)
    except ValueError:
        end_idx = min(range(len(trading_dates)), key=lambda i: abs((trading_dates[i] - date).total_seconds()))

    start_idx = max(0, end_idx - 60)
    lookback_dates = trading_dates[start_idx:end_idx + 1]

    if len(lookback_dates) < 40:
        return {}

    conn = sqlite3.connect(str(MARKET_DB))
    momentum = {}
    for symbol in symbols:
        rows = conn.execute("""
            SELECT trade_date, close FROM daily_kline
            WHERE symbol=? AND trade_date >= ? AND trade_date <= ?
            ORDER BY trade_date ASC
        """, (symbol, str(lookback_dates[0].date()), str(lookback_dates[-1].date()))).fetchall()
        if len(rows) >= 40:
            start_price = rows[0][1]
            end_price = rows[-1][1]
            if start_price and start_price > 0:
                momentum[symbol] = (end_price - start_price) / start_price
    conn.close()
    return momentum


def compute_pe_ttm(df_quarterly, symbol, year, quarter, price):
    """计算 PE(TTM)：最近四个单季 EPS 之和 / 股价"""
    # 找到该股票在目标季度及之前的数据
    stock_data = df_quarterly[
        (df_quarterly['symbol'] == symbol) &
        ((df_quarterly['year'] < year) |
         ((df_quarterly['year'] == year) & (df_quarterly['quarter'] <= quarter)))
    ].sort_values(['year', 'quarter'], ascending=False)

    if len(stock_data) < 4:
        return None

    # 最近四个季度的 eps_single
    recent4 = stock_data.head(4)
    eps_values = recent4['eps_single'].dropna()
    if len(eps_values) < 4:
        return None

    ttm_eps = eps_values.sum()
    if ttm_eps <= 0:
        return None

    return price / ttm_eps


def compute_profit_growth(df_quarterly, symbol, year, quarter):
    """净利润同比增长：当前单季 / 去年同季 - 1"""
    current = df_quarterly[
        (df_quarterly['symbol'] == symbol) &
        (df_quarterly['year'] == year) &
        (df_quarterly['quarter'] == quarter)
    ]
    prev = df_quarterly[
        (df_quarterly['symbol'] == symbol) &
        (df_quarterly['year'] == year - 1) &
        (df_quarterly['quarter'] == quarter)
    ]

    if current.empty or prev.empty:
        return None

    cur_np = current['np_single'].values[0]
    prev_np = prev['np_single'].values[0]

    if pd.isna(cur_np) or pd.isna(prev_np) or prev_np == 0:
        return None

    return cur_np / prev_np - 1


def filter_stocks(df_quarterly, year, quarter, trading_dates, rebalance_date,
                  roe_min=15, pe_max=None, profit_growth_min=None, momentum_min=None):
    """多因子筛选"""
    mask = (df_quarterly['year'] == year) & (df_quarterly['quarter'] == quarter)
    candidates = df_quarterly[mask].copy()

    # 1. ROE 筛选
    candidates = candidates[candidates['roe'] >= roe_min]
    if candidates.empty:
        return candidates

    symbols = candidates['symbol'].tolist()

    # 2. PE(TTM) 筛选
    if pe_max is not None:
        prices = get_prices_batch(symbols, rebalance_date, trading_dates)
        pes = {}
        for sym in symbols:
            price = prices.get(sym)
            if price is None:
                continue
            pe = compute_pe_ttm(df_quarterly, sym, year, quarter, price)
            if pe is not None and pe > 0:
                pes[sym] = pe

        candidates = candidates[candidates['symbol'].isin(pes.keys())]
        candidates['pe_ttm'] = candidates['symbol'].map(pes)
        candidates = candidates[candidates['pe_ttm'] <= pe_max]

    if candidates.empty:
        return candidates

    symbols = candidates['symbol'].tolist()

    # 3. 净利润同比增长筛选
    if profit_growth_min is not None:
        growths = {}
        for sym in symbols:
            g = compute_profit_growth(df_quarterly, sym, year, quarter)
            if g is not None:
                growths[sym] = g

        candidates = candidates[candidates['symbol'].isin(growths.keys())]
        candidates['profit_growth'] = candidates['symbol'].map(growths)
        candidates = candidates[candidates['profit_growth'] > profit_growth_min]

    if candidates.empty:
        return candidates

    symbols = candidates['symbol'].tolist()

    # 4. 动量筛选
    if momentum_min is not None:
        momentums = get_momentum_batch(symbols, rebalance_date, trading_dates)
        candidates = candidates[candidates['symbol'].isin(momentums.keys())]
        candidates['momentum_3m'] = candidates['symbol'].map(momentums)
        candidates = candidates[candidates['momentum_3m'] > momentum_min]

    return candidates


def compute_turnover(prev_symbols, curr_symbols):
    """换手率：1 - 重叠比例"""
    if not prev_symbols or not curr_symbols:
        return None
    overlap = len(set(prev_symbols) & set(curr_symbols))
    return 1 - overlap / len(curr_symbols)


def run_backtest(roe_min=15, hold_num=30, pe_max=None,
                 profit_growth_min=None, momentum_min=None):
    """运行多因子回测"""
    params_desc = f"ROE > {roe_min}%"
    if pe_max:
        params_desc += f" + PE < {pe_max}"
    if profit_growth_min:
        params_desc += f" + NP增长 > {profit_growth_min*100:.0f}%"
    if momentum_min:
        params_desc += f" + 动量 > {momentum_min*100:.0f}%"
    params_desc += f"，持仓 {hold_num} 只，季度调仓"

    print(f"📊 开始回测: {params_desc}")

    print("   加载季报数据...")
    quarterly_df = load_quarterly_data()
    print("   预处理单季数据（累计制→单季值）...")
    quarterly_df = preprocess_quarterly(quarterly_df)
    valid_eps = quarterly_df['eps_single'].notna().sum()
    print(f"   有效单季EPS: {valid_eps}/{len(quarterly_df)}")

    print("   加载交易日列表...")
    trading_dates = get_trading_dates()
    print(f"   交易日: {len(trading_dates)} 天")

    rebalance_dates = get_rebalance_dates(trading_dates)
    print(f"   调仓日期: {len(rebalance_dates)} 个")

    total_value = 1000000  # 初始资金 100 万
    nav_history = []
    trade_log = []
    candidate_stats = []
    pe_distributions = []  # 逐期 PE 均值
    growth_distributions = []  # 逐期增长均值
    momentum_distributions = []  # 逐期动量均值

    prev_selected = []

    for i, rebalance_date in enumerate(rebalance_dates[:-1]):
        month = rebalance_date.month
        if month <= 3:
            year, quarter = rebalance_date.year - 1, 4
        elif month <= 6:
            year, quarter = rebalance_date.year, 1
        elif month <= 9:
            year, quarter = rebalance_date.year, 2
        else:
            year, quarter = rebalance_date.year, 3

        candidates = filter_stocks(
            quarterly_df, year, quarter, trading_dates, rebalance_date,
            roe_min=roe_min, pe_max=pe_max,
            profit_growth_min=profit_growth_min,
            momentum_min=momentum_min
        )
        candidate_count = len(candidates)

        candidate_stats.append({
            'date': rebalance_date,
            'year': year,
            'quarter': quarter,
            'candidate_count': candidate_count
        })

        # 记录因子分布
        if candidate_count > 0:
            if pe_max and 'pe_ttm' in candidates.columns:
                pe_distributions.append({
                    'date': rebalance_date,
                    'mean': float(candidates['pe_ttm'].mean()),
                    'median': float(candidates['pe_ttm'].median())
                })
            if profit_growth_min and 'profit_growth' in candidates.columns:
                growth_distributions.append({
                    'date': rebalance_date,
                    'mean': float(candidates['profit_growth'].mean()),
                    'median': float(candidates['profit_growth'].median())
                })
            if momentum_min and 'momentum_3m' in candidates.columns:
                momentum_distributions.append({
                    'date': rebalance_date,
                    'mean': float(candidates['momentum_3m'].mean()),
                    'median': float(candidates['momentum_3m'].median())
                })

        if candidate_count == 0:
            print(f"   ⚠️ {rebalance_date.strftime('%Y-%m-%d')}: 无候选，空仓")
            prev_selected = []
            nav_history.append({
                'date': rebalance_dates[i + 1],
                'nav': total_value / 1000000,
                'return': 0,
                'stock_count': 0,
                'candidate_count': 0
            })
            continue

        # 兜底逻辑
        actual_hold = min(hold_num, candidate_count)
        candidates = candidates.nlargest(actual_hold, 'roe')
        selected_symbols = candidates['symbol'].tolist()

        next_rebalance = rebalance_dates[i + 1]
        rets = get_stock_returns(selected_symbols, rebalance_date, next_rebalance)
        period_returns = list(rets.values())

        if period_returns:
            portfolio_return = np.mean(period_returns)
            total_value *= (1 + portfolio_return)

            turnover = compute_turnover(prev_selected, selected_symbols)

            nav_history.append({
                'date': next_rebalance,
                'nav': total_value / 1000000,
                'return': portfolio_return,
                'stock_count': actual_hold,
                'candidate_count': candidate_count,
                'turnover': turnover
            })

            trade_log.append({
                'date': rebalance_date,
                'next_date': next_rebalance,
                'stocks': selected_symbols,
                'return': portfolio_return,
                'candidate_count': candidate_count,
                'turnover': turnover,
                'avg_pe': float(candidates['pe_ttm'].mean()) if pe_max and 'pe_ttm' in candidates.columns else None
            })

            print(f"   {rebalance_date.strftime('%Y-%m-%d')}: "
                  f"候选{candidate_count}→持仓{actual_hold}只, "
                  f"收益{portfolio_return*100:+.1f}%, "
                  f"换手{turnover*100:.0f}%" if turnover is not None else f"   {rebalance_date.strftime('%Y-%m-%d')}: 候选{candidate_count}→持仓{actual_hold}只, 收益{portfolio_return*100:+.1f}%")
        else:
            print(f"   ⚠️ {rebalance_date.strftime('%Y-%m-%d')}: 无法获取收益数据")

        prev_selected = selected_symbols

    # 指标计算
    nav_df = pd.DataFrame(nav_history)
    if len(nav_df) == 0:
        print("❌ 回测无数据")
        return None

    total_return = nav_df.iloc[-1]['nav'] - 1
    years = (nav_df.iloc[-1]['date'] - nav_df.iloc[0]['date']).days / 365
    annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

    risk_free_rate = 0.03
    excess_returns = nav_df['return'] - risk_free_rate / 4
    sharpe = np.mean(excess_returns) / np.std(excess_returns) * np.sqrt(4) if np.std(excess_returns) > 0 else 0

    nav_series = nav_df['nav']
    peak = nav_series.expanding().max()
    drawdown = (nav_series - peak) / peak
    max_drawdown = drawdown.min()

    win_rate = (nav_df['return'] > 0).mean()

    candidate_counts = [s['candidate_count'] for s in candidate_stats]
    candidate_min = min(candidate_counts) if candidate_counts else 0
    candidate_median = np.median(candidate_counts) if candidate_counts else 0
    candidate_max = max(candidate_counts) if candidate_counts else 0

    turnovers = [t.get('turnover') for t in nav_history if t.get('turnover') is not None]
    avg_turnover = np.mean(turnovers) if turnovers else None

    print(f"\n📈 回测结果:")
    print(f"   年化收益: {annual_return*100:.2f}%")
    print(f"   夏普比率: {sharpe:.2f}")
    print(f"   最大回撤: {max_drawdown*100:.2f}%")
    print(f"   胜率: {win_rate*100:.1f}%")
    print(f"   总收益: {total_return*100:.2f}%")
    print(f"\n   候选数量: 最小{candidate_min}, 中位{candidate_median:.0f}, 最大{candidate_max}")
    if avg_turnover is not None:
        print(f"   平均换手率: {avg_turnover*100:.0f}%")

    result = {
        'params': {
            'roe_min': roe_min,
            'hold_num': hold_num,
            'pe_max': pe_max,
            'profit_growth_min': profit_growth_min,
            'momentum_min': momentum_min
        },
        'metrics': {
            'annual_return': annual_return,
            'sharpe': sharpe,
            'max_drawdown': max_drawdown,
            'win_rate': win_rate,
            'total_return': total_return,
            'avg_turnover': avg_turnover
        },
        'candidate_stats': {
            'min': candidate_min,
            'median': candidate_median,
            'max': candidate_max
        },
        'nav_history': nav_history,
        'trade_log': trade_log,
        'factor_distributions': {
            'pe': pe_distributions,
            'profit_growth': growth_distributions,
            'momentum': momentum_distributions
        }
    }

    return result


def save_result(result, filename):
    def convert_timestamps(obj):
        if isinstance(obj, dict):
            return {k: convert_timestamps(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [convert_timestamps(item) for item in obj]
        elif isinstance(obj, pd.Timestamp):
            return obj.strftime('%Y-%m-%d')
        elif isinstance(obj, (np.integer,)):
            return int(obj)
        elif isinstance(obj, (np.floating,)):
            return float(obj) if not np.isnan(obj) else None
        elif isinstance(obj, float) and np.isnan(obj):
            return None
        return obj

    result_serializable = convert_timestamps(result)

    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(result_serializable, f, ensure_ascii=False, indent=2)
    print(f"   结果已保存到: {filename}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="多因子回测 — Phase 4")
    parser.add_argument("--roe_min", type=float, default=15)
    parser.add_argument("--hold_num", type=int, default=30)
    parser.add_argument("--pe_max", type=float, default=None)
    parser.add_argument("--profit_growth_min", type=float, default=None)
    parser.add_argument("--momentum_min", type=float, default=None)
    parser.add_argument("--output", type=str, required=True)
    args = parser.parse_args()

    result = run_backtest(
        roe_min=args.roe_min,
        hold_num=args.hold_num,
        pe_max=args.pe_max,
        profit_growth_min=args.profit_growth_min,
        momentum_min=args.momentum_min
    )

    if result:
        save_result(result, args.output)
