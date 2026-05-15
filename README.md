# LongToo Trader

基于 OpenClaw 多 Agent 协同框架的 A 股量化交易系统。

## 策略

**ROE 月频调仓（Top5）**——每月从全市场选出 ROE 最高的 5 只股票等权持有，
非调仓日仅做 -15% 极端止损。选股因子来自 baostock 历史季报数据，
已消除 look-ahead bias。

## 架构

```
AnalystAgent  →  RiskAgent  →  ExecutionAgent
     (选股评分)     (三层风控)      (下单执行)
            ↕ Coordinator 统一编排
     EventStore + CQRS 读写分离
```

## 快速开始

```bash
pip install -r requirements.txt

# 模拟盘
python main.py --mode paper --init     # 初始化（10万虚拟资金）
python main.py --mode paper --daily    # 日度交易（调仓日自动买入 Top5）
python main.py --mode paper --status   # 查看账户

# ROE 数据同步（每周一次）
python -m data.roe_sync

# 历史季报拉取（消除 look-ahead bias）
python -m data.baostock_quarterly --batch 200
```

## 目录

```
├── agents/        # 多 Agent 团队
│   ├── analyst.py             # ROE 排名选股
│   ├── position_manager.py    # 持仓管理（止损/加仓）
│   ├── risk.py                # 风控拦截
│   ├── execution.py           # 交易执行
│   └── report.py              # 日报生成
├── core/          # 核心引擎
│   ├── risk_engine.py         # 三层风控
│   ├── position_engine.py     # 状态管理（唯一写入者）
│   ├── event_store.py         # Event Sourcing
│   ├── query_service.py       # CQRS 读模型
│   └── strategy.py            # 策略实现
├── execution/     # 模拟盘执行器
├── messaging/     # 事件总线
├── data/          # 数据源（mootdx/baostock/akshare）
├── backtest/      # 回测模块
└── config/        # 配置文件模板
```

## 配置

复制 `config/paper_trading.example.yaml` 为 `config/paper_trading.yaml`，
按需修改参数。

数据源需要设置环境变量（可选）：
- `TUSHARE_TOKEN`：tushare 数据接口（实盘模式需要）
- 无环境变量时默认使用 mootdx / baostock / akshare 免费接口

## 模拟盘规则

| 项目 | 说明 |
|------|------|
| 初始资金 | ¥100,000 |
| 持仓上限 | 5 只 |
| 单票仓位 | ≤20% |
| 调仓日止损 | -8% |
| 非调仓日止损 | -15% |
| 滑点 | 0.1% |
| 手续费 | 0.03%（最低 5 元） |

## 许可

MIT License
