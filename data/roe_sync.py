#!/usr/bin/env python3
"""ROE 数据同步 — 从 mootdx 获取全市场基本面数据

每日运行一次，获取最新季报的 ROE/EPS/BVPS/净利润，存入 market_kline.db。
"""

import sqlite3
import logging
import time
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DB_DIR = Path.home() / ".openclaw/workspace/skills/LongToo-trader/data"
DB_PATH = DB_DIR / "market_kline.db"


def init_fundamentals_table(conn: sqlite3.Connection):
    """创建基本面数据表"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fundamentals (
            symbol TEXT NOT NULL,
            roe REAL,
            eps REAL,
            bvps REAL,
            net_profit REAL,
            total_shares REAL,
            net_assets REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (symbol)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fund_symbol ON fundamentals(symbol)")
    conn.commit()


def fetch_and_store_roe(max_retries: int = 3) -> Dict:
    """从 mootdx 获取全市场 ROE 数据并存入 DB

    Returns:
        Dict: {success, stocks_updated, failed, elapsed_seconds}
    """
    from mootdx.quotes import Quotes

    start_time = time.time()

    # 获取股票列表
    db_path = DB_PATH
    if not db_path.exists():
        return {"success": False, "error": "市场数据库不存在，请先运行 --sync"}

    conn = sqlite3.connect(str(db_path))
    init_fundamentals_table(conn)

    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM daily_kline"
    ).fetchall()]

    if not symbols:
        conn.close()
        return {"success": False, "error": "无股票数据"}

    logger.info(f"开始获取 {len(symbols)} 只股票的 ROE 数据...")

    client = Quotes.factory(market='std')

    updated = 0
    failed = 0
    batch = []

    for sym in symbols:
        for attempt in range(max_retries):
            try:
                df = client.finance(symbol=sym)
                if df is not None and len(df) > 0:
                    row = df.iloc[0]
                    zongguben = float(row.get('zongguben', 0) or 0)
                    jingzichan = float(row.get('jingzichan', 0) or 0)
                    jinglirun = float(row.get('jinglirun', 0) or 0)
                    meigujingzichan = float(row.get('meigujingzichan', 0) or 0)

                    roe = (jinglirun / jingzichan * 100) if jingzichan > 0 else None
                    eps = (jinglirun / zongguben) if zongguben > 0 else None

                    if roe is not None and np.isfinite(roe):
                        batch.append((sym, roe, eps, meigujingzichan, jinglirun, zongguben, jingzichan))
                        updated += 1
                    else:
                        failed += 1
                else:
                    failed += 1
                break  # 成功，跳出重试
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                else:
                    failed += 1
                    logger.debug(f"获取 {sym} ROE 失败: {e}")

        # 批量写入（每500只）
        if len(batch) >= 500:
            _batch_insert(conn, batch)
            batch = []
            logger.info(f"  已处理 {updated + failed}/{len(symbols)}")

    # 写入剩余
    if batch:
        _batch_insert(conn, batch)

    # 记录同步时间
    conn.execute("""
        INSERT OR REPLACE INTO sync_meta (key, value, updated_at)
        VALUES ('last_roe_sync', ?, CURRENT_TIMESTAMP)
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
    conn.commit()
    conn.close()

    elapsed = time.time() - start_time
    logger.info(f"ROE 同步完成: {updated} 只更新, {failed} 只失败, 耗时 {elapsed:.0f}秒")

    return {
        "success": True,
        "stocks_updated": updated,
        "failed": failed,
        "elapsed_seconds": round(elapsed, 1),
    }


def _batch_insert(conn: sqlite3.Connection, batch: list):
    """批量插入/更新 ROE 数据"""
    conn.executemany("""
        INSERT OR REPLACE INTO fundamentals
        (symbol, roe, eps, bvps, net_profit, total_shares, net_assets, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, batch)
    conn.commit()


def load_roe_data(min_roe: float = 0, max_roe: float = 100) -> Dict[str, Dict]:
    """从 DB 加载 ROE 数据

    Args:
        min_roe: 最低 ROE 过滤
        max_roe: 最高 ROE 过滤（排除异常值）

    Returns:
        {symbol: {roe, eps, bvps, net_profit}}
    """
    db_path = DB_PATH
    if not db_path.exists():
        return {}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute("""
            SELECT symbol, roe, eps, bvps, net_profit
            FROM fundamentals
            WHERE roe > ? AND roe < ?
            ORDER BY roe DESC
        """, (min_roe, max_roe)).fetchall()

        result = {}
        for r in rows:
            result[r['symbol']] = {
                'roe': r['roe'],
                'eps': r['eps'],
                'bvps': r['bvps'],
                'net_profit': r['net_profit'],
            }
        return result
    except Exception as e:
        logger.warning(f"加载 ROE 数据失败: {e}")
        return {}
    finally:
        conn.close()


def get_roe_ranking(top_n: int = 30, min_price: float = 3.0) -> list:
    """获取 ROE 排名的候选股列表

    Args:
        top_n: 返回前 N 只
        min_price: 最低价格过滤

    Returns:
        [{symbol, roe, eps, price}, ...]
    """
    db_path = DB_PATH
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        # JOIN fundamentals 和最新价格
        rows = conn.execute("""
            SELECT f.symbol, f.roe, f.eps, f.bvps, f.net_profit,
                   k.close as price
            FROM fundamentals f
            INNER JOIN (
                SELECT symbol, close
                FROM daily_kline
                WHERE trade_date = (SELECT MAX(trade_date) FROM daily_kline)
            ) k ON f.symbol = k.symbol
            WHERE f.roe > 0 AND f.roe < 100
              AND k.close >= ?
            ORDER BY f.roe DESC
            LIMIT ?
        """, (min_price, top_n)).fetchall()

        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"获取 ROE 排名失败: {e}")
        return []
    finally:
        conn.close()


def should_sync_roe() -> bool:
    """检查今天是否需要同步 ROE（每周同步一次即可，季报更新频率低）"""
    db_path = DB_PATH
    if not db_path.exists():
        return True
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT value FROM sync_meta WHERE key='last_roe_sync'"
        ).fetchone()
        conn.close()
        if row:
            last_sync = row[0][:10]  # 取日期部分
            # 季报数据每周同步一次足够
            days_since = (datetime.now() - datetime.strptime(last_sync, "%Y-%m-%d")).days
            return days_since >= 7
        return True
    except:
        return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("测试 ROE 同步...")
    result = fetch_and_store_roe()
    print(f"结果: {result}")

    if result["success"]:
        ranking = get_roe_ranking(top_n=10)
        print(f"\nROE Top 10:")
        for i, s in enumerate(ranking, 1):
            print(f"  {i}. {s['symbol']}: ROE={s['roe']:.1f}%, EPS={s['eps']:.2f}, ¥{s['price']:.2f}")
