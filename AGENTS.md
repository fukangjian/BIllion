# AGENTS.md

> 本文件面向 AI 编码代理，描述本仓库的结构、技术栈与开发约定。阅读本文件即可上手，无需先验知识。

## 1. 项目概览

本仓库（`E:/Billion`）是一个 **Obsidian 投资知识库（vault）**，主题为「职业投资者体系」。仓库中唯一的代码项目是根目录下的 **`Invest/`** —— 一个本地 Python 投资研究与量化执行工具集，由原 `AI/`（LLM 研究助手）与 `Quant/`（量化执行工具）合并而来。

核心理念：**人负责战略与决策，AI/量化负责信息整理与计算，纪律负责执行。** Invest 不替代人工决策，承担三类自动化职责：

- **信息整理**：A 股行情抓取（AkShare）、公告/财报/产业链研究（LLM）、每日研究日报
- **计算执行**：技术指标、市场扫描（突破候选）、仓位计算、策略回测
- **纪律审计**：持仓监控（止损/退出警报）、交易日志、入场合规闸门、行为纪律自动审计（5 条规则）、周/月复盘报告、信号验证

仓库其余目录（`【1】宏观经济` … `【11】统一体系`）均为 Markdown 笔记，无代码。其中 `【10】实盘记录/` 是 Invest 复盘模块的输出目标（买入卡/周报/月报/统计直接写入 vault），`【11】统一体系/投资体系_V5.0.md` 是合规规则与策略命名的业务依据。

## 2. 仓库结构

```
E:/Billion/                     # Obsidian vault 根
├── 【1】～【11】*/              # Markdown 笔记（宏观经济、产业研究、实盘记录等）
├── 建立职业投资者体系.md
└── Invest/                     # ★ 全部代码在此，所有命令须在此目录下运行
    ├── README.md               # 使用文档（命令速查）
    ├── ARCHITECTURE.md         # 完整架构与设计文档（含函数索引、数据流图）
    ├── requirements.txt        # 唯一依赖清单（无 pyproject.toml / 无打包）
    ├── config.py               # 统一配置：路径、API 密钥、股票池、合规规则、策略参数
    ├── run_all.py              # 统一入口：盘前一键（取数→扫描→持仓监控→信号结算→日报）
    ├── server.py               # FastAPI 服务 + APScheduler 定时调度（端口 8900）
    ├── position_calculator.py  # 仓位计算器（calc_position 纯函数，口径同 config）
    ├── shared/                 # 共享服务层
    │   ├── utils.py            # 代理绕过、重试、Markdown/frontmatter 工具
    │   ├── data_fetcher.py     # AkShare 数据获取（ThreadPoolExecutor 并行）
    │   ├── llm_client.py       # 多 LLM 路由（Kimi→DeepSeek→Custom）+ 24h 缓存
    │   └── prompts.py          # 提示词模板（纯字符串常量）
    ├── pipeline/               # 数据管道
    │   ├── database.py         # SQLite 缓存（REPLACE INTO upsert，含 signals/limit_pool/hot_pool 表）
    │   ├── indicators.py       # Donchian 通道、ATR、市场状态 A/B/C/D
    │   ├── market_scanner.py   # 市场扫描 + 持仓监控区块 + 超短热点区块，同写 MD + JSON
    │   ├── hot_pool.py         # 超短热点池：涨停/连板/炸板+强板块领涨股，构建与日线补抓
    │   ├── signal_tracker.py   # 信号追踪：突破信号入库、每日结算（按系统分持有天数）、胜率/平均R 统计
    │   └── run_daily.py        # CLI 入口
    ├── research/               # AI 研究助手
    │   ├── daily_report.py         # 每日研究日报
    │   ├── announcement_analyzer.py / announcement_fetcher.py  # 公告 fallback 链 + 防编造护栏 + PDF 提取
    │   ├── financial_comparison.py # 财报对比
    │   └── industry_mapper.py      # 产业链映射
    ├── backtest/               # Backtrader 回测（S1-A / S2-A 策略，与实盘共用 config 策略参数）
    ├── review/                 # 交易复盘
    │   ├── cli.py              # 子命令：add/update/list/show/stats/weekly/monthly/check/positions/import/sell-check/from-scan
    │   ├── trade_log.py        # Trade dataclass + trades.json 存储
    │   ├── monitor.py          # 持仓监控：止损/退出通道警报、回撤状态自动推导
    │   ├── entry_gate.py       # 入场合规闸门（高级违规拒绝，--force 留痕）
    │   ├── buy_card.py         # 建仓后自动生成买入卡（写入 vault 交易日志/）
    │   ├── positions.py        # 持仓视图、风险敞口、未实现盈亏（market.db 收盘价）
    │   ├── import_broker.py    # 券商成交导入（MD 表/CSV → FIFO 配对落库，不过入场闸门）
    │   ├── discipline_audit.py # 纪律自动审计（追高接回/闪电换仓/禁买板块/无止损/非系统交易）
    │   ├── metrics.py / compliance_check.py / report_generator.py
    ├── tests/                  # pytest 单元测试（142 用例）
    ├── data/                   # 数据存储（market.db、trades.json、公告与 LLM 缓存）
    └── output/                 # 报告输出（日报、扫描、持仓监控 JSON、回测图等）
```

## 3. 技术栈

- **语言**：Python 3.13（开发机为 Windows，命令示例用 PowerShell；Git Bash 亦可）
- **无构建/打包系统**：没有 `pyproject.toml`、`setup.py` 或锁文件；纯脚本项目，`pip install -r requirements.txt` 后直接运行
- **主要依赖**（`Invest/requirements.txt`）：
  - `akshare` — A 股行情/公告/财务数据源（免费，内置多源 fallback）
  - `pandas` / `numpy` — 数据处理与指标计算
  - `openai` — 统一调用 Kimi(Moonshot) / DeepSeek / 自定义 OpenAI 兼容端点
  - `backtrader` + `matplotlib` — 回测引擎与图表
  - `fastapi` + `uvicorn` + `apscheduler` — HTTP 服务与定时调度
  - `beautifulsoup4` / `requests` — 巨潮公告 HTML 抓取解析
  - `pypdf` — 巨潮 PDF 公告正文提取（最多前 30 页）
  - `pytest` — 测试
  - `python-dotenv`、`tabulate` 为预留依赖，当前代码未直接 import

## 4. 环境与常用命令

**所有命令均须先 `cd Invest/`**（脚本依赖该目录为工作目录，且内部用 `sys.path.insert` 定位项目根）。

```powershell
cd "e:\Billion\Invest"
pip install -r requirements.txt        # 安装依赖

# —— 盘前工作流 ——
python run_all.py                      # 一键：取数→扫描→持仓监控→信号结算→研究日报
python run_all.py --skip-fetch         # 跳过取数（用已有数据库）
python pipeline/run_daily.py           # 仅数据管道
python research/run_daily_report.py    # 仅研究日报

# —— API 服务（127.0.0.1:8900）——
python server.py                       # 启动；POST /pre-market、/pipeline、/research，GET /status、/latest-scan、/latest-report
$env:ENABLE_SCHEDULER="true"           # 可选：每天 08:30（SCHEDULER_TIME）自动盘前

# —— 交易执行链路 ——
python review/cli.py from-scan 600519             # 只打印突破参数与建议（不落库）
python review/cli.py from-scan 600519 --execute   # 一键建仓：仓位计算→合规闸门→写库→生成买入卡
python review/cli.py add 600519 --account 核心 --system S1-A --entry 1800 --stop 1700 --risk 0.5 --shares 100
python review/cli.py positions                    # 持仓摘要（未实现盈亏/风险敞口/回撤状态）
python review/cli.py check                        # 手动合规检查（末尾附纪律审计摘要）
python review/cli.py import --file 成交.md --year 2026 --dry-run   # 券商成交导入（先演练，--symbol-map 补名称映射）
python review/cli.py sell-check 600519            # 卖点检查单（卖出前：止损/通道/持有天数/建议挂单价）
python position_calculator.py -s 600519 -e 1800 --stop 1700 -t 核心 --equity 1000000

# —— 信号验证与复盘 ——
python pipeline/signal_tracker.py stats --days 90  # 信号胜率/平均R/PF
python pipeline/signal_tracker.py settle           # 手动结算信号（盘前流程已自动执行）
python review/cli.py weekly / monthly              # 周报/月报（含信号验证节）写入 vault【10】实盘记录/
python review/cli.py stats                         # 终端统计 + 写【10】实盘记录/统计/
python backtest/run_backtest.py --strategy S1-A --symbol 600519 --start 2020-01-01

# —— 测试 ——
python -m pytest tests/ -v
```

### LLM 配置（可选）

通过环境变量配置，按优先级自动路由 `Kimi → DeepSeek → Custom`；相同 prompt 缓存 24 小时（`data/llm_cache/`）。**无任何 API Key 时全部功能仍可运行**，研究模块输出原始数据与降级模板。

```powershell
$env:KIMI_API_KEY="sk-..."        # 默认提供商
$env:DEEPSEEK_API_KEY="sk-..."    # 备选
$env:CUSTOM_LLM_API_KEY / CUSTOM_LLM_BASE_URL / CUSTOM_LLM_MODEL  # 自定义端点
```

其他环境变量：`ACCOUNT_EQUITY`（默认 32500，3.25 万实盘）、`DRAWDOWN_STATE`（Normal/Caution/Defensive/Review；未显式设置时由 `review/monitor.py` 按权益曲线自动推导，冲突时以推导值为准）、`WEEKLY_REVIEW_TIME`（15:45，周五自动周报）、`MONTHLY_REVIEW_TIME`（16:00，月末自动月报）、`SERVER_PORT`（8900）等，完整列表见 `Invest/README.md` 与 `Invest/ARCHITECTURE.md` 第 7 章。

## 5. 开发约定

改动代码时请遵守以下既有约定（源自 `ARCHITECTURE.md` 与代码实践）：

- **配置集中化**：所有路径、API、规则参数统一放 `config.py`，**禁止在业务代码中硬编码** vault 路径、密钥或规则数值。合规规则（风险上限、仓位上限、簇限制）映射「投资体系 V5.0」，限额已按 3.25 万小资金校准（2026-07），改动需与该体系对齐；`BANNED_BOARD_PREFIXES=("300","301")` 禁买创业板为建仓闸门高级违规。**策略参数单一来源**：通道周期、ATR、止损倍数、加仓间距等全部在 `STRATEGY_PARAMS` / `ATR_*` / `ADD_SPACING_*`，扫描、监控、信号追踪、回测、仓位计算共同引用，不得另起字面量。
- **注释与文档使用中文**。部分业务数据结构直接使用中文键名/字段名（如 `review/trade_log.py` 的 `Trade` dataclass 字段为中文），保持一致，不要擅自英文化。
- **模块独立可运行**：每个脚本都有 `if __name__ == "__main__"` CLI 入口，可单独调试；脚本/测试文件顶部用 `sys.path.insert(0, str(ROOT))` 定位项目根后再 `from config import ...`。
- **优雅降级**：无 LLM Key、网络失败、数据源不可用时必须仍能输出原始数据或 fallback 模板，并在报告中标注；单只股票抓取失败不阻塞其他标的；持仓监控/信号追踪失败不得拖垮扫描与日报主流程。
- **入场合规闸门**：所有写入 trades.json 的建仓路径（`add`、`from-scan --execute`）必须经 `review/entry_gate.py` 检查；新增建仓入口时同样接入，高级违规默认拒绝、`--force` 强制须在备注留痕。
- **公告防编造护栏**：未取得公告正文时禁止调用 LLM 分析（`announcement_fetcher.has_real_content` 判定），只列标题+链接并标注；LLM 输出不得出现无正文来源的精确数字。
- **Obsidian 原生输出**：报告输出 Markdown + YAML frontmatter（`shared/utils.py` 的 `obsidian_frontmatter` / `write_markdown` / `df_to_markdown_table`），支持 wikilink 与标签检索。
- **结构化双写**：机器可消费的结果（如市场扫描、持仓监控）同时输出 `.md`（人读）与 `.json`（`review/cli.py from-scan` 等程序化消费）。
- **缓存 aside 模式**：公告全文（`data/announcements/`）与 LLM 响应（`data/llm_cache/`，sha256(prompt) 为键，24h TTL）均为本地 JSON 缓存。
- **Windows 代理**：访问外部数据源前用 `shared/utils.py` 的 `patch_bypass_proxy` / `bypass_proxy` 绕过系统代理，否则东财等数据源会失败（历史教训，见 git log）。
- 无 linter/formatter/type-checker 配置，代码风格以周边文件为准（类型标注 + docstring 普遍使用）。
- **修改后主动提交 git**：每次代码/文档修改完成后，主动整理为语义清晰的分批 commit 并提交（用户约定，2026-07-31 起生效）；仅本地提交，推送远程需另行确认。

## 6. 测试

- 框架：pytest，目录 `Invest/tests/`，共 **196 个用例**（指标 13 + 合规 12 + 持仓 8 + 监控 23 + 入场合规闸门 46 + 信号追踪 14 + 策略参数 19 + 回测 12 + 热点池 9 + 券商导入 14 + 纪律审计 14 + 卖点检查 9 + 统计口径 3），已验证全部通过（`196 passed`）。
- 运行：`python -m pytest tests/ -v`（在 `Invest/` 目录下）。
- 测试**不依赖网络与 API Key**：使用 mock DataFrame 与临时文件（如 `tmp_path`、临时 SQLite）隔离数据。新增测试也必须保持这一特性——禁止在单元测试中真实请求 AkShare/LLM。
- 测试通过 `sys.path.insert` 引入项目根模块，无需安装包。
- 目前无 CI；ARCHITECTURE.md 将 GitHub Actions 每日 smoke test（mock AkShare）列为未来方向。

## 7. 数据与安全

- **密钥仅走环境变量**（`KIMI_API_KEY` 等），仓库中不得出现真实密钥；`.env` 已在 `.gitignore` 中。
- `.gitignore` 还排除 `__pycache__/`、`*.pyc`、`*.db`、`*.db-journal` —— `data/market.db` 不入库。
- `data/trades.json`（交易日志）是人工可编辑的核心数据，读写时注意保持 JSON 结构；损坏时 `TradeLog` 会降级为空列表。测试一律用 `tmp_path` 副本，**禁止污染真实 trades.json 与 market.db**。
- FastAPI 服务默认仅绑定 `127.0.0.1`，不要改为对外暴露；长任务经 `_execute_task` 串行执行（单线程池 + 600s 超时 + 409 并发冲突）。
- LLM 缓存与公告缓存含抓取的原文，注意其中可能含未公开信息，不要外传。

## 8. 部署与运行方式

无部署流程——这是纯本地工具，三种运行方式：

1. **CLI**：各模块脚本直接运行（日常主力）。
2. **FastAPI 服务**：`python server.py`，供 Obsidian/Webhook/脚本 HTTP 触发。
3. **定时调度**：服务内设 `ENABLE_SCHEDULER=true` + `SCHEDULER_TIME=08:30`，APScheduler 每日自动盘前（取数→扫描→持仓监控→信号结算→日报）；另有两个 cron job——每周五 `WEEKLY_REVIEW_TIME`（15:45）自动生成周报、每月最后一天 `MONTHLY_REVIEW_TIME`（16:00）自动生成月报。

## 9. 已知限制（摘自 ARCHITECTURE.md 第 13 章）

- 未实现盈亏以 `data/market.db` 最新日线收盘价为市价源（非盘中实时），持仓监控同理——盘前使用足够，盘中需人工盯盘。
- 巨潮 PDF 公告已支持 pypdf 正文提取（前 30 页）；向量检索、Web UI 为未来方向。
- AkShare 依赖公开数据源接口，接口变动可能导致抓取失败——修改数据获取层后务必实际运行 `python run_all.py` 验证。
- 东财行情推送接口（push2）可能对抓取 IP 风控重置（2026-07-30 起本机曾持续触发）；日报/复盘/回测/产业链已配置新浪/同花顺降级链，详见 `Invest/ARCHITECTURE.md` 13.5。

## 10. 参考文档

- `Invest/README.md` — 命令速查与使用说明
- `Invest/ARCHITECTURE.md` — 完整架构文档：分层图、模块依赖、函数索引、SQLite schema、LLM 路由、扩展指南（新增策略/数据源/LLM 提供商/合规规则的步骤）
- `【11】统一体系/投资体系_V5.0.md` — 业务规则依据（合规、仓位、策略命名）
