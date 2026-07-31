# Invest — 投资研究工具集

统一整合 LLM 研究助手与量化执行工具，服务于 Obsidian 投资知识库的日常工作流。

> **愿景**：人负责战略与决策，AI/量化负责信息整理与计算，纪律负责执行。

## 模块概览

| 模块 | 目录 | 功能 | 入口 |
|------|------|------|------|
| 数据管道 | `pipeline/` | 行情并行获取、市场扫描、突破候选、信号追踪、超短热点池 | `python pipeline/run_daily.py` |
| 研究助手 | `research/` | 公告全文分析、财报对比、产业链、每日日报 | `python research/run_daily_report.py` |
| 交易复盘 | `review/` | 交易日志、合规闸门、持仓监控、周/月报 | `python review/cli.py` |
| 回测 | `backtest/` | S1-A / S2-A 策略验证（与实盘同口径参数） | `python backtest/run_backtest.py` |
| 仓位计算 | `position_calculator.py` | 风险预算法 + 已有持仓簇风险检查 | `python position_calculator.py` |
| 统一入口 | `run_all.py` | 盘前一键：取数→热点池构建→扫描→持仓监控→信号结算→日报 | `python run_all.py` |
| API 服务 | `server.py` | FastAPI 服务 + 定时调度 | `python server.py` |
| 测试 | `tests/` | 指标、合规、监控、信号、回测单元测试 | `python -m pytest tests/` |

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

# 一键运行（取数 → 热点池构建 → 市场扫描 → 持仓监控 → 信号结算 → 研究日报）
python run_all.py

# 分步运行
python pipeline/run_daily.py          # 获取数据 + 市场扫描（含持仓监控与信号入库）
python pipeline/hot_pool.py           # 手动构建超短热点池（run_all 已自动执行）
python research/run_daily_report.py   # 生成研究日报
```

盘前流程自动完成：

1. 并行抓取行情（股票池 = `WATCHLIST` ∪ 当前持仓股）
2. **超短热点池构建**：涨停/连板/炸板名单入 `limit_pool` 表 + 强板块领涨股，合并写 `hot_pool` 表并补抓池内日线（`--skip-fetch` 时跳过，失败降级不阻塞）
3. 市场扫描：突破候选 + 市场状态 A/B/C/D + 板块强度 + 超短热点池区块
4. **持仓监控**：逐持仓检查止损价与退出通道（S1-A=10 日低点 / S2-A=20 日低点），触发即警报；自动推导回撤状态
5. **信号结算**：历史扫描信号逐根回放结算（止损/通道退出/到期），当日新信号自动入库
6. 研究日报：公告（含防编造护栏）、宏观、板块、候选汇总

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
| `/latest-scan` | GET | 最新市场扫描内容（含持仓监控 JSON） |
| `/latest-report` | GET | 最新日报内容 |

## 输出目录

```
Invest/output/
├── daily_reports/      # 每日研究日报
├── announcements/      # 公告摘要
├── financial_reports/  # 财报对比
├── industry_maps/      # 产业链映射
├── market_scans/       # 市场扫描（.md+.json）与持仓监控 position_monitor_{date}.json
└── backtest/           # 回测图表（资金曲线 + 回撤）
```

数据文件：

```
Invest/data/
├── market.db           # SQLite 行情数据库（含 signals/limit_pool/hot_pool 表）
├── trades.json         # 交易日志
├── announcements/      # 公告全文缓存
└── llm_cache/          # LLM 响应缓存（24h TTL）
```

买入卡、周报、月报、统计写入 Obsidian vault：`【10】实盘记录/交易日志/`、`每周复盘/`、`每月复盘/`、`统计/`

## 研究助手

### 公告摘要（支持全文分析）

```powershell
python research/announcement_analyzer.py 600519
python research/announcement_analyzer.py 600519 -n 5
```

公告列表经 fallback 链获取（东财当日 → 巨潮 API），全文支持 HTML 解析与 PDF 正文提取（pypdf），长公告分块 RAG 摘要。**防编造护栏**：未取得正文时不调用 LLM，只列标题+链接并标注 ⚠️；最新公告超过 30 天自动标注数据陈旧。

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

# 手动构建超短热点池（run_all 盘前流程已自动执行）
python pipeline/hot_pool.py

# 初始化数据库
python pipeline/database.py
```

数据获取使用 `ThreadPoolExecutor` 并行抓取（默认 4 线程），单股失败不影响其他。

扫描报告输出 Markdown + JSON 两种格式：
- `output/market_scans/market_scan_YYYY-MM-DD.md`（顶部含持仓监控区块）
- `output/market_scans/market_scan_YYYY-MM-DD.json`（含 `position_monitor` 与 `hot_pool` 字段）

扫描报告新增「五、超短热点池（1-5 天，HOT-S）」节：涨停/连板/炸板名单统计 + 热点池突破候选（与主扫描同通道参数）。热点池突破信号以系统 `HOT-S` 写入 signals 表、5 个交易日强制结算，分组胜率见 `python pipeline/signal_tracker.py stats`。

### 信号追踪（可验证性）

每次扫描的突破候选自动写入 SQLite `signals` 表，之后每个盘前流程自动逐根回放结算：

- 触发止损 → 按止损价关闭，记 −1R
- 跌破退出通道（S1=10 日 / S2=20 日低点）→ 按收盘价关闭
- 满 20 个交易日 → 按收盘价到期关闭（HOT-S 超短信号按 `SIGNAL_MAX_HOLDING_BY_SYSTEM` 覆盖为 5 日）

HOT-S 信号不匹配任何退出通道，只有「止损」与「到期（5 日）」两种退出；统计按系统分组，HOT-S 自动独立成组。

```powershell
python pipeline/signal_tracker.py stats --days 90   # 各系统胜率/平均R/PF
python pipeline/signal_tracker.py settle            # 手动触发结算
```

统计结论同时自动写入周报/月报的「信号验证」节。

## 仓位计算器

```powershell
# 基础计算（账户类型：核心/产业/事件/实验，风险率读 config.RISK_LIMITS）
python position_calculator.py -s 600519 -e 1800 --stop 1700 -t 核心 --equity 1000000

# 跳空风险折扣
python position_calculator.py -s 688235 -e 50 --stop 45 -t 事件 --gap-risk announcement

# 检查已有持仓的簇风险（联动 trades.json）
python position_calculator.py -s 600519 -e 1800 --stop 1700 -t 核心 --check-existing
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

输出包含完整资金曲线（逐日权益折线）、回撤曲线、买卖标记点。策略参数（通道周期、ATR、2N 止损、0.5N~1N 加仓间距、最大 4 单位）与实盘扫描/监控共用 `config.STRATEGY_PARAMS`，回测口径即实盘口径。

## 交易复盘

```powershell
# 添加交易（写入前自动过合规闸门；高级违规拒绝，--force 强制并留痕）
python review/cli.py add 600519 --account 核心 --system S1-A \
    --entry 1800 --stop 1700 --risk 0.5 --shares 100

# 从扫描结果查看建议参数（只打印，不落库）
python review/cli.py from-scan 600519

# 从扫描一键建仓：仓位计算 → 合规闸门 → 写库 → 生成买入卡
python review/cli.py from-scan 600519 --execute

# 持仓摘要（未实现盈亏/风险敞口/回撤状态）
python review/cli.py positions

# 列出 / 统计 / 合规检查
python review/cli.py list
python review/cli.py stats --by-strategy   # 同时写【10】实盘记录/统计/
python review/cli.py check

# 生成周报 / 月报（写入 【10】实盘记录/，含「信号验证」节）
python review/cli.py weekly
python review/cli.py monthly
```

### 策略-扫描-交易-复盘闭环

```
market_scanner → 突破候选 → signal_tracker 入库 → 每日自动结算（胜率/平均R 可验证）
                    ↓
           from-scan --execute → 仓位计算 → 合规闸门 → trades.json + 买入卡
                    ↓
           每日盘前 monitor → 止损/退出警报 → 人工执行 → update 平仓 → 周/月报
```

## 测试

```powershell
# 运行全部测试
python -m pytest tests/ -v

# 按模块运行
python -m pytest tests/test_indicators.py -v
python -m pytest tests/test_monitor.py -v
python -m pytest tests/test_signal_tracker.py -v
```

共 151 个用例：指标 13、合规 12、持仓 8、持仓监控 23、入场合规闸门 41、信号追踪 14、策略参数 19、回测 12、热点池 9。全部离线运行，不依赖 API Key 或网络。

## 目录结构

```
Invest/
├── README.md
├── ARCHITECTURE.md        # 架构与设计文档
├── requirements.txt
├── config.py              # 统一配置（路径、API、合规规则、策略参数）
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
│   ├── announcement_fetcher.py  # 公告 fallback 链 + 防编造护栏 + PDF 提取
│   ├── financial_comparison.py
│   └── industry_mapper.py
├── pipeline/              # 数据管道
│   ├── database.py        # 含 signals/limit_pool/hot_pool 表
│   ├── indicators.py
│   ├── market_scanner.py  # 扫描 + 持仓监控区块 + 超短热点区块，输出 MD + JSON
│   ├── hot_pool.py        # 超短热点池构建（涨停/连板/炸板 + 强板块领涨股）与日线补抓
│   ├── signal_tracker.py  # 信号入库/结算/统计（持有天数按系统分，HOT-S=5）
│   └── run_daily.py
├── backtest/              # 回测（与实盘共用 config 策略参数）
│   ├── strategies.py
│   └── run_backtest.py    # 资金曲线 + 回撤图
├── review/                # 交易复盘
│   ├── cli.py             # 含 from-scan/positions 子命令，建仓过合规闸门
│   ├── trade_log.py
│   ├── monitor.py         # 持仓监控 + 回撤状态推导
│   ├── entry_gate.py      # 入场合规闸门
│   ├── buy_card.py        # 买入卡生成（写 vault）
│   ├── positions.py       # 持仓视图与风险敞口（含未实现盈亏）
│   ├── metrics.py
│   ├── compliance_check.py
│   └── report_generator.py
├── tests/                 # 单元测试（151 用例）
├── data/                  # 数据存储
│   ├── market.db
│   ├── trades.json
│   ├── announcements/     # 公告全文缓存
│   └── llm_cache/         # LLM 响应缓存
└── output/                # 报告输出
```

## 设计原则

- **配置集中化**：所有路径、API、规则通过 `config.py` 管理，禁止硬编码；策略参数单一来源（`STRATEGY_PARAMS`），扫描/监控/信号/回测共用
- **优雅降级**：无 LLM Key / 网络异常 / 数据源不可用时，仍输出原始数据或模板；监控与信号追踪失败不拖垮主流程
- **纪律内建**：建仓必过合规闸门；无公告正文不调 LLM（防编造）；持仓每日自动监控止损与退出线
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
| `DRAWDOWN_STATE` | 回撤状态（未设置时自动推导，冲突以推导值为准） | Normal |
| `ENABLE_SCHEDULER` | 启用定时调度 | false |
| `SCHEDULER_TIME` | 定时运行时间 | 08:30 |
| `SERVER_PORT` | API 服务端口 | 8900 |

## 关联文档

- [[基金经理日常工作流]]
- [[投资体系_V5.0]]
- [[90天实盘训练计划]]
- [[ARCHITECTURE]] — 完整架构与设计文档
