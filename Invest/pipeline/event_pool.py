"""
事件驱动短线候选池（EVT-S，2-5 日）— 2026-09

定位（2026-04 基金报调研共识：打板生态机构化，Alpha 来自「选股逻辑 × 短线时机」而非执行速度）：
- 买点逻辑前置：在「事件催化确认 + 20 日通道初动」的点火阶段介入，不死守封板瞬间；
- 排序因子：事件分（catalyst_analyzer.event_factor：满足基础25 + 力度0-40 + 时效0-25 + 波次0-10）
  × 量能确认（当日量/前20日均量 ≥ EVT_VOLUME_RATIO_MIN）× 突破幅度 tie-break；
- 一字板不入池（实盘买不到，与信号统计 by_entry_type 口径一致）；
- 影子验证起步：候选入 signals 表（system=EVT-S，5 日强制结算），计划中展示参数；
  按策略评估框架 ≥30 笔样本达标后再实盘（闸门/仓位全部走事件账户既有口径）。

数据链：market_scanner 热点池突破候选（含 catalyst 判定 dict）→ 本模块纯排序过滤 →
scan JSON hot_pool.event_candidates → daily_plan event_buys + signals 表 EVT-S。
全程优雅降级：无催化数据 → 空列表，不阻塞扫描。

用法:
    python pipeline/event_pool.py            # 读最新 scan JSON 离线重建事件候选（调试用）
"""
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging

import pandas as pd

from config import (
    EVT_MAX_CANDIDATES,
    EVT_MIN_EVENT_SCORE,
    EVT_VOLUME_RATIO_MIN,
)

logger = logging.getLogger(__name__)


def _volume_ratio(df: Optional[pd.DataFrame], days: int = 20) -> Optional[float]:
    """当日成交量 / 前 N 日均量（不含当日）；数据不足返回 None（与 market_scanner 同口径）"""
    if df is None or df.empty or "volume" not in df.columns:
        return None
    vol = pd.to_numeric(df["volume"], errors="coerce").dropna()
    if len(vol) < days + 1:
        return None
    base = vol.iloc[-days - 1:-1].mean()
    if not base or base <= 0:
        return None
    return float(vol.iloc[-1] / base)


def build_event_candidates(
    hot_records: list[dict],
    symbols_data: Optional[dict[str, pd.DataFrame]] = None,
    today: str = "",
) -> list[dict]:
    """
    热点池突破候选 → EVT-S 事件驱动候选（纯函数，可离线单测）。

    hot_records：market_scanner 热点池突破候选（含 catalyst dict：satisfied/event_date/strength/wave）；
    symbols_data：symbol → 日线 DataFrame（量能确认；缺失降级为不判定，量能项不拦人）。
    返回按（事件分, 突破幅度）降序的前 EVT_MAX_CANDIDATES 条；无合格候选返回 []。
    """
    from research.catalyst_analyzer import event_factor

    symbols_data = symbols_data or {}
    out: list[dict] = []
    for rec in hot_records or []:
        try:
            symbol = str(rec.get("symbol", "")).zfill(6)[-6:]
            cat = rec.get("catalyst") or {}
            ef = event_factor(cat, today)
            if ef["score"] < EVT_MIN_EVENT_SCORE:
                continue

            vr = _volume_ratio(symbols_data.get(symbol))
            one_word = bool(rec.get("one_word_board", False))
            notes: list[str] = []
            record = True
            if one_word:
                notes.append("一字板：实盘难以买入，仅记录信号验证（不入实盘候选）")
                record = False
            if vr is not None and vr < EVT_VOLUME_RATIO_MIN:
                notes.append(f"量能不足（量比 {vr:.1f} < {EVT_VOLUME_RATIO_MIN}）")
            elif vr is not None:
                notes.append(f"量能确认（量比 {vr:.1f}）")

            out.append({
                "symbol": symbol,
                "name": str(rec.get("name", "") or ""),
                "sector": str(rec.get("sector", "") or ""),
                "close": round(float(rec.get("close", 0) or 0), 2),
                "channel_high": round(float(rec.get("channel_high", 0) or 0), 2),
                "breakout_pct": round(float(rec.get("breakout_pct", 0) or 0), 2),
                "atr_20": round(float(rec.get("atr_20", 0) or 0), 2),
                "period": int(rec.get("period", 20) or 20),
                "event_score": ef["score"],
                "event_breakdown": ef["breakdown"],
                "days_since": ef.get("days_since"),
                "catalyst_type": str(cat.get("catalyst_type", "") or ""),
                "sustainability": str(cat.get("sustainability", "") or ""),
                "event_date": str(cat.get("event_date", "") or ""),
                "strength": str(cat.get("strength", "") or ""),
                "wave": str(cat.get("wave", "") or ""),
                "basis": str(cat.get("basis", "") or ""),
                "volume_ratio": round(vr, 2) if vr is not None else None,
                "one_word_board": one_word,
                "record": record,
                "note": "；".join(notes),
            })
        except Exception as e:
            logger.warning("事件候选构建失败 %s（跳过该股）: %s", rec.get("symbol"), e)

    out.sort(key=lambda c: (-c["event_score"], -c["breakout_pct"]))
    return out[:EVT_MAX_CANDIDATES]


def build_from_latest_scan() -> list[dict]:
    """读最新 market_scan JSON 的事件候选（调试/复盘用；scan 未生成时返回空）"""
    try:
        from config import MARKET_SCAN_OUTPUT_DIR

        if not MARKET_SCAN_OUTPUT_DIR.exists():
            return []
        files = sorted(MARKET_SCAN_OUTPUT_DIR.glob("market_scan_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return []
        import json

        scan = json.loads(files[0].read_text(encoding="utf-8"))
        return (scan.get("hot_pool") or {}).get("event_candidates", []) or []
    except Exception as e:
        logger.warning("scan JSON 读取失败: %s", e)
        return []


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cands = build_from_latest_scan()
    if not cands:
        print("无事件驱动候选（需先运行盘前流程，或当日无达标催化）")
        return
    print(f"=== EVT-S 事件驱动候选（最新扫描，{len(cands)} 只）===")
    for c in cands:
        fresh = f"{c['days_since']}日前" if c.get("days_since") is not None else "日期未知"
        vr = f"量比 {c['volume_ratio']}" if c.get("volume_ratio") is not None else "量比未知"
        print(f"⚡ {c['symbol']} {c['name']}（{c['sector']}）事件分 {c['event_score']} | "
              f"{c['strength']}·{c['catalyst_type']}·{fresh}·{c['wave']} | {vr} | "
              f"收盘 {c['close']} 突破 +{c['breakout_pct']}% | {c['event_breakdown']}")
        if c.get("sustainability"):
            print(f"   持续性：{c['sustainability']}")
        if c.get("note"):
            print(f"   备注：{c['note']}")


if __name__ == "__main__":
    main()
