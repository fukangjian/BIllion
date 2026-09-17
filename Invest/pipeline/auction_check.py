"""
9:25 集合竞价判定 — 候选竞价分级 + 持仓竞价风控（《二板打法》竞价标准 + 联网调研共识口径）

竞价是短线「70% 的决策点」：本模块把静态核对清单变成自动判定——
- 二板观察池：S/A/B/C 竞价等级（高开幅度 × 竞昨比 × 板块共振，vault《二板打法》原文口径）；
- 龙头候选（HOT-S）：执行/观察/不追/剔除（高开 3-7% 放量执行、一字/过高不追、低开剔除）；
- 持仓股：竞价低开 ≤-2% 或竞价跌破止损 → 开盘处置警报（风控前置）。

口径说明（联网调研 2026-08-24，见 ARCHITECTURE.md）：
- 竞昨比 = 竞价成交量 ÷ 昨日全天成交量；合格线 5%、优秀线 10%；
- 快照取数时点 9:26（9:25-9:30 休市无连续成交），东财全市场快照「今开=竞价撮合价、
  成交量≈竞价量」，一次调用覆盖全部候选；东财失败降级新浪快照（新浪成交量单位为股，÷100 换手）。
  **竞昨比仅在 9:26-9:31 窗口准确**（快照量≈竞价量）；窗口外快照量为当日累计量，竞昨比偏大
  仅供参考（竞价涨幅任何时刻准确，分级以涨幅为主），结果带 vol_in_window 标记与备注；
- 9:20-9:25 不可撤单段走向（盘前分时）：量增价推=抢筹、量缩价落=撤单，仅 Top N 逐股请求。

输出 output/market_scans/auction_check_YYYY-MM-DD.{json,md}；全部数据源失败时
available=False 降级标注，不抛异常（优雅降级约定）。只输出判定与建议，不自动交易。

用法:
    python pipeline/auction_check.py            # 立即跑一次竞价判定并打印结果
"""
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import logging

import pandas as pd

from config import (
    AUCTION_CHECK_ENABLED,
    AUCTION_HOT_EXEC_OPEN,
    AUCTION_POS_LOW_OPEN_ALERT,
    AUCTION_PRE_MIN_MAX,
    AUCTION_SB_A_OPEN,
    AUCTION_SB_A_VOL,
    AUCTION_SB_S_OPEN,
    AUCTION_SB_S_VOL,
    AUCTION_VOL_RATIO_GOOD,
    MARKET_SCAN_OUTPUT_DIR,
)
from pipeline.second_board import limit_up_pct

logger = logging.getLogger(__name__)


# ---------- 纯函数：分级判定 ----------

def _tight_params(phase: Optional[str]) -> Optional[dict]:
    """情绪退潮/冰点相位下的竞价收紧口径（None = 默认口径）"""
    try:
        from pipeline.sentiment_regime import tightened_auction_params

        return tightened_auction_params(phase)
    except Exception:
        return None


def grade_second_board(open_pct: float, vol_ratio: float, sector_resonance: bool,
                       phase: Optional[str] = None) -> tuple[str, str]:
    """
    二板竞价 S/A/B/C 分级（《二板打法》原文口径）。
    open_pct：竞价高开幅度 %（今开/昨收-1）；vol_ratio：竞昨比 %；sector_resonance：同板块有一字板或高开 ≥15%。
    phase：情绪周期相位（退潮/冰点时收紧量能口径：S 级竞昨比 8%→10%、A 级 5%→8%）。
    """
    tight = _tight_params(phase)
    s_vol = tight["sb_s_vol"] if tight else AUCTION_SB_S_VOL
    a_vol = tight["vol_ratio_good"] if tight else AUCTION_SB_A_VOL
    tight_note = "（情绪收紧）" if tight else ""
    if open_pct <= 0:
        return "C", "低开/平开 → 坚决放弃（低开=核按钮）"
    if open_pct > AUCTION_SB_S_OPEN[1]:
        return "B", f"高开 {open_pct:.1f}% 透支空间 → 不追，等盘中回封，不回封不看"
    if AUCTION_SB_S_OPEN[0] <= open_pct and vol_ratio >= s_vol and sector_resonance:
        return "S", (f"高开 {open_pct:.1f}% + 竞昨比 {vol_ratio:.1f}%≥{s_vol:.0f}%{tight_note} + 板块共振"
                     " → 9:24:50 挂涨停价抢筹")
    if open_pct >= AUCTION_SB_A_OPEN[0] and vol_ratio >= a_vol:
        reason = "板块无共振降级" if AUCTION_SB_S_OPEN[0] <= open_pct and vol_ratio >= s_vol else ""
        return "A", (f"高开 {open_pct:.1f}% + 竞昨比 {vol_ratio:.1f}%≥{a_vol:.0f}%{tight_note}"
                     f"{('（' + reason + '）') if reason else ''} → 9:30 后观察，秒板则排板")
    return "B", f"高开 {open_pct:.1f}% / 竞昨比 {vol_ratio:.1f}% 量不足（需 ≥{a_vol:.0f}%{tight_note}） → 放弃主动买，等回封，不回封不看"


def grade_hot_candidate(open_pct: float, vol_ratio: float, is_yizi: bool,
                        phase: Optional[str] = None) -> tuple[str, str]:
    """
    龙头 HOT-S 竞价判定：一字/过高不追、低开剔除、高开 3-7% 放量执行、其余观察。
    phase：情绪退潮/冰点时收紧（执行高开区间 3-7%→4-6%、竞昨比合格线 5%→8%）。
    """
    tight = _tight_params(phase)
    exec_open = tight["hot_exec_open"] if tight else AUCTION_HOT_EXEC_OPEN
    vol_good = tight["vol_ratio_good"] if tight else AUCTION_VOL_RATIO_GOOD
    if is_yizi:
        return "不追", "一字板 → 不追（看同板块龙二）"
    if open_pct <= 0:
        return "剔除", f"低开 {open_pct:.1f}% → 不及预期，剔除"
    if open_pct > exec_open[1]:
        return "不追", f"高开 {open_pct:.1f}% 过大（>{exec_open[1]:.0f}%） → 防高开低走，不追"
    if open_pct >= exec_open[0] and vol_ratio >= vol_good:
        tight_note = "；情绪收紧口径" if tight else ""
        return "执行", (f"高开 {open_pct:.1f}% 且竞昨比 {vol_ratio:.1f}%≥{vol_good:.0f}% 放量"
                        f" → 超预期，按计划执行{tight_note}")
    return "观察", (f"高开 {open_pct:.1f}% / 竞昨比 {vol_ratio:.1f}%"
                    f" → 未达执行口径（需高开 {exec_open[0]:.0f}-{exec_open[1]:.0f}% 且竞昨比 ≥{vol_good:.0f}%），观察")


def grade_position(open_pct: float, auction_price: float, stop: float,
                   phase: Optional[str] = None) -> Optional[str]:
    """持仓竞价风控：竞价跌破止损 → 开盘执行止损；低开 ≤阈值 → 盯防警报；否则 None。
    退潮/冰点相位低开警报线收紧（-2% → -1%）。"""
    if stop > 0 and auction_price > 0 and auction_price < stop:
        return f"竞价 {auction_price:.2f} 跌破止损 {stop:.2f} → 开盘即执行止损，不犹豫"
    alert_line = AUCTION_POS_LOW_OPEN_ALERT
    tight = _tight_params(phase)
    if tight:
        alert_line = tight["pos_low_open_alert"]
    if open_pct <= alert_line:
        tight_note = "（情绪收紧）" if tight else ""
        return f"竞价低开 {open_pct:.1f}% ≤{alert_line:.0f}%{tight_note} → 开盘重点盯防，反弹无力按纪律减仓"
    return None


def auction_metrics(spot: dict, yesterday_volume: float) -> tuple[float, float, bool]:
    """竞价涨幅% / 竞昨比% / 一字板判定（开盘=涨停价）。spot 含 open/prev_close/volume"""
    prev_close = float(spot.get("prev_close") or 0)
    open_price = float(spot.get("open") or 0)
    if prev_close <= 0 or open_price <= 0:
        return 0.0, 0.0, False
    open_pct = round((open_price / prev_close - 1) * 100, 2)
    vol_ratio = round(float(spot.get("volume") or 0) / yesterday_volume * 100, 2) if yesterday_volume > 0 else 0.0
    limit_price = round(prev_close * (1 + limit_up_pct(str(spot.get("symbol", "")))), 2)
    is_yizi = abs(open_price - limit_price) < 0.005
    return open_pct, vol_ratio, is_yizi


def pre_min_trend_from_df(df: pd.DataFrame) -> Optional[dict]:
    """
    盘前分时 DataFrame → 9:20→9:25 走向（纯函数）：
    末段价格变化 >+0.5% 且末段量占比 ≥40% → 抢筹；末段价格变化 <-0.5% → 撤单；否则平淡。
    """
    if df is None or df.empty:
        return None
    cols = {c: str(c) for c in df.columns}
    time_col = next((c for c, n in cols.items() if "时间" in n or "date" in n.lower()), None)
    price_col = next((c for c, n in cols.items() if "价" in n and "均" not in n), None)
    vol_col = next((c for c, n in cols.items() if "成交量" in n or n.lower() == "volume"), None)
    if not (time_col and price_col and vol_col):
        return None
    d = df.copy()
    d["_t"] = d[time_col].astype(str).str.extract(r"(\d{2}:\d{2})")[0]
    d = d.dropna(subset=["_t"]).sort_values("_t")
    if d.empty:
        return None
    price = pd.to_numeric(d[price_col], errors="coerce")
    vol = pd.to_numeric(d[vol_col], errors="coerce").fillna(0)
    late = d["_t"] >= "09:20"
    if not late.any() or late.sum() < 2:
        return None
    p_start, p_end = price[late].iloc[0], price[late].iloc[-1]
    if not p_start or p_start != p_start or p_start <= 0:
        return None
    price_chg = (p_end / p_start - 1) * 100
    total_vol = vol.sum()
    late_vol_pct = (vol[late].sum() / total_vol * 100) if total_vol > 0 else 0.0
    if price_chg > 0.5 and late_vol_pct >= 40:
        trend = "抢筹"
    elif price_chg < -0.5:
        trend = "撤单"
    else:
        trend = "平淡"
    return {"trend": trend, "price_chg": round(price_chg, 2), "late_vol_pct": round(late_vol_pct, 1)}


# ---------- 数据获取（网络，失败降级） ----------

def fetch_spot_snapshot() -> dict[str, dict]:
    """
    全市场快照 → {symbol: {symbol, name, open, prev_close, volume, amount}}（成交量单位统一为手）。
    东财 push2 主机，失败降级新浪（与 fetch_limit_stats 同链）；双源皆失败返回 {}。
    """
    try:
        import akshare as ak

        from shared.utils import retry_fetch

        df = retry_fetch(ak.stock_zh_a_spot_em)
        if df is not None and not df.empty:
            out = {}
            for _, r in df.iterrows():
                sym = str(r.get("代码", "")).zfill(6)[-6:]
                out[sym] = {
                    "symbol": sym, "name": str(r.get("名称", "")),
                    "open": _f(r.get("今开")), "prev_close": _f(r.get("昨收")),
                    "volume": _f(r.get("成交量")), "amount": _f(r.get("成交额")),
                }
            return out
    except Exception as e:
        logger.warning("东财快照获取失败（降级新浪）: %s", e)

    try:
        import akshare as ak

        from shared.utils import retry_fetch

        df = retry_fetch(ak.stock_zh_a_spot)
        if df is not None and not df.empty:
            out = {}
            for _, r in df.iterrows():
                raw = str(r.get("代码", ""))
                sym = raw.replace("sh", "").replace("sz", "").replace("bj", "").zfill(6)[-6:]
                out[sym] = {
                    "symbol": sym, "name": str(r.get("名称", "")),
                    "open": _f(r.get("今开")), "prev_close": _f(r.get("昨收")),
                    # 新浪成交量单位为股，÷100 换手与东财/日线口径对齐
                    "volume": _f(r.get("成交量")) / 100, "amount": _f(r.get("成交金额")),
                }
            return out
    except Exception as e:
        logger.warning("新浪快照获取失败: %s", e)
    return {}


def _f(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def fetch_pre_min_trend(symbol: str) -> Optional[dict]:
    """逐股盘前分时走向（网络；失败返回 None 降级）"""
    try:
        import akshare as ak

        from shared.utils import retry_fetch

        df = retry_fetch(ak.stock_zh_a_hist_pre_min_em, symbol=symbol,
                         start_time="09:15:00", end_time="09:25:00")
        return pre_min_trend_from_df(df)
    except Exception as e:
        logger.debug("盘前分时获取失败 %s（跳过）: %s", symbol, e)
        return None


# ---------- 编排 ----------

def _load_latest_scan_json() -> Optional[dict]:
    """读取最新 market_scan JSON（与 review/trade_ops 同口径，pipeline 层本地实现避免跨层依赖）"""
    if not MARKET_SCAN_OUTPUT_DIR.exists():
        return None
    files = sorted(MARKET_SCAN_OUTPUT_DIR.glob("market_scan_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    try:
        return json.loads(files[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("扫描 JSON 读取失败: %s", e)
        return None


def _yesterday_volume(symbol: str, db_path=None) -> float:
    """昨日全天成交量（手；daily_quotes 末行），缺失返回 0（竞昨比降级为 0）"""
    try:
        from pipeline.database import load_daily_quotes

        df = load_daily_quotes(symbol=symbol, db_path=db_path)
        if not df.empty:
            return _f(df.sort_values("trade_date")["volume"].iloc[-1])
    except Exception as e:
        logger.debug("昨日量读取失败 %s: %s", symbol, e)
    return 0.0


def build_watchlist(db_path=None, trade_log=None, scan: Optional[dict] = None) -> dict:
    """
    竞价观察名单：二板池全部候选 + 龙头候选前 5（页面口径）+ 未平仓持仓。
    返回 {"second_board": [...], "hot": [...], "positions": [...]}，各含 symbol/name/sector/close(/stop)。
    """
    scan = scan if scan is not None else _load_latest_scan_json() or {}
    sb = ((scan.get("hot_pool") or {}).get("second_board") or {}).get("candidates", []) or []
    dragons = ((scan.get("daily_plan") or {}).get("dragon_buys") or [])[:5]
    positions = []
    try:
        if trade_log is None:
            from review.trade_log import TradeLog

            trade_log = TradeLog()
        for t in trade_log.list_all(open_only=True):
            positions.append({"symbol": t.股票代码, "name": t.股票名称 or "",
                              "sector": "", "close": 0.0, "stop": t.止损价})
    except Exception as e:
        logger.warning("持仓读取失败（竞价风控降级跳过）: %s", e)
    return {
        "second_board": [{"symbol": str(c.get("symbol", "")).zfill(6)[-6:],
                          "name": str(c.get("name", "") or ""),
                          "sector": str(c.get("sector", "") or ""),
                          "close": _f(c.get("close"))} for c in sb],
        "hot": [{"symbol": str(b.get("symbol", "")).zfill(6)[-6:],
                 "name": str(b.get("name", "") or ""),
                 "sector": str(b.get("sector", "") or ""),
                 "close": _f(b.get("close"))} for b in dragons],
        "positions": positions,
    }


def run_auction_check(db_path=None, trade_log=None, scan: Optional[dict] = None,
                      snapshot: Optional[dict] = None) -> dict:
    """
    竞价判定编排：名单 → 快照 → 逐股分级 → Top N 分时走向 → 写 JSON+MD。
    snapshot 可注入（测试）；None 时实时获取。全部失败 available=False 降级，不抛异常。
    """
    now = datetime.now()
    result = {"available": False, "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"),
              "second_board": [], "hot": [], "positions": [], "note": ""}
    if not AUCTION_CHECK_ENABLED:
        result["note"] = "竞价判定已禁用（AUCTION_CHECK_ENABLED=false）"
        return result

    watch = build_watchlist(db_path=db_path, trade_log=trade_log, scan=scan)
    if not any(watch.values()):
        result["note"] = "无竞价观察对象（需先运行盘前流程生成候选，或有未平仓持仓）"
        return result

    # 情绪周期相位：优先取 scan JSON，缺省回读 emotion_state 表；未知/缺失不收紧（默认口径）
    phase = ((scan or {}).get("sentiment") or {}).get("phase")
    phase_advice_text = ""
    try:
        from pipeline.sentiment_regime import get_latest_phase, is_banned, phase_advice

        if not phase or phase == "未知":
            row = get_latest_phase(db_path=db_path)
            phase = row.get("phase") if row else None
        if phase and phase != "未知":
            phase_advice_text = phase_advice(phase)
    except Exception as e:
        logger.debug("情绪相位读取失败（竞价按默认口径）: %s", e)
    result["sentiment"] = {"phase": phase or "未知", "advice": phase_advice_text}
    if phase in ("退潮", "冰点"):
        result["sentiment"]["tightened"] = True

    snapshot = snapshot if snapshot is not None else fetch_spot_snapshot()
    if not snapshot:
        result["note"] = "全市场快照获取失败（东财/新浪双源降级）；非交易时段或网络问题"
        return result

    # 板块共振：同板块内有竞价一字板或高开 ≥15%（S 级要件，《二板打法》）
    metrics_cache: dict[str, tuple] = {}

    def _metrics(sym: str) -> tuple[float, float, bool]:
        if sym not in metrics_cache:
            spot = snapshot.get(sym)
            if not spot:
                metrics_cache[sym] = (0.0, 0.0, False)
            else:
                metrics_cache[sym] = auction_metrics(spot, _yesterday_volume(sym, db_path))
        return metrics_cache[sym]

    all_symbols = [w["symbol"] for grp in watch.values() for w in grp]
    resonance_sectors = set()
    for grp in ("second_board", "hot"):
        for w in watch[grp]:
            op, _, yizi = _metrics(w["symbol"])
            if w["sector"] and (yizi or op >= 15):
                resonance_sectors.add(w["sector"])

    missing = 0
    for w in watch["second_board"]:
        op, vr, yizi = _metrics(w["symbol"])
        if not snapshot.get(w["symbol"]):
            missing += 1
        grade, text = grade_second_board(op, vr, w["sector"] in resonance_sectors, phase)
        w.update(open_pct=op, vol_ratio=vr, is_yizi=yizi, grade=grade, text=text)
    for w in watch["hot"]:
        op, vr, yizi = _metrics(w["symbol"])
        if not snapshot.get(w["symbol"]):
            missing += 1
        verdict, text = grade_hot_candidate(op, vr, yizi, phase)
        w.update(open_pct=op, vol_ratio=vr, is_yizi=yizi, verdict=verdict, text=text)
    for w in watch["positions"]:
        spot = snapshot.get(w["symbol"])
        op, vr, _ = _metrics(w["symbol"])
        alert = grade_position(op, _f((spot or {}).get("open")), w.get("stop", 0), phase) if spot else "快照缺失，人工盯盘"
        w.update(open_pct=op, vol_ratio=vr, auction_price=_f((spot or {}).get("open")),
                 alert=alert)

    # 竞价分时走向（仅最相关 Top N：S/A 级二板 + 执行龙头，控制逐股请求量）
    focus = [w for w in watch["second_board"] if w["grade"] in ("S", "A")] + \
            [w for w in watch["hot"] if w["verdict"] == "执行"]
    for w in focus[:AUCTION_PRE_MIN_MAX]:
        trend = fetch_pre_min_trend(w["symbol"])
        if trend:
            w["pre_min"] = trend
            w["text"] += f"；9:20→9:25 {trend['trend']}（价 {trend['price_chg']:+.1f}%/量占 {trend['late_vol_pct']:.0f}%）"

    result.update(available=True, second_board=watch["second_board"],
                  hot=watch["hot"], positions=watch["positions"])
    notes = []
    if missing:
        notes.append(f"{missing}/{len(all_symbols)} 只快照缺失")
    if result["sentiment"].get("tightened"):
        notes.append(f"情绪{phase}：竞价口径收紧（S 竞昨比 10% / A 8% / 龙头执行 4-6% 且 8% / 低开警报 -1%）")
    pos_alerts = sum(1 for w in watch["positions"] if w.get("alert"))
    if pos_alerts:
        notes.append(f"持仓竞价警报 {pos_alerts} 条")
    # 竞昨比口径：仅 9:26-9:31 窗口快照量≈竞价量；窗口外为当日累计量，竞昨比偏大仅供参考
    in_window = "09:26" <= now.strftime("%H:%M") <= "09:31"
    result["vol_in_window"] = in_window
    if not in_window:
        notes.append("非 9:26-9:31 窗口运行，快照量为当日累计量，竞昨比偏大仅供参考（分级以竞价涨幅为准）")
    result["note"] = "；".join(notes)

    try:
        _write_outputs(result)
    except Exception as e:
        logger.warning("竞价判定结果落盘失败（不影响接口返回）: %s", e)
    return result


def _write_outputs(result: dict) -> None:
    """结构化双写：JSON（程序化消费）+ MD（人读）"""
    MARKET_SCAN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    day = result["date"]
    (MARKET_SCAN_OUTPUT_DIR / f"auction_check_{day}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"## ⏰ 9:25 竞价判定（{day} {result['time']}）", ""]
    senti = result.get("sentiment") or {}
    if senti.get("phase") and senti.get("phase") != "未知":
        tight_mark = "（口径已收紧）" if senti.get("tightened") else ""
        lines.append(f"> 🌡️ 情绪相位：**{senti['phase']}**{tight_mark}"
                     + (f" —— {senti.get('advice', '')}" if senti.get("advice") else ""))
        lines.append("")
    if result.get("second_board"):
        lines += ["### 🃏 二板竞价分级", "",
                  "| 代码 | 名称 | 板块 | 竞价涨幅% | 竞昨比% | 等级 | 操作 |",
                  "|------|------|------|-----------|---------|------|------|"]
        for w in result["second_board"]:
            lines.append(f"| {w['symbol']} | {w['name']} | {w['sector'] or '-'} | {w['open_pct']:.2f} "
                         f"| {w['vol_ratio']:.2f} | **{w['grade']}** | {w['text']} |")
        lines.append("")
    if result.get("hot"):
        lines += ["### 🐉 龙头竞价判定", "",
                  "| 代码 | 名称 | 板块 | 竞价涨幅% | 竞昨比% | 判定 | 说明 |",
                  "|------|------|------|-----------|---------|------|------|"]
        for w in result["hot"]:
            lines.append(f"| {w['symbol']} | {w['name']} | {w['sector'] or '-'} | {w['open_pct']:.2f} "
                         f"| {w['vol_ratio']:.2f} | **{w['verdict']}** | {w['text']} |")
        lines.append("")
    if result.get("positions"):
        lines += ["### ⚠️ 持仓竞价风控", "",
                  "| 代码 | 名称 | 竞价涨幅% | 竞昨比% | 警报 |",
                  "|------|------|-----------|---------|------|"]
        for w in result["positions"]:
            lines.append(f"| {w['symbol']} | {w['name']} | {w['open_pct']:.2f} | {w['vol_ratio']:.2f} "
                         f"| {w.get('alert') or '正常'} |")
        lines.append("")
    if result.get("note"):
        lines += [f"> 备注：{result['note']}", ""]
    (MARKET_SCAN_OUTPUT_DIR / f"auction_check_{day}.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    r = run_auction_check()
    if not r["available"]:
        print(r["note"] or "竞价判定不可用")
        return
    print(f"=== 9:25 竞价判定（{r['date']} {r['time']}）===")
    for w in r["second_board"]:
        print(f"🃏 {w['symbol']} {w['name']} [{w['grade']}] 高开 {w['open_pct']}% 竞昨比 {w['vol_ratio']}% → {w['text']}")
    for w in r["hot"]:
        print(f"🐉 {w['symbol']} {w['name']} [{w['verdict']}] 高开 {w['open_pct']}% 竞昨比 {w['vol_ratio']}% → {w['text']}")
    for w in r["positions"]:
        if w.get("alert"):
            print(f"⚠️ {w['symbol']} {w['name']} 低开 {w['open_pct']}% → {w['alert']}")
    if r["note"]:
        print("备注:", r["note"])


if __name__ == "__main__":
    main()
