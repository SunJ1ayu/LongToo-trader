#!/usr/bin/env python3
"""
腾讯财经数据提供者
免费A股实时行情API，响应快速
API: http://qt.gtimg.cn/q=sh601318,sz000001
"""

import logging
import time
import requests
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class TencentFinanceProvider:
    """腾讯财经数据提供者"""
    
    BASE_URL = "http://qt.gtimg.cn/q={}"
    
    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        self._cache = {}
        self._cache_ttl = 3  # 缓存3秒
        
    def _normalize_symbol(self, symbol: str) -> str:
        """标准化股票代码为腾讯格式"""
        symbol = symbol.lower().strip()
        
        # 处理带后缀格式
        if symbol.endswith('.ss') or symbol.endswith('.sh'):
            symbol = 'sh' + symbol[:-3]
        elif symbol.endswith('.sz'):
            symbol = 'sz' + symbol[:-3]
            
        # 确保有前缀
        if not symbol.startswith('sh') and not symbol.startswith('sz'):
            if len(symbol) == 6 and symbol.isdigit():
                if symbol.startswith('6') or symbol.startswith('5'):
                    symbol = f"sh{symbol}"
                else:
                    symbol = f"sz{symbol}"
                    
        return symbol
    
    def get_realtime_quote(self, symbol: str) -> Optional[Dict]:
        """获取单只股票实时行情"""
        code = self._normalize_symbol(symbol)
        
        # 检查缓存
        cache_key = f"quote_{code}"
        if cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                return cached_data
        
        try:
            url = self.BASE_URL.format(code)
            response = self.session.get(url, timeout=self.timeout)
            response.encoding = 'gb2312'
            
            data = response.text
            
            # 解析数据
            # 格式: v_sh601318="1~中国平安~601318~58.52~58.70~58.71~...";
            if '~' not in data:
                logger.warning(f"Invalid response for {code}")
                return None
                
            # 提取内容
            prefix = f'v_{code}="'
            if prefix not in data:
                logger.warning(f"Unexpected format for {code}")
                return None
                
            content = data.split(prefix)[1].rstrip('";')
            fields = content.split('~')
            
            if len(fields) < 45:
                logger.warning(f"Incomplete data for {code}: {len(fields)} fields")
                return None
            
            # 解析字段
            # 字段索引: https://www.jianshu.com/p/3d62b2a2f060
            try:
                price = float(fields[3])
                prev_close = float(fields[4])
                # 手动计算涨跌幅（API返回的fields[32]精度不够）
                if prev_close > 0:
                    change_pct = (price - prev_close) / prev_close * 100
                else:
                    change_pct = 0.0

                result = {
                    'symbol': code,
                    'name': fields[1],
                    'code': fields[2],
                    'price': price,
                    'prev_close': prev_close,
                    'open': float(fields[5]),
                    'high': float(fields[33]),
                    'low': float(fields[34]),
                    'volume': int(fields[6]) * 100,  # 手数转股数
                    'amount': float(fields[37]) * 10000,  # 万元转元
                    'change_pct': change_pct,  # 使用手动计算的涨跌幅
                    'bid1': float(fields[9]),
                    'ask1': float(fields[19]),
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            except (ValueError, IndexError) as e:
                logger.error(f"Parse error for {code}: {e}, fields: {fields[:10]}")
                return None
                
            # 缓存结果
            self._cache[cache_key] = (time.time(), result)
            
            return result
            
        except Exception as e:
            logger.error(f"获取行情失败 {symbol}: {e}")
            return None
    
    def get_batch_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """批量获取股票行情（腾讯支持一次请求多个）"""
        codes = [self._normalize_symbol(s) for s in symbols]
        codes_str = ','.join(codes)
        
        results = {}
        try:
            url = self.BASE_URL.format(codes_str)
            response = self.session.get(url, timeout=self.timeout)
            response.encoding = 'gb2312'
            
            data = response.text
            
            # 解析多个股票数据
            for code in codes:
                prefix = f'v_{code}="'
                if prefix not in data:
                    continue
                    
                content = data.split(prefix)[1].split('";')[0]
                fields = content.split('~')
                
                if len(fields) < 45:
                    continue
                    
                try:
                    result = {
                        'symbol': code,
                        'name': fields[1],
                        'code': fields[2],
                        'price': float(fields[3]),
                        'prev_close': float(fields[4]),
                        'open': float(fields[5]),
                        'high': float(fields[33]),
                        'low': float(fields[34]),
                        'volume': int(fields[6]) * 100,
                        'amount': float(fields[37]) * 10000,
                        'change_pct': float(fields[32]),
                        'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    results[code] = result
                    self._cache[f"quote_{code}"] = (time.time(), result)
                except:
                    continue
                    
        except Exception as e:
            logger.error(f"批量获取失败: {e}")
            
        return results
    
    def get_index_quote(self, index_symbol: str = '000001') -> Optional[Dict]:
        """获取指数行情"""
        if index_symbol == '000001':
            return self.get_realtime_quote('sh000001')
        return self.get_realtime_quote(index_symbol)
    
    def cache_to_klines_db(self, symbol: str, quote: Dict) -> bool:
        """
        将实时行情缓存到K线数据库
        用于攒历史数据，不依赖AkShare
        """
        try:
            import sqlite3
            from pathlib import Path
            
            db_path = Path.home() / ".openclaw" / "workspace" / "memory" / "trading.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 确保表存在
            with sqlite3.connect(db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS stock_kline_cache (
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
                
                # 插入或更新数据
                date_str = quote.get('date', datetime.now().strftime("%Y-%m-%d"))
                conn.execute("""
                    INSERT OR REPLACE INTO stock_kline_cache 
                    (symbol, trade_date, open, high, low, close, volume, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (
                    symbol.replace("sh", "").replace("sz", ""),
                    date_str,
                    quote.get('open', 0),
                    quote.get('high', 0),
                    quote.get('low', 0),
                    quote.get('price', 0),
                    quote.get('volume', 0)
                ))
                conn.commit()
                return True
        except Exception as e:
            logger.debug(f"缓存K线数据失败 {symbol}: {e}")
            return False
    
    def get_historical_kline(self, symbol: str, days: int = 60) -> Optional[List[Dict]]:
        """获取历史K线数据（腾讯财经）
        
        Args:
            symbol: 股票代码
            days: 获取天数
            
        Returns:
            K线数据列表，每条包含 date, open, high, low, close, volume
        """
        code = self._normalize_symbol(symbol)
        try:
            url = 'http://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
            params = {'param': f'{code},day,,,{days},qfq'}
            response = self.session.get(url, params=params, timeout=self.timeout)
            data = response.json()
            
            stock_data = data.get('data', {}).get(code, {})
            # 优先取前复权数据
            klines = stock_data.get('qfqday') or stock_data.get('day') or []
            
            if not klines:
                logger.warning(f"腾讯财经无K线数据: {symbol}")
                return None
            
            result = []
            for k in klines:
                # 格式: [date, open, close, high, low, volume]
                if len(k) >= 6:
                    result.append({
                        'date': k[0],
                        'open': float(k[1]),
                        'close': float(k[2]),
                        'high': float(k[3]),
                        'low': float(k[4]),
                        'volume': int(float(k[5]))
                    })
            
            return result if result else None
            
        except Exception as e:
            logger.error(f"腾讯财经获取K线失败 {symbol}: {e}")
            return None

    def get_index_kline(self, index_symbol: str = '000001', days: int = 35) -> Optional[Dict]:
        """获取指数K线数据（腾讯财经）
        
        Args:
            index_symbol: 指数代码（如 000001）
            days: 获取天数
            
        Returns:
            包含 klines, current, change_pct, ma10, ma30 的字典
        """
        # 指数代码加 sh 前缀
        code = f'sh{index_symbol}' if not index_symbol.startswith('sh') and not index_symbol.startswith('sz') else index_symbol
        
        try:
            url = 'http://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
            params = {'param': f'{code},day,,,{days},'}
            response = self.session.get(url, params=params, timeout=self.timeout)
            data = response.json()
            
            stock_data = data.get('data', {}).get(code, {})
            klines = stock_data.get('day') or []
            
            if not klines:
                logger.warning(f"腾讯财经无指数数据: {index_symbol}")
                return None
            
            # 解析K线
            parsed = []
            for k in klines:
                if len(k) >= 6:
                    parsed.append({
                        'date': k[0],
                        'open': float(k[1]),
                        'close': float(k[2]),
                        'high': float(k[3]),
                        'low': float(k[4]),
                        'volume': int(float(k[5]))
                    })
            
            if not parsed:
                return None
            
            closes = [k['close'] for k in parsed]
            current = closes[-1]
            prev = closes[-2] if len(closes) >= 2 else current
            change_pct = ((current - prev) / prev * 100) if prev else 0
            
            # 计算均线
            def calc_ma(data, n):
                if len(data) < n:
                    return None
                return sum(data[-n:]) / n
            
            return {
                'klines': parsed,
                'current': current,
                'change_pct': round(change_pct, 2),
                'ma10': calc_ma(closes, 10),
                'ma30': calc_ma(closes, 30),
                'high': max(k['high'] for k in parsed[-1:]),
                'low': min(k['low'] for k in parsed[-1:]),
                'volume': parsed[-1]['volume']
            }
            
        except Exception as e:
            logger.error(f"腾讯财经获取指数失败 {index_symbol}: {e}")
            return None

    def clear_cache(self):
        """清除缓存"""
        self._cache.clear()


# 测试
if __name__ == "__main__":
    provider = TencentFinanceProvider()
    
    # 测试单只股票
    print("测试单只股票:")
    data = provider.get_realtime_quote('sh601318')
    if data:
        print(f"{data['name']}: ¥{data['price']:.2f} ({data['change_pct']:+.2f}%)")
    else:
        print("获取失败")
    
    # 测试批量
    print("\n测试批量获取:")
    symbols = ['sh601318', 'sz000001', 'sz000858', 'sh601888']
    start = time.time()
    batch = provider.get_batch_quotes(symbols)
    for sym, data in batch.items():
        print(f"{sym}: ¥{data['price']:.2f} ({data['change_pct']:+.2f}%)")
    print(f"总耗时: {time.time()-start:.2f}秒")
