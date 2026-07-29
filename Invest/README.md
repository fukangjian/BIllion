# Invest — 投资研究工具集

统一整合了原 `AI/`（LLM 研究助手）和 `Quant/`（量化执行工具），服务于 Obsidian 投资知识库的日常工作流。

> **愿景**：人负责战略与决策，AI/量化负责信息整理与计算，纪律负责执行。

## 模块概览

| 模块 | 目录 | 功能 | 入口 |
|------|------|------|------|
| 数据管道 | `pipeline/` | 行情获取、市场扫描、突破候选 | `python pipeline/run_daily.py` |
| 研究助手 | `research/` | 公告、财报、产业链、每日日报 | `python research/run_daily_report.py` |
| 交易复盘 | `review/` | 交易日志、合规检查、周/月报 | `python review/cli.py` |
| 回测 | `backtest/` | S1-A / S2-A 策略验证 | `python backtest/run_backtest.py` |
| 仓位计算 | `position_calculator.py` | 风险预算法股数计算 | `python position_calculator.py` |
| 统一入口 | `run_all.py` | 盘前一键：管道 + 日报 | `python run_all.py` |

## 环境准备

```powershell
cd "e:\Billion\Invest"
pip install -r requirements.txt
```

### LLM 配置（可选）

```powershell
$env:KIMI_API_KEY = "sk-..."
# 可选：$env:KIMI_MODEL = "moonshot-v1-32k"
```

无 API Key 时研究模块仍可运行，输出原始数据和降级模板。

## 盘前工作流（推荐）

```powershell
cd "e:\Billion\Invest"

# 方式一：一键运行
python run_all.py

# 方式二：分步运行
python pipeline/run_daily.py          # 获取数据 + 市场扫描
python research/run_daily_report.py   # 生成研究日报
```

## 输出目录

所有工具输出统一在 `Invest/output/`：

```
Invest/output/
├── daily_reports/      # 每日研究日报
├── announcements/      # 公告摘要
├── financial_reports/  # 财报对比
├── industry_maps/      # 产业链映射
├── market_scans/       # 市场扫描报告
└── backtest/           # 回测图表
```

数据文件：

```
Invest/data/
├── market.db           # SQLite 行情数据库（首次运行自动创建）
└── trades.json         # 交易日志
```

交易复盘周报/月报仍写入 Obsidian vault：

- `【10】实盘记录/每周复盘/`
- `【10】实盘记录/每月复盘/`

## 研究助手

### 公告摘要

```powershell
python research/announcement_analyzer.py 688192
python research/announcement_analyzer.py 600519 -n 5
```

### 财报对比

```powershell
python research/financial_comparison.py 600519 000858 300750
```

### 产业链映射

```powershell
python research/industry_mapper.py 创新药
python research/industry_mapper.py AI算力
```

预置框架：创新药、AI算力、半导体、新能源。

### 每日研究日报

```powershell
python research/run_daily_report.py
python research/run_daily_report.py -w 688192 600519 -s 创新药 AI算力
```

日报包含：今日要点、市场宏观、板块表现、持仓公告、突破候选（联动 `pipeline/market_scanner.py`）。

## 数据管道

```powershell
# 完整流程
python pipeline/run_daily.py

# 仅扫描（已有数据库）
python pipeline/run_daily.py --skip-fetch

# 初始化数据库
python pipeline/database.py
```

扫描报告输出：`output/market_scans/market_scan_YYYY-MM-DD.md`

## 仓位计算器

```powershell
python position_calculator.py -s 600519 -e 1800 --stop 1700 -t core --equity 1000000
python position_calculator.py -s 688235 -e 50 --stop 45 -t event --gap-risk announcement
```

核心公式：

```
风险预算R = 账户权益 × 风险比例
每股风险 = (入场价 - 止损价) × 跳空折扣
股数 = floor(R / 每股风险 / 100) × 100
```

## 回测

```powershell
python backtest/run_backtest.py --strategy S1-A --symbol 600519 --start 2020-01-01
python backtest/run_backtest.py --strategy S2-A --symbol 000858 --start 2018-01-01
```

## 交易复盘

```powershell
# 添加交易
python review/cli.py add 600519 --account 核心 --system S1-A --entry 1800 --stop 1700 --risk 0.5 --shares 100

# 列出 / 统计 / 合规检查
python review/cli.py list
python review/cli.py stats --by-strategy
python review/cli.py check

# 生成周报 / 月报（写入 【10】实盘记录/）
python review/cli.py weekly
python review/cli.py monthly
```

## 目录结构

```
Invest/
├── README.md
├── requirements.txt
├── config.py              # 统一配置
├── run_all.py             # 统一入口
├── position_calculator.py
├── shared/                # 共享工具
│   ├── utils.py           # 代理绕过、重试、Markdown
│   ├── data_fetcher.py    # AkShare 数据获取
│   ├── llm_client.py      # Kimi LLM 客户端
│   └── prompts.py         # 提示词模板
├── research/              # AI 研究助手
├── pipeline/              # 数据管道
├── backtest/              # 回测
├── review/                # 交易复盘
├── data/                  # 数据存储
└── output/                # 报告输出
```

## 设计原则

- 所有路径通过 `config.py` 配置，不硬编码
- 中文注释与输出
- 网络异常自动重试，数据缺失优雅降级
- 每个脚本可独立运行
- 输出 Obsidian 兼容 Markdown，含 YAML frontmatter

## 关联文档

- [[基金经理日常工作流]]
- [[投资体系_V5.0]]
- [[90天实盘训练计划]]
