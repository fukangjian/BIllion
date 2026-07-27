# 【6】量化 — 投资系统工具集

基于 Python 的量化投资工具，服务于个人职业投资体系建设。

## 模块概览

| 模块 | 说明 | 入口 |
|------|------|------|
| data_pipeline | 数据获取 + 市场扫描 | `python data_pipeline/run_daily.py` |
| position_calculator | 仓位计算器 | `python position_calculator.py` |
| backtest | S1-A / S2-A 回测 | `python backtest/run_backtest.py` |

## 环境准备

```powershell
# 进入量化目录
cd "e:\doc\阅读书籍\【6】量化"

# 安装依赖
pip install -r requirements.txt
```

依赖列表：akshare, pandas, numpy, backtrader, matplotlib, tabulate

## 1. 数据获取层

每日自动获取行情数据并生成市场扫描报告。

```powershell
# 完整流程：获取数据 + 扫描 + 生成报告
python data_pipeline/run_daily.py

# 仅扫描（已有数据）
python data_pipeline/run_daily.py --skip-fetch

# 初始化数据库
python data_pipeline/database.py
```

输出：
- 数据库: `data_pipeline/data/market.db`
- 报告: `data_pipeline/output/market_scan_YYYY-MM-DD.md`（可导入 Obsidian）

详见 [data_pipeline/README.md](data_pipeline/README.md)

## 2. 仓位计算器

基于风险预算法计算建议股数，含风险簇检查和跳空折扣。

```powershell
# 核心趋势账户，正常状态，100万本金
python position_calculator.py -s 600519 -e 1800 --stop 1700 -t core --equity 1000000

# 事件交易 + 公告窗口跳空风险
python position_calculator.py -s 688235 -e 50 --stop 45 -t event --gap-risk announcement

# 创新药 + JSON 输出
python position_calculator.py -s 688331 -e 80 --stop 72 -t industry --innovative-pharma --json
```

核心公式：
```
风险预算R = 账户权益 × 风险比例
每股风险 = (入场价 - 止损价) × 跳空折扣
股数 = floor(R / 每股风险 / 100) × 100
```

## 3. 回测框架

验证 S1-A（20日突破）和 S2-A（55日突破）策略历史表现。

```powershell
# S1-A 快速系统
python backtest/run_backtest.py --strategy S1-A --symbol 600519 --start 2020-01-01

# S2-A 慢速系统
python backtest/run_backtest.py --strategy S2-A --symbol 000858 --start 2018-01-01
```

详见 [backtest/README.md](backtest/README.md)

## 典型工作流

```
盘前:
  1. python data_pipeline/run_daily.py          # 获取数据 + 扫描
  2. 查看 output/market_scan_*.md               # 审阅突破候选和市场状态
  3. python position_calculator.py ...          # 计算具体仓位

研究:
  python backtest/run_backtest.py ...           # 验证策略历史表现
```

## 目录结构

```
【6】量化/
├── requirements.txt
├── README.md
├── position_calculator.py
├── data_pipeline/
│   ├── config.py
│   ├── database.py
│   ├── data_fetcher.py
│   ├── indicators.py
│   ├── market_scanner.py
│   ├── run_daily.py
│   ├── data/market.db
│   └── output/
└── backtest/
    ├── strategies.py
    ├── run_backtest.py
    └── output/
```

## 设计原则

- 所有路径通过 config.py 配置，不硬编码
- 中文注释
- 网络异常自动重试，数据缺失优雅降级
- 每个脚本可独立运行（`if __name__ == "__main__"`）
- 输出 Markdown 格式，与 Obsidian 知识库无缝集成
