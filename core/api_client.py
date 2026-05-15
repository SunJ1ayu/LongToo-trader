#!/usr/bin/env python3
"""券商 API 客户端模块 - 增强版（带熔断器、限流器和重试机制）"""

import requests
import json
import time
import logging
import signal
from typing import Dict, List, Optional
from datetime import datetime
from functools import wraps

# 导入熔断器和限流器
from ..risk.circuit_breaker import get_circuit_breaker
from ..risk.rate_limiter import get_rate_limiter

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def timeout_decorator(seconds: int):
    """超时装饰器 - 限制函数执行时间"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            def handler(signum, frame):
                raise TimeoutError(f"函数 {func.__name__} 执行超时（{seconds}秒）")
            
            # 设置信号处理器（仅在 Unix-like 系统有效）
            try:
                old_handler = signal.signal(signal.SIGALRM, handler)
                signal.alarm(seconds)
                try:
                    result = func(*args, **kwargs)
                    signal.alarm(0)  # 取消闹钟
                    return result
                finally:
                    signal.signal(signal.SIGALRM, old_handler)
            except ValueError:
                # Windows 不支持 SIGALRM，直接执行
                return func(*args, **kwargs)
        return wrapper
    return decorator

class LiveAPIClient:
    """实盘 API 客户端 - 带熔断器、限流器和重试机制"""
    
    def __init__(self, api_base: str, auth_token: str, max_retries: int = 3):
        self.api_base = api_base
        self.auth_token = auth_token
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        })
        
        # 集成熔断器和限流器
        self.circuit_breaker = get_circuit_breaker('instreet_api')
        self.rate_limiter = get_rate_limiter('instreet_api')
    
    def _make_request_with_retry(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        """带熔断器、限流器和重试机制的HTTP请求"""
        # 1. 检查熔断器状态
        if not self.circuit_breaker.can_execute():
            print(f"🚫 熔断器开启，拒绝请求: {url}")
            return None
        
        # 2. 检查限流器
        if not self.rate_limiter.try_acquire():
            print(f"⏱️ 触发限流，等待后重试...")
            time.sleep(1)  # 简单等待后重试
            if not self.rate_limiter.try_acquire():
                print(f"🚫 限流器持续触发，跳过请求")
                return None
        
        for attempt in range(self.max_retries):
            try:
                response = self.session.request(method, url, timeout=30, **kwargs)
                
                # 记录成功/失败
                if response.status_code < 500:
                    self.circuit_breaker.record_success()
                else:
                    self.circuit_breaker.record_failure()
                
                # 如果是5xx错误，重试
                if response.status_code >= 500 and attempt < self.max_retries - 1:
                    wait_time = 2 ** attempt  # 指数退避
                    print(f"⚠️ 服务器错误 {response.status_code}，{wait_time}秒后重试...")
                    time.sleep(wait_time)
                    continue
                return response
            except requests.Timeout:
                self.circuit_breaker.record_failure()
                if attempt < self.max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"⏱️ 请求超时，{wait_time}秒后重试...")
                    time.sleep(wait_time)
                else:
                    print(f"❌ 请求超时，已重试{self.max_retries}次")
                    return None
            except requests.RequestException as e:
                self.circuit_breaker.record_failure()
                if attempt < self.max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"⚠️ 网络错误: {e}，{wait_time}秒后重试...")
                    time.sleep(wait_time)
                else:
                    print(f"❌ 网络错误: {e}，已重试{self.max_retries}次")
                    return None
        return None
    
    def _validate_response_data(self, data: Dict, required_fields: List[str]) -> bool:
        """验证响应数据是否包含必需字段"""
        return all(field in data for field in required_fields)
    
    def get_portfolio(self) -> Dict:
        """获取持仓信息 - 带验证和重试"""
        url = f"{self.api_base}/arena/portfolio"
        response = self._make_request_with_retry("GET", url)
        
        if response is None:
            return {"error": "请求失败，请检查网络连接"}
        
        try:
            if response.status_code == 200:
                data = response.json()
                # 验证响应结构
                if not self._validate_response_data(data, ["data"]):
                    print(f"⚠️ API响应格式异常: 缺少'data'字段")
                    return {"error": "API响应格式异常"}
                return data["data"]
            else:
                print(f"❌ 获取持仓失败: HTTP {response.status_code}")
                return {"error": f"HTTP {response.status_code}"}
        except json.JSONDecodeError as e:
            print(f"❌ JSON解析失败: {e}")
            return {"error": "响应解析失败"}
        except Exception as e:
            print(f"❌ 处理响应时出错: {e}")
            return {"error": str(e)}
    
    def get_stock_price(self, symbol: str) -> Optional[float]:
        """获取股票当前价格 - 带验证和重试"""
        try:
            api_symbol = f"sh{symbol}" if symbol.startswith("6") else f"sz{symbol}"
            url = f"{self.api_base}/arena/stocks"
            params = {"search": api_symbol, "limit": 1}
            
            response = self._make_request_with_retry("GET", url, params=params)
            if response is None:
                return None
            
            if response.status_code == 200:
                data = response.json()
                # 验证响应结构
                if not self._validate_response_data(data, ["data"]):
                    print(f"⚠️ 价格API响应格式异常")
                    return None
                
                stocks = data.get("data", {}).get("stocks", [])
                if not stocks:
                    print(f"⚠️ 未找到股票 {symbol}")
                    return None
                
                # 验证价格数据
                price = stocks[0].get("price")
                if price is None:
                    print(f"⚠️ 股票 {symbol} 缺少价格字段")
                    return None
                
                # 验证价格合理性
                if price <= 0:
                    print(f"⚠️ 股票 {symbol} 价格异常: {price}")
                    return None
                
                return float(price)
            else:
                print(f"❌ 获取价格失败: HTTP {response.status_code}")
                return None
                
        except ValueError as e:
            print(f"❌ 价格数据转换失败 {symbol}: {e}")
            return None
        except Exception as e:
            print(f"❌ 获取 {symbol} 价格异常: {e}")
            return None
    
    def execute_order(self, symbol: str, action: str, quantity: int) -> Dict:
        """执行交易订单 - 带验证和重试"""
        url = f"{self.api_base}/trade/order"
        payload = {
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "order_type": "market"
        }
        
        response = self._make_request_with_retry("POST", url, json=payload)
        
        if response is None:
            return {
                "success": False, 
                "error": "网络请求失败，请检查连接", 
                "mode": "manual_required"
            }
        
        try:
            # 检查响应内容类型
            content_type = response.headers.get('content-type', '')
            if 'application/json' not in content_type:
                return {
                    "success": False, 
                    "error": f"API返回非JSON数据. 状态码: {response.status_code}",
                    "mode": "manual_required"
                }
            
            result = response.json()
            
            # 验证响应结构
            if not isinstance(result, dict):
                return {
                    "success": False, 
                    "error": "API返回格式异常", 
                    "mode": "manual_required"
                }
            
            # 检查部分成交
            if result.get("success"):
                filled = result.get("filled_quantity", quantity)
                if filled < quantity:
                    result["partial_fill"] = True
                    result["warning"] = f"部分成交: {filled}/{quantity}"
                    print(f"  ⚠️ {symbol} 部分成交: {filled}/{quantity}")
            
            return result
            
        except json.JSONDecodeError as e:
            return {
                "success": False, 
                "error": f"JSON解析失败: {str(e)}", 
                "mode": "manual_required"
            }
        except Exception as e:
            return {
                "success": False, 
                "error": f"处理响应失败: {str(e)}", 
                "mode": "manual_required"
            }
    
    def confirm_order(self, order_id: str, max_wait: int = 5) -> bool:
        """确认订单是否成功执行"""
        url = f"{self.api_base}/trade/order/{order_id}"
        
        for attempt in range(max_wait):
            try:
                response = self._make_request_with_retry("GET", url)
                if response and response.status_code == 200:
                    data = response.json()
                    status = data.get("data", {}).get("status", "")
                    if status in ["filled", "completed"]:
                        return True
                    elif status in ["failed", "rejected"]:
                        return False
                time.sleep(1)
            except Exception as e:
                print(f"⚠️ 确认订单失败: {e}")
                time.sleep(1)
        
        return False


class DataProvider:
    """数据提供者 - 使用AKShare获取真实历史数据"""
    
    @staticmethod
    def get_stock_history(symbol: str, days: int = 60) -> Optional[List[Dict]]:
        """
        获取真实历史K线数据：腾讯财经（优先）→ AKShare（兜底）
        
        Args:
            symbol: 股票代码 (如 '600362', 'sh600362', '600362.SH')
            days: 获取多少天的数据
        
        Returns:
            K线数据列表或None
        """
        # 优先使用腾讯财经
        try:
            from scripts.data.tencent_provider import TencentFinanceProvider
            provider = TencentFinanceProvider()
            klines = provider.get_historical_kline(symbol, days)
            
            if klines:
                result = []
                for k in klines:
                    result.append({
                        "timestamp": k["date"],
                        "open": k["open"],
                        "high": k["high"],
                        "low": k["low"],
                        "close": k["close"],
                        "volume": k["volume"]
                    })
                return result
        except Exception as e:
            print(f"⚠️ 腾讯财经获取{symbol}失败: {e}，尝试AKShare备用")
        
        # 回退到 AKShare
        return DataProvider._get_stock_history_akshare(symbol, days)
    
    @staticmethod
    def _get_stock_history_akshare(symbol: str, days: int = 60) -> Optional[List[Dict]]:
        """AKShare 备用数据源"""
        try:
            import akshare as ak
            from datetime import datetime, timedelta
            
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days + 10)
            start_str = start_date.strftime("%Y%m%d")
            end_str = end_date.strftime("%Y%m%d")
            pure_symbol = symbol.replace("sh", "").replace("sz", "")
            
            df = ak.stock_zh_a_hist(
                symbol=pure_symbol,
                period="daily",
                start_date=start_str,
                end_date=end_str,
                adjust="qfq"
            )
            
            if df is None or df.empty:
                return None
            
            klines = []
            for _, row in df.iterrows():
                try:
                    klines.append({
                        "timestamp": row["日期"],
                        "open": float(row["开盘"]),
                        "high": float(row["最高"]),
                        "low": float(row["最低"]),
                        "close": float(row["收盘"]),
                        "volume": int(row["成交量"])
                    })
                except (ValueError, TypeError, KeyError):
                    continue
            
            return klines[-days:] if len(klines) > days else klines
            
        except ImportError:
            print(f"⚠️ AKShare未安装")
            return None
        except Exception as e:
            print(f"⚠️ AKShare获取{symbol}历史数据失败: {e}")
            return None
    
    @staticmethod
    def get_index_data(symbol: str = "000001", days: int = 35) -> Optional[Dict]:
        """
        获取大盘指数数据（用于判断市场环境）
        优先使用Tushare，失败回退到AKShare
        
        Args:
            symbol: 指数代码 (000001=上证指数, 399001=深证成指)
            days: 获取多少天的数据（默认35天，足够计算MA30）
        
        Returns:
            Dict: 包含最新价格、MA10、MA30的字典
        """
        # 优先使用腾讯财经
        try:
            from scripts.data.tencent_provider import TencentFinanceProvider
            provider = TencentFinanceProvider()
            index_data = provider.get_index_kline(symbol, days)
            
            if index_data and index_data.get('ma10') and index_data.get('ma30'):
                print(f"   ✅ 使用腾讯财经获取指数{symbol}数据")
                return {
                    "symbol": symbol,
                    "name": "上证指数" if symbol == "000001" else f"指数{symbol}",
                    "price": index_data['current'],
                    "ma10": index_data['ma10'],
                    "ma30": index_data['ma30'],
                    "change_pct": index_data['change_pct'],
                    "timestamp": index_data['klines'][-1]['date'] if index_data.get('klines') else '',
                    "source": "tencent"
                }
        except Exception as e:
            print(f"   ⚠️ 腾讯财经获取指数{symbol}失败: {e}，尝试AKShare")
        
        # 回退到 AKShare
        return DataProvider._get_index_data_akshare(symbol, days)
    
    @staticmethod
    def _get_index_data_akshare(symbol: str = "000001", days: int = 35) -> Optional[Dict]:
        """AKShare备用：获取大盘指数数据"""
        try:
            import akshare as ak
            from datetime import datetime, timedelta
            
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days + 10)
            start_str = start_date.strftime("%Y%m%d")
            end_str = end_date.strftime("%Y%m%d")
            
            df = ak.index_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_str,
                end_date=end_str
            )
            
            if df is None or df.empty or len(df) < 30:
                print(f"⚠️ AKShare未返回指数{symbol}数据")
                return None
            
            df['MA10'] = df['收盘'].rolling(window=10).mean()
            df['MA30'] = df['收盘'].rolling(window=30).mean()
            latest = df.iloc[-1]
            
            print(f"   ✅ 使用AKShare获取指数{symbol}数据")
            return {
                "symbol": symbol,
                "name": "上证指数" if symbol == "000001" else f"指数{symbol}",
                "price": float(latest['收盘']),
                "ma10": float(latest['MA10']),
                "ma30": float(latest['MA30']),
                "change_pct": float(latest.get('涨跌幅', 0)),
                "timestamp": str(latest['日期']),
                "source": "akshare"
            }
            
        except Exception as e:
            print(f"⚠️ AKShare获取指数{symbol}也失败: {e}")
            return None
    
    @staticmethod
    def detect_market_state(index_data: Dict) -> str:
        """
        根据大盘指数数据判断市场环境
        
        Args:
            index_data: 指数数据（包含price, ma10, ma30）
        
        Returns:
            str: 'strong' | 'neutral' | 'weak'
        """
        if not index_data:
            return 'neutral'  # 默认震荡
        
        ma10 = index_data.get('ma10')
        ma30 = index_data.get('ma30')
        price = index_data.get('price', 0)
        
        if ma10 and ma30 and price:
            if ma10 > ma30 and price > ma10:
                return 'strong'  # 强势：MA多头排列，价格在MA10之上
            elif ma10 > ma30 or price > ma10:
                return 'neutral'  # 震荡：部分多头排列
            else:
                return 'weak'  # 弱势：空头排列
        
        return 'neutral'
    
    # ========== 缓存优先的数据获取方法（方案A+B）==========
    
    @staticmethod
    def get_index_data_with_cache(symbol: str = "000001", 
                                   max_cache_age_hours: int = 4,
                                   storage=None) -> Optional[Dict]:
        """
        获取指数数据（缓存优先，方案A）
        
        Args:
            symbol: 指数代码
            max_cache_age_hours: 缓存最大有效期（小时）
            storage: SQLiteStorage 实例，为None时自动获取
        
        Returns:
            指数数据字典或None
        """
        try:
            # 导入存储模块
            if storage is None:
                from storage.sqlite import get_sqlite_storage
                storage = get_sqlite_storage()
            
            # 1. 尝试从缓存读取
            cached = storage.get_index_cache(symbol)
            if cached and not storage.is_index_cache_expired(symbol, max_cache_age_hours):
                print(f"   📦 使用缓存数据: {cached.get('name')} ({cached.get('data_date')})")
                return {
                    "symbol": cached['symbol'],
                    "name": cached['name'],
                    "price": cached['price'],
                    "ma10": cached['ma10'],
                    "ma30": cached['ma30'],
                    "change_pct": cached['change_pct'],
                    "timestamp": cached['data_date']
                }
            
            # 2. 缓存不存在或已过期，从AKShare获取
            print(f"   🌐 从AKShare获取指数数据...")
            fresh_data = DataProvider.get_index_data(symbol)
            
            if fresh_data:
                # 3. 保存到缓存
                storage.save_index_cache(fresh_data)
                print(f"   💾 已缓存到本地数据库")
                return fresh_data
            
            # 4. AKShare失败但有过期缓存，使用过期缓存（降级）
            if cached:
                print(f"   ⚠️ AKShare失败，使用过期缓存: {cached.get('data_date')}")
                return {
                    "symbol": cached['symbol'],
                    "name": cached['name'],
                    "price": cached['price'],
                    "ma10": cached['ma10'],
                    "ma30": cached['ma30'],
                    "change_pct": cached['change_pct'],
                    "timestamp": cached['data_date'],
                    "expired": True  # 标记为过期数据
                }
            
            return None
            
        except Exception as e:
            print(f"   ⚠️ 获取指数缓存数据失败: {e}")
            # 降级到直接获取
            return DataProvider.get_index_data(symbol)
    
    @staticmethod
    @timeout_decorator(30)  # 30秒超时
    def get_stock_history_with_cache(symbol: str, days: int = 60,
                                      storage=None) -> Optional[List[Dict]]:
        """
        获取股票历史K线（缓存优先，支持腾讯财经攒数据）
        
        Args:
            symbol: 股票代码
            days: 获取多少天的数据
            storage: SQLiteStorage 实例
        
        Returns:
            K线数据列表或None
        """
        pure_symbol = symbol.replace("sh", "").replace("sz", "")
        cached_klines = []
        
        try:
            # 1. 优先从本地K线缓存读取（腾讯财经攒的数据）
            import sqlite3
            from pathlib import Path
            
            db_path = Path.home() / ".openclaw" / "workspace" / "memory" / "trading.db"
            if db_path.exists():
                try:
                    with sqlite3.connect(db_path) as conn:
                        cursor = conn.execute("""
                            SELECT trade_date, open, high, low, close, volume
                            FROM stock_kline_cache
                            WHERE symbol = ?
                            ORDER BY trade_date DESC
                            LIMIT ?
                        """, (pure_symbol, days))
                        
                        rows = cursor.fetchall()
                        if rows:
                            for row in reversed(rows):  # 按日期正序
                                cached_klines.append({
                                    "timestamp": row[0],
                                    "open": float(row[1]),
                                    "high": float(row[2]),
                                    "low": float(row[3]),
                                    "close": float(row[4]),
                                    "volume": int(row[5])
                                })
                            print(f"   📦 {symbol}: 使用本地缓存 ({len(cached_klines)}天)")
                            
                            # 如果缓存足够，直接返回
                            if len(cached_klines) >= days:
                                return cached_klines
                except Exception as e:
                    logger.debug(f"读取本地K线缓存失败: {e}")
            
            # 2. 缓存不足，尝试从AKShare补全
            if len(cached_klines) < days:
                logger.info(f"{symbol}: 本地缓存{len(cached_klines)}天，尝试AKShare...")
                fresh_klines = DataProvider.get_stock_history(symbol, days)
                
                if fresh_klines:
                    # 保存到本地缓存（渐进式积累）
                    try:
                        with sqlite3.connect(db_path) as conn:
                            for k in fresh_klines:
                                conn.execute("""
                                    INSERT OR REPLACE INTO stock_kline_cache 
                                    (symbol, trade_date, open, high, low, close, volume)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                """, (
                                    pure_symbol,
                                    k["timestamp"],
                                    k["open"], k["high"], k["low"], k["close"],
                                    k["volume"]
                                ))
                            conn.commit()
                    except Exception as e:
                        logger.debug(f"保存K线缓存失败: {e}")
                    
                    return fresh_klines
            
            # 3. AKShare失败但有部分缓存，使用缓存（降级）
            if cached_klines:
                print(f"   ⚠️ {symbol}: AKShare失败，使用缓存 ({len(cached_klines)}天)")
                return cached_klines
            
            return None
            
        except TimeoutError as e:
            logger.error(f"获取{symbol}K线数据超时: {e}")
            if cached_klines:
                return cached_klines
            return None
        except Exception as e:
            logger.error(f"获取{symbol}K线数据失败: {e}")
            if cached_klines:
                return cached_klines
            return DataProvider.get_stock_history(symbol, days)
    
    @staticmethod
    def generate_kline_data(current_price: float, avg_cost: float, limit: int = 60) -> List[Dict]:
        """生成K线数据用于计算技术指标（AKShare失败时的备用）"""
        from datetime import timedelta
        klines = []
        now = datetime.now()
        
        for i in range(limit, 0, -1):
            progress = i / limit
            base_price = avg_cost + (current_price - avg_cost) * (1 - progress)
            daily_change = (current_price - avg_cost) / limit * (0.8 + 0.4 * ((i * 997) % 100) / 100)
            price = base_price + daily_change
            
            klines.append({
                "timestamp": (now - timedelta(days=i)).strftime("%Y-%m-%d"),
                "open": round(price * 0.998, 2),
                "high": round(price * 1.005, 2),
                "low": round(price * 0.995, 2),
                "close": round(price, 2),
                "volume": 1000000
            })
        
        return klines
