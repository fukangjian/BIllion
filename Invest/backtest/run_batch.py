"""
批量回测 — 标的列表 × 策略，输出汇总报告（MD + JSON）

用途：验证策略参数在不同标的上的稳健性（V5.0：参数调整须有 ≥50 笔样本支持）。
单标的回测复用 run_backtest.run_backtest（plot=False 跳过逐标的资金曲线），
逐标的捕获异常不中断整批。

用法:
    python backtest/run_batch.py --strategy S1-A --start 2020-01-01 --watchlist
    python backtest/run_batch.py --strategy S1-A --symbols 600519,000858 --start 2020-01-01
"""
import argparse
import json
import logging
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore", message="urllib3.*doesn't match a supported version")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import BACKTEST_OUTPUT_DIR, WATCHLIST
from backtest.run_backtest import run_backtest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_batch")


def aggregate_batch(rows: list[dict], strategy: str, start: str, end: str) -> dict:
    """汇总各标的回测结果（纯函数）：整体胜率/PF/加权平均R/样本充足性"""
    ok = [r for r in rows if "error" not in r]
    failed = [r for r in rows if "error" in r]

    total_trades = sum(r.get("total_trades", 0) for r in ok)
    total_won = sum(r.get("won", 0) for r in ok)
    gross_profit = sum(r.get("gross_profit", 0) for r in ok)
    gross_loss = sum(r.get("gross_loss", 0) for r in ok)
    r_weighted_sum = sum(r.get("avg_r", 0) * r.get("r_count", 0) for r in ok)
    r_count = sum(r.get("r_count", 0) for r in ok)

    summary = {
        "strategy": strategy,
        "start": start,
        "end": end,
        "symbols_total": len(rows),
        "symbols_ok": len(ok),
        "symbols_failed": len(failed),
        "total_trades": total_trades,
        "win_rate_pct": round(total_won / total_trades * 100, 1) if total_trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "avg_r": round(r_weighted_sum / r_count, 2) if r_count else 0.0,
        "avg_return_pct": round(sum(r.get("total_return_pct", 0) for r in ok) / len(ok), 2) if ok else 0.0,
        "avg_max_drawdown_pct": round(sum(r.get("max_drawdown_pct", 0) for r in ok) / len(ok), 2) if ok else 0.0,
        "sample_sufficient": total_trades >= 50,  # V5.0：参数调整须 ≥50 笔样本支持
    }
    return {"summary": summary, "rows": rows}


def run_batch(
    strategy: str,
    symbols: list[str],
    start: str,
    end: str | None = None,
    cash: float = 1_000_000,
    commission: float = 0.0003,
    output_dir: Path | None = None,
) -> dict:
    """逐标的回测并汇总；单标的失败记 error 行继续。输出 batch_{strategy}_{date}.md/.json"""
    end = end or datetime.now().strftime("%Y-%m-%d")
    rows: list[dict] = []
    for sym in symbols:
        sym = str(sym).zfill(6)[-6:]
        try:
            stats = run_backtest(
                strategy_name=strategy, symbol=sym, start=start, end=end,
                initial_cash=cash, commission=commission, plot=False,
            )
            rows.append(stats)
            logger.info("%s 完成: 收益 %+.1f%% 交易 %d 笔", sym, stats["total_return_pct"], stats["total_trades"])
        except Exception as e:
            logger.warning("%s 回测失败（跳过）: %s", sym, e)
            rows.append({"symbol": sym, "error": str(e)})

    result = aggregate_batch(rows, strategy, start, end)

    out = output_dir or BACKTEST_OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    date_tag = datetime.now().strftime("%Y-%m-%d")
    json_path = out / f"batch_{strategy}_{date_tag}.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out / f"batch_{strategy}_{date_tag}.md"
    md_path.write_text(batch_to_markdown(result), encoding="utf-8")
    logger.info("批量回测报告: %s / %s", md_path, json_path)
    return result


def batch_to_markdown(result: dict) -> str:
    """批量回测结果 → Markdown（汇总表 + 逐标的明细）"""
    s = result["summary"]
    pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "∞"
    lines = [
        f"# 批量回测汇总 — {s['strategy']}",
        "",
        f"> 区间: {s['start']} ~ {s['end']} | 生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 汇总",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 标的数 | {s['symbols_ok']}/{s['symbols_total']}（失败 {s['symbols_failed']}） |",
        f"| 总交易数 | {s['total_trades']}（{'样本充足 ≥50' if s['sample_sufficient'] else '样本不足 <50，仅供参考'}） |",
        f"| 整体胜率 | {s['win_rate_pct']}% |",
        f"| 整体 PF | {pf} |",
        f"| 加权平均R | {s['avg_r']:+.2f} |",
        f"| 平均收益率 | {s['avg_return_pct']:+.2f}% |",
        f"| 平均最大回撤 | {s['avg_max_drawdown_pct']:.2f}% |",
        "",
        "## 逐标的明细",
        "",
        "| 代码 | 收益率% | 最大回撤% | 交易数 | 胜率% | PF | 平均R | 备注 |",
        "|------|---------|-----------|--------|-------|-----|-------|------|",
    ]
    for r in result["rows"]:
        if "error" in r:
            lines.append(f"| {r['symbol']} | - | - | - | - | - | - | 失败: {r['error']} |")
            continue
        r_pf = f"{r['profit_factor']:.2f}" if r["profit_factor"] != float("inf") else "∞"
        lines.append(
            f"| {r['symbol']} | {r['total_return_pct']:+.2f} | {r['max_drawdown_pct']:.2f} "
            f"| {r['total_trades']} | {r['win_rate_pct']:.1f} | {r_pf} | {r['avg_r']:+.2f} | |"
        )
    lines.extend([
        "",
        "---",
        "_口径：单标的回测（每标的独立 100 万初始资金），止损为入场时锁定的固定止损；"
        "非组合级回测（不做跨标的资金分配），组合级回测为后续扩展_",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="批量回测 — 标的列表 × 策略，汇总稳健性")
    parser.add_argument("--strategy", "-st", required=True, choices=["S1-A", "S2-A"], help="策略")
    parser.add_argument("--symbols", default="", help="逗号分隔代码列表（与 --watchlist 二选一）")
    parser.add_argument("--watchlist", action="store_true", help="使用 config.WATCHLIST")
    parser.add_argument("--start", default="2020-01-01", help="开始日期")
    parser.add_argument("--end", default=None, help="结束日期（默认今天）")
    parser.add_argument("--cash", type=float, default=1_000_000, help="单标的初始资金")
    args = parser.parse_args()

    if args.watchlist or not args.symbols:
        symbols = list(WATCHLIST)
    else:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    result = run_batch(args.strategy, symbols, args.start, args.end, cash=args.cash)
    s = result["summary"]
    print(f"\n=== 批量回测 {args.strategy}（{s['start']} ~ {s['end']}）===")
    print(f"标的 {s['symbols_ok']}/{s['symbols_total']} | 总交易 {s['total_trades']} 笔"
          f"（{'样本充足' if s['sample_sufficient'] else '样本不足'}）")
    print(f"整体胜率 {s['win_rate_pct']}% | 加权平均R {s['avg_r']:+.2f} | "
          f"平均收益 {s['avg_return_pct']:+.2f}% | 平均回撤 {s['avg_max_drawdown_pct']:.2f}%")


if __name__ == "__main__":
    main()
