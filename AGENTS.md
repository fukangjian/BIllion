# AGENTS.md

> 本文件面向 AI 编码代理，描述本仓库的结构、技术栈与开发约定。阅读本文件即可上手，无需先验知识。

## 1. 项目概览

本仓库（`E:/Billion`）是一个 **Obsidian 投资知识库（vault）**，主题为「职业投资者体系」。仓库中唯一的代码项目是根目录下的 **`Invest/`** —— 一个本地 Python 投资研究与量化执行工具集，由原 `AI/`（LLM 研究助手）与 `Quant/`（量化执行工具）合并而来。

核心理念：**人负责战略与决策，AI/量化负责信息整理与计算，纪律负责执行。** Invest 不替代人工决策，承担三类自动化职责：

- **信息整理**：A 股行情抓取（AkShare）、公告/财报/产业链研究（LLM）、每日研究日报
- **计算执行**：技术指标、市场扫描（突破候选）、仓位计算、策略回测
- **纪律审计**：交易日志、合规检查、周/月复盘报告

仓库其余目录（`【1】宏观经济` … `【11】统一体系`）均为 Markdown 笔记，无代码。其中 `【10】实盘记录/` 是 Invest 复盘模块的输出目标（周报/月报/统计直接写入 vault），`【11】统一体系/投资体系_V5.0.md` 是合规规则与策略命名的业务依据。

## 2. 仓库结构

```
E:/Billion/                     # Obsidian vault 根
├── 【1】～【11】*/              # Markdown 笔记（宏观经济、产业研究、实盘记录等）
├── 建立职业投资者体系.md
└── Invest/                     # ★ 全部代码在此，所有命令须在此目录下运行
    ├── README.md               # 使用文档（命令速查）
    ├── ARCHITECTURE.md         # 完整架构与设计文档（含函数索引、数据流图）
    ├── requirements.txt        # 唯一依赖清单（无 pyproject.toml / 无打包）
    ├── config.py               # 统一配置：路径、API 密钥、股票池、合规规则
    ├── run_all.py              # 统一入口：盘前一键（数据管道 + 研究日报）
    ├── server.py               # FastAPI 服务 + APScheduler 定时调度（端口 8900）
    ├── position_calculator.py  # 仓位计算器（风险预算法 + 簇风险检查）
    ├── shared/                 # 共享服务层
    │   ├── utils.py            # 代理绕过、重试、Markdown/frontmatter 工具
    │   ├── data_fetcher.py     # AkShare 数据获取（ThreadPoolExecutor 并行）
    │   ├── llm_client.py       # 多 LLM 路由（Kimi→DeepSeek→Custom）+ 24h 缓存
    │   └── prompts.py          # 提示词模板（纯字符串常量）
    ├── pipeline/               # 数据管道
    │   ├── database.py         # SQLite 缓存（REPLACE INTO upsert）
    │   ├── indicators.py       # Donchian 通道、ATR、市场状态 A/B/C/D
    │   ├── market_scanner.py   # 市场扫描，同写 MD（人读）+ JSON（机器读）
    │   └── run_daily.py        # CLI 入口
    ├── research/               # AI 研究助手
    │   ├── daily_report.py         # 每日研究日报
    │   ├── announcement_analyzer.py / announcement_fetcher.py  # 巨潮公告全文 + 分块摘要
    │   ├── financial_comparison.py # 财报对比
    │   └── industry_mapper.py      # 产业链映射
    ├── backtest/               # Backtrader 回测（S1-A / S2-A 策略，权益曲线 + 回撤图）
    ├── review/                 # 交易复盘
    │   ├── cli.py              # 子命令：add/update/list/stats/weekly/monthly/check/from-scan
    │   ├── trade_log.py        # Trade dataclass + trades.json 存储
    │   ├── positions.py        # 持仓视图与风险敞口
    │   ├── metrics.py / compliance_check.py / report_generator.py
    ├── tests/                  # pytest 单元测试（30 用例）
    ├── data/                   # 数据存储（market.db、trades.json、公告与 LLM 缓存）
    └── output/                 # 报告输出（日报、扫描、回测图等）
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
  - `pytest` — 测试
  - `python-dotenv`、`tabulate` 为预留依赖，当前代码未直接 import

## 4. 环境与常用命令

**所有命令均须先 `cd Invest/`**（脚本依赖该目录为工作目录，且内部用 `sys.path.insert` 定位项目根）。

```powershell
cd "e:\Billion\Invest"
pip install -r requirements.txt        # 安装依赖

# —— 盘前工作流 ——
python run_all.py                      # 一键：并行取数 + 市场扫描 + 研究日报
python run_all.py --skip-fetch         # 跳过取数（用已有数据库）
python pipeline/run_daily.py           # 仅数据管道
python research/run_daily_report.py    # 仅研究日报

# —— API 服务（127.0.0.1:8900）——
python server.py                       # 启动；POST /pre-market、/pipeline、/research，GET /status、/latest-scan、/latest-report
$env:ENABLE_SCHEDULER="true"           # 可选：每天 08:30（SCHEDULER_TIME）自动盘前

# —— 其他工具 ——
python position_calculator.py -s 600519 -e 1800 --stop 1700 -t core --equity 1000000
python backtest/run_backtest.py --strategy S1-A --symbol 600519 --start 2020-01-01
python review/cli.py add 600519 --account 核心 --system S1-A --entry 1800 --stop 1700 --risk 0.5 --shares 100
python review/cli.py from-scan 600519  # 从扫描 JSON 读取突破参数快速建仓
python review/cli.py weekly / monthly  # 周报/月报写入 vault 的【10】实盘记录/

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

其他环境变量：`ACCOUNT_EQUITY`（默认 1000000）、`DRAWDOWN_STATE`（Normal/Caution/Defensive/Review）、`SERVER_PORT`（8900）等，完整列表见 `Invest/README.md` 与 `Invest/ARCHITECTURE.md` 第 7 章。

## 5. 开发约定

改动代码时请遵守以下既有约定（源自 `ARCHITECTURE.md` 与代码实践）：

- **配置集中化**：所有路径、API、规则参数统一放 `config.py`，**禁止在业务代码中硬编码** vault 路径、密钥或规则数值。合规规则（风险上限、仓位上限、簇限制）映射「投资体系 V5.0」，改动需与该体系对齐。
- **注释与文档使用中文**。部分业务数据结构直接使用中文键名/字段名（如 `review/trade_log.py` 的 `Trade` dataclass 字段为中文），保持一致，不要擅自英文化。
- **模块独立可运行**：每个脚本都有 `if __name__ == "__main__"` CLI 入口，可单独调试；脚本/测试文件顶部用 `sys.path.insert(0, str(ROOT))` 定位项目根后再 `from config import ...`。
- **优雅降级**：无 LLM Key、网络失败、数据源不可用时必须仍能输出原始数据或 fallback 模板，并在报告中标注；单只股票抓取失败不阻塞其他标的。
- **Obsidian 原生输出**：报告输出 Markdown + YAML frontmatter（`shared/utils.py` 的 `obsidian_frontmatter` / `write_markdown` / `df_to_markdown_table`），支持 wikilink 与标签检索。
- **结构化双写**：机器可消费的结果（如市场扫描）同时输出 `.md`（人读）与 `.json`（`review/cli.py from-scan` 等程序化消费）。
- **缓存 aside 模式**：公告全文（`data/announcements/`）与 LLM 响应（`data/llm_cache/`，sha256(prompt) 为键，24h TTL）均为本地 JSON 缓存。
- **Windows 代理**：访问外部数据源前用 `shared/utils.py` 的 `patch_bypass_proxy` / `bypass_proxy` 绕过系统代理，否则东财等数据源会失败（历史教训，见 git log）。
- 无 linter/formatter/type-checker 配置，代码风格以周边文件为准（类型标注 + docstring 普遍使用）。

## 6. 测试

- 框架：pytest，目录 `Invest/tests/`，共 **30 个用例**（指标 10 + 合规 12 + 持仓 8），已验证全部通过（`30 passed`）。
- 运行：`python -m pytest tests/ -v`（在 `Invest/` 目录下）。
- 测试**不依赖网络与 API Key**：使用 mock DataFrame 与临时文件（如 `tmp_path`）隔离数据。新增测试也必须保持这一特性——禁止在单元测试中真实请求 AkShare/LLM。
- 测试通过 `sys.path.insert` 引入项目根模块，无需安装包。
- 目前无 CI；ARCHITECTURE.md 将 GitHub Actions 每日 smoke test（mock AkShare）列为未来方向。

## 7. 数据与安全

- **密钥仅走环境变量**（`KIMI_API_KEY` 等），仓库中不得出现真实密钥；`.env` 已在 `.gitignore` 中。
- `.gitignore` 还排除 `__pycache__/`、`*.pyc`、`*.db`、`*.db-journal` —— `data/market.db` 不入库。
- `data/trades.json`（交易日志）是人工可编辑的核心数据，读写时注意保持 JSON 结构；损坏时 `TradeLog` 会降级为空列表。
- FastAPI 服务默认仅绑定 `127.0.0.1`，不要改为对外暴露；长任务经 `_execute_task` 串行执行（单线程池 + 600s 超时 + 409 并发冲突）。
- LLM 缓存与公告缓存含抓取的原文，注意其中可能含未公开信息，不要外传。

## 8. 部署与运行方式

无部署流程——这是纯本地工具，三种运行方式：

1. **CLI**：各模块脚本直接运行（日常主力）。
2. **FastAPI 服务**：`python server.py`，供 Obsidian/Webhook/脚本 HTTP 触发。
3. **定时调度**：服务内设 `ENABLE_SCHEDULER=true` + `SCHEDULER_TIME=08:30`，APScheduler 每日自动盘前。

## 9. 已知限制（摘自 ARCHITECTURE.md 第 13 章）

- 持仓未实现盈亏暂无实时市价源（`positions._calc_unrealized_pnl` 待接入）。
- 巨潮 PDF 公告仅返回链接，未做正文提取；向量检索、Web UI 为未来方向。
- AkShare 依赖公开数据源接口，接口变动可能导致抓取失败——修改数据获取层后务必实际运行 `python run_all.py` 验证。

## 10. 参考文档

- `Invest/README.md` — 命令速查与使用说明
- `Invest/ARCHITECTURE.md` — 完整架构文档：分层图、模块依赖、函数索引、SQLite schema、LLM 路由、扩展指南（新增策略/数据源/LLM 提供商/合规规则的步骤）
- `【11】统一体系/投资体系_V5.0.md` — 业务规则依据（合规、仓位、策略命名）
