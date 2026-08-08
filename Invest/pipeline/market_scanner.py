"""
每日市场扫描 — 突破候选、板块强度、市场状态，输出 Markdown 报告
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import logging
from datetime import datetime

import pandas as pd

from config import (
    CHANNEL_LONG,
    CHANNEL_SHORT,
    DEFAULT_INDEX_SYMBOL,
    FALSE_BREAKOUT_COOLDOWN_DAYS,
    FALSE_BREAKOUT_MAX,
    HOT_CATALYST_ENABLED,
    HOT_CATALYST_MAX,
    HOT_SIGNAL_SYSTEM,
    MARKET_STATE,
    MARKET_SCAN_OUTPUT_DIR,
    WATCHLIST,
)
from pipeline.database import (
    DB_PATH,
    get_connection,
    init_database,
    load_daily_quotes,
    load_hot_pool,
    load_limit_pool,
    load_sector_quotes,
    load_trend_pool,
    save_market_state,
)
from pipeline.indicators import (
    judge_market_state,
    prepare_stock_indicators,
    rank_sectors_by_strength,
    scan_breakout_candidates,
)
from pipeline.trend_filters import evaluate_trend_filters, filters_brief

logger = logging.getLogger(__name__)


def _load_limit_stats_from_db(db_path=None) -> pd.DataFrame:
    path = db_path or DB_PATH
    try:
        with get_connection(path) as conn:
            return pd.read_sql_query(
                "SELECT * FROM limit_stats ORDER BY trade_date DESC LIMIT 5",
                conn,
            )
    except Exception:
        return pd.DataFrame()


def _run_position_monitor(output_dir: Path) -> dict | None:
    """调用持仓监控（review.monitor），失败时降级返回 None，不拖垮扫描"""
    try:
        from review.monitor import run_monitor

        return run_monitor(output_dir=output_dir)
    except Exception as e:
        logger.warning("持仓监控失败（已降级，扫描报告不含监控区块）: %s", e)
        return None


def _load_trend_pool_safe() -> pd.DataFrame:
    """读取最新一期趋势池（未构建或读取失败时降级空表，扫描仅覆盖传入股票池）"""
    try:
        return load_trend_pool()
    except Exception as e:
        logger.warning("趋势池读取失败（趋势扫描仅覆盖传入股票池）: %s", e)
        return pd.DataFrame()


def _build_daily_plan_safe(scan_json: dict, monitor_result: dict | None) -> dict | None:
    """盘前操作清单（pipeline/daily_plan），失败降级返回 None，不拖垮扫描"""
    try:
        from pipeline.daily_plan import build_daily_plan

        return build_daily_plan(scan_json, monitor_result)
    except Exception as e:
        logger.warning("明日操作计划构建失败（已降级，报告不含该节）: %s", e)
        return None


def _sector_rank_pct_map(sector_rank: pd.DataFrame) -> dict[str, float]:
    """板块名 → 强度排名百分位（rank/总数，供三重滤网「板块共振」判定）"""
    if sector_rank is None or sector_rank.empty:
        return {}
    total = len(sector_rank)
    return {str(r["sector_name"]): float(r["rank"]) / total for _, r in sector_rank.iterrows()}


def _enrich_breakout(
    breakout: pd.DataFrame,
    prepared: dict[str, pd.DataFrame],
    sector_pct: dict[str, float],
    pool_sector: dict[str, str],
    system: str,
    signal_date: str,
    db_path=None,
) -> pd.DataFrame:
    """
    突破候选附加三重滤网结果与系统1过滤附注（V5.0 §4.2/§4.3；逐股降级，单股失败不影响其他）：
    - filters_passed/filters_required/filter_brief：滤网通过数与明细（pipeline/trend_filters）
    - note：「滤网未全通过，仅观察/极小仓」「冷却中」「首仓建议降50%」等附注
    - record：是否入库 signals 表（冷却期候选不入库，对应连续假突破移出可交易池 20 日）
    """
    if breakout.empty:
        return breakout

    from pipeline.signal_tracker import consecutive_stop_outs, last_signal_won

    high_55_col = f"high_{CHANNEL_LONG}"
    rows = []
    for _, r in breakout.iterrows():
        row = r.to_dict()
        sym = row["symbol"]
        try:
            sector = pool_sector.get(sym, "")
            pct = sector_pct.get(sector.split("+")[0]) if sector else None
            filters = evaluate_trend_filters(prepared.get(sym), pct, system)
            notes: list[str] = []
            record = True
            if not filters["all_passed"]:
                notes.append("滤网未全通过，仅观察/极小仓")

            if "S1" in system:
                consec, last_exit = consecutive_stop_outs(sym, system, db_path=db_path)
                cooled = (
                    consec >= FALSE_BREAKOUT_MAX
                    and last_exit
                    and 0 <= (
                        datetime.strptime(signal_date, "%Y-%m-%d")
                        - datetime.strptime(last_exit, "%Y-%m-%d")
                    ).days <= FALSE_BREAKOUT_COOLDOWN_DAYS
                )
                if cooled:
                    notes.append(f"冷却中（连续假突破×{consec}，{FALSE_BREAKOUT_COOLDOWN_DAYS}日内不入库）")
                    record = False
                elif last_signal_won(sym, system, db_path=db_path):
                    # 上次突破盈利且离 55 日新高较远（>1×ATR）：首仓建议降 50%（V5.0 §4.3 系统1过滤）
                    s_df = prepared.get(sym)
                    high_55 = None
                    if s_df is not None and high_55_col in s_df.columns:
                        v = s_df.iloc[-1][high_55_col]
                        high_55 = float(v) if pd.notna(v) else None
                    atr = float(row.get("atr_20") or 0)
                    if high_55 and atr > 0 and (high_55 - float(row["close"])) > atr:
                        notes.append("上次突破盈利且远离55日新高，首仓建议降50%")

            row["filters_passed"] = filters["passed"]
            row["filters_required"] = filters["required"]
            row["filter_brief"] = filters_brief(filters)
            row["note"] = "；".join(notes)
            row["record"] = record
        except Exception as e:
            logger.warning("滤网评估失败 %s（降级为未评估，照常入库）: %s", sym, e)
            row["note"] = row.get("note", "")
            row["record"] = True
        rows.append(row)
    return pd.DataFrame(rows)


def run_scan(
    symbols: list[str] | None = None,
    output_dir: Path | None = None,
) -> Path:
    """执行完整市场扫描，生成 Markdown 报告（扫描池 = 传入 symbols ∪ 最新一期趋势池）"""
    init_database()
    symbols = list(symbols or WATCHLIST)
    output_dir = output_dir or MARKET_SCAN_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    report_path = output_dir / f"market_scan_{today}.md"

    # 趋势动态池并入扫描池（成分股来自强势板块；池外个股板块滤网降级为无法判定）
    trend_pool_df = _load_trend_pool_safe()
    pool_sector: dict[str, str] = {}
    if not trend_pool_df.empty:
        for _, pr in trend_pool_df.iterrows():
            pool_sector[str(pr["symbol"])] = str(pr.get("source_sector", "") or "")
        extra = [s for s in pool_sector if s not in symbols]
        if extra:
            logger.info("趋势池并入扫描: +%d 只（总 %d 只）", len(extra), len(symbols) + len(extra))
            symbols.extend(extra)

    symbols_data = {}
    for sym in symbols:
        df = load_daily_quotes(symbol=sym)
        if not df.empty:
            symbols_data[sym] = df

    hs300_df = load_daily_quotes(symbol=DEFAULT_INDEX_SYMBOL)
    if hs300_df.empty:
        hs300_df = load_daily_quotes(symbol="000300")

    limit_stats = _load_limit_stats_from_db()
    prepared = {sym: prepare_stock_indicators(df) for sym, df in symbols_data.items()}
    breakout_20 = scan_breakout_candidates(prepared, CHANNEL_SHORT)
    breakout_55 = scan_breakout_candidates(prepared, CHANNEL_LONG)
    sector_rank = _build_sector_ranking(hs300_df)

    # 三重滤网 + 系统1过滤附注（S1-A 冷却/降仓提示；滤网未全通过标注观察级）
    sector_pct = _sector_rank_pct_map(sector_rank)
    breakout_20 = _enrich_breakout(breakout_20, prepared, sector_pct, pool_sector, "S1-A", today)
    breakout_55 = _enrich_breakout(breakout_55, prepared, sector_pct, pool_sector, "S2-A", today)

    state_info = judge_market_state(
        hs300_df,
        limit_stats,
        ma_weeks=MARKET_STATE["ma_weeks"],
        volume_median_days=MARKET_STATE["volume_median_days"],
        breadth_bull=MARKET_STATE["breadth_bull"],
        breadth_bear=MARKET_STATE["breadth_bear"],
    )

    if hs300_df is not None and not hs300_df.empty:
        save_market_state({
            "trade_date": today,
            "state": state_info["state"],
            "hs300_close": state_info["hs300_close"],
            "hs300_ma20w": state_info["hs300_ma20w"],
            "volume_ratio": state_info["volume_ratio"],
            "breadth_ratio": state_info["breadth_ratio"],
            "suggested_pos": state_info["suggested_pos"],
            "notes": "; ".join(state_info["notes"]),
        })

    monitor_result = _run_position_monitor(output_dir)
    hot_section = _build_hot_section(today, sector_rank, state_info.get("state", ""))

    scan_json = _build_scan_json(today, state_info, breakout_20, breakout_55, sector_rank, hot_section)
    scan_json["position_monitor"] = monitor_result  # None 表示监控已降级
    daily_plan = _build_daily_plan_safe(scan_json, monitor_result)
    scan_json["daily_plan"] = daily_plan

    md = _format_report(today, state_info, breakout_20, breakout_55, sector_rank, symbols,
                        monitor_result, hot_section, daily_plan)
    report_path.write_text(md, encoding="utf-8")

    json_path = output_dir / f"market_scan_{today}.json"
    json_path.write_text(json.dumps(scan_json, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("扫描报告已生成: %s", report_path)
    logger.info("扫描 JSON 已生成: %s", json_path)
    _record_breakout_signals(scan_json, today)
    return report_path


def _record_breakout_signals(scan_json: dict, signal_date: str) -> None:
    """突破候选写入 signals 表（信号验证）；失败降级不阻塞扫描。

    冷却期候选（record=False）不入库；入库信号携带当日市场状态与滤网通过数，
    供信号分层统计与复盘（V5.0 §4.2/§6）。
    """
    try:
        from pipeline.signal_tracker import record_signals

        state = scan_json.get("market_state")

        def _eligible(key: str) -> list[dict]:
            cands = []
            for c in scan_json.get(key, []):
                if not c.get("record", True):
                    continue
                c["market_state"] = state
                cands.append(c)
            return cands

        n1 = record_signals(_eligible("breakout_s1a"), system="S1-A", signal_date=signal_date)
        n2 = record_signals(_eligible("breakout_s2a"), system="S2-A", signal_date=signal_date)
        n3 = record_signals(
            [
                {**c, "market_state": state}
                for c in scan_json.get("hot_pool", {}).get("hot_breakout", [])
            ],
            system=HOT_SIGNAL_SYSTEM,
            signal_date=signal_date,
        )
        if n1 or n2 or n3:
            logger.info("信号入库: S1-A +%d, S2-A +%d, %s +%d", n1, n2, HOT_SIGNAL_SYSTEM, n3)
    except Exception as e:
        logger.warning("信号入库失败（已降级，不影响扫描结果）: %s", e)


def _df_to_breakout_list(df: pd.DataFrame) -> list[dict]:
    """将突破候选 DataFrame 转为 JSON 可序列化列表（含三重滤网与系统1附注）"""
    if df.empty:
        return []
    records = []
    for _, r in df.iterrows():
        rec = {
            "symbol": r["symbol"],
            "close": round(float(r["close"]), 2),
            "channel_high": round(float(r["channel_high"]), 2),
            "breakout_pct": round(float(r["breakout_pct"]), 2),
            "atr_20": round(float(r.get("atr_20", 0) or 0), 2),
            "period": int(r.get("period", 0)),
        }
        if "filters_passed" in r and pd.notna(r.get("filters_passed")):
            rec["filter_passed"] = int(r["filters_passed"])
            rec["filters_required"] = int(r.get("filters_required", 0) or 0)
            rec["filter_brief"] = str(r.get("filter_brief", ""))
            rec["note"] = str(r.get("note", ""))
            rec["record"] = bool(r.get("record", True))
        records.append(rec)
    return records


def _df_to_sector_list(df: pd.DataFrame) -> list[dict]:
    """将板块排名 DataFrame 转为 JSON 可序列化列表"""
    if df.empty:
        return []
    records = []
    for _, r in df.head(15).iterrows():
        rs = r.get("relative_strength", 0)
        ret = r.get("period_return", 0)
        records.append({
            "rank": int(r["rank"]),
            "sector_name": r["sector_name"],
            "relative_strength": round(float(rs), 4) if pd.notna(rs) else None,
            "period_return": round(float(ret), 4) if pd.notna(ret) else None,
        })
    return records


def _build_scan_json(
    date: str,
    state_info: dict,
    breakout_20: pd.DataFrame,
    breakout_55: pd.DataFrame,
    sector_rank: pd.DataFrame,
    hot_section: dict | None = None,
) -> dict:
    """构建市场扫描结构化 JSON"""
    hot = hot_section or {}
    return {
        "date": date,
        "market_state": state_info.get("state", "C"),
        "breakout_s1a": _df_to_breakout_list(breakout_20),
        "breakout_s2a": _df_to_breakout_list(breakout_55),
        "sector_ranking": _df_to_sector_list(sector_rank),
        "hot_pool": {
            "available": bool(hot.get("available", False)),
            "limit_up_count": len(hot.get("limit_up", [])),
            "lianban_count": len(hot.get("lianban", [])),
            "broken_count": len(hot.get("broken", [])),
            "lianban": hot.get("lianban", []),
            "hot_breakout": hot.get("hot_breakout", []),
            "dragon_candidates": hot.get("dragon_candidates", []),
            "dragon_primary": hot.get("dragon_primary", ""),
            "dragon_market_comment": hot.get("dragon_market_comment", ""),
            "dragon_note": hot.get("dragon_note", ""),
            "sector_focus": hot.get("sector_focus", []),
            "event_calendar": hot.get("event_calendar", {}),
            "evidence_chains": hot.get("evidence_chains", {}),
            "note": hot.get("note", ""),
        },
    }


def _build_sector_ranking(benchmark_df: pd.DataFrame) -> pd.DataFrame:
    try:
        with get_connection(DB_PATH) as conn:
            sectors = pd.read_sql_query(
                "SELECT DISTINCT sector_name FROM sector_quotes",
                conn,
            )
    except Exception:
        return pd.DataFrame()

    sector_data = {}
    for name in sectors["sector_name"].tolist():
        df = load_sector_quotes(sector_name=name)
        if not df.empty:
            sector_data[name] = df

    if not sector_data or benchmark_df.empty:
        return pd.DataFrame()

    return rank_sectors_by_strength(sector_data, benchmark_df)


# 买入规则 8 条（用户手写体系）：满足 4 条以上才允许买入；⑧ 逻辑/持续性最重要
_BUY_RULE_NAMES = {
    1: "板块Top5",
    2: "涨停家数增加",
    3: "前排",
    4: "放量突破",
    5: "次日无高开低走",
    6: "分时均价线承接",
    7: "大盘环境",
    8: "事件催化",
}

_RULE_NUMERALS = "①②③④⑤⑥⑦⑧"


def _volume_ratio(df: pd.DataFrame, days: int = 20) -> float | None:
    """当日成交量 / 前 N 日均量（不含当日）；数据不足返回 None"""
    if df is None or df.empty or "volume" not in df.columns:
        return None
    vol = pd.to_numeric(df["volume"], errors="coerce").dropna()
    if len(vol) < days + 1:
        return None
    base = vol.iloc[-days - 1:-1].mean()
    if not base or base <= 0:
        return None
    return float(vol.iloc[-1] / base)


def _evaluate_buy_rules(
    rec: dict,
    info_row,
    df: pd.DataFrame | None,
    top_sectors: set,
    limit_today_by_sector: dict,
    limit_prev_by_sector: dict | None,
    market_state: str,
    catalyst: dict | None = None,
) -> dict:
    """
    按手写买入规则 8 条逐条核对（盘前离线可判 ①②③④⑦；⑤⑥ 需次日/盘中观察；
    ⑧ 由 catalyst_analyzer 公告+LLM 判定经 catalyst 参数传入，未分析时降级人工核对）。
    返回 {"rules": {编号: True/False/None}, "met": 自动满足条数, "text": 推荐分析文本}
    True=满足，False=明确不满足，None=无法自动判定。
    """
    sector = str(rec.get("sector", "") or "")
    source = str(rec.get("source", "") or "")
    try:
        lbc = int(info_row.get("lbc", 0) or 0) if info_row is not None and len(info_row) else 0
    except (TypeError, ValueError):
        lbc = 0
    rules: dict[int, bool | None] = {}

    # ① 所属板块当天涨幅排名前 5（板块名双向子串近似匹配）
    if not top_sectors:
        rules[1] = None
    else:
        rules[1] = bool(sector) and any(sector == s or sector in s or s in sector for s in top_sectors)

    # ② 板块涨停家数较上一交易日增加
    today_n = limit_today_by_sector.get(sector, 0)
    if limit_prev_by_sector is None:
        rules[2] = None
    else:
        prev_n = limit_prev_by_sector.get(sector, 0)
        rules[2] = today_n >= 1 and today_n > prev_n

    # ③ 前排（来源含连板/领涨或 lbc>=2 近似；「有逻辑支撑」部分归入⑧人工）
    rules[3] = ("连板" in source) or ("领涨" in source) or lbc >= 2

    # ④ 放量突破，或涨停后换手承接强（两分支满足其一）
    vr = _volume_ratio(df)
    if "涨停" in source or "连板" in source:
        rules[4] = True  # 涨停/连板来源视为「涨停后换手承接强」分支
    elif vr is None:
        rules[4] = None
    else:
        rules[4] = vr >= 1.5

    # ⑤ 次日没有高开低走 / ⑥ 跌破分时均价线能快速拉回——次日/盘中观察项，盘前不判
    rules[5] = None
    rules[6] = None

    # ⑦ 大盘红盘数改善（用市场状态 A/B 近似）
    rules[7] = market_state in ("A", "B") if market_state else None

    # ⑧ 事件/政策/业绩/技术突破/转型——catalyst_analyzer（公告+LLM）判定传入；
    # 未分析或判定失败为 None，降级人工核对
    c8 = (catalyst or {}).get("satisfied")
    rules[8] = c8 if c8 is not None else None

    met = sum(1 for v in rules.values() if v is True)
    auto_ok = [n for n, v in rules.items() if v is True]
    auto_no = [n for n, v in rules.items() if v is False]
    numerals = "".join(_RULE_NUMERALS[n - 1] for n in auto_ok)

    if met >= 4:
        verdict = f"符合 {met} 条（{numerals}）"
    else:
        verdict = f"自动满足 {met}/8 条（{numerals or '无'}，差 {4 - met} 条）"
    parts = [verdict]
    if auto_no:
        parts.append("✗" + "、".join(_BUY_RULE_NAMES[n] for n in auto_no))
    parts.append("⑤⑥盘中确认")
    if rules[8] is True:
        ctype = (catalyst or {}).get("catalyst_type", "")
        sustain = (catalyst or {}).get("sustainability", "")
        parts.append(f"⑧满足·{ctype}（{sustain}）" if ctype else "⑧满足")
    elif rules[8] is False:
        parts.append("⑧无明确催化")
    else:
        parts.append("⑧人工核对(最重要)")
    return {"rules": rules, "met": met, "text": "；".join(parts)}


def _limit_count_by_sector(df: pd.DataFrame) -> dict:
    """涨停池（pool_type=up）按板块统计家数"""
    if df is None or df.empty:
        return {}
    up = df[df["pool_type"] == "up"] if "pool_type" in df.columns else df
    return up.groupby("sector").size().to_dict() if not up.empty else {}


def _analyze_catalysts_safe(records: list) -> dict:
    """
    对 Top N 候选做规则⑧催化分析（公告 + LLM，全程缓存）；
    HOT_CATALYST_ENABLED=false、无 LLM Key、网络失败时逐股降级为空，不拖垮扫描。
    """
    if not HOT_CATALYST_ENABLED or not records:
        return {}
    out = {}
    for rec in records:
        try:
            from research.catalyst_analyzer import analyze_catalyst

            out[rec["symbol"]] = analyze_catalyst(
                rec["symbol"], name=rec.get("name", ""), sector=rec.get("sector", "")
            )
        except Exception as e:
            logger.warning("催化分析失败 %s（该股规则⑧降级人工核对）: %s", rec["symbol"], e)
    return out


def _build_hot_section(today: str, sector_rank: pd.DataFrame | None = None, market_state: str = "") -> dict:
    """
    超短热点区块：涨停/连板/炸板名单 + 热点池突破候选（HOT-S，含名称与 8 条买入规则推荐分析）。
    数据全部来自本地 DB（limit_pool / hot_pool / daily_quotes，由 run_all 取数阶段写入），
    热点池未构建或构建失败时降级为标注，不拖垮扫描报告。
    """
    result = {
        "available": False,
        "limit_up": [],
        "lianban": [],
        "broken": [],
        "hot_breakout": [],
        "dragon_candidates": [],
        "dragon_primary": "",
        "dragon_market_comment": "",
        "dragon_note": "",
        "sector_focus": [],
        "event_calendar": {},
        "evidence_chains": {},
        "note": "热点池未构建（需先运行 run_all 取数流程构建热点池）",
    }
    try:
        limit_today = load_limit_pool(trade_date=today)
        pool = load_hot_pool(trade_date=today)
        if limit_today.empty and pool.empty:
            return result

        up = limit_today[limit_today["pool_type"] == "up"] if not limit_today.empty else pd.DataFrame()
        broken = limit_today[limit_today["pool_type"] == "broken"] if not limit_today.empty else pd.DataFrame()
        result["limit_up"] = up.to_dict("records") if not up.empty else []
        result["lianban"] = (
            up[up["lbc"] >= 2].sort_values("lbc", ascending=False).to_dict("records")
            if not up.empty else []
        )
        result["broken"] = broken.to_dict("records") if not broken.empty else []

        # 规则①②⑦所需上下文：板块 Top5、今日/上一交易日各板块涨停家数、市场状态
        top_sectors: set = set()
        if sector_rank is not None and not sector_rank.empty and "sector_name" in sector_rank.columns:
            top_sectors = {str(s) for s in sector_rank["sector_name"].head(5)}
        limit_today_by_sector = _limit_count_by_sector(limit_today)
        limit_prev_by_sector = None
        try:
            limit_all = load_limit_pool()
            prev_dates = sorted(d for d in limit_all["trade_date"].unique() if d < today)
            if prev_dates:
                limit_prev_by_sector = _limit_count_by_sector(
                    limit_all[limit_all["trade_date"] == prev_dates[-1]]
                )
        except Exception as e:
            logger.warning("上一交易日涨停池读取失败（规则②降级为不判定）: %s", e)

        # 热点池突破扫描（S1-A 20 日通道，与主扫描同函数同参数）
        if not pool.empty:
            symbols_data = {}
            for sym in pool["symbol"]:
                df = load_daily_quotes(symbol=sym)
                if not df.empty:
                    symbols_data[sym] = df
            breakout = scan_breakout_candidates(symbols_data, CHANNEL_SHORT)
            if not breakout.empty:
                info = pool.set_index("symbol")
                entries = []
                for _, r in breakout.iterrows():
                    prow = info.loc[r["symbol"]] if r["symbol"] in info.index else {}
                    rec = {
                        "symbol": r["symbol"],
                        "name": str(prow.get("name", "")) if len(prow) else "",
                        "close": round(float(r["close"]), 2),
                        "channel_high": round(float(r["channel_high"]), 2),
                        "breakout_pct": round(float(r["breakout_pct"]), 2),
                        "atr_20": round(float(r.get("atr_20", 0) or 0), 2),
                        "period": int(r.get("period", 0)),
                        "source": str(prow.get("source", "")) if len(prow) else "",
                        "sector": str(prow.get("sector", "")) if len(prow) else "",
                    }
                    entries.append((rec, prow))

                # 第一遍：离线规则初评（⑧待定），取最有希望的 Top N 做催化分析
                for rec, prow in entries:
                    ev = _evaluate_buy_rules(
                        rec, prow, symbols_data.get(rec["symbol"]),
                        top_sectors, limit_today_by_sector, limit_prev_by_sector, market_state,
                    )
                    rec["rules_met"] = ev["met"]
                entries.sort(key=lambda e: (-e[0]["rules_met"], -e[0]["breakout_pct"]))
                catalyst_map = _analyze_catalysts_safe([e[0] for e in entries[:HOT_CATALYST_MAX]])

                # 终评：带入⑧催化判定，重算推荐分析并重新排序
                records = []
                for rec, prow in entries:
                    cat = catalyst_map.get(rec["symbol"])
                    ev = _evaluate_buy_rules(
                        rec, prow, symbols_data.get(rec["symbol"]),
                        top_sectors, limit_today_by_sector, limit_prev_by_sector, market_state,
                        catalyst=cat,
                    )
                    rec["rules_met"] = ev["met"]
                    rec["analysis"] = ev["text"]
                    if cat:
                        if cat.get("basis"):
                            rec["catalyst_basis"] = cat["basis"]
                        if cat.get("titles"):
                            rec["catalyst_titles"] = cat["titles"]
                    records.append(rec)
                # 规则满足条数高的排前面，便于盘前快速筛选
                records.sort(key=lambda x: (-x["rules_met"], -x["breakout_pct"]))
                result["hot_breakout"] = records

                # 龙头评分（《如何识别真假龙头》三维验证可量化部分）：
                # 合并涨停池身位/强度字段 + 一字板判定 + ⑧催化，逐条打分，产出 dragon_candidates
                try:
                    from pipeline.dragon_head import (
                        build_dragon_context,
                        dragon_top,
                        grade_hot_candidates,
                        to_float,
                        to_int,
                    )

                    limit_info = {}
                    if not limit_today.empty:
                        for _, lr in limit_today[limit_today["pool_type"] == "up"].iterrows():
                            limit_info[str(lr["symbol"])] = lr
                    for rec in records:
                        lr = limit_info.get(rec["symbol"])
                        if lr is not None:
                            rec["lbc"] = to_int(lr.get("lbc"))
                            rec["fbt"] = str(lr.get("fbt", "") or "")
                            rec["turnover"] = to_float(lr.get("turnover"))
                            rec["seal_amount"] = to_float(lr.get("seal_amount"))
                            rec["zbc"] = to_int(lr.get("zbc"))
                            rec["reason"] = str(lr.get("reason", "") or "")  # THS 涨停归因（逻辑维度证据）
                        sdf = symbols_data.get(rec["symbol"])
                        if sdf is not None and not sdf.empty:
                            last = sdf.sort_values("trade_date").iloc[-1]
                            rec["one_word_board"] = bool(
                                last["open"] == last["high"] == last["low"] == last["close"]
                            )
                        rec["catalyst"] = catalyst_map.get(rec["symbol"])
                    dragon_ctx = build_dragon_context(today)
                    grade_hot_candidates(records, dragon_ctx)
                    result["dragon_candidates"] = dragon_top(records)

                    # Kimi 深度推理（系统自主辨龙头，模型见 KIMI_MODEL_REASONING；失败降级按量化评分排序并标注）
                    try:
                        from research.dragon_reasoner import VERDICT_PRIORITY, reason_dragons

                        # 事件日历：候选个股结构化事项 + 板块未来 30 天关键事项（联网探查）
                        calendar = {}
                        try:
                            from research.event_calendar import build_event_calendar

                            cal_symbols = [r["symbol"] for r in result["dragon_candidates"]]
                            cal_sectors = [
                                n for n, s in sorted(
                                    dragon_ctx.get("sectors", {}).items(),
                                    key=lambda kv: -kv[1].get("count", 0),
                                )[:6]
                            ]
                            calendar = build_event_calendar(cal_symbols, cal_sectors, today)
                            result["event_calendar"] = calendar
                        except Exception as e:
                            logger.warning("事件日历构建失败（降级）: %s", e)
                            result["event_calendar"] = {}

                        # 个股证据链深挖（Top N 联网检索：引爆点/推导链/正反证据，注入推理）
                        evidence_chains = {}
                        try:
                            from research.evidence_chain import build_evidence_chains

                            evidence_chains = build_evidence_chains(result["dragon_candidates"], today)
                            result["evidence_chains"] = evidence_chains
                        except Exception as e:
                            logger.warning("证据链深挖失败（降级）: %s", e)
                            result["evidence_chains"] = {}

                        reasoning = reason_dragons(result["dragon_candidates"], dragon_ctx,
                                                   market_state, calendar=calendar,
                                                   evidence=evidence_chains)
                        if reasoning:
                            vmap = {v["symbol"]: v for v in reasoning["verdicts"]}
                            for rec in result["dragon_candidates"]:
                                v = vmap.get(rec["symbol"])
                                if v:
                                    rec["verdict"] = v["verdict"]
                                    rec["confidence"] = v["confidence"]
                                    rec["reasoning"] = v["reasoning"]
                                    rec["risk"] = v["risk"]
                            result["dragon_candidates"].sort(
                                key=lambda r: (VERDICT_PRIORITY.get(r.get("verdict", ""), 9),
                                               -r.get("dragon_score", 0)))
                            result["dragon_primary"] = reasoning.get("primary") or ""
                            result["dragon_market_comment"] = reasoning.get("market_comment", "")
                            result["sector_focus"] = reasoning.get("sector_focus", [])
                        elif result["dragon_candidates"]:
                            from config import DRAGON_REASON_ENABLED
                            from shared.llm_client import has_llm_api_key

                            result["dragon_note"] = (
                                "⚠️ K3 深度推理未启用（未配置 LLM Key 或已禁用），当前按量化评分排序"
                                if (not has_llm_api_key() or not DRAGON_REASON_ENABLED) else
                                "⚠️ K3 深度推理失败，当前按量化评分排序（详见日志）"
                            )
                    except Exception as e:
                        logger.warning("龙头深度推理失败（已降级按量化评分排序）: %s", e)
                except Exception as e:
                    logger.warning("龙头评分失败（已降级，候选不含龙头字段）: %s", e)
                    result["dragon_candidates"] = []

        result["available"] = True
        result["note"] = ""
    except Exception as e:
        logger.warning("超短热点区块构建失败（已降级）: %s", e)
        result["note"] = f"超短热点区块构建失败（已降级）: {e}"
    return result


def _append_breakout_table(lines: list, df: pd.DataFrame) -> None:
    """突破候选表格（含三重滤网通过数与系统1附注；未评估的行降级显示 —）"""
    lines.append("| 代码 | 收盘价 | 通道高点 | 突破幅度% | ATR(20) | 滤网 | 备注 |")
    lines.append("|------|--------|----------|-----------|---------|------|------|")
    for _, r in df.iterrows():
        fp = r.get("filters_passed")
        if pd.notna(fp):
            brief = f"{int(fp)}/{int(r.get('filters_required', 0) or 0)} {r.get('filter_brief', '')}"
        else:
            brief = "—"
        note = r.get("note", "") or "—"
        lines.append(
            f"| {r['symbol']} | {r['close']:.2f} | {r['channel_high']:.2f} "
            f"| {r['breakout_pct']:.2f} | {r.get('atr_20', 0):.2f} | {brief} | {note} |"
        )


def _format_report(
    date: str,
    state_info: dict,
    breakout_20: pd.DataFrame,
    breakout_55: pd.DataFrame,
    sector_rank: pd.DataFrame,
    symbols: list[str],
    monitor_result: dict | None = None,
    hot_section: dict | None = None,
    daily_plan: dict | None = None,
) -> str:
    lines = [
        "# 每日市场扫描报告",
        "",
        f"> 生成时间: {date}",
        f"> 扫描股票数: {len(symbols)}",
        "",
        "---",
        "",
    ]

    # 持仓监控区块（报告顶部）；monitor_result 为 None 表示监控降级
    if monitor_result is None:
        lines.extend(["## 持仓监控", "", "_持仓监控不可用（已降级跳过，详见日志）_", ""])
    else:
        try:
            from review.monitor import monitor_to_markdown

            lines.extend(monitor_to_markdown(monitor_result))
        except Exception as e:
            lines.extend(["## 持仓监控", "", f"_持仓监控渲染失败（已降级）: {e}_", ""])

    lines.extend([
        "---",
        "",
        "## 一、市场状态判断",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| **市场状态** | **{state_info['state']}** |",
        f"| 建议仓位 | {state_info['suggested_pos']} |",
        f"| 沪深300收盘 | {state_info.get('hs300_close', 'N/A')} |",
        f"| 20周均线 | {state_info.get('hs300_ma20w', 'N/A')} |",
        f"| 成交额/中位数 | {state_info.get('volume_ratio', 1.0):.2f} |",
        f"| 上涨/下跌比 | {state_info.get('breadth_ratio', 1.0):.2f} |",
        "",
        f"**判断依据**: {'; '.join(state_info.get('notes', []))}",
        "",
        "### 状态说明",
        "",
        "- **A** (70%-90%): 指数、成交、主线共振",
        "- **B** (40%-70%): 结构性行情",
        "- **C** (20%-50%): 震荡轮动",
        "- **D** (0%-30%): 系统性下跌",
        "",
        "---",
        "",
    ])

    # 明日操作计划（盘前操作清单）：渲染失败/构建失败均降级标注，不影响后续节
    if daily_plan is None:
        lines.extend(["## 二、明日操作计划", "", "_操作计划不可用（已降级跳过，详见日志）_", "", "---", ""])
    else:
        try:
            from pipeline.daily_plan import daily_plan_to_markdown

            lines.extend(daily_plan_to_markdown(daily_plan))
            lines.extend(["---", ""])
        except Exception as e:
            lines.extend(["## 二、明日操作计划", "", f"_操作计划渲染失败（已降级）: {e}_", "", "---", ""])

    lines.extend([
        "## 三、20日通道突破候选 (S1-A)",
        "",
    ])

    if breakout_20.empty:
        lines.append("_暂无突破候选_")
    else:
        _append_breakout_table(lines, breakout_20)

    lines.extend(["", "---", "", "## 四、55日通道突破候选 (S2-A)", ""])

    if breakout_55.empty:
        lines.append("_暂无突破候选_")
    else:
        _append_breakout_table(lines, breakout_55)

    lines.extend(["", "---", "", "## 五、板块相对强度排名 (Top 15)", ""])

    if sector_rank.empty:
        lines.append("_暂无板块数据，请先运行数据获取_")
    else:
        lines.append("| 排名 | 板块 | 相对强度(百分点) | 20日涨幅 |")
        lines.append("|------|------|----------|----------|")
        for _, r in sector_rank.head(15).iterrows():
            rs = r.get("relative_strength", 0)
            # 差值法口径：+5.0 = 板块 20 日涨幅跑赢基准 5 个百分点
            rs_str = f"{rs*100:+.1f}" if pd.notna(rs) else "N/A"
            ret = r.get("period_return", 0)
            ret_str = f"{ret*100:.1f}%" if pd.notna(ret) else "N/A"
            lines.append(f"| {r['rank']} | {r['sector_name']} | {rs_str} | {ret_str} |")

    lines.extend(["", "---", "", "## 六、超短热点池（1-5 天，HOT-S）", ""])

    hot = hot_section or {}
    if not hot.get("available"):
        note = hot.get("note") or "热点池未构建（需先运行 run_all 取数流程构建热点池）"
        lines.append(f"_{note}_")
    else:
        up_count = len(hot.get("limit_up", []))
        lb = hot.get("lianban", [])
        bk_count = len(hot.get("broken", []))
        lines.extend([
            f"涨停 {up_count} 只（连板 {len(lb)} 只）· 炸板 {bk_count} 只（完整名单见 limit_pool 表 / JSON）",
            "",
        ])
        if lb:
            lines.extend([
                "### 连板股（情绪龙头）",
                "",
                "| 代码 | 名称 | 连板数 | 涨跌幅% | 所属行业 |",
                "|------|------|--------|---------|----------|",
            ])
            for r in lb[:10]:
                lines.append(
                    f"| {r['symbol']} | {r['name']} | {r.get('lbc', 0)} "
                    f"| {r.get('change_pct', 0):.1f} | {r.get('sector', '')} |"
                )
            lines.append("")

        hb = hot.get("hot_breakout", [])
        if not hb:
            lines.append("_热点池暂无 20 日通道突破候选_")
        else:
            lines.extend([
                "### 热点池突破候选（S1-A 通道；HOT-S 信号跟踪，5 日强制结算）",
                "",
                "| 代码 | 名称 | 来源 | 板块 | 收盘价 | 通道高点 | 突破幅度% | ATR(20) | 推荐分析 |",
                "|------|------|------|------|--------|----------|-----------|---------|----------|",
            ])
            for r in hb:
                lines.append(
                    f"| {r['symbol']} | {r.get('name', '')} | {r.get('source', '')} | {r.get('sector', '')} "
                    f"| {r['close']:.2f} | {r['channel_high']:.2f} "
                    f"| {r['breakout_pct']:.2f} | {r.get('atr_20', 0):.2f} | {r.get('analysis', '')} |"
                )
            basis_items = [
                (r.get("name") or r["symbol"], r["catalyst_basis"])
                for r in hb if r.get("catalyst_basis")
            ]
            if basis_items:
                lines.extend(["", "**候选⑧催化依据（公告事实 / LLM 判定）**：", ""])
                for nm, b in basis_items[:10]:
                    lines.append(f"- {nm}：{b}")

            # 龙头候选子表（三维验证评分：身位/梯队/强度/逻辑/情绪，S/A/B 级 + K3 系统判定）
            dragons = hot.get("dragon_candidates", [])
            if dragons:
                lines.extend([
                    "",
                    "### 🐉 龙头候选（三维验证评分 + Kimi 深度推理判定）",
                    "",
                ])
                primary = hot.get("dragon_primary")
                if primary:
                    p_name = next((r.get("name", "") for r in dragons if r["symbol"] == primary), "")
                    comment = hot.get("dragon_market_comment", "")
                    lines.append(f"**本期系统认定龙头：{p_name}（{primary}）**"
                                 + (f" —— {comment}" if comment else ""))
                    lines.append("")
                if hot.get("dragon_note"):
                    lines.extend([f"_{hot['dragon_note']}_", ""])
                lines.extend([
                    "| 代码 | 名称 | 板块 | 连板 | 等级 | 总分 | 系统判定 | 身位 | 梯队 | 强度 | 逻辑 | 情绪 |",
                    "|------|------|------|------|------|------|----------|------|------|------|------|------|",
                ])
                for r in dragons:
                    d = r.get("dragon_dims", {})
                    verdict = r.get("verdict", "-")
                    conf = r.get("confidence")
                    verdict_text = f"{verdict} {conf}%" if conf is not None else verdict
                    lines.append(
                        f"| {r['symbol']} | {r.get('name', '')} | {r.get('sector', '')} "
                        f"| {r.get('lbc', 0)} | **{r.get('dragon_grade', '')}** "
                        f"| {r.get('dragon_score', 0)} | {verdict_text} | {d.get('身位', '-')} "
                        f"| {d.get('梯队', '-')} | {d.get('强度', '-')} | {d.get('逻辑', '-')} | {d.get('情绪', '-')} |"
                    )
                reason_items = [
                    (r.get("name") or r["symbol"], r.get("verdict", ""), r.get("reasoning", ""),
                     r.get("risk", ""), str(r.get("reason", "") or ""))
                    for r in dragons[:5] if r.get("reasoning")
                ]
                if reason_items:
                    lines.extend(["", "**系统判定理由（Kimi 推理，引用数据）**：", ""])
                    for nm, verdict, reasoning, risk, reason in reason_items:
                        reason_part = f"归因「{reason}」；" if reason else ""
                        risk_part = f"；风险：{risk}" if risk else ""
                        lines.append(f"- {nm}（{verdict}）：{reason_part}{reasoning}{risk_part}")

                # 个股证据链（Top N 联网深挖：引爆点/推导链/正反证据/关联个股）
                evidence_map = hot.get("evidence_chains", {}) or {}
                if evidence_map:
                    lines.extend(["", "#### 🔍 个股证据链（联网深挖）", ""])
                    for sym, ev in list(evidence_map.items())[:5]:
                        nm = next((r.get("name", "") for r in dragons if r["symbol"] == sym), sym)
                        lines.append(f"**{nm}（{sym}）｜行业地位：{ev.get('industry_position', '未知')}**")
                        lines.append("")
                        lines.append(f"- 🔥 引爆点：{ev.get('ignition', '')}")
                        lines.append(f"- 🔗 推导链：{ev.get('chain', '')}")
                        for p in (ev.get("positives") or [])[:3]:
                            lines.append(f"- ✅ {p}")
                        for n in (ev.get("negatives") or [])[:3]:
                            lines.append(f"- ⚠️ {n}")
                        for rel in (ev.get("relations") or []):
                            lines.append(f"- 🔀 关联：{rel.get('symbol_name', '')}"
                                         f"（{rel.get('relation', '')}）{rel.get('note', '')}")
                        lines.append("")
                lines.extend([
                    "",
                    "> 龙头评分口径：身位（板块最高板 30 / 首板封板前 3 得 20 / 跟风 8）+ 梯队（板块涨停家数与层级）"
                    "+ 强度（换手率/封单/炸板次数，一字板降档）+ 逻辑（⑧催化）+ 情绪（大盘涨停家数）；"
                    "系统判定由 Kimi 推理模型综合上述事实给出（无 Key 或调用失败时降级为纯量化评分排序并标注）。",
                ])
            lines.extend([
                "",
                "> **口径说明**：突破幅度% =（收盘价 − 20 日通道高点）/ 通道高点 ×100，即收盘越过前 20 日最高价的幅度；",
                "> ATR(20) = 20 日平均真实波幅（该股一天的平均波动金额），用于设止损：建议止损 = 收盘价 − ATR × 倍数。",
                "> **推荐分析**：按买入规则 8 条核对——①板块涨幅Top5 ②板块涨停家数增加 ③前排（连板/领涨）④放量突破 ⑦大盘环境 由系统自动判定；",
                "> ⑧事件/政策/业绩/技术突破/转型（逻辑与持续性，最重要）由系统读取近期公告自动判定（仅前 N 只候选；无 LLM Key 或未取得公告正文时降级为「人工核对」，上方列出公告标题事实）；",
                "> ⑤次日无高开低走、⑥分时均价线承接 需次日/盘中观察。满足 4 条以上才允许买入；候选按规则满足条数排序。",
            ])
        lines.append("")

    lines.extend([
        "",
        "---",
        "",
        "## 七、使用说明",
        "",
        "1. 20日突破对应 **S1-A 快速系统**，10日通道退出",
        "2. 55日突破对应 **S2-A 慢速系统**，20日通道退出",
        "3. 入场前请结合板块强度和市场状态调整仓位",
        "4. 使用 `position_calculator.py` 计算具体股数",
        "5. 超短热点池为 1-5 天交易候选来源（详见 vault《超短操作手册》），非买入指令",
        "6. **滤网列**：三重滤网通过数（周线趋势 / 板块强度前20% / 量能确认，S2-A 另加 MA20>MA60）；"
        "未全通过者按体系只可观察或极小仓测试。备注列含「冷却中」「首仓建议降50%」等系统1过滤附注（V5.0 §4.3）",
        "7. **明日操作计划**（第二节）：滤网全过候选自动带参考买入价/建议止损/建议股数/闸门预检结论，"
        "持仓警报逐条转行动；清单是候选与参数，不是自动买入指令",
        "",
        "---",
        "_本报告由量化系统自动生成，仅供参考，不构成投资建议_",
    ])

    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    path = run_scan()
    print(f"报告路径: {path}")
