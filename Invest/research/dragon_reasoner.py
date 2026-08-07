"""
龙头深度推理 — Kimi K3 多维信息综合研判（系统自主辨龙头）

量化五维评分（pipeline/dragon_head）给出候选与分数后，本模块把候选的结构化事实
（五维明细、连板/封板/换手/封单/炸板、⑧催化结论与公告标题）与上下文（板块梯队、
大盘情绪、市场状态）一次性交给 Kimi K3 做综合推理，产出系统判定：
真龙头/疑似龙头/跟风/伪龙头 + 置信度 + 引用数据的判定理由 + 本期直选龙头（primary）。

防编造护栏（与公告分析同哲学）：symbol 必须在候选集内、verdict 必须枚举值、
confidence 截断 0-100；无 Key / 调用失败 / 解析失败一律返回 None，
调用方降级为「未经深度推理，按量化评分排序」并标注。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import logging

from config import DRAGON_REASON_ENABLED, DRAGON_REASON_MAX
from shared.llm_client import call_llm, has_llm_api_key
from shared.prompts import DRAGON_REASONING, DRAGON_REASONING_SYSTEM

logger = logging.getLogger(__name__)

VERDICTS = ("真龙头", "疑似龙头", "跟风", "伪龙头")
VERDICT_PRIORITY = {"真龙头": 0, "疑似龙头": 1, "跟风": 2, "伪龙头": 3}


def build_reasoning_payload(candidates: list[dict], ctx: dict, market_state: str = "",
                            calendar: dict | None = None) -> dict:
    """把候选与上下文整理成 prompt 占位内容（只含事实，供 LLM 引用说理）"""
    emotion = ctx.get("emotion", {})
    sectors = ctx.get("sectors", {})
    sector_sorted = sorted(sectors.items(), key=lambda kv: -kv[1].get("count", 0))[:10]
    sector_lines = [
        f"- {name}: 涨停 {s.get('count', 0)} 家 / 最高 {s.get('max_lbc', 0)} 板"
        for name, s in sector_sorted
    ] or ["- 无板块梯队数据"]

    # 未来 30 天关键事项（事件日历：结构化事项 + 联网探查行业事件）
    symbol_events = (calendar or {}).get("symbol_events", {})
    sector_events = (calendar or {}).get("sector_events", [])
    event_lines = [
        f"- {e.get('sector', '')}｜{e.get('date', '')}｜{e.get('type', '')}｜{e.get('title', '')}"
        f"（传导：{e.get('chain', '')}；来源：{e.get('source', '')}）"
        for e in sector_events
    ] or ["- 无（未启用联网探查或无可靠事项）"]

    candidate_lines = []
    for c in candidates:
        dims = c.get("dragon_dims", {})
        notes = "；".join(c.get("dragon_notes", []))[:150]
        cat = c.get("catalyst") or {}
        if cat.get("satisfied") is True:
            cat_text = f"满足（{cat.get('catalyst_type', '')}）：{str(cat.get('basis', ''))[:120]}"
        elif cat.get("satisfied") is False:
            cat_text = "不满足"
        else:
            cat_text = "未判定（待人工核对）"
        titles = "、".join((c.get("catalyst_titles") or [])[:2])[:100]
        reason = str(c.get("reason", "") or "").strip()
        reason_text = f"；涨停归因：{reason}" if reason and reason not in ("其他", "未知", "-") else ""
        sym_events = symbol_events.get(str(c.get("symbol", "")).zfill(6)[-6:], [])
        events_text = ("；未来事项：" + "、".join(
            f"{e['date']}{e['type']}" for e in sym_events[:3])) if sym_events else ""
        candidate_lines.append(
            f"- {c['symbol']} {c.get('name', '')}（{c.get('sector', '')}，{c.get('lbc', 0)} 连板）："
            f"五维 身位{dims.get('身位', '-')}/梯队{dims.get('梯队', '-')}/强度{dims.get('强度', '-')}"
            f"/逻辑{dims.get('逻辑', '-')}/情绪{dims.get('情绪', '-')}（总分 {c.get('dragon_score', '-') }）；"
            f"换手率 {c.get('turnover', '-') }%，封单 {round(float(c.get('seal_amount', 0) or 0) / 1e8, 2)} 亿，"
            f"首次封板 {c.get('fbt') or '-'}，炸板 {c.get('zbc', 0)} 次；"
            f"评分依据：{notes}；⑧催化：{cat_text}{reason_text}{events_text}"
            f"{('；公告：' + titles) if titles else ''}"
        )

    return {
        "market_state": market_state or "未知",
        "limit_up_count": emotion.get("limit_up_count", 0),
        "up_count": emotion.get("up_count", 0),
        "down_count": emotion.get("down_count", 0),
        "sector_lines": "\n".join(sector_lines),
        "event_lines": "\n".join(event_lines),
        "candidate_lines": "\n".join(candidate_lines),
    }


def _parse_verdicts(text: str | None, valid_symbols: set, ctx_sectors: set | None = None) -> dict | None:
    """
    解析 LLM JSON 输出（防编造护栏）：未知 symbol 丢弃、verdict 枚举校验、
    confidence 截断 0-100、primary 必须在候选集内、sector_focus 板块名须在梯队上下文内
    且可持续性枚举校验。解析失败返回 None。
    """
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`").lstrip("jsonJSON").strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None

    verdicts = []
    for v in data.get("verdicts") or []:
        sym = str(v.get("symbol", "")).zfill(6)[-6:]
        verdict = str(v.get("verdict", ""))
        if sym not in valid_symbols or verdict not in VERDICTS:
            continue
        try:
            conf = max(0, min(100, int(float(v.get("confidence", 50)))))
        except (TypeError, ValueError):
            conf = 50
        verdicts.append({
            "symbol": sym,
            "verdict": verdict,
            "confidence": conf,
            "reasoning": str(v.get("reasoning", ""))[:300],
            "risk": str(v.get("risk", ""))[:120],
        })

    primary = str(data.get("primary", "")).zfill(6)[-6:]
    if primary not in valid_symbols:
        primary = ""

    # 明日板块聚焦（架构A 顶层逻辑梳理）：板块名/持续性枚举护栏
    sector_focus = []
    valid_sectors = set(ctx_sectors or [])
    for s in data.get("sector_focus") or []:
        name = str(s.get("sector", ""))
        if not name or (valid_sectors and name not in valid_sectors):
            continue
        sustainability = str(s.get("sustainability", ""))
        if sustainability not in ("持续", "一日", "退潮"):
            sustainability = "一日"
        sector_focus.append({
            "sector": name,
            "sustainability": sustainability,
            "reason": str(s.get("reason", ""))[:150],
        })

    return {
        "primary": primary,
        "verdicts": verdicts,
        "sector_focus": sector_focus,
        "market_comment": str(data.get("market_comment", ""))[:200],
    }


def reason_dragons(
    candidates: list[dict],
    ctx: dict,
    market_state: str = "",
    calendar: dict | None = None,
) -> dict | None:
    """
    对龙头候选做 Kimi 深度推理（单次调用，走 llm_client 24h 缓存）。
    calendar 为事件日历（个股结构化事项 + 板块联网探查事件），注入推理证据。
    返回 {"primary", "verdicts", "sector_focus", "market_comment"}；
    未启用 / 无 Key / 调用失败 / 解析失败 → None（调用方降级按量化评分排序）。
    """
    if not DRAGON_REASON_ENABLED or not candidates:
        return None
    if not has_llm_api_key():
        logger.info("未配置 LLM Key，龙头深度推理跳过（按量化评分排序）")
        return None

    cands = candidates[:DRAGON_REASON_MAX]
    payload = build_reasoning_payload(cands, ctx, market_state, calendar=calendar)
    prompt = DRAGON_REASONING.format(**payload)
    text = call_llm(
        prompt,
        system_prompt=DRAGON_REASONING_SYSTEM,
        temperature=1.0,  # kimi-k3 仅允许 temperature=1（API 硬约束）
        max_tokens=8000,  # K3 推理输出较长，预留余量防截断
        task_type="reasoning",
    )
    valid = {str(c["symbol"]).zfill(6)[-6:] for c in cands}
    result = _parse_verdicts(text, valid, ctx_sectors=set((ctx.get("sectors") or {}).keys()))
    if result is None:
        logger.warning("龙头推理结果解析失败（降级量化评分排序）")
    return result
