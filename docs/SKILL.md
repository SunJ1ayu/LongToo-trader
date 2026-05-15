# LongToo 龙兔量化交易 Skill

**版本**: v3.0.1
**最后更新**: 2026-05-15
**策略**: ROE 月频调仓（Top5）
**架构**: Coordinator + Agent + Event Sourcing + CQRS
**状态**: ✅ 生产就绪

---

## 🚀 快速开始

### 模拟盘模式（推荐）

```bash
# 初始化模拟盘账户（10万虚拟资金）
python3 main.py --mode paper --init

# 查看账户状态
python3 main.py --mode paper --status

# 日度自动交易（定时任务用，调仓日自动买入 Top5）
python3 main.py --mode paper --daily

# 手动买入
python3 main.py --mode paper --buy 000001.SZ 1000

# 手动卖出
python3 main.py --mode paper --sell 000001.SZ

# ROE 数据同步（每周一次）
python3 -m scripts.data.roe_sync

# 历史季报拉取（增量，解决 look-ahead bias）
python3 -m scripts.data.baostock_quarterly --batch 200
python3 -m scripts.data.baostock_quarterly --status
```

---

## ✨ v3.0.0 ROE 月频调仓策略

### 策略逻辑

| 环节 | 说明 |
|------|------|
| 选股因子 | ROE（净资产收益率），从 mootdx/baostock 获取季报数据 |
| 调仓频率 | 月频（每月首个交易日，或持仓不足 5 只时自动补仓） |
| 持仓数量 | Top 5（单票仓位 ~20%） |
| 非调仓日 | 仅检查 -15% 极端止损，不做趋势止损/替换/加仓 |
| 实时过滤 | 低开 > -3% 的不买 |
| 每日上限 | 5 笔（调仓日临时提高到 10 笔） |

### 调仓日流程

```
Step 0:   数据同步（增量更新全 A 股行情）
Step 1:   持仓管理（-8% 止损 / 趋势破坏 / 持仓超时）
Step 1.5: 加仓赢家（盈利 > 5% 触发，最多 1 次）
Step 2:   分析阶段（ROE 排名选股 Top5）
Step 3:   实时行情过滤（低开 > -3% 排除）
Step 4:   风控检查
Step 5:   执行买入
Step 6:   持仓监控
```

### 非调仓日流程

```
Step 0:   数据同步
Step 1:   极端止损检查（-15%）
Step 6:   持仓监控
```

### 数据源

| 数据 | 来源 | 更新频率 | 存储位置 |
|------|------|----------|----------|
| ROE/EPS（最新季报） | mootdx finance | 每周一次 | market_kline.db → fundamentals 表 |
| 历史季报（含 pubDate） | baostock | 一次性拉取 | quarterly_fundamentals.db |
| 日 K 线 | baostock | 每日增量 | market_kline.db → daily_kline 表 |
| 实时行情快照 | mootdx（通达信 TCP） | 按需（~7s/全市场） | 内存缓存 |
| 候选池 | --preselect 生成 | 每日 | candidates.json |

### Look-Ahead Bias 处理

- `fundamentals` 表（mootdx）：当前季报快照，存在 look-ahead bias，仅供实盘参考
- `quarterly_fundamentals` 表（baostock）：含 `pub_date`（公布日期），回测时只用 `pub_date < 交易日` 的数据，彻底消除 bias

---

## ✨ v2.5.0 架构演进

### 核心架构（已完成）

| 组件 | 文件 | 职责 |
|------|------|------|
| RiskEngine | `core/risk_engine.py` | 风控中央化，三阶段检查 |
| PositionEngine | `core/position_engine.py` | 状态字段单一修改者 |
| OrderManager | `core/order_manager.py` | 订单生命周期管理 |
| PortfolioEngine | `core/portfolio_engine.py` | 组合级风险快照 |
| EventStore | `core/event_store.py` | 事件存储（Append-only） |
| QueryService | `core/query_service.py` | CQRS 读模型 |

### 架构原则

| 原则 | 说明 |
|------|------|
| Single Source of Truth | RiskEngine、PositionEngine 中央化 |
| Event-centric | Event Bus + Event Sourcing |
| Defense in Depth | 三层风控检查 |
| CQRS | QueryService 分离读写 |
| Crash Recovery | 事件日志可重建状态 |

### 事件类型（Event Sourcing）

| 事件 | 说明 |
|------|------|
| `POSITION_OPENED` | 开仓 |
| `PEAK_PNL_UPDATED` | 峰值盈利更新（关键！） |
| `REDUCE_FLAG_SET` | 减仓标记设置 |
| `ADD_COUNT_INCREMENTED` | 加仓次数增加 |
| `POSITION_CLOSED` | 清仓 |

---

## 📁 目录结构

```
LongToo-trader/
├── main.py                         # 主入口
├── scripts/
│   ├── agents/                     # Agent Team
│   │   ├── analyst.py              # 分析 Agent（ROE 排名选股）
│   │   ├── position_manager.py     # 持仓管理 Agent（止损/替换/加仓）
│   │   ├── position_state_machine.py # 状态机
│   │   ├── risk.py                 # 风控 Agent
│   │   ├── execution.py            # 执行 Agent
│   │   ├── position_monitor.py     # 持仓监控
│   │   └── report.py               # 报告 Agent
│   ├── coordinator.py              # 交易编排器（ROE 月频调仓）
│   ├── data/
│   │   ├── roe_sync.py             # ROE 数据同步（mootdx）
│   │   ├── baostock_quarterly.py   # 历史季报增量拉取（baostock）
│   │   ├── market_sync.py          # 全 A 股行情同步
│   │   ├── candidates.py           # 候选池管理
│   │   └── realtime_filter.py      # 实时行情过滤
│   ├── execution/
│   │   ├── paper_executor.py       # 模拟盘执行器
│   │   └── paper_adapter.py        # 模拟盘适配器
│   ├── core/                       # 核心模块
│   │   ├── risk_engine.py          # 风控引擎
│   │   ├── position_engine.py      # 状态管理
│   │   ├── portfolio_engine.py     # 组合风险
│   │   ├── event_store.py          # 事件存储
│   │   ├── query_service.py        # CQRS 读模型
│   │   ├── config.py               # 统一配置
│   │   └── strategy.py             # 策略实现
│   └── messaging/
│       └── local_bus.py            # 事件总线
├── data/
│   ├── paper_trading.db            # 模拟盘数据库
│   ├── events.db                   # 事件日志
│   ├── market_kline.db             # K 线 + fundamentals（mootdx ROE）
│   ├── quarterly_fundamentals.db   # 历史季报（baostock，含 pub_date）
│   └── candidates.json             # 候选池
└── config/
    └── paper_trading.yaml          # 模拟盘配置
```

---

## ⏰ 定时任务

| 时段 | 时间 | 命令 | 说明 |
|------|------|------|------|
| 早盘交易 | 9:35 | `--daily` | ROE 排名选股，调仓日买入 Top5 |
| ROE 同步 | 每周一 | `roe_sync.py` | 更新最新季报 ROE 数据 |
| 季报拉取 | 按需 | `baostock_quarterly.py` | 增量拉取历史季报 |

---

## 📊 模拟盘说明

**初始资金**: ¥100,000（可自定义）

**交易规则**:
- 滑点: 0.1%
- 手续费: 0.03%（最低5元）
- 印花税: 卖出时 0.1%
- 最大持仓: 5 只（ROE Top5）
- 单票仓位: 最大 20%
- 每日交易上限: 5 笔（调仓日 10 笔）
- 调仓日止损: -8%（PositionManager）
- 非调仓日止损: -15%（极端止损）
- 加仓门槛: 盈利 > 5%，最多 1 次

---

## 📝 更新日志

### v3.0.1 (2026-05-15) - 数据源切换
- ✅ 实时行情快照从 akshare（东方财富 HTTP）切换为 mootdx（通达信 TCP）
- ✅ akshare 仍保留用于历史 K 线查询
- ✅ 日线行情拉取完成：4,849 只，379 万条（2020-2026）
- ℹ️ 东方财富 push2 接口 5/15 起返回 502，mootdx 不受影响

### v3.0.0 (2026-05-14) - ROE 月频调仓策略
- ✅ 策略从"动量趋势"切换为"ROE 月频调仓"
- ✅ ROE 数据同步（mootdx → fundamentals 表，4960 只股票）
- ✅ 历史季报拉取（baostock → quarterly_fundamentals.db，含 pub_date）
- ✅ 调仓日判断：本月无买入记录 → 触发调仓
- ✅ 持仓不足 5 只自动补仓（次日继续买入直到买满）
- ✅ 调仓日跳过替换逻辑（ROE 评分与 exit_score 量纲不可比）
- ✅ 调仓日交易上限临时提高到 10（卖出 + 买入）
- ✅ PositionManager 止损从 -3% 放宽到 -8%（匹配月频持有期）
- ✅ 非调仓日仅检查 -15% 极端止损
- ✅ 每日交易上限 3→5（买满 Top5）
- ✅ 修复 _is_rebalance_day 列名 bug（trade_date → timestamp）
- ✅ 修复 YAML 配置冲突（max_positions 10→5）

### v2.5.0 (2026-05-12) - 架构演进
- ✅ P0-1: RiskEngine 中央化风控
- ✅ P0-2: PositionEngine 状态字段单一修改者
- ✅ P0-3: Event Bus 事件驱动基础
- ✅ P1-1: OrderManager 订单生命周期管理
- ✅ P1-2: PortfolioEngine 组合级风险快照
- ✅ P2-1: Event Sourcing 事件存储，支持崩溃恢复
- ✅ P2-2: CQRS 读写分离

### v2.3.0 (2026-05-11)
- ✅ 盈利保护系统（分层减仓，Peak PnL 永不重置）
- ✅ 单次加仓系统（盈利>5%触发，最多加仓1次）
- ✅ 状态机风控（5种状态，动态止损阈值）

### v2.2.0 (2026-04-16)
- ✅ 模拟盘模式（Paper Trading）
- ✅ 定时任务推送交易报告到 QQ

---

**ROE 月频调仓策略已上线！** 🎉
