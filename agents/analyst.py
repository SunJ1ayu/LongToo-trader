"""Analyst Agent - 分析逻辑封装（阶段2：支持消息总线）"""

import logging
from typing import Dict, List, Optional
from .base import BaseAgent, AgentResult
from ..messaging import Channel, Message

logger = logging.getLogger(__name__)


class AnalystAgent(BaseAgent):
    """分析 Agent
    
    负责获取市场数据，计算技术指标，生成交易信号。
    阶段2：支持消息总线，订阅 C1_ANALYSIS_CMD，发布到 C2_ANALYSIS_RESULT
    """
    
    def __init__(self, api_client, strategy, memory_manager=None):
        """初始化 AnalystAgent
        
        Args:
            api_client: API 客户端实例
            strategy: 交易策略实例
            memory_manager: 内存管理器实例（可选）
        """
        super().__init__()
        self.api_client = api_client
        self.strategy = strategy
        self.memory_manager = memory_manager
    
    def setup_subscriptions(self):
        """设置消息订阅（阶段2新增）"""
        self.subscribe(Channel.C1_ANALYSIS_CMD, self.handle_message)
        print(f"🔔 {self.name} 已订阅 {Channel.C1_ANALYSIS_CMD.value}")
    
    def handle_message(self, message: Message):
        """处理收到的消息（阶段2新增）
        
        收到 C1_ANALYSIS_CMD 后执行分析，发布结果到 C2
        """
        if message.channel == Channel.C1_ANALYSIS_CMD:
            print(f"📥 {self.name} 收到分析指令: {message.msg_id}")
            
            # 执行分析
            result = self.process(message.payload)
            
            # 发布结果到 C2
            response = Message(
                channel=Channel.C2_ANALYSIS_RESULT,
                sender=self.name,
                msg_type="result" if result.success else "error",
                payload=result.data if result.success else {"error": result.error},
                correlation_id=message.msg_id
            )
            self.publish(response)
            print(f"📤 {self.name} 发布分析结果到 {Channel.C2_ANALYSIS_RESULT.value}")
    
    def process(self, input_data: Dict) -> AgentResult:
        """分析投资组合并生成交易信号
        
        Args:
            input_data: 输入数据字典
                
        Returns:
            AgentResult: 包含 signals 列表的结果
        """
        try:
            # 0. 获取大盘指数数据判断市场环境
            print("📈 AnalystAgent: 获取市场环境...")
            
            # 使用 api_client 的数据源（腾讯财经）
            if hasattr(self.api_client, 'data_provider'):
                index_data = self.api_client.data_provider.get_index_quote('000001')
            else:
                index_data = None
            
            if index_data:
                change_pct = index_data.get('change_pct', 0)
                if change_pct > 1:
                    market_state = 'bullish'
                elif change_pct < -1:
                    market_state = 'bearish'
                else:
                    market_state = 'neutral'
                print(f"   上证指数: {index_data.get('price', 0):.2f} ({change_pct:+.3f}%)")
                print(f"   市场环境: {market_state.upper()}")
            else:
                market_state = 'neutral'
                print("   ⚠️ 无法获取指数数据，使用默认震荡市场")
            
            # 1. 获取账户持仓
            portfolio = self.api_client.get_portfolio()
            holdings = portfolio.get("holdings", [])
            portfolio_data = portfolio.get("portfolio", {})
            
            cash = input_data.get("cash") or portfolio_data.get("cash", 150000)
            
            signals = []
            
            # 有现金就扫描全市场寻找买入机会
            if cash > 10000:
                print(f"   📋 可用现金 ¥{cash:,.0f}，扫描全市场寻找买入机会...")
                market_signals = self._scan_market(cash, market_state)
                signals.extend(market_signals)
            
            # 2. 分析每只股票（传入市场环境）
            failed_symbols = []
            for holding in holdings:
                try:
                    signal = self._analyze_holding(holding, cash, market_state)
                    if signal:
                        signals.append(signal)
                except Exception as e:
                    symbol = holding.get("symbol", "unknown")
                    name = holding.get("name", "unknown")
                    failed_symbols.append(f"{name}({symbol})")
                    logger.error(f"分析股票 {name}({symbol}) 失败: {e}", exc_info=True)
                    continue
            
            if failed_symbols:
                logger.warning(f"以下股票分析失败: {', '.join(failed_symbols)}")
            
            return AgentResult(
                success=True,
                data={
                    "signals": signals,
                    "holdings_count": len(holdings),
                    "cash": cash,
                    "market_state": market_state
                }
            )
            
        except Exception as e:
            return AgentResult(
                success=False,
                data={"signals": []},
                error=f"分析投资组合失败: {str(e)}"
            )
    
    def _analyze_holding(self, holding: Dict, cash: float, market_state: str = 'neutral') -> Optional[Dict]:
        """分析单个持仓（支持市场环境参数）"""
        # 导入技术指标模块（延迟导入避免循环依赖）
        from scripts.core.indicators import TechnicalIndicators
        from scripts.core.api_client import DataProvider
        from scripts.storage.sqlite import get_sqlite_storage
        
        full_symbol = holding.get("symbol", "")
        # 统一使用带前缀的symbol
        if not full_symbol.startswith("sh") and not full_symbol.startswith("sz"):
            if len(full_symbol) == 6 and full_symbol.isdigit():
                if full_symbol.startswith("6") or full_symbol.startswith("5"):
                    full_symbol = f"sh{full_symbol}"
                else:
                    full_symbol = f"sz{full_symbol}"
        symbol = full_symbol  # 保留完整前缀
        name = holding.get("name", "")
        shares = holding.get("shares", 0)
        current_price = holding.get("current_price")
        avg_cost = holding.get("avg_cost")
        
        if not current_price:
            return None
        
        # 使用缓存优先的K线数据获取（方案B）
        storage = get_sqlite_storage()
        klines = DataProvider.get_stock_history_with_cache(full_symbol, days=60, storage=storage)
        
        if not klines:
            # 缓存和AKShare都失败，使用模拟数据
            print(f"   ⚠️ {name}: 无法获取历史数据，使用模拟数据")
            klines = DataProvider.generate_kline_data(current_price, avg_cost or current_price)
        
        closes = [float(k["close"]) for k in klines]
        
        # 计算技术指标
        ma10 = TechnicalIndicators.calculate_ma(closes, 10)
        ma30 = TechnicalIndicators.calculate_ma(closes, 30)
        atr = TechnicalIndicators.calculate_atr(klines)
        macd, _, _ = TechnicalIndicators.calculate_macd(closes)
        trend_strength = TechnicalIndicators.get_trend_strength(klines)
        
        # 计算动量评分
        prev_close = closes[-2] if len(closes) >= 2 else current_price
        momentum_score = TechnicalIndicators.calculate_momentum_score(
            current_price,
            prev_close
        )

        # ===== 补全评分所需数据 =====

        # 1. 今日涨跌幅 (change_pct)
        change_pct = ((current_price - prev_close) / prev_close * 100) if prev_close > 0 else 0

        # 2. 连续上涨天数 (consecutive_up_days)
        consecutive_up_days = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] > closes[i - 1]:
                consecutive_up_days += 1
            else:
                break

        # 3. 连续下跌天数 (consecutive_down_days)
        consecutive_down_days = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] < closes[i - 1]:
                consecutive_down_days += 1
            else:
                break

        # 4. 计算持仓期间的最高价（用于追踪止损）
        highest_price_since_entry = current_price
        if avg_cost and avg_cost > 0:
            # 从买入点之后的最高价
            highest_price_since_entry = max(closes[-min(30, len(closes)):])

        # 5. 计算RSI（14日）
        rsi = TechnicalIndicators.calculate_rsi(closes, 14)

        # 6. 计算量比
        volumes = [float(k.get('volume', 0)) for k in klines]
        vol_ma5 = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else volumes[-1]
        volume_ratio = volumes[-1] / vol_ma5 if vol_ma5 > 0 else 1.0

        # 读取持久化的止损价
        stored_stop_loss = None
        if self.memory_manager:
            stored_stop_loss = self.memory_manager.get_stop_loss(symbol)

        # 调用策略分析生成信号（传入大盘市场环境）
        signal_data = self.strategy.analyze({
            'symbol': symbol,
            'name': name,
            'price': current_price,
            'ma10': ma10,
            'ma30': ma30,
            'momentum_score': momentum_score,
            'macd': macd,
            'atr': atr,
            'trend_strength': trend_strength,
            'rsi': rsi,  # 补全：RSI
            'volume_ratio': volume_ratio,  # 补全：量比
            'change_pct': change_pct,  # 补全：今日涨跌幅
            'consecutive_up_days': consecutive_up_days,  # 补全：连续上涨天数
            'consecutive_down_days': consecutive_down_days,  # 补全：连续下跌天数
            'highest_price_since_entry': highest_price_since_entry,  # 补全：持仓期最高价
            'holding_shares': shares,
            'cash': cash,
            'avg_cost': avg_cost,
            'stored_stop_loss': stored_stop_loss
        }, market_state=market_state)
        
        # 补充额外信息
        signal_data.update({
            'symbol': symbol,
            'name': name,
            'price': current_price,
            'ma10': ma10,
            'ma30': ma30,
            'momentum_score': momentum_score,
            'macd': macd,
            'atr': atr,
            'trend_strength': trend_strength,
            'holding_shares': shares
        })
        
        return signal_data
    
    def _scan_market_coarse(self, market_state: str = 'neutral',
                             top_n: int = 30) -> List[Dict]:
        """ROE 排名扫描全A股 - 尾盘预选用

        使用 ROE（净资产收益率）作为选股因子，替代原有的技术分析粗评分。

        Args:
            market_state: 市场环境状态（ROE 策略不依赖，保留接口兼容）
            top_n: 返回 ROE 最高的前 N 只

        Returns:
            候选列表（包含 symbol, name, coarse_score=ROE, price）
        """
        from scripts.data.roe_sync import get_roe_ranking, should_sync_roe, fetch_and_store_roe

        # 如果 ROE 数据过期（>7天），自动同步
        if should_sync_roe():
            print("   📥 ROE 数据过期，自动同步...")
            result = fetch_and_store_roe()
            if result.get("success"):
                print(f"   ✅ ROE 同步完成: {result['stocks_updated']} 只")
            else:
                print(f"   ⚠️ ROE 同步失败: {result.get('error')}")

        # 加载股票名称映射
        stock_names = {}
        try:
            import json
            from scripts.data.market_sync import STOCK_LIST_CACHE
            if STOCK_LIST_CACHE.exists():
                cache = json.loads(STOCK_LIST_CACHE.read_text(encoding='utf-8'))
                for s in cache.get('stocks', []):
                    stock_names[s['code']] = s['name']
        except Exception:
            pass

        print(f"   📊 ROE 排名扫描全A股...")

        # 获取 ROE 排名
        ranking = get_roe_ranking(top_n=top_n, min_price=3.0)

        if not ranking:
            print("   ❌ 无 ROE 数据，请先运行 ROE 同步")
            return []

        candidates = []
        for s in ranking:
            candidates.append({
                'symbol': s['symbol'],
                'name': stock_names.get(s['symbol'], s['symbol']),
                'coarse_score': round(s['roe'], 1),  # ROE 值作为评分
                'close': s['price'],
                'strategy_type': 'roe',  # 标记为 ROE 策略
                'roe': s['roe'],
                'eps': s.get('eps'),
            })

        print(f"   📈 ROE 排名完成: 选出 TOP {len(candidates)}")

        if candidates:
            for i, c in enumerate(candidates[:5], 1):
                print(f"      #{i} {c['name']}({c['symbol']}): "
                      f"ROE {c['coarse_score']:.1f}% ¥{c['close']:.2f}")

        return candidates

    def _scan_market(self, cash: float, market_state: str = 'neutral',
                      top_n: int = 30) -> List[Dict]:
        """从本地DB扫描全A股，寻找买入机会

        支持两阶段选股：
        1. 如果候选池存在且有效（日期是昨天），从候选池精确分析
        2. 否则全市场扫描（原有逻辑）

        Args:
            cash: 可用资金
            market_state: 市场环境状态
            top_n: 返回评分最高的前N只

        Returns:
            交易信号列表（按评分降序，最多top_n条）
        """
        from scripts.data.candidates import load_candidates

        # ===== 两阶段选股：优先使用候选池（严格模式：必须是昨天） =====
        candidates_data = load_candidates(strict=True)
        if candidates_data:
            return self._scan_from_candidates(candidates_data, cash, market_state)

        # ===== 回退：全市场扫描 =====
        return self._scan_full_market(cash, market_state, top_n)

    def _scan_from_candidates(self, candidates_data: Dict,
                               cash: float, market_state: str) -> List[Dict]:
        """从候选池生成交易信号（两阶段选股 - 阶段2）

        候选池由 _scan_market_coarse（ROE 排名）生成。
        直接使用预选时的 ROE 值作为评分，不重新计算。

        Args:
            candidates_data: 候选池数据
            cash: 可用资金
            market_state: 市场环境状态（ROE 策略不依赖）

        Returns:
            交易信号列表
        """
        candidates = candidates_data.get('candidates', [])
        print(f"   📋 从候选池读取 {len(candidates)} 只股票（ROE 排名）...")

        # 按 ROE 排序
        candidates.sort(key=lambda c: c.get('coarse_score', 0), reverse=True)

        # 最多买入 5 只，等权分配
        max_buy = 5
        per_stock_cash = cash / max_buy * 0.95

        # ROE 归一化到 0-100
        max_roe = max(c.get('coarse_score', 1) for c in candidates) if candidates else 50

        signals = []
        for candidate in candidates[:max_buy]:
            symbol = candidate.get('symbol', '')
            if not symbol:
                continue

            candidate_close = candidate.get('close', candidate.get('price', 0))
            roe = candidate.get('coarse_score', 0)  # ROE 值

            # 计算买入股数
            buy_shares = int(per_stock_cash / candidate_close / 100) * 100 if candidate_close > 0 else 0
            if buy_shares < 100:
                continue

            # 归一化 ROE 到 0-100
            normalized_score = min(roe / max_roe * 100, 100)

            signal = {
                'symbol': symbol,
                'name': candidate.get('name', symbol),
                'signal': 'buy',
                'score': round(normalized_score, 1),
                'roe_raw': roe,
                'price': candidate_close,
                'strategy_type': candidate.get('strategy_type', 'roe'),
                'close': candidate_close,
                'action_shares': buy_shares,
                'position_size': 15.0,  # 等权 15%
                'roe': roe,
                'eps': candidate.get('eps'),
            }
            signals.append(signal)

        print(f"   📈 候选池分析完成: {len(signals)} 个买入信号")

        if signals:
            for i, s in enumerate(signals[:5], 1):
                print(f"      #{i} {s.get('name', s['symbol'])}: "
                      f"ROE {s.get('score', 0):.1f}% ¥{s.get('price', 0):.2f}")

        return signals

    def _scan_full_market(self, cash: float, market_state: str,
                          top_n: int = 30) -> List[Dict]:
        """全市场扫描 — ROE 排名选股

        使用 ROE 排名选择 top N，生成买入信号。
        ROE 数据从 fundamentals 表读取（由 roe_sync 维护）。
        """
        from scripts.data.roe_sync import get_roe_ranking, should_sync_roe, fetch_and_store_roe

        # 如果 ROE 数据过期，自动同步
        if should_sync_roe():
            print("   📥 ROE 数据过期，自动同步...")
            result = fetch_and_store_roe()
            if result.get("success"):
                print(f"   ✅ ROE 同步完成: {result['stocks_updated']} 只")
            else:
                print(f"   ⚠️ ROE 同步失败: {result.get('error')}")

        # 加载股票名称映射
        stock_names = {}
        try:
            import json
            from scripts.data.market_sync import STOCK_LIST_CACHE
            if STOCK_LIST_CACHE.exists():
                cache = json.loads(STOCK_LIST_CACHE.read_text(encoding='utf-8'))
                for s in cache.get('stocks', []):
                    stock_names[s['code']] = s['name']
        except Exception:
            pass

        print(f"   📊 ROE 排名选股（全市场）...")

        # 获取 ROE 排名（多取一些，后面按仓位计算过滤）
        ranking = get_roe_ranking(top_n=top_n, min_price=3.0)

        if not ranking:
            print("   ❌ 无 ROE 数据")
            return []

        print(f"   📋 ROE Top {len(ranking)} 候选")

        # 计算仓位和买入数量（5只各15%，留25%现金缓冲）
        per_stock_cash = cash * 0.15  # 每只15%

        # ROE 归一化到 0-100（用于替换模块的差值判断）
        max_roe = max(s['roe'] for s in ranking) if ranking else 50

        signals = []
        for s in ranking:
            symbol = s['symbol']
            price = s['price']
            roe = s['roe']

            # 计算买入股数
            buy_shares = int(per_stock_cash / price / 100) * 100 if price > 0 else 0
            if buy_shares < 100:
                continue

            # 归一化 ROE 到 0-100 范围（线性映射）
            normalized_score = min(roe / max_roe * 100, 100)

            signal = {
                'symbol': symbol,
                'name': stock_names.get(symbol, symbol),
                'signal': 'buy',
                'score': round(normalized_score, 1),  # 归一化后的评分（用于替换判断）
                'roe_raw': roe,  # 原始 ROE 值
                'price': price,
                'action_shares': buy_shares,
                'position_size': 15.0,  # 等权 15%
                'strategy_type': 'roe',
                'roe': roe,
                'eps': s.get('eps'),
                'close': price,  # 实时过滤用
            }
            signals.append(signal)

        # 按 ROE 降序
        signals.sort(key=lambda x: x.get('score', 0), reverse=True)

        print(f"   📈 ROE 选股完成: {len(signals)} 个买入信号")

        if signals:
            for i, s in enumerate(signals[:5], 1):
                print(f"      #{i} {s['name']}({s['symbol']}): "
                      f"ROE {s['score']:.1f}% ¥{s['price']:.2f} {s['action_shares']}股")

        return signals
    
    def _scan_watchlist(self, cash: float, market_state: str = 'neutral') -> List[Dict]:
        """扫描 watchlist 寻找买入机会（兼容旧模式）
        
        Args:
            cash: 可用资金
            market_state: 市场环境状态
            
        Returns:
            交易信号列表
        """
        import json
        from pathlib import Path
        
        signals = []
        
        # 读取 watchlist
        watchlist_path = Path(__file__).parent.parent.parent / "watchlist.json"
        if not watchlist_path.exists():
            print(f"   ⚠️ 未找到 watchlist 文件: {watchlist_path}")
            return signals
        
        try:
            with open(watchlist_path, 'r', encoding='utf-8') as f:
                watchlist_data = json.load(f)
        except Exception as e:
            print(f"   ⚠️ 读取 watchlist 失败: {e}")
            return signals
        
        # 获取候选股票列表（使用 stocks 字段）
        candidates = watchlist_data.get("stocks", [])
        if not candidates:
            print("   ⚠️ watchlist 中没有候选股票")
            return signals
        
        print(f"   📊 扫描 {len(candidates)} 只候选股票...")
        
        # 分析每只股票
        for candidate in candidates:
            try:
                symbol = candidate.get("symbol", "")
                name = candidate.get("name", "")
                
                if not symbol:
                    continue
                
                # 获取实时行情（腾讯财经）
                quote = None
                if hasattr(self.api_client, 'data_provider'):
                    quote = self.api_client.data_provider.get_realtime_quote(symbol)
                    # 自动缓存到本地数据库（攒历史数据）
                    if quote and hasattr(self.api_client.data_provider, 'cache_to_klines_db'):
                        self.api_client.data_provider.cache_to_klines_db(symbol, quote)
                
                if not quote:
                    print(f"   ⚠️ {name}: 无法获取行情")
                    continue
                
                current_price = quote.get("price", 0)
                if current_price <= 0:
                    continue
                
                # 使用缓存优先的K线数据获取
                from scripts.core.api_client import DataProvider
                from scripts.storage.sqlite import get_sqlite_storage
                
                storage = get_sqlite_storage()
                klines = DataProvider.get_stock_history_with_cache(symbol, days=60, storage=storage)
                
                if not klines:
                    print(f"   ⚠️ {name}: 无法获取历史数据")
                    continue
                
                # 计算技术指标
                from scripts.core.indicators import TechnicalIndicators
                closes = [float(k["close"]) for k in klines]
                
                ma10 = TechnicalIndicators.calculate_ma(closes, 10)
                ma30 = TechnicalIndicators.calculate_ma(closes, 30)
                atr = TechnicalIndicators.calculate_atr(klines)
                macd, _, _ = TechnicalIndicators.calculate_macd(closes)
                trend_strength = TechnicalIndicators.get_trend_strength(klines)
                
                prev_close = closes[-2] if len(closes) >= 2 else current_price
                momentum_score = TechnicalIndicators.calculate_momentum_score(current_price, prev_close)
                
                # 调用策略分析
                signal_data = self.strategy.analyze({
                    'symbol': symbol,
                    'name': name,
                    'price': current_price,
                    'ma10': ma10,
                    'ma30': ma30,
                    'momentum_score': momentum_score,
                    'macd': macd,
                    'atr': atr,
                    'trend_strength': trend_strength,
                    'holding_shares': 0,
                    'cash': cash
                }, market_state=market_state)
                
                if signal_data and signal_data.get('signal') == 'buy':
                    print(f"   ✅ {name}: 买入评分 {signal_data.get('score', 0):.0f} 分")
                    # 确保信号中包含 symbol
                    signal_data['symbol'] = symbol
                    signals.append(signal_data)
                else:
                    reason = signal_data.get('reason', '无买入信号') if signal_data else '分析失败'
                    print(f"   ⏸️ {name}: {reason}")
                
            except Exception as e:
                print(f"   ❌ 分析 {candidate.get('name', 'unknown')} 失败: {e}")
                continue
        
        print(f"   📈 扫描完成，发现 {len(signals)} 个买入信号")
        return signals
    
    def health_check(self) -> bool:
        """健康检查"""
        try:
            if not self.api_client or not self.strategy:
                return False
            portfolio = self.api_client.get_portfolio()
            return portfolio is not None
        except Exception:
            return False
