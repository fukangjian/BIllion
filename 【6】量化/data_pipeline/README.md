# 数据获取层

基于 AkShare + SQLite 的本地市场数据管道，支持日线行情、板块强度、涨跌停统计、ETF 资金流向和龙虎榜数据。

## 目录结构

```
data_pipeline/
├── config.py          # 配置文件（数据库路径、参数）
├── database.py        # SQLite 数据库管理
├── data_fetcher.py    # AkShare 数据获取
├── indicators.py      # 技术指标计算
├── market_scanner.py  # 每日市场扫描
├── run_daily.py       # 每日运行入口
├── data/              # SQLite 数据库（自动生成）
└── output/            # Markdown 扫描报告（自动生成）
```

## 快速开始

```powershell
# 1. 安装依赖（在上级目录）
cd "e:\doc\阅读书籍\【6】量化"
pip install -r requirements.txt

# 2. 初始化数据库
python data_pipeline/database.py

# 3. 获取数据并生成扫描报告
python data_pipeline/run_daily.py

# 4. 仅扫描（使用已有数据）
python data_pipeline/run_daily.py --skip-fetch

# 5. 指定股票列表
python data_pipeline/run_daily.py --symbols 600519 000858 300750
```

## 数据库表

| 表名 | 说明 |
|------|------|
| daily_quotes | 个股/指数日线 OHLCV |
| sector_quotes | 板块指数行情 |
| etf_flow | ETF 资金流向 |
| limit_stats | 涨跌停及市场宽度 |
| dragon_tiger | 龙虎榜 |
| market_state | 市场状态 A/B/C/D 记录 |

## 市场状态判断

| 状态 | 条件 | 建议仓位 |
|------|------|----------|
| A | 指数>20周均线 + 成交活跃 + 涨跌比>1.5 | 70%-90% |
| B | 指数偏强但共振不足 | 40%-70% |
| C | 信号不一致，震荡 | 20%-50% |
| D | 指数<20周均线 + 涨跌比<0.7 | 0%-30% |

## 输出

扫描报告保存为 Markdown 格式，可直接导入 Obsidian：

```
data_pipeline/output/market_scan_2026-07-27.md
```

报告包含：
- 市场状态 A/B/C/D 判断
- 20日突破候选（S1-A）
- 55日突破候选（S2-A）
- 板块相对强度 Top 15

## 注意事项

- AkShare 为免费接口，请控制请求频率（脚本内置重试和延迟）
- 首次运行需联网获取数据
- 网络异常时会跳过失败项，使用已有数据继续扫描
