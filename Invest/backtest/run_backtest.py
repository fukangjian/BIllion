"""
回测执行脚本 — 支持 S1-A / S2-A 策略

用法:
    python backtest/run_backtest.py --strategy S1-A --symbol 600519 --start 2020-01-01 --end 2025-01-01
"""
import argparse
import logging
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore", message="urllib3.*doesn't match a supported version")

import backtrader as bt
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest.strategies import STRATEGY_MAP
from config import BACKTEST_OUTPUT_DIR
from pipeline.database import load_daily_quotes
from shared.data_fetcher import fetch_stock_daily

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_backtest")

OUTPUT_DIR = BACKTEST_OUTPUT_DIR


class EquityCurveAnalyzer(bt.Analyzer):
    """记录每日账户权益，用于绘制完整资金曲线"""

    def __init__(self):
        self.equity_curve = []

    def next(self):
        dt = self.strategy.datas[0].datetime.date(0)
        val = self.strategy.broker.getvalue()
        self.equity_curve.append((dt, val))

    def get_analysis(self):
        return {"equity_curve": self.equity_curve}


class TradeMarkerAnalyzer(bt.Analyzer):
    """记录买卖时点，用于在资金曲线上标注"""

    def __init__(self):
        self.markers = []
        self._prev_pos = 0

    def next(self):
        pos = self.strategy.position.size
        if pos != self._prev_pos:
            dt = self.strategy.datas[0].datetime.date(0)
            price = float(self.strategy.data.close[0])
            if pos > self._prev_pos:
                self.markers.append({"date": dt, "type": "buy", "price": price})
            elif pos < self._prev_pos:
                self.markers.append({"date": dt, "type": "sell", "price": price})
        self._prev_pos = pos

    def get_analysis(self):
        return {"markers": self.markers}


def fetch_data(symbol: str, start: str, end: str) -> pd.DataFrame:
    logger.info("获取 %s 数据 %s ~ %s", symbol, start, end)
    # 与数据管道共用双源获取（新浪优先，东方财富备用），避免单一数据源不可用
    raw = fetch_stock_daily(symbol, start_date=start, end_date=end)
    if raw is None or raw.empty:
        raise ValueError(f"无法获取 {symbol} 的数据")

    df = pd.DataFrame({
        "datetime": pd.to_datetime(raw["trade_date"]),
        "open": raw["open"].astype(float),
        "high": raw["high"].astype(float),
        "low": raw["low"].astype(float),
        "close": raw["close"].astype(float),
        "volume": raw["volume"].astype(float),
    })
    df.set_index("datetime", inplace=True)
    df.sort_index(inplace=True)
    return df


def load_data_from_db(symbol: str, start: str, end: str) -> pd.DataFrame:
    try:
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
    end = end or datetime.now().strftime("%Y-%m-%d")

    df = load_data_from_db(symbol, start, end)
    if df.empty:
        df = fetch_data(symbol, start, end)

    if df.empty or len(df) < 60:
        raise ValueError(f"数据不足: {symbol} 仅 {len(df)} 条")

    cerebro = bt.Cerebro()
    cerebro.broker.setcash(initial_cash)
    cerebro.broker.setcommission(commission=commission)

    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)

    strategy_cls = STRATEGY_MAP.get(strategy_name)
    if strategy_cls is None:
        raise ValueError(f"未知策略: {strategy_name}，可选: {list(STRATEGY_MAP.keys())}")

    cerebro.addstrategy(strategy_cls, printlog=printlog)

    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", riskfreerate=0.02)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(EquityCurveAnalyzer, _name="equity_curve")
    cerebro.addanalyzer(TradeMarkerAnalyzer, _name="trade_markers")

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

    equity_data = strat.analyzers.equity_curve.get_analysis()
    marker_data = strat.analyzers.trade_markers.get_analysis()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    chart_path = OUTPUT_DIR / f"{strategy_name}_{symbol}_{start}_{end}.png"
    try:
        _plot_equity_curve(
            equity_curve=equity_data.get("equity_curve", []),
            markers=marker_data.get("markers", []),
            chart_path=chart_path,
            strategy_name=strategy_name,
            symbol=symbol,
            start=start,
            end=end,
            initial_cash=initial_cash,
        )
        stats["chart_path"] = str(chart_path)
        logger.info("资金曲线已保存: %s", chart_path)
    except Exception as e:
        logger.warning("绘图失败: %s", e)
        stats["chart_path"] = ""

    return stats


def _plot_equity_curve(
    equity_curve: list,
    markers: list,
    chart_path: Path,
    strategy_name: str,
    symbol: str,
    start: str,
    end: str,
    initial_cash: float,
):
    """绘制完整资金曲线、回撤曲线，并标注买卖点"""
    try:
        plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass

    if not equity_curve:
        # 无权益数据时回退为简图
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.bar(["初始", "最终"], [initial_cash, initial_cash], color="#78909C", width=0.5)
        ax.set_title(f"{strategy_name} / {symbol}  {start} ~ {end}")
        fig.tight_layout()
        fig.savefig(str(chart_path), dpi=100, bbox_inches="tight")
        plt.close(fig)
        return

    dates = [pd.Timestamp(d) for d, _ in equity_curve]
    values = [v for _, v in equity_curve]

    # 计算回撤序列
    peak = values[0]
    drawdowns = []
    for v in values:
        if v > peak:
            peak = v
        dd_pct = (v - peak) / peak * 100 if peak > 0 else 0
        drawdowns.append(dd_pct)

    final_value = values[-1]
    ret_pct = (final_value / initial_cash - 1) * 100 if initial_cash > 0 else 0
    max_dd = min(drawdowns) if drawdowns else 0

    fig, (ax_equity, ax_dd) = plt.subplots(
        2, 1, figsize=(12, 7), sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08},
    )

    # 资金曲线
    ax_equity.plot(dates, values, color="#1565C0", linewidth=1.5, label="账户权益")
    ax_equity.axhline(initial_cash, color="#78909C", linestyle="--", linewidth=0.8, alpha=0.7, label="初始资金")
    ax_equity.fill_between(dates, initial_cash, values, alpha=0.08, color="#1565C0")
    ax_equity.set_ylabel("资金 (元)")
    ax_equity.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax_equity.legend(loc="upper left", fontsize=9)
    ax_equity.grid(True, alpha=0.3)
    ax_equity.set_title(
        f"{strategy_name} / {symbol}  {start} ~ {end}  "
        f"收益: {ret_pct:+.2f}%  最大回撤: {max_dd:.2f}%",
        fontsize=12,
    )

    # 标注买卖点
    date_to_value = {pd.Timestamp(d): v for d, v in equity_curve}
    for m in markers:
        m_date = pd.Timestamp(m["date"])
        m_val = date_to_value.get(m_date)
        if m_val is None:
            continue
        if m["type"] == "buy":
            ax_equity.scatter(m_date, m_val, marker="^", color="#43A047", s=80, zorder=5)
        else:
            ax_equity.scatter(m_date, m_val, marker="v", color="#E53935", s=80, zorder=5)

    # 手动添加买卖点图例
    legend_extra = [
        Line2D([0], [0], marker="^", color="w", markerfacecolor="#43A047", markersize=8, label="买入"),
        Line2D([0], [0], marker="v", color="w", markerfacecolor="#E53935", markersize=8, label="卖出"),
    ]
    handles, labels = ax_equity.get_legend_handles_labels()
    ax_equity.legend(
        handles + legend_extra,
        labels + ["买入", "卖出"],
        loc="upper left",
        fontsize=9,
    )

    # 回撤曲线
    ax_dd.fill_between(dates, drawdowns, 0, color="#E53935", alpha=0.4)
    ax_dd.plot(dates, drawdowns, color="#C62828", linewidth=1)
    ax_dd.set_ylabel("回撤 (%)")
    ax_dd.set_xlabel("日期")
    ax_dd.grid(True, alpha=0.3)
    ax_dd.set_ylim(min(drawdowns) * 1.1 if drawdowns else -5, 1)

    fig.autofmt_xdate()
    fig.subplots_adjust(hspace=0.08)
    fig.savefig(str(chart_path), dpi=120, bbox_inches="tight")
    plt.close(fig)


def _extract_stats(strat, start_value: float, end_value: float, initial_cash: float) -> dict:
    total_return = (end_value - initial_cash) / initial_cash

    dd = strat.analyzers.drawdown.get_analysis()
    max_dd = dd.get("max", {}).get("drawdown", 0) / 100

    trades = strat.analyzers.trades.get_analysis()
    total_trades = trades.get("total", {}).get("closed", 0)
    won = trades.get("won", {}).get("total", 0)
    lost = trades.get("lost", {}).get("total", 0)
    win_rate = won / total_trades if total_trades > 0 else 0

    gross_profit = trades.get("won", {}).get("pnl", {}).get("total", 0)
    gross_loss = abs(trades.get("lost", {}).get("pnl", {}).get("total", 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    trade_results = getattr(strat, "trade_results", [])
    r_multiples = [t["r_multiple"] for t in trade_results if t.get("r_multiple") is not None]
    avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0

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
