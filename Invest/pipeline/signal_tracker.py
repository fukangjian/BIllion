"""
信号追踪 — 突破信号入库、每日结算、分组统计（信号验证闭环）

回答「扫描出的突破信号到底有没有用」：market_scanner.run_scan() 扫出的每个
S1-A/S2-A 突破候选自动写入 signals 表，此后每日按最新日线结算
（止损 / 通道退出 / 到期），并可按系统统计近 N 天胜率、平均R、期望值、PF。

行情数据默认来自本地 market.db（离线），不联网；settle_signals(refetch_missing=True)
时对缺K线的 open 信号补抓日线（run_all/CLI 入口按 config.SIGNAL_SETTLE_REFETCH 启用），
超龄仍无数据的信号以「数据缺失」关闭（不产生 R，统计剔除）——修复掉池股票
信号永远 open 造成的幸存者偏差。统计另提供信号日入场形态分层（一字板/涨停收盘/
非涨停）：一字板与涨停封板收盘的纸面收益实盘难以复制，决策看「非涨停」组。

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
    SIGNAL_SETTLE_REFETCH,
    SIGNAL_SETTLE_REFETCH_MAX,
    SIGNAL_STALE_GRACE_DAYS,
    SIGNAL_STATS_MIN_SAMPLE,
)
from pipeline.database import (
    get_connection,
    init_database,
    load_daily_quotes,
    save_daily_quotes,
)
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


def _stale_threshold_days(system: str) -> int:
    """信号「数据缺失」关闭的自然日门槛：最大持有交易日 ×1.7（折算自然日）+ 宽限日"""
    return int(_max_holding_days(system) * 1.7) + SIGNAL_STALE_GRACE_DAYS


def _symbol_max_dates(conn, symbols: set[str]) -> dict[str, Optional[str]]:
    """批量取每只股票 daily_quotes 的最新交易日（无行情返回 None）——判定信号是否缺新K线"""
    if not symbols:
        return {}
    placeholders = ",".join("?" * len(symbols))
    cur = conn.execute(
        f"SELECT symbol, MAX(trade_date) FROM daily_quotes "
        f"WHERE symbol IN ({placeholders}) GROUP BY symbol",
        tuple(symbols),
    )
    return dict(cur.fetchall())


def _default_fetch_daily(symbol: str, start_date: str) -> pd.DataFrame:
    """补抓缺K线信号的默认日线源（延迟导入 shared.data_fetcher，保持模块默认离线可测）"""
    from shared.data_fetcher import fetch_stock_daily

    return fetch_stock_daily(symbol, start_date=start_date)


def _refetch_missing_bars(
    stale_signals: list[dict],
    db_path: Optional[Path],
    fetch_fn=None,
    refetch_max: Optional[int] = None,
) -> int:
    """
    对缺K线的 open 信号按股票去重补抓日线（最老信号优先，截断上限防雪崩）。
    单股失败降级跳过；返回成功补到数据的股票数。
    """
    refetch_max = SIGNAL_SETTLE_REFETCH_MAX if refetch_max is None else refetch_max
    fetch_fn = fetch_fn or _default_fetch_daily

    by_symbol: dict[str, str] = {}
    for s in sorted(stale_signals, key=lambda x: x["signal_date"]):
        by_symbol.setdefault(s["symbol"], s["signal_date"])

    fetched = 0
    for symbol, sig_date in list(by_symbol.items())[:refetch_max]:
        try:
            df = fetch_fn(symbol, sig_date)
        except Exception as e:
            logger.warning("信号补抓日线失败 %s（跳过）: %s", symbol, e)
            continue
        if df is None or df.empty:
            continue
        try:
            save_daily_quotes(df, db_path)
            fetched += 1
        except Exception as e:
            logger.warning("信号补抓入库失败 %s（跳过）: %s", symbol, e)
    if fetched:
        logger.info("信号缺K线补抓: %d/%d 只成功", fetched, len(by_symbol))
    return fetched


def _entry_barrier_type(open_, high, close) -> str:
    """
    信号日入场形态：一字板（开=收=最高，实盘无法买入）/ 涨停收盘（收=最高≈封板，
    盘中或可排单）/ 非涨停。行情缺失归「未知」。纸面收益的可实现性分层依据。
    """
    if open_ is None or high is None or close is None:
        return "未知"
    try:
        o, h, c = float(open_), float(high), float(close)
    except (TypeError, ValueError):
        return "未知"
    if abs(c - h) < 1e-6:
        return "一字板" if abs(o - c) < 1e-6 else "涨停收盘"
    return "非涨停"


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
    refetch_missing: bool = False,
    fetch_fn=None,
) -> dict:
    """
    每日结算：对全部 open 信号（signal_date < settle_date，当天新记录的信号不结算）：
      a) 最低价 ≤ 止损价 → 按止损价关闭，exit_reason="止损"，R=−1
      b) 收盘价 < 退出通道下轨（S1=10日/S2=20日） → 按收盘价关闭，exit_reason="通道退出"
      c) 信号满 SIGNAL_MAX_HOLDING_DAYS 个交易日 → 按收盘价关闭，exit_reason="到期"
      d) 超过 最大持有×1.7+宽限 自然日仍无信号日后K线 → exit_reason="数据缺失"关闭
         （r_multiple 置空不进统计；修复掉池股票信号永远 open 的幸存者偏差）

    refetch_missing=True 时先对缺K线信号补抓日线（联网；run_all/CLI 按
    config.SIGNAL_SETTLE_REFETCH 启用，模块默认 False 保持离线可测）；fetch_fn
    可注入替换默认抓取源（测试用）。补抓/超龄关闭均失败降级，不阻塞正常结算。
    """
    settle_date = settle_date or datetime.now().strftime("%Y-%m-%d")
    result = {
        "settle_date": settle_date,
        "checked": 0,
        "settled": 0,
        "still_open": 0,
        "by_reason": {"止损": 0, "通道退出": 0, "到期": 0},
        "data_missing_closed": 0,
        "refetched_symbols": 0,
        "details": [],
    }

    init_database(db_path)

    # 预检：缺K线的 open 信号先补抓（独立连接写库，避免与结算连接互相持锁）
    if refetch_missing:
        try:
            with get_connection(db_path) as conn:
                pre = [s for s in _load_open_signals(conn) if s["signal_date"] < settle_date]
                pre_max = _symbol_max_dates(conn, {s["symbol"] for s in pre})
            stale_pre = [s for s in pre if (pre_max.get(s["symbol"]) or "") <= s["signal_date"]]
            if stale_pre:
                result["refetched_symbols"] = _refetch_missing_bars(stale_pre, db_path, fetch_fn)
        except Exception as e:
            logger.warning("信号缺K线补跳失败（降级，保持 open）: %s", e)

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

        # d) 超龄无数据关闭：结算循环后仍未关闭、且信号日后无任何K线、
        #    超过宽限门槛的信号 → 「数据缺失」关闭（不产生 R，统计剔除）
        closed_keys = {(d["signal_date"], d["symbol"], d["system"]) for d in result["details"]}
        remain = [s for s in open_signals if (s["signal_date"], s["symbol"], s["system"]) not in closed_keys]
        if remain:
            remain_max = _symbol_max_dates(conn, {s["symbol"] for s in remain})
            settle_dt = datetime.strptime(settle_date, "%Y-%m-%d")
            for s in remain:
                if (remain_max.get(s["symbol"]) or "") > s["signal_date"]:
                    continue  # 有信号日后K线（含补抓），只是尚未触发退出
                age_days = (settle_dt - datetime.strptime(s["signal_date"], "%Y-%m-%d")).days
                if age_days <= _stale_threshold_days(s["system"]):
                    continue  # 宽限期内，保持 open 等待补数据
                conn.execute(
                    """
                    UPDATE signals
                    SET status = 'closed', exit_date = ?, exit_price = NULL,
                        exit_reason = '数据缺失', r_multiple = NULL
                    WHERE signal_date = ? AND symbol = ? AND system = ?
                    """,
                    (settle_date, s["signal_date"], s["symbol"], s["system"]),
                )
                result["still_open"] -= 1
                result["data_missing_closed"] += 1
                result["details"].append({
                    "signal_date": s["signal_date"],
                    "symbol": s["symbol"],
                    "system": s["system"],
                    "exit_date": settle_date,
                    "exit_price": None,
                    "exit_reason": "数据缺失",
                    "r_multiple": None,
                })

    if result["settled"] or result["data_missing_closed"]:
        logger.info(
            "信号结算: 关闭 %d 个（止损 %d / 通道退出 %d / 到期 %d / 数据缺失 %d）",
            result["settled"],
            result["by_reason"]["止损"],
            result["by_reason"]["通道退出"],
            result["by_reason"]["到期"],
            result["data_missing_closed"],
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
    + 按信号日市场状态分层（by_state，验证不同市场状态下信号质量；老数据无状态归入「未知」）
    + 按信号日入场形态分层（by_entry_type：一字板/涨停收盘/非涨停——前两者纸面收益
    实盘难以复制，决策看「非涨停」组）+ 数据缺失关闭条数（data_missing，未计入统计）"""
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")

    init_database(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT s.system, s.r_multiple, s.market_state, q.open, q.high, q.close
            FROM signals s
            LEFT JOIN daily_quotes q ON q.symbol = s.symbol AND q.trade_date = s.signal_date
            WHERE s.status = 'closed' AND s.exit_date >= ? AND s.r_multiple IS NOT NULL
            """,
            (cutoff,),
        )
        closed_rows = cur.fetchall()
        cur = conn.execute(
            "SELECT system, COUNT(*) FROM signals WHERE status = 'open' GROUP BY system"
        )
        open_by_system = dict(cur.fetchall())
        cur = conn.execute(
            "SELECT system, COUNT(*) FROM signals "
            "WHERE status = 'closed' AND exit_reason = '数据缺失' AND exit_date >= ? "
            "GROUP BY system",
            (cutoff,),
        )
        data_missing = dict(cur.fetchall())

    groups: dict[str, list[float]] = {}
    state_groups: dict[str, dict[str, list[float]]] = {}
    entry_groups: dict[str, dict[str, list[float]]] = {}
    board_locked: dict[str, int] = {}
    for system, r, mstate, q_open, q_high, q_close in closed_rows:
        r = float(r)
        groups.setdefault(system, []).append(r)
        state_groups.setdefault(mstate or "未知", {}).setdefault(system, []).append(r)
        etype = _entry_barrier_type(q_open, q_high, q_close)
        entry_groups.setdefault(etype, {}).setdefault(system, []).append(r)
        if etype in ("一字板", "涨停收盘"):
            board_locked[system] = board_locked.get(system, 0) + 1

    systems = {system: _group_stats(rs) for system, rs in sorted(groups.items())}
    by_state = {
        state: {system: _group_stats(rs) for system, rs in sorted(sys_map.items())}
        for state, sys_map in sorted(state_groups.items())
    }
    by_entry_type = {
        etype: {system: _group_stats(rs) for system, rs in sorted(sys_map.items())}
        for etype, sys_map in sorted(entry_groups.items())
    }
    return {
        "as_of": as_of,
        "days": days,
        "open_count": sum(open_by_system.values()),
        "open_by_system": open_by_system,
        "systems": systems,
        "by_state": by_state,
        "by_entry_type": by_entry_type,
        "board_locked_by_system": board_locked,
        "data_missing": data_missing,
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

    by_entry = stats.get("by_entry_type", {})
    if by_entry:
        lines.extend([
            "",
            "**按信号日入场形态分层（一字板/涨停收盘=收盘封板，实盘难以按信号价买入；决策看「非涨停」组）**：",
            "",
        ])
        lines.append("| 入场形态 | 系统 | 样本数 | 胜率 | 平均R | PF |")
        lines.append("|----------|------|--------|------|-------|-----|")
        for etype, sys_map in by_entry.items():
            for system, s in sys_map.items():
                pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "∞"
                lines.append(
                    f"| {etype} | {system} | {s['closed']} | {s['win_rate']}% "
                    f"| {s['avg_r']:+.2f} | {pf} |"
                )

    board_locked = stats.get("board_locked_by_system", {})
    notes = []
    for system, s in systems.items():
        if s["sample_sufficient"]:
            note = f"{system} 近 {stats['days']} 天期望值 {s['expectancy']:+.2f}R，样本 {s['closed']}"
        else:
            note = f"{system} 样本不足（{s['closed']} < {SIGNAL_STATS_MIN_SAMPLE}），继续观察"
        if board_locked.get(system):
            note += f"（含一字/涨停收盘 {board_locked[system]} 条，纸面口径偏乐观）"
        notes.append(note)
    open_count = stats.get("open_count", 0)
    data_missing_total = sum((stats.get("data_missing") or {}).values())
    if data_missing_total:
        notes.append(f"另有 {data_missing_total} 条信号因数据缺失关闭（掉池无K线，未计入统计）")
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
    p_settle.add_argument(
        "--no-refetch", action="store_true",
        help="跳过缺K线信号补抓（纯离线结算；默认按 config.SIGNAL_SETTLE_REFETCH）",
    )

    p_stats = sub.add_parser("stats", help="近 N 天已关闭信号分组统计")
    p_stats.add_argument("--days", type=int, default=90, help="统计窗口天数（默认 90）")

    args = parser.parse_args()
    if args.command == "settle":
        result = settle_signals(
            settle_date=args.date,
            refetch_missing=SIGNAL_SETTLE_REFETCH and not args.no_refetch,
        )
        print(f"\n=== 信号结算 {result['settle_date']} ===\n")
        print(f"检查 open 信号 {result['checked']} 个，关闭 {result['settled']} 个"
              f"（止损 {result['by_reason']['止损']} / 通道退出 {result['by_reason']['通道退出']}"
              f" / 到期 {result['by_reason']['到期']}）")
        if result.get("refetched_symbols"):
            print(f"缺K线补抓 {result['refetched_symbols']} 只")
        if result.get("data_missing_closed"):
            print(f"数据缺失关闭 {result['data_missing_closed']} 个（掉池无K线，超龄标记，未产生R）")
        for d in result["details"]:
            r = f"{d['r_multiple']:+.2f}" if d["r_multiple"] is not None else "-"
            price = d["exit_price"] if d["exit_price"] is not None else "-"
            print(f"  {d['symbol']} {d['system']} 信号日 {d['signal_date']} → "
                  f"{d['exit_reason']} @ {price}（{d['exit_date']}，R {r}）")
    elif args.command == "stats":
        stats = signal_stats(days=args.days)
        print(f"\n=== 信号验证统计（近 {args.days} 天，截至 {stats['as_of']}） ===\n")
        print(signal_stats_to_markdown(stats))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
