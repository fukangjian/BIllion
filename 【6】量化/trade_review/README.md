# 交易复盘自动化

基于投资体系 V5.0 规则的交易日志管理、指标计算、合规检查和报告生成工具。

## 功能

| 功能 | 说明 |
|---|---|
| 交易日志 | JSON 存储，支持增删改查 |
| R 倍数 | `(退出价 - 入场价) / (入场价 - 止损价)` |
| MFE/MAE | 持仓期间最大有利/不利波动（可自动从 AkShare 获取） |
| 累计统计 | 胜率、期望值、Profit Factor、最大连续亏损、最大回撤 |
| 合规检查 | 单笔风险、止损、仓位、风险簇、回撤状态 |
| 周报/月报 | 输出 Obsidian 兼容 Markdown 到 `【10】实盘记录/` |

## 安装

依赖已包含在 `【6】量化/requirements.txt` 中：

```powershell
cd "e:\doc\阅读书籍\【6】量化"
pip install -r requirements.txt
```

## 配置

环境变量（可选）：

```powershell
$env:ACCOUNT_EQUITY = "1000000"      # 账户权益，用于仓位比例计算
$env:DRAWDOWN_STATE = "Normal"        # 回撤状态: Normal/Caution/Defensive/Review
```

交易日志存储路径：`data/trades.json`

## 使用方法

所有命令通过 `cli.py` 执行：

### 添加交易

```powershell
cd "e:\doc\阅读书籍\【6】量化\trade_review"

python cli.py add 688192 `
  --name 迪哲医药 `
  --account 产业 `
  --cluster 创新药 `
  --system S1-A `
  --logic "舒沃替尼商业化加速" `
  --entry 68.80 `
  --stop 62.10 `
  --risk 0.3 `
  --shares 400
```

### 更新交易（平仓）

```powershell
python cli.py update T20260715_abc123 `
  --exit-price 65.00 `
  --exit-date 2026-07-28 `
  --exit-reason "逻辑削弱" `
  --fetch-prices
```

`--fetch-prices` 会自动从 AkShare 获取持仓期间最高/最低价，计算 MFE/MAE。

### 列出交易

```powershell
python cli.py list
python cli.py list --closed
python cli.py list --open
```

### 查看统计

```powershell
python cli.py stats
python cli.py stats --closed --by-strategy --by-account
```

### 合规检查

```powershell
python cli.py check
python cli.py check --drawdown Caution --equity 950000
```

检查项：
- 单笔风险是否超过限额（正常/回撤期不同阈值）
- 是否有止损计划
- 单票仓位是否超限
- 风险簇暴露和止损风险是否超限
- 回撤降级状态下是否执行了被禁止的交易

### 生成报告

```powershell
# 周报 → 【10】实盘记录/每周复盘/
python cli.py weekly

# 月报 → 【10】实盘记录/每月复盘/
python cli.py monthly

# 含自动 MFE/MAE 计算
python cli.py weekly --fetch-prices
```

## 目录结构

```
trade_review/
├── config.py           # 路径与合规规则
├── trade_log.py        # Trade 数据类 + JSON 存储
├── metrics.py          # R/MFE/MAE/统计
├── compliance_check.py # 规则偏差检查
├── report_generator.py # 周报/月报
├── cli.py              # 命令行入口
├── data/
│   └── trades.json     # 交易记录
└── README.md
```

## Trade 字段说明

| 字段 | 说明 |
|---|---|
| 交易编号 | 自动生成，格式 TYYYYMMDD_xxxxxx |
| 日期 | 入场日期 |
| 股票代码/名称 | 6 位代码 |
| 账户类型 | 核心/产业/事件/实验 |
| 风险簇 | 创新药/AI算力/半导体等 |
| 入场系统 | S1-A/S2-A/预埋 |
| 核心逻辑 | 一句话投资逻辑 |
| 入场价/止损价 | 用于 R 倍数计算 |
| 风险率 | 单笔风险占账户比例 (%) |
| 股数/仓位金额 | 仓位信息 |
| 实际退出价/退出日期/退出原因 | 平仓信息 |
| 是否系统内交易 | 默认 true |
| MFE/MAE/R倍数 | 自动或手动填写 |

## 合规规则来源

规则参数来自 `【11】统一体系/投资体系_V5.0.md`：
- 单笔风险上限（正常/回撤期）
- 单票仓位上限（核心 10%、产业 8%、创新药 6%、事件 4%）
- 风险簇上限（创新药 25%/1.2% 等）
- 回撤状态机禁止交易类型

## 与 Obsidian 工作流

1. 盘前/盘中：用 `cli.py add` 记录新交易
2. 平仓后：`cli.py update --fetch-prices` 自动计算指标
3. 每日：`cli.py check` 检查合规
4. 每周日：`cli.py weekly` 生成周报
5. 每月末：`cli.py monthly` 生成系统健康报告

报告输出到 Obsidian 知识库对应目录，可直接链接和编辑。

## 独立运行

各模块均支持独立运行：

```powershell
python -m trade_log      # 不推荐，通过 cli.py 使用
python compliance_check.py  # 需在 __main__ 中调用
```

推荐统一使用 `python cli.py <command>`。
