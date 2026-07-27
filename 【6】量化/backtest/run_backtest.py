"""
回测执行脚本 — 支持 S1-A / S2-A 策略

用法:
    python run_backtest.py --strategy S1-A --symbol 600519 --start 2020-01-01 --end 2025-01-01
    python run_backtest.py --strategy S2-A --symbol 000858 --start 2018-01-01
"""
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import akshare as ak
import backtrader as bt
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

# 路径设置
BACKTEST_ROOT = Path(__file__).resolve().parent
QUANT_ROOT = BACKTEST_ROOT.parent
sys.path.insert(0, str(BACKTEST_ROOT))

from strategies import STRATEGY_MAP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_backtest")

OUTPUT_DIR = BACKTEST_ROOT / "output"


def fetch_data(symbol: str, start: str, end: str) -> pd.DataFrame:
    """从 AkShare 获取回测数据"""
    start_fmt = start.replace("-", "")
    end_fmt = end.replace("-", "")

    logger.info("获取 %s 数据 %s ~ %s", symbol, start, end)
    raw = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start_fmt,
        end_date=end_fmt,
        adjust="qfq",
    )
    if raw is None or raw.empty:
        raise ValueError(f"无法获取 {symbol} 的数据")

    df = pd.DataFrame({
        "datetime": pd.to_datetime(raw["日期"]),
        "open": raw["开盘"].astype(float),
        "high": raw["最高"].astype(float),
        "low": raw["最低"].astype(float),
        "close": raw["收盘"].astype(float),
        "volume": raw["成交量"].astype(float),
    })
    df.set_index("datetime", inplace=True)
    df.sort_index(inplace=True)
    return df


def load_data_from_db(symbol: str, start: str, end: str) -> pd.DataFrame:
    """尝试从本地 SQLite 加载数据"""
    try:
        sys.path.insert(0, str(QUANT_ROOT / "data_pipeline"))
        from database import load_daily_quotes

        df = load_daily_quotes(symbol=symbol, start_date=start, end_date=end)
        if df.empty:
            return pd.DataFrame()

        result = pd.DataFrame({
            "datetime": pd.to_datetime(df["trade_date"]),
            "open": df["open"],
            "high": df["high"],
            "low": df["low"],
            "close": df["close"],
            "volume": df["volume"],
        })
        result.set_index("datetime", inplace=True)
        return result
    except Exception as e:
        logger.debug("本地数据库加载失败: %s", e)
        return pd.DataFrame()


def run_backtest(
    strategy_name: str,
    symbol: str,
    start: str = "2020-01-01",
    end: str = None,
    initial_cash: float = 1_000_000,
    commission: float = 0.0003,
    printlog: bool = False,
) -> dict:
    """
    执行回测
    返回统计指标字典
    """
    end = end or datetime.now().strftime("%Y-%m-%d")

    # 加载数据
    df = load_data_from_db(symbol, start, end)
    if df.empty:
        df = fetch_data(symbol, start, end)

    if df.empty or len(df) < 60:
        raise ValueError(f"数据不足: {symbol} 仅 {len(df)} 条")

    # 创建 Cerebro
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(initial_cash)
    cerebro.broker.setcommission(commission=commission)

    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)

    strategy_cls = STRATEGY_MAP.get(strategy_name)
    if strategy_cls is None:
        raise ValueError(f"未知策略: {strategy_name}，可选: {list(STRATEGY_MAP.keys())}")

    cerebro.addstrategy(strategy_cls, printlog=printlog)

    # 分析器
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", riskfreerate=0.02)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")

    logger.info("开始回测 %s / %s", strategy_name, symbol)
    start_value = cerebro.broker.getvalue()
    results = cerebro.run()
    end_value = cerebro.broker.getvalue()

    strat = results[0]
    stats = _extract_stats(strat, start_value, end_value, initial_cash)
    stats["strategy"] = strategy_name
    stats["symbol"] = symbol
    stats["start"] = start
    stats["end"] = end

    # 保存资金曲线图
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    chart_path = OUTPUT_DIR / f"{strategy_name}_{symbol}_{start}_{end}.png"
    try:
        _plot_equity_curve(initial_cash, end_value, chart_path, strategy_name, symbol)
        stats["chart_path"] = str(chart_path)
        logger.info("资金曲线已保存: %s", chart_path)
    except Exception as e:
        logger.warning("绘图失败: %s", e)
        stats["chart_path"] = ""

    return stats


def _plot_equity_curve(
    initial: float, final: float, chart_path: Path,
    strategy_name: str, symbol: str,
):
    """绘制资金曲线（简单对比图）"""
    # Windows 中文字体
    try:
        plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass

    fig, ax = plt.subplots(figsize=(10, 5))
    ret_pct = (final / initial - 1) * 100 if initial > 0 else 0
    colors = ["#78909C", "#43A047" if final >= initial else "#E53935"]
    ax.bar(["Initial", "Final"], [initial, final], color=colors, width=0.5)
    ax.set_ylabel("Capital (CNY)")
    ax.set_title(f"{strategy_name} / {symbol}  Return: {ret_pct:+.2f}%")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    for i, v in enumerate([initial, final]):
        ax.text(i, v, f"{v:,.0f}", ha="center", va="bottom", fontsize=10)
    fig.tight_layout()
    fig.savefig(str(chart_path), dpi=100, bbox_inches="tight")
    plt.close(fig)


def _extract_stats(strat, start_value: float, end_value: float, initial_cash: float) -> dict:
    """提取回测统计指标"""
    total_return = (end_value - initial_cash) / initial_cash

    # 最大回撤
    dd = strat.analyzers.drawdown.get_analysis()
    max_dd = dd.get("max", {}).get("drawdown", 0) / 100

    # 交易分析
    trades = strat.analyzers.trades.get_analysis()
    total_trades = trades.get("total", {}).get("closed", 0)
    won = trades.get("won", {}).get("total", 0)
    lost = trades.get("lost", {}).get("total", 0)
    win_rate = won / total_trades if total_trades > 0 else 0

    # Profit Factor
    gross_profit = trades.get("won", {}).get("pnl", {}).get("total", 0)
    gross_loss = abs(trades.get("lost", {}).get("pnl", {}).get("total", 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # 平均 R
    trade_results = getattr(strat, "trade_results", [])
    r_multiples = [t["r_multiple"] for t in trade_results if t.get("r_multiple") is not None]
    avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0

    # Sharpe
    sharpe = strat.analyzers.sharpe.get_analysis().get("sharperatio", 0)
    if sharpe is None:
        sharpe = 0

    return {
        "initial_cash": initial_cash,
        "final_value": end_value,
        "total_return": total_return,
        "total_return_pct": total_return * 100,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd * 100,
        "total_trades": total_trades,
        "won": won,
        "lost": lost,
        "win_rate": win_rate,
        "win_rate_pct": win_rate * 100,
        "profit_factor": profit_factor,
        "avg_r": avg_r,
        "sharpe_ratio": sharpe,
    }


def print_stats(stats: dict):
    """打印回测结果"""
    print("\n" + "=" * 55)
    print(f"  回测结果 — {stats['strategy']} / {stats['symbol']}")
    print(f"  区间: {stats['start']} ~ {stats['end']}")
    print("=" * 55)
    print(f"  初始资金:     {stats['initial_cash']:>15,.0f}")
    print(f"  最终资金:     {stats['final_value']:>15,.0f}")
    print(f"  总收益率:     {stats['total_return_pct']:>14.2f}%")
    print(f"  最大回撤:     {stats['max_drawdown_pct']:>14.2f}%")
    print("-" * 55)
    print(f"  总交易次数:   {stats['total_trades']:>15}")
    print(f"  胜率:         {stats['win_rate_pct']:>14.1f}%")
    print(f"  平均 R:       {stats['avg_r']:>15.2f}")
    print(f"  Profit Factor:{stats['profit_factor']:>15.2f}")
    print(f"  Sharpe Ratio: {stats['sharpe_ratio']:>15.2f}")
    print("-" * 55)
    if stats.get("chart_path"):
        print(f"  资金曲线:     {stats['chart_path']}")
    print("=" * 55)


def main():
    parser = argparse.ArgumentParser(description="Backtrader 回测 — S1-A / S2-A")
    parser.add_argument(
        "--strategy", "-st",
        required=True,
        choices=["S1-A", "S2-A", "s1a", "s2a"],
        help="策略选择",
    )
    parser.add_argument("--symbol", "-s", required=True, help="股票代码（6位）")
    parser.add_argument("--start", default="2020-01-01", help="开始日期")
    parser.add_argument("--end", default=None, help="结束日期（默认今天）")
    parser.add_argument("--cash", type=float, default=1_000_000, help="初始资金")
    parser.add_argument("--commission", type=float, default=0.0003, help="佣金率")
    parser.add_argument("--verbose", action="store_true", help="打印交易日志")
    args = parser.parse_args()

    try:
        stats = run_backtest(
            strategy_name=args.strategy,
            symbol=args.symbol,
            start=args.start,
            end=args.end,
            initial_cash=args.cash,
            commission=args.commission,
            printlog=args.verbose,
        )
        print_stats(stats)
    except Exception as e:
        logger.error("回测失败: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
