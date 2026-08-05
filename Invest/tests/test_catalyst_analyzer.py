"""
research/catalyst_analyzer（买入规则⑧催化判定）与 market_scanner ⑧接入测试（零网络，全部 mock）
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import market_scanner as ms
from research import catalyst_analyzer as ca


def _ann_df(rows):
    return pd.DataFrame(rows, columns=["title", "date", "category", "url"])


SAMPLE_ANN = _ann_df([
    ["关于中标重大项目的公告", "2026-08-01", "重大合同", "http://x/1"],
    ["关于股东质押股份的公告", "2026-07-28", "质押", "http://x/2"],
    ["2026年半年度业绩预告", "2026-07-25", "业绩", "http://x/3"],
])


class TestParseVerdict:
    def test_satisfied(self):
        text = "判定：满足\n催化类型：业绩\n持续性：可持续\n依据：《业绩预告》(2026-07-25)"
        v = ca.parse_verdict(text)
        assert v["satisfied"] is True
        assert v["catalyst_type"] == "业绩"
        assert "可持续" in v["sustainability"]

    def test_not_satisfied(self):
        assert ca.parse_verdict("判定：不满足\n催化类型：无")["satisfied"] is False

    def test_garbage_returns_none(self):
        assert ca.parse_verdict("这是一段没有判定行的输出") is None
        assert ca.parse_verdict("") is None
        assert ca.parse_verdict(None) is None


class TestPickRelevant:
    def test_keyword_hit_first(self):
        picked = ca._pick_relevant_announcements(SAMPLE_ANN, limit=2)
        titles = picked["title"].tolist()
        assert "关于中标重大项目的公告" in titles
        assert len(picked) == 2

    def test_empty(self):
        assert ca._pick_relevant_announcements(pd.DataFrame()).empty


class TestAnalyzeCatalyst:
    def test_no_announcements(self, monkeypatch):
        monkeypatch.setattr("research.announcement_fetcher.fetch_latest_announcements",
                            lambda *a, **k: pd.DataFrame())
        res = ca.analyze_catalyst("600000")
        assert res["satisfied"] is None
        assert "无公告数据" in res["basis"]

    def test_no_llm_key_returns_titles_only(self, monkeypatch):
        monkeypatch.setattr("research.announcement_fetcher.fetch_latest_announcements",
                            lambda *a, **k: SAMPLE_ANN)
        monkeypatch.setattr("shared.llm_client.has_llm_api_key", lambda: False)
        res = ca.analyze_catalyst("600000", name="测试")
        assert res["satisfied"] is None
        assert len(res["titles"]) == 3
        assert "未配置 LLM" in res["basis"]

    def test_no_real_content_guard(self, monkeypatch):
        monkeypatch.setattr("research.announcement_fetcher.fetch_latest_announcements",
                            lambda *a, **k: SAMPLE_ANN)
        monkeypatch.setattr("shared.llm_client.has_llm_api_key", lambda: True)
        monkeypatch.setattr("research.announcement_fetcher.fetch_announcement_full_text",
                            lambda *a, **k: "（PDF 正文提取失败）")
        monkeypatch.setattr("research.announcement_fetcher.has_real_content", lambda t: False)
        called = []
        monkeypatch.setattr("shared.llm_client.call_llm",
                            lambda *a, **k: called.append(1) or "判定：满足")
        res = ca.analyze_catalyst("600000")
        assert res["satisfied"] is None
        assert "未取得公告正文" in res["basis"]
        assert not called  # 护栏：无正文禁止调用 LLM

    def test_llm_verdict_satisfied(self, monkeypatch):
        monkeypatch.setattr("research.announcement_fetcher.fetch_latest_announcements",
                            lambda *a, **k: SAMPLE_ANN)
        monkeypatch.setattr("shared.llm_client.has_llm_api_key", lambda: True)
        monkeypatch.setattr("research.announcement_fetcher.fetch_announcement_full_text",
                            lambda *a, **k: "公告正文内容" * 100)
        monkeypatch.setattr("research.announcement_fetcher.has_real_content", lambda t: True)
        monkeypatch.setattr("shared.llm_client.call_llm",
                            lambda *a, **k: "判定：满足\n催化类型：事件\n持续性：一次性\n依据：《中标公告》(2026-08-01)")
        res = ca.analyze_catalyst("600000")
        assert res["satisfied"] is True
        assert res["catalyst_type"] == "事件"
        assert res["titles"]

    def test_llm_failure_degrades(self, monkeypatch):
        monkeypatch.setattr("research.announcement_fetcher.fetch_latest_announcements",
                            lambda *a, **k: SAMPLE_ANN)
        monkeypatch.setattr("shared.llm_client.has_llm_api_key", lambda: True)
        monkeypatch.setattr("research.announcement_fetcher.fetch_announcement_full_text",
                            lambda *a, **k: "公告正文内容" * 100)
        monkeypatch.setattr("research.announcement_fetcher.has_real_content", lambda t: True)
        monkeypatch.setattr("shared.llm_client.call_llm", lambda *a, **k: None)
        res = ca.analyze_catalyst("600000")
        assert res["satisfied"] is None
        assert "需人工核对" in res["basis"]


def _base_eval_args():
    rec = {"symbol": "600000", "sector": "锂电池", "source": "连板"}
    info = pd.Series({"lbc": 2})
    return rec, info, None, {"锂电池"}, {"锂电池": 2}, {"锂电池": 1}, "A"


class TestEvaluateWithCatalyst:
    def test_catalyst_true_bumps_met(self):
        rec, info, df, top, today, prev, state = _base_eval_args()
        base = ms._evaluate_buy_rules(rec, info, df, top, today, prev, state)
        with_cat = ms._evaluate_buy_rules(rec, info, df, top, today, prev, state, catalyst={
            "satisfied": True, "catalyst_type": "业绩", "sustainability": "可持续", "basis": "b"})
        assert with_cat["met"] == base["met"] + 1
        assert with_cat["rules"][8] is True
        assert "⑧满足·业绩" in with_cat["text"]

    def test_catalyst_false(self):
        rec, info, df, top, today, prev, state = _base_eval_args()
        out = ms._evaluate_buy_rules(rec, info, df, top, today, prev, state,
                                     catalyst={"satisfied": False})
        assert out["rules"][8] is False
        assert "⑧无明确催化" in out["text"]

    def test_catalyst_none_keeps_manual(self):
        rec, info, df, top, today, prev, state = _base_eval_args()
        out = ms._evaluate_buy_rules(rec, info, df, top, today, prev, state,
                                     catalyst={"satisfied": None, "basis": "人工核对"})
        assert out["rules"][8] is None
        assert "⑧人工核对(最重要)" in out["text"]


class TestAnalyzeCatalystsSafe:
    def test_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ms, "HOT_CATALYST_ENABLED", False)
        assert ms._analyze_catalysts_safe([{"symbol": "600000"}]) == {}

    def test_exception_skipped(self, monkeypatch):
        monkeypatch.setattr(ms, "HOT_CATALYST_ENABLED", True)

        def _boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr("research.catalyst_analyzer.analyze_catalyst", _boom)
        assert ms._analyze_catalysts_safe([{"symbol": "600000"}]) == {}

    def test_map_filled(self, monkeypatch):
        monkeypatch.setattr(ms, "HOT_CATALYST_ENABLED", True)
        monkeypatch.setattr("research.catalyst_analyzer.analyze_catalyst",
                            lambda symbol, name="", sector="": {"satisfied": True})
        out = ms._analyze_catalysts_safe([{"symbol": "600000"}])
        assert out["600000"]["satisfied"] is True


class TestHotSectionCatalystIntegration:
    def _setup_mocks(self, monkeypatch, catalyst_map):
        today = "2026-07-31"
        limit_df = pd.DataFrame([
            {"trade_date": today, "symbol": "600000", "name": "测试A", "pool_type": "up",
             "change_pct": 10.0, "amount": 1e8, "lbc": 3, "sector": "锂电池"},
        ])
        pool_df = pd.DataFrame([
            {"trade_date": today, "symbol": "600000", "name": "测试A", "source": "连板",
             "sector": "锂电池", "change_pct": 10.0, "lbc": 3},
        ])
        monkeypatch.setattr(ms, "load_limit_pool", lambda trade_date=None: (
            limit_df[limit_df["trade_date"] == trade_date] if trade_date else limit_df))
        monkeypatch.setattr(ms, "load_hot_pool", lambda trade_date=None: pool_df)
        monkeypatch.setattr(ms, "load_daily_quotes",
                            lambda symbol=None: pd.DataFrame({"volume": [100.0] * 20 + [300.0]}))
        monkeypatch.setattr(ms, "scan_breakout_candidates", lambda data, period: pd.DataFrame([
            {"symbol": "600000", "close": 12.0, "channel_high": 11.5,
             "breakout_pct": 4.35, "atr_20": 0.5, "period": period},
        ]))
        monkeypatch.setattr(ms, "_analyze_catalysts_safe", lambda records: catalyst_map)
        sector_rank = pd.DataFrame({"sector_name": ["锂电池"], "rank": [1]})
        return today, sector_rank

    def test_catalyst_true_in_records(self, monkeypatch):
        today, sector_rank = self._setup_mocks(monkeypatch, {
            "600000": {"satisfied": True, "catalyst_type": "业绩", "sustainability": "可持续",
                       "basis": "《业绩预告》(2026-07-25)", "titles": ["2026-07-25《业绩预告》"]},
        })
        out = ms._build_hot_section(today, sector_rank, "A")
        rec = out["hot_breakout"][0]
        # 无昨日涨停数据→②None；①③④⑦=4 条 + ⑧满足 = 5
        assert rec["rules_met"] == 5
        assert "⑧满足·业绩" in rec["analysis"]
        assert rec["catalyst_basis"] == "《业绩预告》(2026-07-25)"
        assert rec["catalyst_titles"] == ["2026-07-25《业绩预告》"]

    def test_catalyst_empty_degrades(self, monkeypatch):
        today, sector_rank = self._setup_mocks(monkeypatch, {})
        out = ms._build_hot_section(today, sector_rank, "A")
        rec = out["hot_breakout"][0]
        assert "⑧人工核对" in rec["analysis"]
        assert "catalyst_basis" not in rec
