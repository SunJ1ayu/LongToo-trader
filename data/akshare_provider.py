#!/usr/bin/env python3
"""
A股实时行情数据提供者
mootdx (通达信 TCP) 为主数据源，腾讯财经为备用
"""

import logging
import time
import json
import functools
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import pandas as pd

logger = logging.getLogger(__name__)

# 股票名称缓存文件（与 market_sync 共用）
_STOCK_LIST_CACHE = Path(__file__).parent.parent.parent / "data" / "stock_list_cache.json"


def timeout_handler(seconds: int):
    """超时装饰器"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(func, *args, **kwargs)
                try:
                    return future.result(timeout=seconds)
                except FuturesTimeoutError:
                    logger.error(f"函数 {func.__name__} 执行超时 ({seconds}s)")
                    return None
        return wrapper
    return decorator


class AkShareProvider:
    """AkShare 数据提供者（腾讯财经备用）"""

    # 缓存配置
    _CACHE_TTL = 3  # 缓存有效期（秒）
    _FULL_CACHE_TTL = 5  # 全市场缓存有效期（秒）

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self._cache = {}
        self._full_market_cache = None
        self._full_market_cache_time = 0
        self._mootdx = None
        self._stock_names = None

        try:
            from .tencent_provider import TencentFinanceProvider
            self._fallback = TencentFinanceProvider(timeout=timeout)
        except Exception as e:
            self._fallback = None
            logger.warning(f"腾讯财经备用初始化失败: {e}")

        try:
            import akshare as ak
            self.ak = ak
        except ImportError:
            self.ak = None

    def _init_mootdx(self):
        """延迟初始化 mootdx（避免 TCP 连接在非交易时段浪费资源）"""
        if self._mootdx is not None:
            return self._mootdx
        try:
            from mootdx.quotes import Quotes
            self._mootdx = Quotes.factory(market='standard')
            logger.info("mootdx 数据源已初始化（通达信 TCP）")
        except Exception as e:
            logger.warning(f"mootdx 初始化失败: {e}")
        return self._mootdx

    def _load_stock_names(self) -> Dict[str, str]:
        """从缓存或 mootdx 加载 code->name 映射"""
        if self._stock_names is not None:
            return self._stock_names

        names = {}
        # 1. 尝试从缓存文件加载
        try:
            if _STOCK_LIST_CACHE.exists():
                data = json.loads(_STOCK_LIST_CACHE.read_text(encoding='utf-8'))
                for s in data.get('stocks', []):
                    names[s['code']] = s['name']
                if names:
                    self._stock_names = names
                    return names
        except Exception:
            pass

        # 2. 从 mootdx 获取
        try:
            client = self._init_mootdx()
            if client:
                df = client.stocks()
                if df is not None and len(df) > 0:
                    for _, row in df.iterrows():
                        code = str(row['code'])
                        if code.isdigit() and len(code) == 6:
                            names[code] = str(row['name'])
            self._stock_names = names
        except Exception as e:
            logger.warning(f"从 mootdx 获取股票名称失败: {e}")

        return names

    def _normalize_symbol(self, symbol: str) -> str:
        """标准化股票代码，返回纯代码"""
        if symbol.startswith(("sh", "sz", "SH", "SZ")):
            return symbol[2:]
        if "." in symbol:
            return symbol.split(".")[0]
        return symbol

    def _get_from_cache(self, key: tuple, ttl: int = None) -> Optional[any]:
        """从缓存获取数据"""
        ttl = ttl or self._CACHE_TTL
        if key in self._cache:
            ts, data = self._cache[key]
            if time.time() - ts < ttl:
                return data
        return None

    def _set_cache(self, key: tuple, data: any):
        """设置缓存"""
        self._cache[key] = (time.time(), data)

    @timeout_handler(30)
    def _fetch_full_market_data(self) -> Optional[any]:
        """获取全市场快照数据（mootdx 通达信 TCP）"""
        now = time.time()

        if self._full_market_cache is not None:
            if now - self._full_market_cache_time < self._FULL_CACHE_TTL:
                return self._full_market_cache

        df = self._fetch_full_market_data_from_mootdx()
        if df is not None and not df.empty:
            self._full_market_cache = df
            self._full_market_cache_time = now
            return df

        logger.error("mootdx 全市场快照失败")
        return self._full_market_cache

    def _fetch_full_market_data_from_mootdx(self) -> Optional[pd.DataFrame]:
        """通过 mootdx 获取全市场快照，返回与 akshare stock_zh_a_spot_em 兼容的 DataFrame"""
        try:
            client = self._init_mootdx()
            if client is None:
                return None

            # 从数据库获取已拉取的股票代码
            import sqlite3
            db_path = Path(__file__).parent.parent.parent / "data" / "market_kline.db"
            if not db_path.exists():
                logger.warning("market_kline.db 不存在，无法通过 mootdx 获取全市场数据")
                return None

            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            codes = [r[0] for r in cursor.execute(
                "SELECT DISTINCT symbol FROM daily_kline"
            ).fetchall()]
            conn.close()

            if not codes:
                logger.warning("数据库中无股票数据")
                return None

            logger.info(f"mootdx 批量获取 {len(codes)} 只股票行情...")
            names = self._load_stock_names()

            # 分批获取，每批 80 只（通达信单次请求上限）
            BATCH = 80
            all_rows = []
            for i in range(0, len(codes), BATCH):
                batch = codes[i:i + BATCH]
                try:
                    df = client.quotes(symbol=batch)
                    if df is not None and len(df) > 0:
                        all_rows.append(df)
                except Exception as e:
                    logger.debug(f"mootdx 批量查询失败 (offset={i}): {e}")
                    continue

            if not all_rows:
                return None

            full = pd.concat(all_rows, ignore_index=True)

            # 映射到 akshare 列名
            result = pd.DataFrame()
            result["代码"] = full["code"].astype(str)
            result["名称"] = full["code"].astype(str).map(names).fillna("")
            result["最新价"] = pd.to_numeric(full["price"], errors="coerce")
            result["今开"] = pd.to_numeric(full["open"], errors="coerce")
            result["最高价"] = pd.to_numeric(full["high"], errors="coerce")
            result["最低价"] = pd.to_numeric(full["low"], errors="coerce")
            result["昨收"] = pd.to_numeric(full["last_close"], errors="coerce")
            result["成交量"] = pd.to_numeric(full["vol"], errors="coerce")
            result["成交额"] = pd.to_numeric(full["amount"], errors="coerce")
            # 计算涨跌幅
            prev = result["昨收"]
            price = result["最新价"]
            result["涨跌幅"] = ((price - prev) / prev.replace(0, pd.NA) * 100).fillna(0)

            logger.info(f"mootdx 全市场快照: {len(result)} 只")
            return result

        except Exception as e:
            logger.error(f"mootdx 全市场快照失败: {e}")
            return None

    def get_realtime_quote(self, symbol: str) -> Optional[Dict]:
        """
        获取实时行情

        Args:
            symbol: 股票代码，如 "sh601318", "000001" 或 "000001.SZ"

        Returns:
            行情字典或 None
        """
        code = self._normalize_symbol(symbol)

        # 检查缓存
        cache_key = ("quote", code)
        cached = self._get_from_cache(cache_key)
        if cached:
            return cached

        df = self._fetch_full_market_data()
        if df is not None and not df.empty:
            stock_row = df[df["代码"] == code]
            if not stock_row.empty:
                row = stock_row.iloc[0]

                def safe_float(val, default=0.0):
                    try:
                        return float(val) if pd.notna(val) else default
                    except (ValueError, TypeError):
                        return default

                result = {
                    "symbol": code,
                    "name": str(row["名称"]),
                    "price": safe_float(row["最新价"]),
                    "open": safe_float(row["今开"]),
                    "high": safe_float(row["最高价"]),
                    "low": safe_float(row["最低价"]),
                    "prev_close": safe_float(row["昨收"]),
                    "volume": int(safe_float(row["成交量"], 0)),
                    "amount": safe_float(row["成交额"]),
                    "change_pct": safe_float(row["涨跌幅"]),
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                self._set_cache(cache_key, result)
                return result
        return None

    def get_batch_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """
        批量获取行情（高效实现）

        Args:
            symbols: 股票代码列表

        Returns:
            {symbol: quote_dict, ...}
        """
        results = {}

        try:
            df = self._fetch_full_market_data()
            if df is None or df.empty:
                logger.warning("全市场数据获取失败")
                return {}

            import pandas as pd

            def safe_float(val, default=0.0):
                try:
                    return float(val) if pd.notna(val) else default
                except (ValueError, TypeError):
                    return default

            for symbol in symbols:
                code = self._normalize_symbol(symbol)
                stock_row = df[df["代码"] == code]

                if not stock_row.empty:
                    row = stock_row.iloc[0]
                    results[code] = {
                        "symbol": code,
                        "name": str(row["名称"]),
                        "price": safe_float(row["最新价"]),
                        "open": safe_float(row["今开"]),
                        "high": safe_float(row["最高价"]),
                        "low": safe_float(row["最低价"]),
                        "prev_close": safe_float(row["昨收"]),
                        "volume": int(safe_float(row["成交量"], 0)),
                        "change_pct": safe_float(row["涨跌幅"]),
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                else:
                    logger.debug(f"未找到股票 {code}")

            return results

        except Exception as e:
            logger.error(f"批量获取行情失败: {e}")
            return {}

    def get_historical_data(self, symbol: str, period: str = "daily",
                           start_date: str = None, end_date: str = None) -> Optional[List[Dict]]:
        """
        获取历史数据

        Args:
            symbol: 股票代码
            period: 周期（daily/weekly/monthly）
            start_date: 开始日期（YYYYMMDD）
            end_date: 结束日期（YYYYMMDD）
        """
        if self.ak is None:
            return None

        code = self._normalize_symbol(symbol)

        cache_key = ("hist", code, period, start_date, end_date)
        cached = self._get_from_cache(cache_key, ttl=60)
        if cached:
            return cached

        try:
            if not end_date:
                end_date = datetime.now().strftime("%Y%m%d")
            if not start_date:
                start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

            df = self.ak.stock_zh_a_hist(
                symbol=code,
                period=period,
                start_date=start_date,
                end_date=end_date,
                adjust="qfq"
            )

            if df is None or df.empty:
                logger.warning(f"历史数据为空: {code}")
                return None

            data = []
            for _, row in df.iterrows():
                try:
                    data.append({
                        "date": str(row["日期"]),
                        "open": float(row["开盘"]),
                        "high": float(row["最高"]),
                        "low": float(row["最低"]),
                        "close": float(row["收盘"]),
                        "volume": int(row["成交量"]),
                        "amount": float(row["成交额"])
                    })
                except (KeyError, ValueError) as e:
                    logger.debug(f"解析历史数据行失败: {e}")
                    continue

            self._set_cache(cache_key, data)
            return data

        except Exception as e:
            logger.error(f"获取历史数据失败 {symbol}: {e}")
            return None

    def get_stock_list(self) -> List[Dict]:
        """获取股票列表"""
        try:
            df = self._fetch_full_market_data()
            if df is None:
                return []

            stocks = []
            for _, row in df.iterrows():
                try:
                    stocks.append({
                        "symbol": str(row["代码"]),
                        "name": str(row["名称"]),
                        "price": float(row["最新价"]) if pd.notna(row["最新价"]) else 0,
                        "change_pct": float(row["涨跌幅"]) if pd.notna(row["涨跌幅"]) else 0
                    })
                except (KeyError, ValueError):
                    continue
            return stocks

        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            return []

    def get_index_quote(self, index_symbol: str = "000001") -> Optional[Dict]:
        """
        获取大盘指数行情（mootdx）

        Args:
            index_symbol: 指数代码 "000001" 上证指数 / "399001" 深证成指 / "399006" 创业板指
        """
        clean = self._normalize_symbol(index_symbol)
        # mootdx 中上证指数代码为 999999
        mootdx_code = "999999" if clean == "000001" else clean
        try:
            client = self._init_mootdx()
            if client:
                df = client.quotes(symbol=[mootdx_code])
                if df is not None and not df.empty:
                    row = df.iloc[0]
                    price = float(row['price'])
                    prev = float(row['last_close'])
                    name_map = {"999999": "上证指数", "399001": "深证成指", "399006": "创业板指"}
                    name = name_map.get(mootdx_code, str(row.get('code', '')))
                    return {
                        "symbol": index_symbol,
                        "name": name,
                        "price": price,
                        "open": float(row['open']),
                        "high": float(row['high']),
                        "low": float(row['low']),
                        "prev_close": prev,
                        "change_pct": ((price - prev) / prev * 100) if prev > 0 else 0,
                        "ma10": None,
                        "ma30": None
                    }
        except Exception as e:
            logger.error(f"mootdx 指数行情失败 {index_symbol}: {e}")
        return None

    def clear_cache(self):
        """清除缓存"""
        self._cache.clear()
        self._full_market_cache = None
        self._full_market_cache_time = 0
        logger.debug("缓存已清除")


# 测试代码
if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    provider = AkShareProvider()

    # 测试单只股票
    print("\n=== 测试 get_realtime_quote ===")
    test_symbols = ["sh601318", "000001", "600519.SS"]
    for sym in test_symbols:
        quote = provider.get_realtime_quote(sym)
        if quote:
            print(f"✓ {sym}: {quote['name']} ¥{quote['price']:.2f} ({quote['change_pct']:+.2f}%)")
        else:
            print(f"✗ {sym}: 获取失败")

    # 测试批量获取
    print("\n=== 测试 get_batch_quotes ===")
    quotes = provider.get_batch_quotes(["000001", "600519", "000858"])
    for code, q in quotes.items():
        print(f"✓ {code}: {q['name']} ¥{q['price']:.2f}")

    # 测试指数
    print("\n=== 测试 get_index_quote ===")
    index = provider.get_index_quote("000001")
    if index:
        print(f"✓ 上证指数: ¥{index['price']:.2f} MA10={index['ma10']:.2f} MA30={index['ma30']:.2f}")
