# Invest — 投资研究工具集

统一整合 LLM 研究助手与量化执行工具，服务于 Obsidian 投资知识库的日常工作流。

> **愿景**：人负责战略与决策，AI/量化负责信息整理与计算，纪律负责执行。

## 模块概览

| 模块 | 目录 | 功能 | 入口 |
|------|------|------|------|
| 数据管道 | `pipeline/` | 行情并行获取、市场扫描、突破候选 | `python pipeline/run_daily.py` |
| 研究助手 | `research/` | 公告全文分析、财报对比、产业链、每日日报 | `python research/run_daily_report.py` |
| 交易复盘 | `review/` | 交易日志、合规检查、持仓视图、周/月报 | `python review/cli.py` |
| 回测 | `backtest/` | S1-A / S2-A 策略验证（含资金曲线） | `python backtest/run_backtest.py` |
| 仓位计算 | `position_calculator.py` | 风险预算法 + 已有持仓簇风险检查 | `python position_calculator.py` |
| 统一入口 | `run_all.py` | 盘前一键：管道 + 日报 | `python run_all.py` |
| API 服务 | `server.py` | FastAPI 服务 + 定时调度 | `python server.py` |
| 测试 | `tests/` | 指标、合规、持仓单元测试 | `python -m pytest tests/` |

## 环境准备

```powershell
cd "e:\Billion\Invest"
pip install -r requirements.txt
```

### LLM 配置（可选，支持多提供商）

```powershell
# Kimi（默认）
$env:KIMI_API_KEY = "sk-..."

# DeepSeek（备选）
$env:DEEPSEEK_API_KEY = "sk-..."

# 自定义 OpenAI 兼容接口
$env:CUSTOM_LLM_API_KEY = "sk-..."
$env:CUSTOM_LLM_BASE_URL = "https://your-api.com/v1"
$env:CUSTOM_LLM_MODEL = "your-model"
```

无 API Key 时研究模块仍可运行，输出原始数据和降级模板。LLM 按优先级自动路由：Kimi → DeepSeek → Custom。相同 prompt 缓存 24 小时。

## 盘前工作流

### 方式一：命令行一键运行

```powershell
cd "e:\Billion\Invest"

# 一键运行（并行获取数据 + 市场扫描 + 研究日报）
python run_all.py

# 分步运行
python pipeline/run_daily.py          # 获取数据 + 市场扫描
python research/run_daily_report.py   # 生成研究日报
```

### 方式二：FastAPI 服务

```powershell
# 启动服务
python server.py

# 触发盘前流程
curl -X POST http://127.0.0.1:8900/pre-market

# 启用定时调度（每天 08:30 自动运行）
$env:ENABLE_SCHEDULER = "true"
$env:SCHEDULER_TIME = "08:30"
python server.py
```

API 端点：

| 端点 | 方法 | 功能 |
|------|------|------|
| `/pre-market` | POST | 完整盘前流程（管道 + 日报） |
| `/pipeline` | POST | 仅运行数据管道 |
| `/research` | POST | 仅生成研究日报 |
| `/status` | GET | 运行状态与输出文件列表 |
| `/latest-scan` | GET | 最新市场扫描内容 |
| `/latest-report` | GET | 最新日报内容 |

## 输出目录

```
Invest/output/
├── daily_reports/      # 每日研究日报
├── announcements/      # 公告摘要
├── financial_reports/  # 财报对比
├── industry_maps/      # 产业链映射
├── market_scans/       # 市场扫描报告（.md + .json）
└── backtest/           # 回测图表（资金曲线 + 回撤）
```

数据文件：

```
Invest/data/
├── market.db           # SQLite 行情数据库
├── trades.json         # 交易日志
├── announcements/      # 公告全文缓存
└── llm_cache/          # LLM 响应缓存（24h TTL）
```

交易复盘周报/月报写入 Obsidian vault：`【10】实盘记录/每周复盘/`、`【10】实盘记录/每月复盘/`

## 研究助手

### 公告摘要（支持全文分析）

```powershell
python research/announcement_analyzer.py 600519
python research/announcement_analyzer.py 600519 -n 5
```

自动从巨潮信息网获取公告全文（HTML 解析），长公告分块 RAG 摘要。

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
python research/run_daily_report.py -w 600519 300750 -s 创新药 AI算力
```

日报包含：今日要点、市场宏观、板块表现、持仓公告、突破候选。默认关注列表 = 当前持仓 + `RESEARCH_WATCHLIST`。

## 数据管道

```powershell
# 完整流程（并行获取 + 扫描）
python pipeline/run_daily.py

# 仅扫描（已有数据库）
python pipeline/run_daily.py --skip-fetch

# 初始化数据库
python pipeline/database.py
```

数据获取使用 `ThreadPoolExecutor` 并行抓取（默认 4 线程），单股失败不影响其他。

扫描报告输出 Markdown + JSON 两种格式：
- `output/market_scans/market_scan_YYYY-MM-DD.md`
- `output/market_scans/market_scan_YYYY-MM-DD.json`

## 仓位计算器

```powershell
# 基础计算
python position_calculator.py -s 600519 -e 1800 --stop 1700 -t core --equity 1000000

# 跳空风险折扣
python position_calculator.py -s 688235 -e 50 --stop 45 -t event --gap-risk announcement

# 检查已有持仓的簇风险（联动 trades.json）
python position_calculator.py -s 600519 -e 1800 --stop 1700 -t core --check-existing
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

输出包含完整资金曲线（逐日权益折线）、回撤曲线、买卖标记点。

## 交易复盘

```powershell
# 添加交易
python review/cli.py add 600519 --account 核心 --system S1-A \
    --entry 1800 --stop 1700 --risk 0.5 --shares 100

# 从扫描结果快速建仓（自动填充系统、止损）
python review/cli.py from-scan 600519

# 列出 / 统计 / 合规检查
python review/cli.py list
python review/cli.py stats --by-strategy
python review/cli.py check

# 查看当前持仓摘要
python -c "from review.positions import print_portfolio_summary; print_portfolio_summary()"

# 生成周报 / 月报（写入 【10】实盘记录/）
python review/cli.py weekly
python review/cli.py monthly
```

### 策略-扫描-复盘闭环

```
market_scanner → 突破候选 JSON → review from-scan → 自动填充交易参数 → 合规检查
```

## 测试

```powershell
# 运行全部测试
python -m pytest tests/ -v

# 仅运行指标测试
python -m pytest tests/test_indicators.py -v

# 仅运行合规测试
python -m pytest tests/test_compliance.py -v
```

测试覆盖：技术指标计算、合规规则边界、持仓视图导出。不依赖 API Key 或网络。

## 目录结构

```
Invest/
├── README.md
├── ARCHITECTURE.md        # 架构与设计文档
├── requirements.txt
├── config.py              # 统一配置（路径、API、合规规则）
├── run_all.py             # 统一入口
├── server.py              # FastAPI 服务 + 定时调度
├── position_calculator.py
├── shared/                # 共享服务层
│   ├── utils.py           # 代理绕过、重试、Markdown
│   ├── data_fetcher.py    # AkShare 并行数据获取
│   ├── llm_client.py      # 多 LLM 路由 + 缓存
│   └── prompts.py         # 提示词模板
├── research/              # AI 研究助手
│   ├── daily_report.py
│   ├── announcement_analyzer.py
│   ├── announcement_fetcher.py  # 公告全文抓取 + RAG
│   ├── financial_comparison.py
│   └── industry_mapper.py
├── pipeline/              # 数据管道
│   ├── database.py
│   ├── indicators.py
│   ├── market_scanner.py  # 输出 MD + JSON
│   └── run_daily.py
├── backtest/              # 回测
│   ├── strategies.py
│   └── run_backtest.py    # 资金曲线 + 回撤图
├── review/                # 交易复盘
│   ├── cli.py             # 含 from-scan 子命令
│   ├── trade_log.py
│   ├── positions.py       # 持仓视图与风险敞口
│   ├── metrics.py
│   ├── compliance_check.py
│   └── report_generator.py
├── tests/                 # 单元测试
│   ├── test_indicators.py
│   ├── test_compliance.py
│   └── test_positions.py
├── data/                  # 数据存储
│   ├── market.db
│   ├── trades.json
│   ├── announcements/     # 公告全文缓存
│   └── llm_cache/         # LLM 响应缓存
└── output/                # 报告输出
```

## 设计原则

- **配置集中化**：所有路径、API、规则通过 `config.py` 管理，禁止硬编码
- **优雅降级**：无 LLM Key / 网络异常 / 数据源不可用时，仍输出原始数据或模板
- **模块独立**：每个脚本可独立运行，也可通过 `run_all.py` 或 API 服务编排
- **Obsidian 原生**：输出 Markdown + YAML frontmatter，支持 wikilink 与标签检索
- **投资体系对齐**：合规规则、仓位计算、策略命名映射「投资体系 V5.0」

## 环境变量速查

| 变量 | 用途 | 默认值 |
|------|------|--------|
| `KIMI_API_KEY` | Kimi LLM API 密钥 | 空（降级运行） |
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | 空 |
| `CUSTOM_LLM_API_KEY` | 自定义 LLM 密钥 | 空 |
| `CUSTOM_LLM_BASE_URL` | 自定义 LLM 端点 | 空 |
| `ACCOUNT_EQUITY` | 账户权益（元） | 1000000 |
| `DRAWDOWN_STATE` | 回撤状态 | Normal |
| `ENABLE_SCHEDULER` | 启用定时调度 | false |
| `SCHEDULER_TIME` | 定时运行时间 | 08:30 |
| `SERVER_PORT` | API 服务端口 | 8900 |

## 关联文档

- [[基金经理日常工作流]]
- [[投资体系_V5.0]]
- [[90天实盘训练计划]]
- [[ARCHITECTURE]] — 完整架构与设计文档
