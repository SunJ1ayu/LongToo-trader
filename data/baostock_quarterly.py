#!/usr/bin/env python3
"""baostock 历史季报增量拉取

每次运行拉取 N 只股票的全部季度利润表，存入 quarterly_fundamentals 表。
可重复运行，已拉取的股票会跳过（基于 progress 表）。

用法:
    python3 -m scripts.data.baostock_quarterly              # 拉取 200 只（默认）
    python3 -m scripts.data.baostock_quarterly --batch 500  # 拉取 500 只
    python3 -m scripts.data.baostock_quarterly --all        # 拉取全部
    python3 -m scripts.data.baostock_quarterly --status     # 查看进度
"""

import sqlite3
import logging
import time
import argparse
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

DB_DIR = Path.home() / ".openclaw/workspace/skills/LongToo-trader/data"
MARKET_DB = DB_DIR / "market_kline.db"
QUARTERLY_DB = DB_DIR / "quarterly_fundamentals.db"

# baostock 季报字段
PROFIT_FIELDS = [
    'code', 'pubDate', 'statDate', 'roeAvg', 'npMargin', 'gpMargin',
    'netProfit', 'epsTTM', 'MBRevenue', 'totalShare', 'liqaShare'
]

# 拉取年份范围
YEAR_START = 2020
YEAR_END = 2026


def init_db():
    """初始化季报数据库"""
    conn = sqlite3.connect(str(QUARTERLY_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quarterly_fundamentals (
            symbol TEXT NOT NULL,
            year INTEGER NOT NULL,
            quarter INTEGER NOT NULL,
            stat_date TEXT,         -- 财报统计日 (如 2025-03-31)
            pub_date TEXT,          -- 公布日 (如 2025-04-30)
            roe REAL,               -- ROE (小数，如 0.05 = 5%)
            eps REAL,               -- EPS (TTM)
            net_profit REAL,        -- 净利润
            total_shares REAL,      -- 总股本
            np_margin REAL,         -- 净利率
            gp_margin REAL,         -- 毛利率
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (symbol, year, quarter)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quarterly_pull_progress (
            symbol TEXT PRIMARY KEY,
            pulled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            quarters_found INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qf_symbol ON quarterly_fundamentals(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qf_pub_date ON quarterly_fundamentals(pub_date)")
    conn.commit()
    return conn


def get_stock_list():
    """从 market_kline.db 获取全 A 股列表"""
    conn = sqlite3.connect(str(MARKET_DB))
    rows = conn.execute("SELECT DISTINCT symbol FROM daily_kline").fetchall()
    conn.close()
    # 转换格式: 600030 -> sh.600030, 000001 -> sz.000001
    symbols = []
    for r in rows:
        sym = r[0]
        if sym.startswith('6') or sym.startswith('9'):
            symbols.append(f"sh.{sym}")
        else:
            symbols.append(f"sz.{sym}")
    return symbols


def get_pulled_symbols(conn):
    """获取已拉取的股票列表"""
    rows = conn.execute("SELECT symbol FROM quarterly_pull_progress").fetchall()
    return set(r[0] for r in rows)


def pull_one_stock(conn, bs_client, symbol):
    """拉取一只股票的全部季报数据"""
    batch = []
    for year in range(YEAR_START, YEAR_END + 1):
        for quarter in range(1, 5):
            try:
                rs = bs_client.query_profit_data(code=symbol, year=year, quarter=quarter)
                while rs.next():
                    row = rs.get_row_data()
                    pub_date = row[1]
                    stat_date = row[2]
                    roe_str = row[3]
                    np_margin_str = row[4]
                    gp_margin_str = row[5]
                    net_profit_str = row[6]
                    eps_str = row[7]
                    total_share_str = row[9]

                    # 跳过空数据
                    if not roe_str or float(roe_str) == 0:
                        continue

                    # baostock ROE 是小数 (0.05 = 5%)，转为百分比
                    roe = float(roe_str) * 100
                    eps = float(eps_str) if eps_str else None
                    net_profit = float(net_profit_str) if net_profit_str else None
                    total_shares = float(total_share_str) if total_share_str else None
                    np_margin = float(np_margin_str) if np_margin_str else None
                    gp_margin = float(gp_margin_str) if gp_margin_str else None

                    # 去掉 sh./sz. 前缀，与 market_kline.db 格式一致
                    pure_symbol = symbol.replace('sh.', '').replace('sz.', '')

                    batch.append((
                        pure_symbol, year, quarter, stat_date, pub_date,
                        roe, eps, net_profit, total_shares, np_margin, gp_margin
                    ))
            except Exception as e:
                logger.debug(f"{symbol} {year}Q{quarter} 失败: {e}")

    if batch:
        conn.executemany("""
            INSERT OR REPLACE INTO quarterly_fundamentals
            (symbol, year, quarter, stat_date, pub_date, roe, eps, net_profit,
             total_shares, np_margin, gp_margin, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, batch)

    # 记录进度
    conn.execute("""
        INSERT OR REPLACE INTO quarterly_pull_progress (symbol, pulled_at, quarters_found)
        VALUES (?, CURRENT_TIMESTAMP, ?)
    """, (symbol, len(batch)))
    conn.commit()

    return len(batch)


def pull_batch(batch_size=None):
    """批量拉取季报数据"""
    import baostock as bs

    conn = init_db()
    all_symbols = get_stock_list()
    pulled = get_pulled_symbols(conn)
    todo = [s for s in all_symbols if s not in pulled]

    if not todo:
        print(f"✅ 全部 {len(all_symbols)} 只股票已拉取完成")
        conn.close()
        return

    if batch_size:
        todo = todo[:batch_size]

    print(f"📥 开始拉取季报: 本轮 {len(todo)} 只, 剩余 {len(all_symbols) - len(pulled) - len(todo) + len(todo)} 只")

    lg = bs.login()
    if lg.error_code != '0':
        print(f"❌ baostock 登录失败: {lg.error_msg}")
        conn.close()
        return

    total_quarters = 0
    start_time = time.time()

    for i, symbol in enumerate(todo, 1):
        count = pull_one_stock(conn, bs, symbol)
        total_quarters += count

        if i % 50 == 0 or i == len(todo):
            elapsed = time.time() - start_time
            rate = i / elapsed if elapsed > 0 else 0
            remaining = (len(todo) - i) / rate if rate > 0 else 0
            print(f"   [{i}/{len(todo)}] 已拉取 {total_quarters} 条季报, "
                  f"速度 {rate:.1f} 只/秒, 剩余 ~{remaining/60:.0f} 分钟")

        # 控制请求频率，避免被封
        time.sleep(0.1)

    bs.logout()
    conn.close()

    elapsed = time.time() - start_time
    print(f"\n✅ 本轮完成: {len(todo)} 只股票, {total_quarters} 条季报, 耗时 {elapsed:.0f}秒")


def show_status():
    """显示拉取进度"""
    if not QUARTERLY_DB.exists():
        print("❌ 季报数据库不存在，还未开始拉取")
        return

    conn = sqlite3.connect(str(QUARTERLY_DB))
    total = conn.execute("SELECT COUNT(*) FROM quarterly_pull_progress").fetchone()[0]
    quarters = conn.execute("SELECT COUNT(*) FROM quarterly_fundamentals").fetchone()[0]

    # 获取全市场股票总数
    market_conn = sqlite3.connect(str(MARKET_DB))
    market_total = market_conn.execute("SELECT COUNT(DISTINCT symbol) FROM daily_kline").fetchone()[0]
    market_conn.close()

    # 最新拉取时间
    latest = conn.execute(
        "SELECT MAX(pulled_at) FROM quarterly_pull_progress"
    ).fetchone()[0]

    # pubDate 分布
    pub_dist = conn.execute("""
        SELECT substr(pub_date, 1, 4) as year, COUNT(*)
        FROM quarterly_fundamentals
        GROUP BY year ORDER BY year
    """).fetchall()

    conn.close()

    print(f"📊 季报拉取进度")
    print(f"   已拉取: {total}/{market_total} 只 ({total/market_total*100:.1f}%)")
    print(f"   季报条数: {quarters}")
    print(f"   最新拉取: {latest}")
    print(f"\n   公布日期分布:")
    for year, count in pub_dist:
        print(f"     {year}: {count} 条")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(description="baostock 历史季报增量拉取")
    parser.add_argument("--batch", type=int, default=200, help="本轮拉取股票数（默认200）")
    parser.add_argument("--all", action="store_true", help="拉取全部剩余股票")
    parser.add_argument("--status", action="store_true", help="查看拉取进度")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.all:
        pull_batch()
    else:
        pull_batch(args.batch)
