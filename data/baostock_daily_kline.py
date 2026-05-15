#!/usr/bin/env python3
"""baostock 历史日线行情增量拉取

每次运行拉取 N 只股票的全部日线数据，存入 daily_kline 表。
可重复运行，已拉取的股票会跳过（基于 progress 表）。

用法:
    python3 scripts/data/baostock_daily_kline.py              # 拉取 200 只（默认）
    python3 scripts/data/baostock_daily_kline.py --batch 500  # 拉取 500 只
    python3 scripts/data/baostock_daily_kline.py --all        # 拉取全部
    python3 scripts/data/baostock_daily_kline.py --status     # 查看进度
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

# 拉取年份范围
YEAR_START = 2020
YEAR_END = 2026


def init_db():
    """初始化行情数据库"""
    conn = sqlite3.connect(str(MARKET_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_kline (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (symbol, trade_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kline_pull_progress (
            symbol TEXT PRIMARY KEY,
            pulled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            days_found INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dk_symbol ON daily_kline(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dk_trade_date ON daily_kline(trade_date)")
    conn.commit()
    return conn


def get_stock_list():
    """从 quarterly_fundamentals.db 获取全 A 股列表"""
    quarterly_db = DB_DIR / "quarterly_fundamentals.db"
    conn = sqlite3.connect(str(quarterly_db))
    rows = conn.execute("SELECT DISTINCT symbol FROM quarterly_fundamentals").fetchall()
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
    rows = conn.execute("SELECT symbol FROM kline_pull_progress").fetchall()
    return set(r[0] for r in rows)


def pull_one_stock(conn, bs_client, symbol):
    """拉取一只股票的全部日线数据"""
    batch = []
    for year in range(YEAR_START, YEAR_END + 1):
        try:
            rs = bs_client.query_history_k_data_plus(
                code=symbol,
                fields="date,open,high,low,close,volume",
                start_date=f"{year}-01-01",
                end_date=f"{year}-12-31",
                frequency="d",
                adjustflag="3"  # 不复权
            )
            while rs.next():
                row = rs.get_row_data()
                trade_date = row[0]
                open_price = float(row[1]) if row[1] else None
                high_price = float(row[2]) if row[2] else None
                low_price = float(row[3]) if row[3] else None
                close_price = float(row[4]) if row[4] else None
                volume = int(row[5]) if row[5] else None

                # 跳过无效数据
                if close_price is None or close_price == 0:
                    continue

                # 去掉 sh./sz. 前缀
                pure_symbol = symbol.replace('sh.', '').replace('sz.', '')

                batch.append((
                    pure_symbol, trade_date, open_price, high_price,
                    low_price, close_price, volume
                ))
        except Exception as e:
            logger.debug(f"{symbol} {year} 失败: {e}")

    if batch:
        conn.executemany("""
            INSERT OR REPLACE INTO daily_kline
            (symbol, trade_date, open, high, low, close, volume, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, batch)

    # 记录进度
    conn.execute("""
        INSERT OR REPLACE INTO kline_pull_progress (symbol, pulled_at, days_found)
        VALUES (?, CURRENT_TIMESTAMP, ?)
    """, (symbol, len(batch)))
    conn.commit()

    return len(batch)


def pull_batch(batch_size=None):
    """批量拉取日线数据（单轮）"""
    import baostock as bs

    conn = init_db()
    all_symbols = get_stock_list()
    pulled = get_pulled_symbols(conn)
    todo = [s for s in all_symbols if s not in pulled]

    if not todo:
        print(f"✅ 全部 {len(all_symbols)} 只股票已拉取完成")
        conn.close()
        return 0

    if batch_size:
        todo = todo[:batch_size]

    print(f"📥 开始拉取日线行情: 本轮 {len(todo)} 只, 剩余 {len(all_symbols) - len(pulled)} 只")

    lg = bs.login()
    if lg.error_code != '0':
        print(f"❌ baostock 登录失败: {lg.error_msg}")
        conn.close()
        return -1

    total_days = 0
    start_time = time.time()

    for i, symbol in enumerate(todo, 1):
        try:
            count = pull_one_stock(conn, bs, symbol)
            total_days += count
        except Exception as e:
            logger.warning(f"{symbol} 拉取异常: {e}")
            continue

        if i % 50 == 0 or i == len(todo):
            elapsed = time.time() - start_time
            rate = i / elapsed if elapsed > 0 else 0
            remaining = (len(todo) - i) / rate if rate > 0 else 0
            print(f"   [{i}/{len(todo)}] 已拉取 {total_days} 条日线, "
                  f"速度 {rate:.1f} 只/秒, 剩余 ~{remaining/60:.0f} 分钟")

        time.sleep(0.05)

    bs.logout()
    conn.close()

    elapsed = time.time() - start_time
    print(f"\n✅ 本轮完成: {len(todo)} 只股票, {total_days} 条日线, 耗时 {elapsed:.0f}秒")
    return len(todo)


def pull_with_retry(batch_size=None, max_retries=10, retry_delay=30):
    """带自动重试的批量拉取，断线自动续上"""
    consecutive_failures = 0

    while True:
        try:
            result = pull_batch(batch_size)
        except Exception as e:
            result = -1
            print(f"\n❌ 轮次异常: {e}")

        if result == 0:
            print("\n🎉 全部拉取完成！")
            break
        elif result == -1:
            consecutive_failures += 1
            if consecutive_failures >= max_retries:
                print(f"\n❌ 连续 {max_retries} 次失败，停止重试")
                break
            print(f"⏳ {retry_delay}秒后重试（第 {consecutive_failures} 次）...")
            time.sleep(retry_delay)
        else:
            consecutive_failures = 0  # 成功了，重置失败计数
            # 如果是 --batch 模式（指定了数量），跑完一轮就停
            if batch_size:
                break
            # 否则继续下一轮
            print(f"⏳ 5秒后开始下一轮...")
            time.sleep(5)


def show_status():
    """显示拉取进度"""
    if not MARKET_DB.exists():
        print("❌ 行情数据库不存在，还未开始拉取")
        return

    conn = sqlite3.connect(str(MARKET_DB))
    total = conn.execute("SELECT COUNT(*) FROM kline_pull_progress").fetchone()[0]
    days = conn.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0]

    # 获取季报中的股票总数
    quarterly_db = DB_DIR / "quarterly_fundamentals.db"
    quarterly_conn = sqlite3.connect(str(quarterly_db))
    market_total = quarterly_conn.execute("SELECT COUNT(DISTINCT symbol) FROM quarterly_fundamentals").fetchone()[0]
    quarterly_conn.close()

    # 最新拉取时间
    latest = conn.execute(
        "SELECT MAX(pulled_at) FROM kline_pull_progress"
    ).fetchone()[0]

    # 日期分布
    date_dist = conn.execute("""
        SELECT substr(trade_date, 1, 4) as year, COUNT(*)
        FROM daily_kline
        GROUP BY year ORDER BY year
    """).fetchall()

    conn.close()

    print(f"📊 日线行情拉取进度")
    print(f"   已拉取: {total}/{market_total} 只 ({total/market_total*100:.1f}%)")
    print(f"   日线条数: {days}")
    print(f"   最新拉取: {latest}")
    print(f"\n   年份分布:")
    for year, count in date_dist:
        print(f"     {year}: {count} 条")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(description="baostock 历史日线行情增量拉取")
    parser.add_argument("--batch", type=int, default=200, help="本轮拉取股票数（默认200）")
    parser.add_argument("--all", action="store_true", help="拉取全部剩余股票")
    parser.add_argument("--status", action="store_true", help="查看拉取进度")
    parser.add_argument("--retry", action="store_true", help="自动重试，断线续上")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.retry:
        pull_with_retry(batch_size=args.batch if not args.all else None)
    elif args.all:
        pull_batch()
    else:
        pull_batch(args.batch)
