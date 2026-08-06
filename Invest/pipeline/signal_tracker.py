"""
信号追踪 — 突破信号入库、每日结算、分组统计（信号验证闭环）

回答「扫描出的突破信号到底有没有用」：market_scanner.run_scan() 扫出的每个
S1-A/S2-A 突破候选自动写入 signals 表，此后每日按最新日线结算
（止损 / 通道退出 / 到期），并可按系统统计近 N 天胜率、平均R、期望值、PF。

行情数据全部来自本地 market.db（离线），不联网。
R 口径与 review/metrics.py 一致：R = (结算价 − 入场价) / (入场价 − 止损价)。

用法:
    python pipeline/signal_tracker.py settle            # 手动结算全部 open 信号
    python pipeline/signal_tracker.py stats [--days 90] # 打印近 N 天信号统计
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import argparse
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from config import (
    ATR_STOP_MULT,
    EXIT_CHANNEL_PERIODS,
    SIGNAL_MAX_HOLDING_BY_SYSTEM,
    SIGNAL_MAX_HOLDING_DAYS,
    SIGNAL_STATS_MIN_SAMPLE,
)
from pipeline.database import get_connection, init_database, load_daily_quotes
from pipeline.indicators import calc_donchian_channel
from review.metrics import calc_r_multiple
from shared.utils import normalize_symbol

logger = logging.getLogger(__name__)


def _exit_channel_period(entry_system: str) -> Optional[int]:
    """按入场系统匹配退出通道周期（与 review/monitor.py 同一口径：含 S1→10 日，含 S2→20 日）"""
    s = (entry_system or "").upper()
    for key, period in EXIT_CHANNEL_PERIODS.items():
        if key in s:
            return period
    return None


def _max_holding_days(system: str) -> int:
    """信号最大持有交易日数（按系统覆盖，默认全局值；HOT-S=5 对应超短强制离场）"""
    return SIGNAL_MAX_HOLDING_BY_SYSTEM.get(system, SIGNAL_MAX_HOLDING_DAYS)


def consecutive_stop_outs(
    symbol: str,
    system: str,
    db_path: Optional[Path] = None,
) -> tuple[int, Optional[str]]:
    """
    最近连续止损次数与最近一次止损退出日（S1-A 系统1过滤：假突破计数，V5.0 §4.3）。
    按 exit_date 倒序取同标的同系统已关闭信号，连续 exit_reason="止损" 的个数；
    遇到非止损退出即中断。无历史返回 (0, None)。
    """
    init_database(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT exit_reason, exit_date FROM signals "
            "WHERE symbol = ? AND system = ? AND status = 'closed' "
            "ORDER BY exit_date DESC LIMIT 10",
            (symbol, system),
        )
        rows = cur.fetchall()

    count = 0
    last_date: Optional[str] = None
    for reason, exit_date in rows:
        if reason != "止损":
            break
        count += 1
        if last_date is None:
            last_date = exit_date
    return count, last_date


def last_signal_won(
    symbol: str,
    system: str,
    db_path: Optional[Path] = None,
) -> Optional[bool]:
    """最近一次同标的同系统已关闭信号 R>0（系统1过滤：上次突破是否盈利）；无历史返回 None"""
    init_database(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT r_multiple FROM signals "
            "WHERE symbol = ? AND system = ? AND status = 'closed' AND r_multiple IS NOT NULL "
            "ORDER BY exit_date DESC LIMIT 1",
            (symbol, system),
        )
        row = cur.fetchone()
    if not row:
        return None
    return float(row[0]) > 0


def record_signals(
    candidates: list[dict],
    system: str,
    signal_date: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> int:
    """
    将突破候选写入 signals 表（入场价=close，止损=close−ATR_STOP_MULT×ATR(20)）。

    去重规则：同一 (symbol, system) 存在 open 信号时跳过（避免连续突破日重复刷屏）；
    旧信号关闭后，新日期的同系统信号才可再入库。返回新入库条数。
    """
    if not candidates:
        return 0

    signal_date = signal_date or datetime.now().strftime("%Y-%m-%d")
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0

    init_database(db_path)
    with get_connection(db_path) as conn:
        for item in candidates:
            symbol = normalize_symbol(item.get("symbol", ""))
            if not symbol:
                continue
            cur = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE symbol = ? AND system = ? AND status = 'open'",
                (symbol, system),
            )
            if cur.fetchone()[0] > 0:
                continue

            entry = round(float(item["close"]), 2)
            atr = float(item.get("atr_20") or 0)
            # ATR 缺失时按 from-scan 口径降级为收盘价的 95%
            stop = round(entry - atr * ATR_STOP_MULT, 2) if atr > 0 else round(entry * 0.95, 2)
            conn.execute(
                """
                INSERT OR IGNORE INTO signals
                (signal_date, symbol, system, entry_price, stop_price, channel_period,
                 status, market_state, filter_passed, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
                """,
                (
                    signal_date,
                    symbol,
                    system,
                    entry,
                    stop,
                    int(item.get("period", 0) or 0),
                    item.get("market_state"),
                    item.get("filter_passed"),
                    item.get("note"),
                    created_at,
                ),
            )
            inserted += 1

    if inserted:
        logger.info("信号入库: %s +%d（%s）", system, inserted, signal_date)
    return inserted


def _load_open_signals(conn) -> list[dict]:
    """读取全部 open 信号"""
    cur = conn.execute(
        "SELECT signal_date, symbol, system, entry_price, stop_price "
        "FROM signals WHERE status = 'open' ORDER BY signal_date"
    )
    cols = ["signal_date", "symbol", "system", "entry_price", "stop_price"]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _settle_one_signal(signal: dict, db_path: Optional[Path]) -> Optional[dict]:
    """
    回放信号日之后的日线，逐根判定退出（同日先判止损再判通道，保守）。
    返回关闭信息；无新行情或未触发退出返回 None。
    """
    symbol = signal["symbol"]
    entry = float(signal["entry_price"])
    stop = float(signal["stop_price"]) if signal["stop_price"] else None

    df = load_daily_quotes(symbol=symbol, db_path=db_path)
    if df.empty:
        return None
    df = df.sort_values("trade_date").reset_index(drop=True)

    bars = df[df["trade_date"] > signal["signal_date"]]
    if bars.empty:
        return None

    # 退出通道（shift(1) 不含当根 K 线，与持仓监控/回测口径一致，无未来函数）
    period = _exit_channel_period(signal["system"])
    channel_lows = None
    if period is not None:
        channel_lows = calc_donchian_channel(df, period)[f"low_{period}"]

    for count, pos in enumerate(bars.index.tolist(), start=1):
        bar = df.iloc[pos]
        low = float(bar["low"])
        close = float(bar["close"])
        trade_date = str(bar["trade_date"])

        exit_price = exit_reason = None
        r_multiple = None
        if stop is not None and low <= stop:
            # a) 止损：保守按止损价成交，R=−1
            exit_price, exit_reason, r_multiple = stop, "止损", -1.0
        else:
            channel_low = None
            if channel_lows is not None:
                val = channel_lows.iloc[pos]
                if pd.notna(val):
                    channel_low = float(val)
            if channel_low is not None and close < channel_low:
                # b) 通道退出：按当日收盘价结算
                exit_price, exit_reason = close, "通道退出"
                r_multiple = calc_r_multiple(entry, close, stop) if stop else None
            elif count >= _max_holding_days(signal["system"]):
                # c) 到期：信号满持有上限（按系统覆盖，HOT-S=5）仍未触发退出，按收盘价结算
                exit_price, exit_reason = close, "到期"
                r_multiple = calc_r_multiple(entry, close, stop) if stop else None

        if exit_reason:
            if r_multiple is not None:
                r_multiple = round(r_multiple, 2)
            return {
                "signal_date": signal["signal_date"],
                "symbol": symbol,
                "system": signal["system"],
                "exit_date": trade_date,
                "exit_price": round(exit_price, 2),
                "exit_reason": exit_reason,
                "r_multiple": r_multiple,
            }

    return None


def settle_signals(
    db_path: Optional[Path] = None,
    settle_date: Optional[str] = None,
) -> dict:
    """
    每日结算：对全部 open 信号（signal_date < settle_date，当天新记录的信号不结算）：
      a) 最低价 ≤ 止损价 → 按止损价关闭，exit_reason="止损"，R=−1
      b) 收盘价 < 退出通道下轨（S1=10日/S2=20日） → 按收盘价关闭，exit_reason="通道退出"
      c) 信号满 SIGNAL_MAX_HOLDING_DAYS 个交易日 → 按收盘价关闭，exit_reason="到期"
    """
    settle_date = settle_date or datetime.now().strftime("%Y-%m-%d")
    result = {
        "settle_date": settle_date,
        "checked": 0,
        "settled": 0,
        "still_open": 0,
        "by_reason": {"止损": 0, "通道退出": 0, "到期": 0},
        "details": [],
    }

    init_database(db_path)
    with get_connection(db_path) as conn:
        open_signals = [
            s for s in _load_open_signals(conn) if s["signal_date"] < settle_date
        ]
        result["checked"] = len(open_signals)

        for signal in open_signals:
            try:
                closed = _settle_one_signal(signal, db_path)
            except Exception as e:
                # 单信号结算失败不阻塞其他信号
                logger.warning("信号结算失败 %s %s（跳过）: %s", signal["symbol"], signal["system"], e)
                closed = None
            if not closed:
                result["still_open"] += 1
                continue
            conn.execute(
                """
                UPDATE signals
                SET status = 'closed', exit_date = ?, exit_price = ?,
                    exit_reason = ?, r_multiple = ?
                WHERE signal_date = ? AND symbol = ? AND system = ?
                """,
                (
                    closed["exit_date"],
                    closed["exit_price"],
                    closed["exit_reason"],
                    closed["r_multiple"],
                    closed["signal_date"],
                    closed["symbol"],
                    closed["system"],
                ),
            )
            result["settled"] += 1
            result["by_reason"][closed["exit_reason"]] += 1
            result["details"].append(closed)

    if result["settled"]:
        logger.info(
            "信号结算: 关闭 %d 个（止损 %d / 通道退出 %d / 到期 %d）",
            result["settled"],
            result["by_reason"]["止损"],
            result["by_reason"]["通道退出"],
            result["by_reason"]["到期"],
        )
    return result


def _group_stats(r_values: list[float]) -> dict:
    """按 review/metrics.py 口径计算一组 R 的统计（胜率/平均R/期望值/PF）"""
    n = len(r_values)
    wins = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r <= 0]

    win_rate = round(len(wins) / n * 100, 1)
    avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
    expectancy = round((win_rate / 100) * avg_win + (1 - win_rate / 100) * avg_loss, 2)
    total_profit = sum(wins)
    total_loss = abs(sum(losses))
    profit_factor = round(total_profit / total_loss, 2) if total_loss > 0 else float("inf")

    return {
        "closed": n,
        "win_rate": win_rate,
        "avg_r": round(sum(r_values) / n, 2),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "sample_sufficient": n >= SIGNAL_STATS_MIN_SAMPLE,
        "note": "" if n >= SIGNAL_STATS_MIN_SAMPLE else "样本不足",
    }


def signal_stats(
    db_path: Optional[Path] = None,
    days: int = 90,
    as_of: Optional[str] = None,
) -> dict:
    """近 N 天已关闭信号按系统分组统计（按 exit_date 落在窗口内筛选）+ open 信号数
    + 按信号日市场状态分层（by_state，验证不同市场状态下信号质量；老数据无状态归入「未知」）"""
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")

    init_database(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT system, r_multiple, market_state FROM signals "
            "WHERE status = 'closed' AND exit_date >= ? AND r_multiple IS NOT NULL",
            (cutoff,),
        )
        closed_rows = cur.fetchall()
        cur = conn.execute(
            "SELECT system, COUNT(*) FROM signals WHERE status = 'open' GROUP BY system"
        )
        open_by_system = dict(cur.fetchall())

    groups: dict[str, list[float]] = {}
    state_groups: dict[str, dict[str, list[float]]] = {}
    for system, r, mstate in closed_rows:
        groups.setdefault(system, []).append(float(r))
        state_groups.setdefault(mstate or "未知", {}).setdefault(system, []).append(float(r))

    systems = {system: _group_stats(rs) for system, rs in sorted(groups.items())}
    by_state = {
        state: {system: _group_stats(rs) for system, rs in sorted(sys_map.items())}
        for state, sys_map in sorted(state_groups.items())
    }
    return {
        "as_of": as_of,
        "days": days,
        "open_count": sum(open_by_system.values()),
        "open_by_system": open_by_system,
        "systems": systems,
        "by_state": by_state,
    }


def signal_stats_to_markdown(stats: dict) -> str:
    """信号统计 → Markdown 表格 + 自动解读（周报/月报/统计快照/CLI 共用）"""
    systems = stats.get("systems", {})
    lines = []
    if not systems:
        lines.append("_暂无已关闭信号_")
    else:
        lines.append("| 系统 | 样本数 | 胜率 | 平均R | 期望值 | PF | 备注 |")
        lines.append("|------|--------|------|-------|--------|-----|------|")
        for system, s in systems.items():
            pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "∞"
            lines.append(
                f"| {system} | {s['closed']} | {s['win_rate']}% "
                f"| {s['avg_r']:+.2f} | {s['expectancy']:+.2f} | {pf} | {s['note']} |"
            )

    by_state = stats.get("by_state", {})
    if by_state:
        lines.extend(["", "**按市场状态分层（信号日状态；验证 D 状态信号是否真差）**：", ""])
        lines.append("| 市场状态 | 系统 | 样本数 | 胜率 | 平均R | PF |")
        lines.append("|----------|------|--------|------|-------|-----|")
        for state, sys_map in by_state.items():
            for system, s in sys_map.items():
                pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "∞"
                lines.append(
                    f"| {state} | {system} | {s['closed']} | {s['win_rate']}% "
                    f"| {s['avg_r']:+.2f} | {pf} |"
                )

    notes = []
    for system, s in systems.items():
        if s["sample_sufficient"]:
            notes.append(f"{system} 近 {stats['days']} 天期望值 {s['expectancy']:+.2f}R，样本 {s['closed']}")
        else:
            notes.append(f"{system} 样本不足（{s['closed']} < {SIGNAL_STATS_MIN_SAMPLE}），继续观察")
    open_count = stats.get("open_count", 0)
    if notes:
        lines.append("")
        lines.append("> " + "；".join(notes) + f"。当前 open 信号 {open_count} 个。")
    elif open_count:
        lines.append("")
        lines.append(f"> 暂无已关闭信号，当前 open 信号 {open_count} 个，继续观察。")
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="信号追踪 — 突破信号每日结算与验证统计")
    sub = parser.add_subparsers(dest="command", help="子命令")

    p_settle = sub.add_parser("settle", help="手动结算全部 open 信号")
    p_settle.add_argument("--date", default=None, help="结算日期 YYYY-MM-DD（默认今天）")

    p_stats = sub.add_parser("stats", help="近 N 天已关闭信号分组统计")
    p_stats.add_argument("--days", type=int, default=90, help="统计窗口天数（默认 90）")

    args = parser.parse_args()
    if args.command == "settle":
        result = settle_signals(settle_date=args.date)
        print(f"\n=== 信号结算 {result['settle_date']} ===\n")
        print(f"检查 open 信号 {result['checked']} 个，关闭 {result['settled']} 个"
              f"（止损 {result['by_reason']['止损']} / 通道退出 {result['by_reason']['通道退出']}"
              f" / 到期 {result['by_reason']['到期']}）")
        for d in result["details"]:
            r = f"{d['r_multiple']:+.2f}" if d["r_multiple"] is not None else "-"
            print(f"  {d['symbol']} {d['system']} 信号日 {d['signal_date']} → "
                  f"{d['exit_reason']} @ {d['exit_price']}（{d['exit_date']}，R {r}）")
    elif args.command == "stats":
        stats = signal_stats(days=args.days)
        print(f"\n=== 信号验证统计（近 {args.days} 天，截至 {stats['as_of']}） ===\n")
        print(signal_stats_to_markdown(stats))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
