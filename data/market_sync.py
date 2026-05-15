#!/usr/bin/env python3
"""
全A股市场数据同步模块

使用 AKShare 获取股票列表，腾讯财经批量下载60天K线，存入本地 SQLite。
支持增量更新（只下载当天数据，历史数据保留）和并发下载。

用法:
    python3 -m scripts.data.market_sync              # 全量同步
    python3 -m scripts.data.market_sync --incremental # 增量更新
    python3 -m scripts.data.market_sync --test 5      # 测试：只同步5只
"""

import logging
import sqlite3
import time
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

logger = logging.getLogger(__name__)

# ============================================================
# 数据库路径
# ============================================================
DB_DIR = Path(__file__).parent.parent.parent / "data"
DB_PATH = DB_DIR / "market_kline.db"


def get_db_path() -> Path:
    """获取数据库路径"""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return DB_PATH


def init_db(db_path: Path = None) -> sqlite3.Connection:
    """初始化数据库，创建表和索引"""
    db_path = db_path or get_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_symbol ON daily_kline(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_date ON daily_kline(trade_date)")
    # 元数据表（记录同步状态）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def should_sync_today() -> bool:
    """检查今天是否需要同步（每天只需同步一次）"""
    db_path = get_db_path()
    if not db_path.exists():
        return True
    try:
        conn = init_db(db_path)
        row = conn.execute(
            "SELECT value FROM sync_meta WHERE key='last_sync_date'"
        ).fetchone()
        conn.close()
        if row:
            return row[0] != datetime.now().strftime("%Y-%m-%d")
        return True
    except:
        return True


def mark_sync_done():
    """标记今天同步完成"""
    try:
        conn = init_db()
        conn.execute("""
            INSERT OR REPLACE INTO sync_meta (key, value, updated_at)
            VALUES ('last_sync_date', ?, CURRENT_TIMESTAMP)
        """, (datetime.now().strftime("%Y-%m-%d"),))
        conn.commit()
        conn.close()
    except:
        pass


# 股票列表缓存文件
STOCK_LIST_CACHE = DB_DIR / "stock_list_cache.json"
STOCK_LIST_CACHE_TTL = 7 * 24 * 3600  # 缓存7天


def get_stock_list() -> List[Dict]:
    """获取全A股股票列表（缓存优先，AKShare兜底）
    
    优先从缓存读取，缓存过期才用AKShare刷新。
    股票列表变化很小（每周几只新股），7天刷新一次足够。
    
    Returns:
        股票列表，每项包含 code, name
    """
    import json
    
    # 1. 尝试读缓存
    if STOCK_LIST_CACHE.exists():
        try:
            data = json.loads(STOCK_LIST_CACHE.read_text(encoding='utf-8'))
            cached_at = data.get('cached_at', 0)
            stocks = data.get('stocks', [])
            
            if stocks and (time.time() - cached_at) < STOCK_LIST_CACHE_TTL:
                logger.info(f"股票列表缓存命中: {len(stocks)} 只（缓存于 {datetime.fromtimestamp(cached_at).strftime('%m-%d %H:%M')}）")
                return stocks
        except Exception:
            pass
    
    # 2. 缓存过期，用AKShare刷新
    logger.info("股票列表缓存过期，从AKShare刷新...")
    stocks = _get_stock_list_from_akshare()
    
    if stocks:
        # 写入缓存
        try:
            STOCK_LIST_CACHE.parent.mkdir(parents=True, exist_ok=True)
            cache_data = {
                'cached_at': time.time(),
                'count': len(stocks),
                'stocks': stocks
            }
            STOCK_LIST_CACHE.write_text(json.dumps(cache_data, ensure_ascii=False), encoding='utf-8')
            logger.info(f"股票列表已缓存: {len(stocks)} 只")
        except Exception as e:
            logger.warning(f"缓存写入失败: {e}")
    
    return stocks


def _get_stock_list_from_akshare() -> List[Dict]:
    """使用 AKShare 获取全A股股票列表（内部方法）"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        
        stocks = []
        for _, row in df.iterrows():
            code = str(row["代码"]).strip()
            name = str(row["名称"]).strip()
            
            # 过滤 ST、退市、北交所等
            if "ST" in name or "退" in name:
                continue
            # 只保留沪深主板+创业板+科创板 (60/00/30/68 开头)
            if not (code.startswith("60") or code.startswith("00") 
                    or code.startswith("30") or code.startswith("68")):
                continue
            
            stocks.append({
                "code": code,
                "name": name
            })
        
        logger.info(f"AKShare 获取全A股列表: {len(stocks)} 只（过滤后）")
        return stocks
        
    except ImportError:
        logger.error("AKShare 未安装，请运行: pip install akshare")
        return []
    except Exception as e:
        logger.error(f"AKShare 获取股票列表失败: {e}")
        return []


def _code_to_tencent(code: str) -> str:
    """将纯数字代码转为腾讯格式 (sh600519 / sz000001)"""
    if code.startswith("6") or code.startswith("5"):
        return f"sh{code}"
    else:
        return f"sz{code}"


def download_kline_batch(
    symbols: List[str],
    days: int = 60,
    max_workers: int = 8,
    request_interval: float = 0.05
) -> Dict[str, List[Dict]]:
    """并发批量下载K线数据
    
    Args:
        symbols: 纯数字代码列表（如 ['600519', '000001']）
        days: 获取天数
        max_workers: 并发线程数
        request_interval: 请求间隔（秒），防止被封
        
    Returns:
        {code: kline_list, ...}
    """
    from .tencent_provider import TencentFinanceProvider
    
    results = {}
    failed = []
    lock = Lock()
    total = len(symbols)
    done_count = [0]
    start_time = time.time()
    
    def _fetch_one(code: str) -> Tuple[str, Optional[List[Dict]]]:
        """下载单只股票K线"""
        provider = TencentFinanceProvider(timeout=15)
        tencent_code = _code_to_tencent(code)
        
        try:
            klines = provider.get_historical_kline(tencent_code, days)
            time.sleep(request_interval)  # 限速
            if klines:
                return code, klines
            else:
                return code, None
        except Exception as e:
            logger.debug(f"下载失败 {code}: {e}")
            return code, None
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_code = {
            executor.submit(_fetch_one, code): code
            for code in symbols
        }
        
        for future in as_completed(future_to_code):
            code = future_to_code[future]
            try:
                code, klines = future.result()
                with lock:
                    done_count[0] += 1
                    if klines:
                        results[code] = klines
                    else:
                        failed.append(code)
                    
                    # 每100只打印一次进度
                    if done_count[0] % 200 == 0 or done_count[0] == total:
                        elapsed = time.time() - start_time
                        speed = done_count[0] / elapsed if elapsed > 0 else 0
                        eta = (total - done_count[0]) / speed if speed > 0 else 0
                        logger.info(
                            f"  进度: {done_count[0]}/{total} "
                            f"({done_count[0]/total*100:.1f}%) "
                            f"成功: {len(results)} 失败: {len(failed)} "
                            f"速度: {speed:.1f}只/秒 ETA: {eta:.0f}秒"
                        )
            except Exception as e:
                logger.error(f"处理 {code} 结果异常: {e}")
                with lock:
                    done_count[0] += 1
                    failed.append(code)
    
    elapsed = time.time() - start_time
    logger.info(
        f"下载完成: {len(results)} 成功 / {len(failed)} 失败 / "
        f"{total} 总计, 耗时 {elapsed:.1f}秒"
    )
    
    return results


def save_klines_to_db(
    conn: sqlite3.Connection,
    kline_data: Dict[str, List[Dict]],
    batch_size: int = 5000
) -> int:
    """将K线数据批量写入数据库
    
    Args:
        conn: 数据库连接
        kline_data: {code: kline_list, ...}
        batch_size: 每批写入条数
        
    Returns:
        写入的总行数
    """
    total_rows = 0
    
    cursor = conn.cursor()
    batch = []
    
    for code, klines in kline_data.items():
        for k in klines:
            batch.append((
                code,
                k["date"],
                k.get("open"),
                k.get("high"),
                k.get("low"),
                k.get("close"),
                k.get("volume"),
            ))
            
            if len(batch) >= batch_size:
                cursor.executemany(
                    """INSERT OR REPLACE INTO daily_kline 
                       (symbol, trade_date, open, high, low, close, volume, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    batch
                )
                total_rows += len(batch)
                batch.clear()
    
    # 写入剩余
    if batch:
        cursor.executemany(
            """INSERT OR REPLACE INTO daily_kline 
               (symbol, trade_date, open, high, low, close, volume, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            batch
        )
        total_rows += len(batch)
    
    conn.commit()
    return total_rows


def sync_full(
    days: int = 60,
    max_workers: int = 8,
    test_limit: int = 0
) -> Dict:
    """全量同步全A股K线数据
    
    Args:
        days: 获取天数
        max_workers: 并发线程数
        test_limit: 测试模式，只同步前N只（0表示全部）
        
    Returns:
        统计信息字典
    """
    logger.info("=" * 60)
    logger.info("🚀 全A股数据同步 - 全量模式")
    logger.info("=" * 60)
    
    start_time = time.time()
    
    # 1. 初始化数据库
    conn = init_db()
    
    # 2. 获取股票列表
    logger.info("📋 Step 1: 获取全A股股票列表...")
    stocks = get_stock_list()
    if not stocks:
        logger.error("❌ 获取股票列表失败")
        return {"success": False, "error": "获取股票列表失败"}
    
    if test_limit > 0:
        stocks = stocks[:test_limit]
        logger.info(f"🧪 测试模式: 只同步前 {test_limit} 只")
    
    codes = [s["code"] for s in stocks]
    logger.info(f"   共 {len(codes)} 只股票")
    
    # 3. 并发下载K线
    logger.info(f"\n📥 Step 2: 下载 {days} 天K线数据 (并发={max_workers})...")
    kline_data = download_kline_batch(codes, days=days, max_workers=max_workers)
    
    # 4. 写入数据库
    logger.info(f"\n💾 Step 3: 写入数据库...")
    rows = save_klines_to_db(conn, kline_data)
    
    # 5. 统计
    elapsed = time.time() - start_time
    
    # 查询数据库统计
    cursor = conn.cursor()
    symbol_count = cursor.execute("SELECT COUNT(DISTINCT symbol) FROM daily_kline").fetchone()[0]
    total_rows = cursor.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0]
    db_size = DB_PATH.stat().st_size / 1024 / 1024 if DB_PATH.exists() else 0
    
    conn.close()
    
    stats = {
        "success": True,
        "mode": "full",
        "stocks_listed": len(stocks),
        "stocks_downloaded": len(kline_data),
        "stocks_failed": len(stocks) - len(kline_data),
        "rows_written": rows,
        "symbols_in_db": symbol_count,
        "total_rows_in_db": total_rows,
        "db_size_mb": round(db_size, 2),
        "elapsed_seconds": round(elapsed, 1),
        "speed_per_second": round(len(kline_data) / elapsed, 1) if elapsed > 0 else 0
    }
    
    logger.info("\n" + "=" * 60)
    logger.info("✅ 全量同步完成!")
    logger.info("=" * 60)
    logger.info(f"   股票列表: {stats['stocks_listed']} 只")
    logger.info(f"   下载成功: {stats['stocks_downloaded']} 只")
    logger.info(f"   下载失败: {stats['stocks_failed']} 只")
    logger.info(f"   写入行数: {stats['rows_written']}")
    logger.info(f"   DB中股票: {stats['symbols_in_db']} 只")
    logger.info(f"   DB总行数: {stats['total_rows_in_db']}")
    logger.info(f"   DB大小:   {stats['db_size_mb']} MB")
    logger.info(f"   耗时:     {stats['elapsed_seconds']} 秒")
    logger.info(f"   速度:     {stats['speed_per_second']} 只/秒")
    logger.info("=" * 60)
    
    return stats


def sync_incremental(days: int = 1, max_workers: int = 8) -> Dict:
    """增量更新 - 用腾讯实时行情快速更新当天数据
    
    Args:
        days: 获取天数（默认1天，即今天的数据）
        max_workers: 并发线程数
        
    Returns:
        统计信息字典
    """
    logger.info("=" * 60)
    logger.info("🔄 全A股数据同步 - 增量模式（快速）")
    logger.info("=" * 60)
    
    start_time = time.time()
    
    # 1. 检查数据库是否存在
    if not DB_PATH.exists():
        logger.warning("⚠️ 数据库不存在，切换到全量模式")
        return sync_full(max_workers=max_workers)
    
    conn = init_db()
    
    # 2. 获取数据库中已有的股票列表
    cursor = conn.cursor()
    existing_codes = [
        row[0] for row in
        cursor.execute("SELECT DISTINCT symbol FROM daily_kline").fetchall()
    ]
    
    if not existing_codes:
        logger.warning("⚠️ 数据库为空，切换到全量模式")
        conn.close()
        return sync_full(max_workers=max_workers)
    
    logger.info(f"📋 数据库中已有 {len(existing_codes)} 只股票")
    
    # 3. 用腾讯批量行情接口快速获取当天数据（比逐只拉K线快10倍）
    logger.info(f"\n📥 批量获取当天实时行情...")
    from .tencent_provider import TencentFinanceProvider
    tp = TencentFinanceProvider()
    
    # 分批批量获取，每批50只
    batch_size = 50
    today_str = datetime.now().strftime("%Y-%m-%d")
    saved = 0
    failed = 0
    
    for i in range(0, len(existing_codes), batch_size):
        batch = existing_codes[i:i+batch_size]
        tencent_codes = [_code_to_tencent(c) for c in batch]
        
        quotes = tp.get_batch_quotes(tencent_codes)
        
        for code in batch:
            tcode = _code_to_tencent(code)
            q = quotes.get(tcode)
            if q and q.get('price', 0) > 0:
                conn.execute("""
                    INSERT OR REPLACE INTO daily_kline
                    (symbol, trade_date, open, high, low, close, volume, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (
                    code, today_str,
                    q.get('open', 0), q.get('high', 0),
                    q.get('low', 0), q.get('price', 0),
                    q.get('volume', 0)
                ))
                saved += 1
            else:
                failed += 1
        
        done = min(i + batch_size, len(existing_codes))
        if done % 500 == 0 or done == len(existing_codes):
            logger.info(f"  进度: {done}/{len(existing_codes)} ({done*100//len(existing_codes)}%)")
    
    conn.commit()
    
    # 4. 清理过期数据（保留最近120天，供回测使用）
    logger.info("\n🧹 清理过期数据...")
    cutoff_date = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    deleted = cursor.execute(
        "DELETE FROM daily_kline WHERE trade_date < ?", (cutoff_date,)
    ).rowcount
    conn.commit()
    
    # 6. 统计
    elapsed = time.time() - start_time
    symbol_count = cursor.execute("SELECT COUNT(DISTINCT symbol) FROM daily_kline").fetchone()[0]
    total_rows = cursor.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0]
    db_size = DB_PATH.stat().st_size / 1024 / 1024 if DB_PATH.exists() else 0
    
    conn.close()
    
    stats = {
        "success": True,
        "mode": "incremental",
        "stocks_updated": saved,
        "stocks_failed": failed,
        "rows_written": saved,
        "rows_deleted": deleted,
        "symbols_in_db": symbol_count,
        "total_rows_in_db": total_rows,
        "db_size_mb": round(db_size, 2),
        "elapsed_seconds": round(elapsed, 1)
    }
    
    logger.info("\n" + "=" * 60)
    logger.info("✅ 增量同步完成!")
    logger.info("=" * 60)
    logger.info(f"   更新成功: {stats['stocks_updated']} 只")
    logger.info(f"   更新失败: {stats['stocks_failed']} 只")
    logger.info(f"   写入行数: {stats['rows_written']}")
    logger.info(f"   清理行数: {stats['rows_deleted']}")
    logger.info(f"   DB大小:   {stats['db_size_mb']} MB")
    logger.info(f"   耗时:     {stats['elapsed_seconds']} 秒")
    logger.info("=" * 60)
    
    return stats


def get_all_klines_from_db(
    min_days: int = 30,
    conn: sqlite3.Connection = None
) -> Dict[str, List[Dict]]:
    """从数据库读取所有股票的K线数据
    
    Args:
        min_days: 最少需要多少天数据（数据不足的股票会被过滤）
        conn: 数据库连接（可选）
        
    Returns:
        {symbol: [{'date', 'open', 'high', 'low', 'close', 'volume'}, ...], ...}
    """
    close_conn = False
    if conn is None:
        db_path = get_db_path()
        if not db_path.exists():
            logger.warning("数据库不存在")
            return {}
        conn = sqlite3.connect(str(db_path))
        close_conn = True
    
    cursor = conn.cursor()
    
    # 获取数据充足的股票
    rows = cursor.execute("""
        SELECT symbol, COUNT(*) as cnt 
        FROM daily_kline 
        GROUP BY symbol 
        HAVING cnt >= ?
    """, (min_days,)).fetchall()
    
    valid_symbols = [r[0] for r in rows]
    logger.info(f"数据库中数据充足的股票: {len(valid_symbols)} 只 (≥{min_days}天)")
    
    # 批量读取
    result = {}
    for symbol in valid_symbols:
        klines = cursor.execute("""
            SELECT trade_date, open, high, low, close, volume
            FROM daily_kline 
            WHERE symbol = ?
            ORDER BY trade_date ASC
        """, (symbol,)).fetchall()
        
        result[symbol] = [
            {
                "date": r[0],
                "open": float(r[1]) if r[1] else 0,
                "high": float(r[2]) if r[2] else 0,
                "low": float(r[3]) if r[3] else 0,
                "close": float(r[4]) if r[4] else 0,
                "volume": int(r[5]) if r[5] else 0,
            }
            for r in klines
        ]
    
    if close_conn:
        conn.close()

    return result


def get_klines_for_symbol(symbol: str, days: int = 60,
                          conn: sqlite3.Connection = None) -> Optional[List[Dict]]:
    """从数据库读取单只股票的K线数据

    Args:
        symbol: 股票代码（不含前缀）
        days: 获取多少天的数据
        conn: 数据库连接（可选）

    Returns:
        K线数据列表或None
    """
    close_conn = False
    if conn is None:
        db_path = get_db_path()
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path))
        close_conn = True

    # 去掉前缀（sh/sz）
    pure_symbol = symbol.replace("sh", "").replace("sz", "")

    cursor = conn.cursor()
    klines = cursor.execute("""
        SELECT trade_date, open, high, low, close, volume
        FROM daily_kline
        WHERE symbol = ?
        ORDER BY trade_date DESC
        LIMIT ?
    """, (pure_symbol, days)).fetchall()

    result = [
        {
            "date": r[0],
            "open": float(r[1]) if r[1] else 0,
            "high": float(r[2]) if r[2] else 0,
            "low": float(r[3]) if r[3] else 0,
            "close": float(r[4]) if r[4] else 0,
            "volume": int(r[5]) if r[5] else 0,
        }
        for r in reversed(klines)  # 按日期正序
    ]

    if close_conn:
        conn.close()

    return result if result else None


def get_db_stats() -> Dict:
    """获取数据库统计信息"""
    db_path = get_db_path()
    if not db_path.exists():
        return {"exists": False}
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    symbol_count = cursor.execute("SELECT COUNT(DISTINCT symbol) FROM daily_kline").fetchone()[0]
    total_rows = cursor.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0]
    
    # 最新数据日期
    latest = cursor.execute("SELECT MAX(trade_date) FROM daily_kline").fetchone()[0]
    earliest = cursor.execute("SELECT MIN(trade_date) FROM daily_kline").fetchone()[0]
    
    conn.close()
    
    db_size = db_path.stat().st_size / 1024 / 1024
    
    return {
        "exists": True,
        "symbol_count": symbol_count,
        "total_rows": total_rows,
        "latest_date": latest,
        "earliest_date": earliest,
        "db_size_mb": round(db_size, 2)
    }


# ============================================================
# CLI 入口
# ============================================================
if __name__ == "__main__":
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )
    
    parser = argparse.ArgumentParser(description="全A股K线数据同步")
    parser.add_argument("--incremental", "-i", action="store_true", help="增量更新模式")
    parser.add_argument("--full", "-f", action="store_true", help="全量同步模式（默认）")
    parser.add_argument("--days", "-d", type=int, default=60, help="获取天数（默认60）")
    parser.add_argument("--workers", "-w", type=int, default=8, help="并发线程数（默认8）")
    parser.add_argument("--test", "-t", type=int, default=0, help="测试模式：只同步前N只")
    parser.add_argument("--stats", "-s", action="store_true", help="显示数据库统计")
    
    args = parser.parse_args()
    
    if args.stats:
        stats = get_db_stats()
        if not stats.get("exists"):
            print("❌ 数据库不存在，请先运行同步")
        else:
            print(f"📊 数据库统计:")
            print(f"   股票数量: {stats['symbol_count']}")
            print(f"   总行数:   {stats['total_rows']}")
            print(f"   日期范围: {stats['earliest_date']} ~ {stats['latest_date']}")
            print(f"   文件大小: {stats['db_size_mb']} MB")
    elif args.incremental:
        sync_incremental(days=args.days, max_workers=args.workers)
    else:
        sync_full(days=args.days, max_workers=args.workers, test_limit=args.test)
