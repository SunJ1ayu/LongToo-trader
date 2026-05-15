# 架构设计说明

## 系统架构（v2.5.0）

```
┌─────────────────────────────────────────────────────────────┐
│                    LongToo 龙兔量化交易系统                      │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │  AnalystAgent │→│   RiskAgent   │→│ ExecutionAgent│      │
│  │   (分析)      │  │   (风控)      │  │   (执行)      │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│         │                │                │                │
│         └────────────────┴────────────────┘                │
│                      Coordinator                           │
├─────────────────────────────────────────────────────────────┤
│  核心引擎层（v2.5.0 架构演进）                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │ RiskEngine  │  │PositionEngine│ │OrderManager │        │
│  │ (风控中央化) │  │(状态中央化)  │ │(订单管理)   │        │
│  └─────────────┘  └─────────────┘  └─────────────┘        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │PortfolioEng │  │ EventStore  │  │QueryService │        │
│  │(组合风险)   │  │(事件存储)   │  │(CQRS读模型) │        │
│  └─────────────┘  └─────────────┘  └─────────────┘        │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │   Storage   │  │  Event Bus  │  │   Alerts    │        │
│  │   (存储)     │  │  (消息总线)  │  │   (报警)     │        │
│  └─────────────┘  └─────────────┘  └─────────────┘        │
└─────────────────────────────────────────────────────────────┘
```

## 架构演进（v2.5.0）

### P0 层（必须做）

| 任务 | 文件 | 收益 |
|------|------|------|
| RiskEngine | `core/risk_engine.py` | 风控逻辑不漂移 |
| PositionEngine | `core/position_engine.py` | 状态字段一致性 |
| Event Bus | `messaging/local_bus.py` | 解耦、审计 |

### P1 层（很重要）

| 任务 | 文件 | 收益 |
|------|------|------|
| OrderManager | `core/order_manager.py` | 订单可追踪 |
| PortfolioEngine | `core/portfolio_engine.py` | 组合风险视角 |

### P2 层（高级）

| 任务 | 文件 | 收益 |
|------|------|------|
| Event Sourcing | `core/event_store.py` | 崩溃恢复 |
| CQRS | `core/query_service.py` | 读写分离 |

## 数据流向

```
市场数据 → AnalystAgent → RiskAgent → ExecutionAgent → 交易执行
                ↓              ↓              ↓
            策略评分      风控检查      盈亏计算
                ↓              ↓              ↓
            买入信号      通过/拦截      更新持仓
                ↓              ↓              ↓
            ────────────→ EventStore ←────────────
                           (事件追加)
```

## CQRS 分离

```
┌─────────────────────────────────────────┐
│           Command（写）                  │
├─────────────────────────────────────────┤
│  PositionEngine.update_peak_pnl()       │
│  OrderManager.mark_filled()             │
│  RiskEngine.validate()                  │
│           ↓                             │
│      EventStore.append()                │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│           Query（读）                    │
├─────────────────────────────────────────┤
│  QueryService.get_portfolio_summary()   │
│  QueryService.get_active_positions()    │
│  QueryService.get_event_history()       │
│           ↓                             │
│      (可缓存、可优化)                     │
└─────────────────────────────────────────┘
```

## Event Sourcing

### 事件类型

| 事件 | 触发时机 |
|------|----------|
| `POSITION_OPENED` | 开仓成功 |
| `PEAK_PNL_UPDATED` | 峰值盈利创新高 |
| `REDUCE_FLAG_SET` | 触发减仓保护 |
| `ADD_COUNT_INCREMENTED` | 加仓成功 |
| `POSITION_CLOSED` | 清仓 |

### 状态重建

```python
# 从事件重建持仓状态
events = event_store.get_events(aggregate_id="sh600519")
position = rebuild_position(events)
```

## 风控决策树

```
开始
 │
 ├─→ RiskEngine.validate(stage="pre_trade")
 │   ├─→ 紧急停止？→ 拦截
 │   ├─→ 冷却期？→ 拦截
 │   ├─→ 单日亏损超限？→ 拦截
 │   └─→ 交易次数超限？→ 拦截
 │
 ├─→ RiskEngine.validate(stage="pre_execution")
 │   └─→ 仓位超限？→ 截断股数
 │
 └─→ RiskEngine.validate(stage="hard_block")
     ├─→ 资金不足？→ 拦截
     └─→ 通过 → 执行交易
```

## 核心模块职责

| 模块 | 职责 | 关键类 |
|------|------|--------|
| `core/risk_engine.py` | 风控中央化 | `RiskEngine` |
| `core/position_engine.py` | 状态管理 | `PositionEngine` |
| `core/order_manager.py` | 订单生命周期 | `OrderManager` |
| `core/portfolio_engine.py` | 组合风险 | `PortfolioEngine` |
| `core/event_store.py` | 事件存储 | `EventStore` |
| `core/query_service.py` | 查询服务 | `QueryService` |
| `core/strategy.py` | 策略逻辑 | `MomentumTrendStrategy` |

## 存储设计

### 数据库表

```sql
-- 策略状态表
strategy_state (id, data, updated_at)

-- 交易记录表
trades (id, order_id, symbol, action, quantity, price, 
        amount, status, api_response, created_at)

-- 持仓表（含状态字段）
positions (symbol, shares, avg_cost, current_price, 
           peak_pnl, peak_price, add_count, reduced_from_peak,
           stop_loss, updated_at)

-- 事件日志表（v2.5.0）
events (sequence, event_id, event_type, aggregate_id,
        aggregate_type, timestamp, payload, metadata)

-- 每日盈亏表
daily_pnl (date, total_assets, cash, pnl_amount, pnl_pct)
```

## 架构原则

| 原则 | 说明 |
|------|------|
| Single Source of Truth | RiskEngine、PositionEngine 中央化 |
| Event-centric | Event Bus + Event Sourcing |
| Defense in Depth | 三层风控检查 |
| CQRS | QueryService 分离读写 |
| Crash Recovery | 事件日志可重建状态 |

## 扩展性设计

### 当前（v2.5.0）
- 本地消息总线（LocalMessageBus）
- 事件驱动架构
- 读写分离

### 未来
- Redis 消息总线（分布式）
- Event Sourcing 完整回放
- 分布式调度

## 安全设计

- API Token 环境变量化
- SQL 参数化查询（防注入）
- 文件锁（防竞态条件）
- 熔断器（防雪崩）
- 事件日志（审计追踪）
