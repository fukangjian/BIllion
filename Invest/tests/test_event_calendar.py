"""
事件日历测试 — mock akshare 与 Kimi 联网调用，不触网
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import research.event_calendar as ec


def _future(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


class TestFetchSymbolEvents:
    def test_earnings_schedule_included(self, monkeypatch):
        """财报预约在未来 30 天内 → 入事件；过期的不入"""
        monkeypatch.setattr("akshare.stock_report_disclosure", lambda **kw: pd.DataFrame([
            {"股票代码": "600721", "股票简称": "百花医药", "首次预约": pd.Timestamp(_future(10)),
             "初次变更": pd.NaT, "二次变更": pd.NaT, "三次变更": pd.NaT, "实际披露": pd.NaT},  # NaT 为真值，or 兜底会失效（回归）
            {"股票代码": "600519", "股票简称": "贵州茅台", "首次预约": pd.Timestamp(_future(60)),
             "初次变更": pd.NaT, "二次变更": pd.NaT, "三次变更": pd.NaT, "实际披露": pd.NaT},
        ]))
        monkeypatch.setattr("akshare.stock_restricted_release_queue_em",
                            lambda symbol: pd.DataFrame())
        out = ec.fetch_symbol_events(["600721", "600519"], days=30)
        assert "600721" in out
        assert out["600721"][0]["type"] == "财报披露"
        assert "600519" not in out  # 60 天后超出窗口

    def test_fetch_failure_degrades(self, monkeypatch):
        monkeypatch.setattr("akshare.stock_report_disclosure",
                            lambda **kw: (_ for _ in ()).throw(ConnectionError("reset")))
        monkeypatch.setattr("akshare.stock_restricted_release_queue_em",
                            lambda symbol: pd.DataFrame())
        assert ec.fetch_symbol_events(["600721"]) == {}


class TestSectorEvents:
    def test_parse_with_guards(self):
        text = ('{"events": ['
                '{"sector": "通信设备", "date": "2026-08-20", "type": "会议", "title": "光通信大会", '
                '"chain": "光模块订单预期", "source": "新闻"},'
                '{"sector": "不在集合", "date": "x", "type": "会议", "title": "y", "chain": "", "source": ""}'
                ']}')
        out = ec._parse_sector_events(text, {"通信设备"})
        assert len(out) == 1
        assert out[0]["sector"] == "通信设备"

    def test_parse_garbage_empty(self):
        assert ec._parse_sector_events("不是 JSON", {"通信设备"}) == []
        assert ec._parse_sector_events(None, set()) == []

    def test_day_cache_reuse(self, tmp_path, monkeypatch):
        """当日缓存存在时不调用 LLM"""
        date = "2026-08-07"
        cache_dir = tmp_path / "event_calendar"
        cache_dir.mkdir()
        (cache_dir / f"sector_events_{date}.json").write_text(
            json.dumps([{"sector": "通信设备", "date": "2026-08-20", "type": "会议",
                         "title": " cached", "chain": "", "source": ""}]), encoding="utf-8")
        monkeypatch.setattr(ec, "EVENT_CACHE_DIR", cache_dir)
        monkeypatch.setattr(ec, "_call_kimi_with_web_search",
                            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应调用 LLM")))
        out = ec.explore_sector_events(["通信设备"], trade_date=date)
        assert out[0]["title"] == " cached"

    def test_no_llm_only_structured(self):
        cal = ec.build_event_calendar([], ["通信设备"], trade_date="2026-08-07", use_llm=False)
        assert cal["sector_events"] == []
        assert cal["llm_available"] is False


class TestPayloadInjection:
    def test_calendar_injected(self):
        from research.dragon_reasoner import build_reasoning_payload

        c = {
            "symbol": "600721", "name": "百花医药", "sector": "医疗服务", "lbc": 4,
            "turnover": 15.6, "seal_amount": 1.6e8, "fbt": "093100", "zbc": 0,
            "dragon_score": 80, "dragon_dims": {"身位": 30, "梯队": 20, "强度": 18, "逻辑": 12, "情绪": 10},
            "dragon_notes": [], "catalyst": None, "reason": "CRO+创新药",
        }
        calendar = {
            "symbol_events": {"600721": [{"date": _future(10), "type": "财报披露", "title": "半年报"}]},
            "sector_events": [{"sector": "医疗服务", "date": "2026-08-29", "type": "政策",
                               "title": "医疗卫生强基工程", "chain": "基层医疗扩容", "source": "国常会"}],
        }
        p = build_reasoning_payload([c], {"sectors": {}, "emotion": {}}, "C", calendar=calendar)
        assert "医疗卫生强基工程" in p["event_lines"]
        assert "未来事项" in p["candidate_lines"]
        assert "财报披露" in p["candidate_lines"]
