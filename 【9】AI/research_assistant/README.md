# AI 研究助手

基于 AkShare + LLM 的 A 股研究自动化工具，输出 Obsidian 兼容 Markdown，直接写入知识库。

## 功能模块

| 模块 | 脚本 | 功能 |
|---|---|---|
| 公告摘要 | `announcement_analyzer.py` | 获取最新公告，LLM 结构化摘要 |
| 财报对比 | `financial_comparison.py` | 多公司财务指标对比 + 差异分析 |
| 产业链映射 | `industry_mapper.py` | 上中下游结构 + 代表公司 + 催化 |
| 每日日报 | `daily_report.py` / `run_daily_report.py` | 综合宏观、板块、公告、突破候选 |

## 安装

```powershell
cd "e:\doc\阅读书籍\【9】AI\research_assistant"
pip install -r requirements.txt
```

## 配置

通过环境变量配置（PowerShell 示例）：

```powershell
# OpenAI（默认）
$env:OPENAI_API_KEY = "sk-..."
$env:OPENAI_MODEL = "gpt-4o-mini"

# 或使用 Anthropic
$env:LLM_PROVIDER = "anthropic"
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:ANTHROPIC_MODEL = "claude-3-5-haiku-20241022"
```

也可在项目根目录创建 `.env` 文件（需 python-dotenv 支持）。

**无 API Key 时**：工具仍可运行，输出原始数据和降级模板，跳过 LLM 分析。

## 使用方法

### 公告摘要

```powershell
python announcement_analyzer.py 688192
python announcement_analyzer.py 600519 -n 5
```

输出: `【9】AI/announcements/YYYY-MM-DD_代码_名称_公告摘要.md`

### 财报对比

```powershell
python financial_comparison.py 600519 000858 300750
```

输出: `【9】AI/financial_reports/YYYY-MM-DD_财报对比_代码.md`

### 产业链映射

```powershell
python industry_mapper.py 创新药
python industry_mapper.py AI算力
python industry_mapper.py 半导体
```

预置框架: 创新药、AI算力、半导体、新能源。未知关键词由 LLM 扩展。

输出: `【9】AI/industry_maps/YYYY-MM-DD_产业链_关键词.md`

### 每日研究日报

```powershell
# 使用默认关注列表
python run_daily_report.py

# 自定义持仓和板块
python run_daily_report.py -w 688192 600519 -s 创新药 AI算力
```

输出: `【9】AI/daily_reports/YYYY-MM-DD_每日研究日报.md`

日报包含：
- 今日要点（LLM 摘要）
- 市场宏观（指数、涨跌家数）
- 关注板块表现
- 持仓公司最新公告
- 突破候选（联动 `【6】量化/data_pipeline/market_scanner.py`）

## 目录结构

```
research_assistant/
├── config.py              # 配置
├── llm_client.py          # LLM 调用（重试 + 降级）
├── utils.py               # 通用工具
├── prompts.py             # 提示词模板
├── announcement_analyzer.py
├── financial_comparison.py
├── industry_mapper.py
├── daily_report.py
├── run_daily_report.py    # 日报入口
├── requirements.txt
└── README.md
```

## 与量化模块联动

每日日报会自动尝试读取 `【6】量化/data_pipeline/output/market_scan_YYYY-MM-DD.md`。
若不存在，会尝试调用 `market_scanner.run_scan()` 生成。

建议盘前工作流：

```powershell
# 1. 更新行情数据并扫描
cd "e:\doc\阅读书籍\【6】量化\data_pipeline"
python run_daily.py

# 2. 生成 AI 日报
cd "e:\doc\阅读书籍\【9】AI\research_assistant"
python run_daily_report.py
```

## 注意事项

- AkShare 数据依赖网络，失败时会自动重试 3 次
- LLM 调用失败时 graceful degrade，不影响数据输出
- 所有输出为 Obsidian 兼容 Markdown，含 YAML frontmatter
- Windows 路径使用 pathlib，无需手动转义
