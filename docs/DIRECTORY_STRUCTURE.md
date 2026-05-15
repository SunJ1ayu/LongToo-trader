# 目录结构说明

**版本**: v2.1.0  
**最后更新**: 2026-04-07

---

## 根目录

```
LongToo-trader/
├── main.py                 # 主入口 (Coordinator + Agent 架构)
├── trigger.py              # 定时任务触发器
├── README.md               # 项目说明
├── CHANGELOG.md -> docs/   # 更新历史 (软链接)
├── SKILL.md                # OpenClaw Skill 定义
├── watchlist.json          # 股票池配置
├── install.sh              # 安装脚本
├── config/                 # 配置文件
├── docs/                   # 项目文档
├── references/             # 参考资料
├── scripts/                # 源代码
└── tests/                  # 单元测试
```

---

## scripts/ 目录

```
scripts/
├── coordinator.py          # 交易编排器 (核心)
├── memory_manager.py       # 内存管理
├── message_coordinator.py  # 消息驱动版本
├── backtest.py             # 回测系统
├── stock_selector.py       # 选股器
├── liquidation.py          # 清仓工具
│
├── agents/                 # S09: Agent Team
│   ├── base.py
│   ├── analyst.py          # 分析 Agent (动态阈值+缓存)
│   ├── risk.py             # 风控 Agent
│   └── execution.py        # 执行 Agent
│
├── core/                   # 核心模块
│   ├── api_client.py       # API 客户端 (含缓存)
│   ├── strategy.py         # 策略实现 (动态阈值)
│   ├── indicators.py       # 技术指标
│   └── config.py           # 配置管理
│
├── messaging/              # S10: Agent Protocol
│   ├── local_bus.py        # 本地消息总线
│   └── redis_bus.py        # Redis 消息总线
│
├── risk/                   # 风控组件
│   ├── circuit_breaker.py  # 熔断器
│   └── rate_limiter.py     # 限流器
│
├── storage/                # 存储层
│   ├── sqlite.py           # SQLite (含缓存表)
│   └── base.py             # 存储基类
│
└── utils/                  # 工具函数
    ├── alerts.py           # 报警通知
    ├── logger.py           # 日志系统
    └── metrics.py          # 监控指标
```

---

## docs/ 文档目录

```
docs/
├── ARCHITECTURE.md         # 架构设计
├── CHANGELOG.md            # 更新历史
├── RELEASE_CHECKLIST_v2.1.0.md  # 验收清单
├── S_SERIES_MAPPING.md     # S系列课程映射
├── STRATEGY_V2.md          # 策略V2说明
├── PRODUCTION_READY.md     # 生产就绪检查
├── FIXES.md                # Bug修复记录
└── ALERTS.md               # 报警配置
```

---

## 关键文件说明

### 入口文件

| 文件 | 用途 | 备注 |
|------|------|------|
| `main.py` | 主入口 | Coordinator + Agent 架构 ✅ |
| `trigger.py` | 定时触发 | cron 调用 |
| `run_*_agent.py` | 分布式入口 | 阶段3使用 |

### 核心模块

| 文件 | 功能 | 今日更新 |
|------|------|---------|
| `coordinator.py` | 编排 Agent 执行 | ✅ |
| `core/strategy.py` | 动态阈值策略 | ✅ |
| `core/api_client.py` | 缓存优先数据获取 | ✅ |
| `storage/sqlite.py` | 指数+K线缓存 | ✅ |
| `agents/analyst.py` | 集成动态阈值和缓存 | ✅ |

---

## 定时任务配置

```cron
# 早盘分析
0 10 * * 1-5 cd /path/to/LongToo-trader && python3 trigger.py --dry-run

# 午盘分析
30 13 * * 1-5 cd /path/to/LongToo-trader && python3 trigger.py --dry-run

# 尾盘分析
50 14 * * 1-5 cd /path/to/LongToo-trader && python3 trigger.py --dry-run
```

---

## 文件数量统计

```
Python 文件: 44 个
测试文件: 3 个 (29 个测试)
文档文件: 11 个
配置文件: 2 个
```

---

**目录结构已优化，清晰整洁！** 🎉
