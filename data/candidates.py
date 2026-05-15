"""候选池存储模块 - 两阶段选股的核心数据交换"""

import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional

# 候选池文件路径
CANDIDATES_FILE = Path(__file__).parent.parent.parent / "data" / "candidates.json"

# 策略类型及其阈值配置
STRATEGY_THRESHOLDS = {
    'breakout': {'high': 7, 'low': -3},      # 突破策略，高开本身就是强
    'momentum': {'high': 5, 'low': -3},      # 动量策略
    'mean_revert': {'high': 2, 'low': -5},   # 均值回归，不追高
    'low_absorb': {'high': 3, 'low': -2},    # 低位吸筹
}


def save_candidates(candidates: List[Dict], market_state: str = 'neutral') -> bool:
    """保存候选池到文件

    Args:
        candidates: 候选股票列表，每项包含 symbol, name, coarse_score, close, strategy_type 等
        market_state: 市场环境状态

    Returns:
        bool: 是否成功
    """
    try:
        CANDIDATES_FILE.parent.mkdir(parents=True, exist_ok=True)

        now = datetime.now()
        data = {
            "candidate_date": now.strftime("%Y-%m-%d"),  # 预选日期
            "time": now.strftime("%H:%M"),
            "market_state": market_state,
            "count": len(candidates),
            "candidates": candidates
        }

        CANDIDATES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"   ✅ 已保存 {len(candidates)} 只候选到 {CANDIDATES_FILE}")
        return True

    except Exception as e:
        print(f"   ❌ 保存候选池失败: {e}")
        return False


def load_candidates(strict: bool = True) -> Optional[Dict]:
    """从文件加载候选池

    Args:
        strict: 严格模式，校验候选池日期必须是上一个交易日（用于早盘交易）
                非严格模式：允许最近5天内的数据（容忍周末和节假日）

    Returns:
        Dict: 候选池数据，包含 candidate_date, market_state, candidates 等
        None: 文件不存在、读取失败、或日期校验失败
    """
    try:
        if not CANDIDATES_FILE.exists():
            print("   ⚠️ 候选池文件不存在")
            return None

        data = json.loads(CANDIDATES_FILE.read_text(encoding='utf-8'))

        # 获取候选池日期
        candidate_date = data.get("candidate_date", data.get("date", ""))
        today = datetime.now().strftime("%Y-%m-%d")

        # 计算日期差异
        try:
            candidate_dt = datetime.strptime(candidate_date, "%Y-%m-%d")
            days_diff = (datetime.now() - candidate_dt).days
        except ValueError:
            print(f"   ⚠️ 候选池日期格式错误: {candidate_date}")
            return None

        if strict:
            # 严格模式：候选池日期必须是上一个交易日
            # 回溯5天内都算有效（容忍周末+节假日）
            max_days_back = 5

            if days_diff > max_days_back:
                print(f"   ⚠️ 候选池已过期 {days_diff} 天，建议重新预选（有效期{max_days_back}天）")
                return None

            # 检查是否是工作日候选池（周六日的候选池无效）
            if candidate_dt.weekday() >= 5:  # 5=周六, 6=周日
                print(f"   ⚠️ 候选池日期 {candidate_date} 是周末，无效")
                return None

        else:
            # 非严格模式：允许最近5天内的数据
            if days_diff > 5:
                print(f"   ⚠️ 候选池已过期 {days_diff} 天，建议重新预选")
                return None

        print(f"   ✅ 候选池有效: {candidate_date} ({days_diff}天前)")
        return data

    except Exception as e:
        print(f"   ❌ 加载候选池失败: {e}")
        return None


def get_candidate_symbols() -> List[str]:
    """获取候选股票代码列表（快捷方法）

    Returns:
        List[str]: 候选股票代码列表
    """
    data = load_candidates(strict=False)
    if not data:
        return []

    return [c.get("symbol", "") for c in data.get("candidates", [])]


def get_strategy_threshold(strategy_type: str) -> Dict:
    """获取策略类型的阈值配置

    Args:
        strategy_type: 策略类型

    Returns:
        Dict: {'high': 高开阈值, 'low': 低开阈值}
    """
    return STRATEGY_THRESHOLDS.get(strategy_type, STRATEGY_THRESHOLDS['momentum'])


def clear_candidates() -> bool:
    """清空候选池"""
    try:
        if CANDIDATES_FILE.exists():
            CANDIDATES_FILE.unlink()
        return True
    except Exception as e:
        print(f"   ❌ 清空候选池失败: {e}")
        return False