"""实时行情过滤模块 - 两阶段选股的风控层

核心原则：日线指标只用完成K线计算
- 预选阶段：用完整日K计算技术指标
- 执行阶段：用实时行情做开盘状态过滤（高开/低开）

避免 Partial Candle Bias：不用未完成的当天K线算MACD/MA/RSI
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


def realtime_filter(candidates: List[Dict],
                    thresholds: Dict = None,
                    use_vwap: bool = False) -> List[Dict]:
    """实时行情过滤 - 过滤高开过多或低开太弱的股票

    Args:
        candidates: 候选信号列表，每项需包含 symbol, close, strategy_type
        thresholds: 策略阈值配置，默认使用 STRATEGY_THRESHOLDS
        use_vwap: 是否使用 VWAP（前5分钟均价），需要实时行情支持

    Returns:
        过滤后的候选列表
    """
    from .tencent_provider import TencentFinanceProvider
    from .candidates import get_strategy_threshold

    if thresholds is None:
        thresholds = {}

    print(f"\n🔍 实时行情过滤: {len(candidates)} 只候选")

    provider = TencentFinanceProvider()
    filtered = []
    skipped = []

    for candidate in candidates:
        symbol = candidate.get('symbol', '')
        close = candidate.get('close', 0)  # 预选时的收盘价（昨收）
        strategy_type = candidate.get('strategy_type', 'momentum')

        if not symbol or close <= 0:
            continue

        # 获取实时行情
        quote = provider.get_realtime_quote(symbol)
        if not quote:
            logger.debug(f"无法获取 {symbol} 实时行情，跳过")
            continue

        # 计算涨跌幅
        current_price = quote.get('price', 0)
        open_price = quote.get('open', current_price)
        prev_close = quote.get('prev_close', close)

        # 使用开盘价计算（更稳定，避免瞬时波动）
        # 或使用当前价（更实时）
        gap_pct = ((current_price / prev_close) - 1) * 100 if prev_close > 0 else 0
        open_gap_pct = ((open_price / prev_close) - 1) * 100 if prev_close > 0 else 0

        # 获取策略阈值
        threshold = thresholds.get(strategy_type, get_strategy_threshold(strategy_type))
        high_limit = threshold.get('high', 5)
        low_limit = threshold.get('low', -3)

        # 过滤逻辑
        skip_reason = None

        # 过滤1: 高开过多（盈亏比恶化）
        if gap_pct > high_limit:
            skip_reason = f"高开{gap_pct:.1f}%>{high_limit}%"

        # 过滤2: 低开太弱（预期证伪）
        elif gap_pct < low_limit:
            skip_reason = f"低开{gap_pct:.1f}%<{low_limit}%"

        # 过滤3: 涨停板（买不进）
        elif gap_pct >= 9.5:
            skip_reason = f"涨停{gap_pct:.1f}%"

        # 过滤4: 跌停板（风险大）
        elif gap_pct <= -9.5:
            skip_reason = f"跌停{gap_pct:.1f}%"

        if skip_reason:
            skipped.append({
                'symbol': symbol,
                'name': candidate.get('name', symbol),
                'reason': skip_reason,
                'gap_pct': gap_pct
            })
        else:
            # 更新信号中的实时价格
            candidate['realtime_price'] = current_price
            candidate['gap_pct'] = round(gap_pct, 2)
            candidate['open_gap_pct'] = round(open_gap_pct, 2)
            filtered.append(candidate)

    # 输出过滤结果
    print(f"   ✅ 通过过滤: {len(filtered)} 只")
    if skipped:
        print(f"   ❌ 被过滤: {len(skipped)} 只")
        for s in skipped[:5]:  # 只显示前5个
            print(f"      {s['name']}({s['symbol']}): {s['reason']}")

    return filtered


def batch_realtime_filter(candidates: List[Dict],
                          thresholds: Dict = None) -> List[Dict]:
    """批量实时行情过滤（使用批量接口，效率更高）

    Args:
        candidates: 候选信号列表
        thresholds: 策略阈值配置

    Returns:
        过滤后的候选列表
    """
    from .tencent_provider import TencentFinanceProvider
    from .candidates import get_strategy_threshold

    if thresholds is None:
        thresholds = {}

    print(f"\n🔍 批量实时行情过滤: {len(candidates)} 只候选")

    provider = TencentFinanceProvider()

    # 批量获取行情
    symbols = [c.get('symbol', '') for c in candidates if c.get('symbol')]
    quotes = provider.get_batch_quotes(symbols)

    filtered = []
    skipped = []

    for candidate in candidates:
        symbol = candidate.get('symbol', '')
        close = candidate.get('close', 0)
        strategy_type = candidate.get('strategy_type', 'momentum')

        if not symbol or close <= 0:
            continue

        quote = quotes.get(symbol)
        if not quote:
            logger.debug(f"无法获取 {symbol} 实时行情")
            continue

        current_price = quote.get('price', 0)
        prev_close = quote.get('prev_close', close)

        gap_pct = ((current_price / prev_close) - 1) * 100 if prev_close > 0 else 0

        threshold = thresholds.get(strategy_type, get_strategy_threshold(strategy_type))
        high_limit = threshold.get('high', 5)
        low_limit = threshold.get('low', -3)

        skip_reason = None

        if gap_pct > high_limit:
            skip_reason = f"高开{gap_pct:.1f}%>{high_limit}%"
        elif gap_pct < low_limit:
            skip_reason = f"低开{gap_pct:.1f}%<{low_limit}%"
        elif gap_pct >= 9.5:
            skip_reason = f"涨停{gap_pct:.1f}%"
        elif gap_pct <= -9.5:
            skip_reason = f"跌停{gap_pct:.1f}%"

        if skip_reason:
            skipped.append({
                'symbol': symbol,
                'name': candidate.get('name', symbol),
                'reason': skip_reason,
                'gap_pct': gap_pct
            })
        else:
            candidate['realtime_price'] = current_price
            candidate['gap_pct'] = round(gap_pct, 2)
            filtered.append(candidate)

    print(f"   ✅ 通过过滤: {len(filtered)} 只")
    if skipped:
        print(f"   ❌ 被过滤: {len(skipped)} 只")
        for s in skipped[:5]:
            print(f"      {s['name']}({s['symbol']}): {s['reason']}")

    return filtered
