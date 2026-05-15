#!/usr/bin/env python3
"""
新浪财经数据提供者
免费A股实时行情API，响应快速稳定
API: https://hq.sinajs.cn/list=sh601318,sz000001
"""

import logging
import time
import requests
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class SinaFinanceProvider:
    """新浪财经数据提供者"""
    
    BASE_URL = "https://hq.sinajs.cn/list={}"
    
    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        self._cache = {}
        self._cache_ttl = 3  # 缓存3秒
        
    def _normalize_symbol(self, symbol: str) -> str:
        """标准化股票代码"""
        symbol = symbol.lower().strip()
        
        # 处理带后缀格式
        if symbol.endswith('.ss') or symbol.endswith('.sz'):
            symbol = symbol[:-3]
        elif symbol.endswith('.sh') or symbol.endswith('.sz'):
            symbol = symbol[:-3]
            
        # 处理前缀格式
        if symbol.startswith('sh') or symbol.startswith('sz'):
            return symbol
            
        # 纯数字，根据代码规则判断
        if len(symbol) == 6 and symbol.isdigit():
            if symbol.startswith('6') or symbol.startswith('5'):
                return f"sh{symbol}"
            else:
                return f"sz{symbol}"
                
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
            
            # 解析返回数据
            # 格式: var hq_str_sh601318="中国平安,48.50,48.60,48.55,48.70,48.40,...";
            data = response.text
            
            if '="' not in data or 'var hq_str_' not in data:
                logger.warning(f"Invalid response for {code}")
                return None
                
            # 提取数据部分
            content = data.split('="')[1].rstrip('";')
            
            if not content or content == '':
                logger.warning(f"Empty data for {code}")
                return None
                
            fields = content.split(',')
            
            if len(fields) < 33:
                logger.warning(f"Incomplete data for {code}: {len(fields)} fields")
                return None
            
            # 解析字段
            # 字段说明: https://blog.csdn.net/afgasdg/article/details/8606489
            result = {
                'symbol': code,
                'name': fields[0],
                'open': float(fields[1]),
                'prev_close': float(fields[2]),
                'price': float(fields[3]),
                'high': float(fields[4]),
                'low': float(fields[5]),
                'bid': float(fields[6]),
                'ask': float(fields[7]),
                'volume': int(fields[8]),
                'amount': float(fields[9]),
                'bid1_vol': int(fields[10]),
                'bid1': float(fields[11]),
                'bid2_vol': int(fields[12]),
                'bid2': float(fields[13]),
                'bid3_vol': int(fields[14]),
                'bid3': float(fields[15]),
                'bid4_vol': int(fields[16]),
                'bid4': float(fields[17]),
                'bid5_vol': int(fields[18]),
                'bid5': float(fields[19]),
                'ask1_vol': int(fields[20]),
                'ask1': float(fields[21]),
                'ask2_vol': int(fields[22]),
                'ask2': float(fields[23]),
                'ask3_vol': int(fields[24]),
                'ask3': float(fields[25]),
                'ask4_vol': int(fields[26]),
                'ask4': float(fields[27]),
                'ask5_vol': int(fields[28]),
                'ask5': float(fields[29]),
                'date': fields[30],
                'time': fields[31],
                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            # 计算涨跌幅
            if result['prev_close'] > 0:
                result['change_pct'] = (result['price'] - result['prev_close']) / result['prev_close'] * 100
            else:
                result['change_pct'] = 0.0
                
            # 缓存结果
            self._cache[cache_key] = (time.time(), result)
            
            return result
            
        except Exception as e:
            logger.error(f"获取行情失败 {symbol}: {e}")
            return None
    
    def get_batch_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """批量获取股票行情"""
        results = {}
        for symbol in symbols:
            data = self.get_realtime_quote(symbol)
            if data:
                results[symbol] = data
            time.sleep(0.1)  # 避免请求过快
        return results
    
    def get_index_quote(self, index_symbol: str = '000001') -> Optional[Dict]:
        """获取指数行情"""
        # 上证指数: sh000001
        if index_symbol == '000001':
            return self.get_realtime_quote('sh000001')
        return self.get_realtime_quote(index_symbol)
    
    def clear_cache(self):
        """清除缓存"""
        self._cache.clear()


# 测试
if __name__ == "__main__":
    provider = SinaFinanceProvider()
    
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
        print(f"{sym}: ¥{data['price']:.2f}")
    print(f"总耗时: {time.time()-start:.2f}秒")
