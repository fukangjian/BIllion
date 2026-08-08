"""
事件日历 — 主动探查未来 30 天关键事项（提前识别逻辑链的证据层）

两路证据：
1. 结构化事项（免 LLM，akshare）：财报预约披露日、限售解禁——确定性强、零成本；
2. 行业事件探查（Kimi K2.6 + $web_search 联网，每日一次、当日缓存）：
   未来 30 天候选板块的政策窗口/行业会议/数据发布/产品发布等。

输出供龙头深度推理引用（候选个股「未来事项」+ 板块事件），并在
明日操作计划 / UI 控制台展示「未来 30 天关键事项」。
无 LLM Key / 联网失败时降级为仅结构化事项并标注。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from config import DATA_DIR, KIMI_API_KEY, KIMI_BASE_URL, KIMI_MODEL_REASONING

logger = logging.getLogger(__name__)

EVENT_CACHE_DIR = DATA_DIR / "event_calendar"


# ---------- 结构化事项（akshare，免 LLM） ----------

def _current_report_period() -> str:
    """按当前月份推断财报披露期（预约表 period 参数）"""
    now = datetime.now()
    if now.month <= 4:
        return f"{now.year - 1}年报"
    if now.month <= 8:
        return f"{now.year}半年报"
    if now.month <= 10:
        return f"{now.year}三季报"
    return f"{now.year}年报"


def fetch_symbol_events(symbols: list[str], days: int = 30) -> dict[str, list[dict]]:
    """
    候选个股未来 N 天结构化事项：财报预约披露 + 限售解禁。
    返回 {symbol: [{date, type, title, source}]}；数据源失败逐股降级为空列表。
    """
    import akshare as ak
    import pandas as pd

    from shared.utils import bypass_proxy

    events: dict[str, list[dict]] = {}
    today = datetime.now().date()
    horizon = today + timedelta(days=days)

    # 财报预约披露（全市场一张表，本地过滤）
    schedule = {}
    try:
        with bypass_proxy():
            df = ak.stock_report_disclosure(market="沪深京", period=_current_report_period())
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                sym = str(row.get("股票代码", "")).zfill(6)[-6:]
                # 注意：pd.NaT 为真值，不能用 or 兜底（NaT or 首次预约 → NaT）
                d = row.get("实际披露")
                if pd.isna(d):
                    d = row.get("首次预约")
                if pd.notna(d):
                    schedule[sym] = str(pd.Timestamp(d).date())
    except Exception as e:
        logger.warning("财报预约表获取失败（降级跳过）: %s", e)

    for symbol in symbols:
        sym = str(symbol).zfill(6)[-6:]
        items: list[dict] = []
        if sym in schedule:
            d = schedule[sym]
            if today <= datetime.strptime(d, "%Y-%m-%d").date() <= horizon:
                items.append({"date": d, "type": "财报披露",
                              "title": f"定期报告预约披露（{_current_report_period()}）", "source": "交易所预约"})
        try:
            with bypass_proxy():
                rel = ak.stock_restricted_release_queue_em(symbol=sym)
            if rel is not None and not rel.empty:
                date_col = next((c for c in rel.columns if "日期" in str(c)), None)
                amt_col = next((c for c in rel.columns if "解禁市值" in str(c) or "解禁数量" in str(c)), None)
                for _, row in rel.iterrows():
                    if not date_col:
                        continue
                    d = str(row.get(date_col, ""))[:10]
                    try:
                        d_date = datetime.strptime(d, "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if today <= d_date <= horizon:
                        amt = f"，规模 {row[amt_col]}" if amt_col and pd.notna(row.get(amt_col)) else ""
                        items.append({"date": d, "type": "限售解禁",
                                      "title": f"限售股解禁{amt}", "source": "东财"})
        except Exception as e:
            logger.debug("解禁查询失败 %s（跳过）: %s", sym, e)
        if items:
            events[sym] = sorted(items, key=lambda x: x["date"])
    return events


# ---------- 行业事件探查（K2.6 + $web_search，当日缓存） ----------

_SECTOR_EVENTS_SYSTEM = """你是 A 股行业研究助理。利用联网搜索，回答指定板块未来 30 天的关键事项。
只输出 JSON，所有事项须来自搜索到的公开信息并附来源；搜不到就少说，不得编造。"""

_SECTOR_EVENTS_PROMPT = """今天是 {date}。请联网搜索以下 A 股板块未来 30 天（至 {end_date}）的重要事项：

板块：{sectors}

关注类型：政策/监管文件、行业会议与展会、重要数据发布（价格/销量/招标）、产品发布、
大型订单或合同窗口、业绩集中披露期。

输出严格 JSON（不要 markdown 代码块）：
{{
  "events": [
    {{"sector": "板块名（必须在给定列表内）", "date": "YYYY-MM-DD 或 区间描述",
      "type": "政策/会议/数据/产品/订单/业绩", "title": "事项一句话",
      "chain": "对板块内上市公司的传导逻辑一句话", "source": "来源名称"}}
  ]
}}
搜不到可靠信息的板块不要硬写；总数不超过 8 条。"""


def _call_kimi_with_web_search(prompt: str, system_prompt: str, max_rounds: int = 3) -> Optional[str]:
    """Kimi $web_search 内建工具多轮调用（服务端执行搜索），返回最终文本；超过 max_rounds 未收敛返回 None"""
    if not KIMI_API_KEY:
        return None
    from openai import OpenAI

    client = OpenAI(api_key=KIMI_API_KEY, base_url=KIMI_BASE_URL)
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "builtin_function", "function": {"name": "$web_search"}}]
    for _ in range(max_rounds):
        resp = client.chat.completions.create(
            model=KIMI_MODEL_REASONING,
            messages=messages,
            temperature=1.0,  # kimi-k2.6/k3 仅允许 1
            max_tokens=6000,
            tools=tools,
        )
        msg = resp.choices[0].message
        if not getattr(msg, "tool_calls", None):
            return msg.content or ""
        messages.append(msg)
        for tc in msg.tool_calls:
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "name": tc.function.name, "content": tc.function.arguments,
            })
    logger.warning("联网探查超过 %d 轮未收敛，放弃", max_rounds)
    return None


def _parse_sector_events(text: str | None, valid_sectors: set) -> list[dict]:
    """解析行业事件 JSON（护栏：板块名必须在候选集合内；逐条字段截断；相同事件去重为「多板块」）"""
    if not text:
        return []
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`").lstrip("jsonJSON").strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return []
    seen: dict[tuple, dict] = {}
    for e in data.get("events") or []:
        sec = str(e.get("sector", ""))
        title = str(e.get("title", ""))[:120]
        key = (str(e.get("type", "")), title)
        if key in seen:
            # 相同事件多板块重复（如半年报集中披露期）→ 合并为「多板块」
            seen[key]["sector"] = "多板块"
            continue
        if not sec or (valid_sectors and sec not in valid_sectors):
            continue
        seen[key] = {
            "sector": sec,
            "date": str(e.get("date", ""))[:30],
            "type": str(e.get("type", ""))[:10],
            "title": title,
            "chain": str(e.get("chain", ""))[:120],
            "source": str(e.get("source", ""))[:40],
        }
    return list(seen.values())[:8]


def explore_sector_events(sectors: list[str], trade_date: Optional[str] = None,
                          use_cache: bool = True) -> list[dict]:
    """K2.6 联网探查板块未来 30 天关键事项；当日缓存（data/event_calendar/），失败降级空"""
    if not sectors:
        return []
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    EVENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = EVENT_CACHE_DIR / f"sector_events_{trade_date}.json"
    if use_cache and cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    start = datetime.strptime(trade_date, "%Y-%m-%d")
    prompt = _SECTOR_EVENTS_PROMPT.format(
        date=trade_date,
        end_date=(start + timedelta(days=30)).strftime("%Y-%m-%d"),
        sectors="、".join(sectors[:6]),
    )
    try:
        text = _call_kimi_with_web_search(prompt, _SECTOR_EVENTS_SYSTEM)
    except Exception as e:
        logger.warning("行业事件联网探查失败（降级）: %s", e)
        text = None
    events = _parse_sector_events(text, set(sectors))
    if events:
        cache_file.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    return events


# ---------- 汇总 ----------

def build_event_calendar(
    symbols: list[str],
    sectors: list[str],
    trade_date: Optional[str] = None,
    days: int = 30,
    use_llm: bool = True,
) -> dict:
    """
    汇总事件日历：{symbol_events: {symbol: [...]}, sector_events: [...], llm_available: bool}
    use_llm=False 或无 Key 时只出结构化事项（sector_events 为空）。
    """
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    symbol_events: dict[str, list[dict]] = {}
    if symbols:
        try:
            symbol_events = fetch_symbol_events(symbols, days=days)
        except Exception as e:
            logger.warning("结构化事项获取失败（降级）: %s", e)

    sector_events: list[dict] = []
    llm_available = bool(KIMI_API_KEY) and use_llm
    if llm_available and sectors:
        sector_events = explore_sector_events(sectors, trade_date)

    return {
        "date": trade_date,
        "days": days,
        "symbol_events": symbol_events,
        "sector_events": sector_events,
        "llm_available": llm_available,
    }
