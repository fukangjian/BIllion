"""
事件驱动短线（EVT-S）测试 — 事件因子评分 / 解析 / 候选构建 / 龙头逻辑分档 / 计划集成，全部离线
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import EVT_MAX_CANDIDATES, EVT_MIN_EVENT_SCORE
from pipeline.daily_plan import build_daily_plan
from pipeline.dragon_head import _score_logic
from pipeline.event_pool import build_event_candidates
from research.catalyst_analyzer import event_factor, parse_verdict
from review.trade_log import TradeLog


def _cat(satisfied=True, strength="中等", event_date="2026-09-16", wave="首次", ctype="业绩"):
    return {"satisfied": satisfied, "catalyst_type": ctype, "sustainability": "可持续",
            "event_date": event_date, "strength": strength, "wave": wave, "basis": "测试"}


# ---------- 事件因子评分 ----------

class TestEventFactor:
    def test_full_score(self):
        """满足 + 重磅 + 1 日内 + 首次 → 97（20+40+25+12）"""
        ef = event_factor(_cat(strength="重磅", event_date="2026-09-16"), today="2026-09-17")
        assert ef["score"] == 97
        assert ef["days_since"] == 1

    def test_medium_qualifies(self):
        """中等力度 + 3 日内 + 首次 → 74（≥70 入选）"""
        ef = event_factor(_cat(strength="中等", event_date="2026-09-15"), today="2026-09-17")
        assert ef["score"] == 20 + 24 + 18 + 12

    def test_light_fails_threshold(self):
        """轻微力度即便最新鲜也只有 67 分，低于入选线 70"""
        ef = event_factor(_cat(strength="轻微", event_date="2026-09-16"), today="2026-09-17")
        assert ef["score"] == 20 + 10 + 25 + 12
        assert ef["score"] < EVT_MIN_EVENT_SCORE

    def test_stale_event_decays(self):
        """事件过期（>5 日）时效衰减为 2：中等+过期+首次 = 58，不入选"""
        ef = event_factor(_cat(strength="中等", event_date="2026-09-01"), today="2026-09-17")
        assert ef["score"] == 20 + 24 + 2 + 12

    def test_not_satisfied_zero(self):
        """⑧未判定/不满足 → 0 分（事件驱动不参与，原 ⑧ 口径不受影响）"""
        assert event_factor({"satisfied": False})["score"] == 0
        assert event_factor({"satisfied": None})["score"] == 0
        assert event_factor(None)["score"] == 0

    def test_legacy_fields_fallback(self):
        """旧格式催化剂（无力度/日期/波次字段）→ 回退口径 48 分，保守不入选"""
        ef = event_factor({"satisfied": True, "catalyst_type": "政策"}, today="2026-09-17")
        assert ef["score"] == 20 + 20 + 2 + 6

    def test_second_wave_discount(self):
        """第二波折扣：中等+3 日内+第二波 = 65 < 70 不入选（首次同期 74 入选）"""
        ef = event_factor(_cat(strength="中等", event_date="2026-09-15", wave="第二波及以上"),
                          today="2026-09-17")
        assert ef["score"] == 20 + 24 + 18 + 3
        assert ef["score"] < EVT_MIN_EVENT_SCORE

    def test_bad_date_treated_unknown(self):
        ef = event_factor(_cat(event_date="not-a-date"), today="2026-09-17")
        assert ef["days_since"] is None
        assert ef["score"] == 20 + 24 + 2 + 12


# ---------- LLM 七行格式解析（向后兼容） ----------

class TestParseVerdict:
    def test_new_seven_line_format(self):
        text = ("判定：满足\n催化类型：业绩\n持续性：可持续逻辑\n"
                "事件日期：2026-09-16\n事件力度：重磅\n炒作波次：首次\n"
                "依据：《2026年半年度业绩预增公告》(2026-09-16)")
        v = parse_verdict(text)
        assert v["satisfied"] is True
        assert v["catalyst_type"] == "业绩"
        assert v["event_date"] == "2026-09-16"
        assert v["strength"] == "重磅"
        assert v["wave"] == "首次"

    def test_legacy_four_line_format(self):
        """旧四行格式仍可解析（扩展字段为空，评分走回退口径）"""
        text = "判定：不满足\n催化类型：无\n持续性：-\n依据：例行公告"
        v = parse_verdict(text)
        assert v["satisfied"] is False
        assert v["event_date"] == "" and v["strength"] == "" and v["wave"] == ""

    def test_unparseable(self):
        assert parse_verdict("") is None
        assert parse_verdict("随便说说没有格式") is None


# ---------- 龙头逻辑维度分档 ----------

class TestDragonLogicGrading:
    def test_strength_grading(self):
        assert _score_logic({"catalyst": _cat(strength="重磅")})[0] == 20
        assert _score_logic({"catalyst": _cat(strength="中等")})[0] == 16
        assert _score_logic({"catalyst": _cat(strength="轻微")})[0] == 12

    def test_legacy_no_strength_falls_back_20(self):
        cat = {"satisfied": True, "catalyst_type": "政策"}
        assert _score_logic({"catalyst": cat}) == (20, "事件催化确认（政策）")

    def test_reason_and_undecided_unchanged(self):
        """归因/待核对/不满足路径保持原口径（回归保护）"""
        assert _score_logic({"reason": "算力租赁", "catalyst": {}})[0] == 12
        assert _score_logic({"catalyst": {}})[0] == 8
        assert _score_logic({"catalyst": {"satisfied": False}})[0] == 0


# ---------- 候选构建 ----------

def _hot_rec(symbol, close=20.0, breakout=3.0, atr=1.0, cat=None, one_word=False):
    return {"symbol": symbol, "name": f"股{symbol}", "sector": "半导体", "close": close,
            "channel_high": close * 0.97, "breakout_pct": breakout, "atr_20": atr,
            "period": 20, "one_word_board": one_word, "catalyst": cat or _cat()}


class TestBuildEventCandidates:
    def test_filters_and_sorting(self):
        """按事件分降序 + 低分剔除 + 上限截断"""
        recs = [
            _hot_rec("600001", cat=_cat(strength="中等", event_date="2026-09-16")),   # 81
            _hot_rec("600002", cat=_cat(strength="重磅", event_date="2026-09-16")),   # 97
            _hot_rec("600003", cat=_cat(strength="轻微", event_date="2026-09-16")),   # 67 ✗
            _hot_rec("600004", cat={"satisfied": None}),                              # 0 ✗
        ]
        out = build_event_candidates(recs, {}, today="2026-09-17")
        assert [c["symbol"] for c in out] == ["600002", "600001"]
        assert out[0]["event_score"] == 97 and out[0]["record"] is True

    def test_one_word_board_not_tradable(self):
        """一字板：信号仍记录（验证口径）但 record=False（实盘买不到）"""
        out = build_event_candidates([_hot_rec("600005", one_word=True)], {}, today="2026-09-17")
        assert len(out) == 1
        assert out[0]["record"] is False
        assert "一字板" in out[0]["note"]

    def test_volume_ratio_annotation(self):
        """量能确认：量比 ≥1.5 标注确认，<1.5 标注不足（不剔除，留给人工/竞价确认）"""
        import pandas as pd

        def _df(last_vol):
            rows = [{"trade_date": f"2026-08-{d:02d}", "volume": 1000.0} for d in range(1, 21)]
            rows.append({"trade_date": "2026-09-16", "volume": last_vol})
            return pd.DataFrame(rows)

        out_ok = build_event_candidates(
            [_hot_rec("600006")], {"600006": _df(2000.0)}, today="2026-09-17")
        assert "量能确认" in out_ok[0]["note"]
        out_low = build_event_candidates(
            [_hot_rec("600007")], {"600007": _df(1000.0)}, today="2026-09-17")
        assert "量能不足" in out_low[0]["note"]

    def test_top_n_cap(self):
        recs = [_hot_rec(f"6001{i:02d}", cat=_cat(strength="重磅")) for i in range(8)]
        out = build_event_candidates(recs, {}, today="2026-09-17")
        assert len(out) == EVT_MAX_CANDIDATES

    def test_empty_input(self):
        assert build_event_candidates([], {}, today="2026-09-17") == []
        assert build_event_candidates(None, None, "") == []


# ---------- daily_plan 集成 ----------

def _scan(events, dragons=None, sentiment=None, s1a=None):
    return {
        "date": "2026-09-17",
        "market_state": "B",
        "sentiment": sentiment,
        "breakout_s1a": s1a or [],
        "breakout_s2a": [],
        "sector_ranking": [],
        "hot_pool": {"event_candidates": events, "dragon_candidates": dragons or []},
    }


def _evt_rec(symbol="600100", score=90, record=True):
    return {"symbol": symbol, "name": "事件股", "sector": "半导体", "close": 30.0,
            "channel_high": 29.0, "breakout_pct": 3.4, "atr_20": 1.2, "period": 20,
            "event_score": score, "event_breakdown": "测试", "days_since": 1,
            "catalyst_type": "业绩", "strength": "重磅", "wave": "首次",
            "sustainability": "可持续", "record": record, "note": ""}


class TestDailyPlanEvents:
    def _plan(self, tmp_path, events, sentiment=None, s1a=None):
        log_file = tmp_path / "trades.json"
        log_file.write_text("[]", encoding="utf-8")
        return build_daily_plan(
            _scan(events, sentiment=sentiment, s1a=s1a),
            {"drawdown_state": {"state": "Normal"}, "alerts": []},
            equity=1_000_000,
            trade_log=TradeLog(log_file),
            db_path=None,
        )

    def test_event_buys_built_with_params(self, tmp_path):
        """EVT-S 候选入计划：止损/股数/闸门预检齐全，记录影子验证口径"""
        plan = self._plan(tmp_path, [_evt_rec()])
        assert len(plan["event_buys"]) == 1
        b = plan["event_buys"][0]
        assert b["system"] == "EVT-S" and b["account"] == "事件"
        assert b["stop"] < b["close"] and b["shares"] > 0
        assert b["event_score"] == 90

    def test_record_false_excluded(self, tmp_path):
        plan = self._plan(tmp_path, [_evt_rec(record=False)])
        assert plan["event_buys"] == []

    def test_sentiment_ebb_blocks_events(self, tmp_path):
        """情绪退潮：EVT-S 建议股数归零 + 闸门标注禁开新仓（与闸门高级违规一致）"""
        plan = self._plan(tmp_path, [_evt_rec()],
                          sentiment={"phase": "退潮", "metrics": {}})
        b = plan["event_buys"][0]
        assert b["shares"] == 0
        assert "禁开新仓" in b["gate"]
        assert any("退潮" in c and "禁开新仓" in c for c in plan["no_trade_conditions"])

    def test_trend_cross_annotation(self, tmp_path):
        """趋势候选与事件候选同股 → 趋势行交叉标注（逻辑前置确认）"""
        s1 = {"symbol": "600100", "close": 30.0, "atr_20": 1.2, "period": 20,
              "filter_passed": 3, "filters_required": 3, "filter_brief": "全过",
              "note": "", "record": True, "breakout_pct": 3.4}
        plan = self._plan(tmp_path, [_evt_rec()], s1a=[s1])
        assert len(plan["trend_buys"]) == 1
        assert "EVT-S" in plan["trend_buys"][0]["note"]

    def test_markdown_renders_event_section(self, tmp_path):
        from pipeline.daily_plan import daily_plan_to_markdown

        plan = self._plan(tmp_path, [_evt_rec()])
        md = "\n".join(daily_plan_to_markdown(plan))
        assert "事件驱动候选" in md and "EVT-S" in md and "影子验证" in md
