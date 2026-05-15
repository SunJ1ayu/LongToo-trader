"""Config - 统一配置管理

解决硬编码问题，集中管理所有配置。
支持从环境变量读取敏感信息。
"""

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class APIConfig:
    """API 配置"""
    base_url: str = "https://instreet.coze.site/api/v1"
    auth_token: str = ""
    timeout: int = 30
    
    def __post_init__(self):
        # 优先从环境变量读取
        if not self.auth_token:
            self.auth_token = os.getenv("INSTREET_API_KEY", "")


@dataclass
class RedisConfig:
    """Redis 配置"""
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: Optional[str] = None
    
    def __post_init__(self):
        # 支持从环境变量读取
        self.host = os.getenv("REDIS_HOST", self.host)
        self.port = int(os.getenv("REDIS_PORT", self.port))
        self.password = os.getenv("REDIS_PASSWORD", self.password)


@dataclass
class TradingConfig:
    """交易配置"""
    strategy_name: str = "momentum_trend"
    dry_run: bool = True
    max_positions: int = 5  # 最大持仓数量（统一为5只）
    max_position_pct: float = 0.15  # 单只最大仓位比例（15%）
    stop_loss_atr_mult: float = 2.5
    
    def __post_init__(self):
        # 从环境变量读取实盘/模拟设置
        dry_run_env = os.getenv("DRY_RUN", "true").lower()
        self.dry_run = dry_run_env in ("true", "1", "yes")
        
        # 策略名称
        self.strategy_name = os.getenv("STRATEGY", self.strategy_name)


@dataclass
class Config:
    """全局配置"""
    api: APIConfig
    redis: RedisConfig
    trading: TradingConfig
    memory_path: str = "~/.openclaw/workspace"
    
    @classmethod
    def default(cls) -> "Config":
        """创建默认配置"""
        return cls(
            api=APIConfig(),
            redis=RedisConfig(),
            trading=TradingConfig()
        )
    
    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量创建配置"""
        return cls.default()


# 全局配置实例
_config: Optional[Config] = None


def get_config() -> Config:
    """获取全局配置（单例）"""
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config


def reset_config():
    """重置配置（测试用）"""
    global _config
    _config = None
