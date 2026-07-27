# 回测框架

基于 Backtrader 的 S1-A / S2-A 突破系统回测。

## 策略说明

### S1-A 快速系统
- **入场**: 收盘价突破 20 日最高价
- **退出**: 收盘价跌破 10 日最低价
- **止损**: 2 × ATR(20)
- **加仓**: 每上涨 0.5N~1N 加一单位，最多 4 单位

### S2-A 慢速系统
- **入场**: 收盘价突破 55 日最高价
- **退出**: 收盘价跌破 20 日最低价
- **止损**: 2 × ATR(20)
- **加仓**: 每上涨 0.5N~1N 加一单位，最多 4 单位

### 仓位管理
- 单笔风险: 账户权益 × 0.5%
- 单位大小 = 风险金额 / (2 × ATR)
- A股100股一手

## 使用方法

```powershell
cd "e:\doc\阅读书籍\【6】量化"

# S1-A 回测贵州茅台 2020-2025
python backtest/run_backtest.py --strategy S1-A --symbol 600519 --start 2020-01-01 --end 2025-01-01

# S2-A 回测五粮液
python backtest/run_backtest.py --strategy S2-A --symbol 000858 --start 2018-01-01

# 打印详细交易日志
python backtest/run_backtest.py --strategy S1-A --symbol 600519 --start 2020-01-01 --verbose
```

## 输出指标

| 指标 | 说明 |
|------|------|
| 总收益率 | 回测区间总回报 |
| 最大回撤 | 峰值到谷底最大跌幅 |
| 胜率 | 盈利交易 / 总交易 |
| 平均 R | 每笔交易平均 R 倍数 |
| Profit Factor | 总盈利 / 总亏损 |
| Sharpe Ratio | 风险调整收益 |

## 输出文件

资金曲线图保存在 `backtest/output/` 目录：

```
backtest/output/S1-A_600519_2020-01-01_2025-01-01.png
```

## 数据来源

1. 优先从本地 SQLite 数据库读取（需先运行 data_pipeline）
2. 数据库无数据时自动从 AkShare 在线获取

## 建议验证区间

| 区间 | 市场特征 |
|------|----------|
| 2018 | 熊市 |
| 2020 | 疫情后牛市 |
| 2022 | 调整 |
| 2024-2025 | 近期 |

```powershell
# 批量测试不同区间
python backtest/run_backtest.py -st S1-A -s 600519 --start 2018-01-01 --end 2018-12-31
python backtest/run_backtest.py -st S1-A -s 600519 --start 2020-01-01 --end 2020-12-31
python backtest/run_backtest.py -st S2-A -s 600519 --start 2022-01-01 --end 2022-12-31
```

## 注意事项

- 回测仅用于排除明显无效的策略，不用于精确优化参数
- 未考虑滑点、涨跌停无法成交等 A 股特殊因素
- 结果仅供参考，不构成投资建议
