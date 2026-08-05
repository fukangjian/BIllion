"""
market_scanner 热点候选「买入规则 8 条推荐分析」单元测试（零网络）
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import market_scanner as ms


def _vol_df(volumes):
    return pd.DataFrame({"volume": volumes})


def _rec(sector="锂电池", source="连板"):
    return {"symbol": "600000", "sector": sector, "source": source}


class TestVolumeRatio:
    def test_basic_ratio(self):
        df = _vol_df([100.0] * 20 + [300.0])
        assert ms._volume_ratio(df) == 3.0

    def test_insufficient_data_returns_none(self):
        assert ms._volume_ratio(_vol_df([100.0] * 10)) is None

    def test_zero_base_returns_none(self):
        assert ms._volume_ratio(_vol_df([0.0] * 21)) is None

    def test_empty_or_missing_column(self):
        assert ms._volume_ratio(pd.DataFrame()) is None
        assert ms._volume_ratio(None) is None


class TestEvaluateBuyRules:
    def _eval(self, rec=None, df=None, top=("锂电池",), today_n=None, prev=None, state="A", lbc=2):
        rec = rec or _rec()
        info = pd.Series({"lbc": lbc})
        today = {rec["sector"]: 2} if today_n is None else today_n
        return ms._evaluate_buy_rules(rec, info, df, set(top), today, prev, state)

    def test_all_auto_rules_met(self):
        """①②③④⑦ 全部满足 → met=5，判定「符合」"""
        out = self._eval(prev={"锂电池": 1})
        assert out["met"] == 5
        assert out["text"].startswith("符合 5 条")
        assert "⑧人工核对(最重要)" in out["text"]

    def test_rule1_false_when_sector_not_top5(self):
        out = self._eval(top=("白酒",), prev={"锂电池": 1})
        assert out["rules"][1] is False
        assert "✗板块Top5" in out["text"]

    def test_rule1_none_without_sector_rank(self):
        out = self._eval(top=(), prev={"锂电池": 1})
        assert out["rules"][1] is None

    def test_rule2_requires_increase(self):
        assert self._eval(prev={"锂电池": 2})["rules"][2] is False   # 持平不算增加
        assert self._eval(prev={"锂电池": 1})["rules"][2] is True
        assert self._eval(prev={})["rules"][2] is True               # 昨日 0 家 → 增加
        assert self._eval(prev=None)["rules"][2] is None             # 无昨日数据不判

    def test_rule3_front_row_sources(self):
        assert self._eval(rec=_rec(source="连板"))["rules"][3] is True
        assert self._eval(rec=_rec(source="领涨"))["rules"][3] is True
        assert self._eval(rec=_rec(source="涨停"), lbc=1)["rules"][3] is False
        assert self._eval(rec=_rec(source="炸板"), lbc=1)["rules"][3] is False
        assert self._eval(rec=_rec(source="炸板"), lbc=3)["rules"][3] is True  # lbc>=2 亦算前排

    def test_rule4_zt_branch_and_volume_branch(self):
        assert self._eval(rec=_rec(source="涨停"), lbc=1)["rules"][4] is True   # 涨停承接分支
        assert self._eval(rec=_rec(source="领涨"), df=_vol_df([100.0] * 20 + [200.0]))["rules"][4] is True   # 放量分支
        assert self._eval(rec=_rec(source="领涨"), df=_vol_df([100.0] * 21))["rules"][4] is False  # 未放量
        assert self._eval(rec=_rec(source="领涨"), df=None)["rules"][4] is None                    # 无数据

    def test_rule7_market_state(self):
        assert self._eval(state="A")["rules"][7] is True
        assert self._eval(state="B")["rules"][7] is True
        assert self._eval(state="C")["rules"][7] is False
        assert self._eval(state="")["rules"][7] is None

    def test_rules_5_6_8_never_auto_judged(self):
        out = self._eval(prev={"锂电池": 1})
        assert out["rules"][5] is None
        assert out["rules"][6] is None
        assert out["rules"][8] is None

    def test_below_4_shows_gap(self):
        out = self._eval(rec=_rec(sector="其他", source="炸板"), top=("锂电池",),
                         today_n={}, prev=None, state="C", lbc=0, df=None)
        assert out["met"] < 4
        assert "差" in out["text"]


class TestLimitCountBySector:
    def test_counts_up_only(self):
        df = pd.DataFrame([
            {"pool_type": "up", "sector": "锂电池"},
            {"pool_type": "up", "sector": "锂电池"},
            {"pool_type": "broken", "sector": "锂电池"},
            {"pool_type": "up", "sector": "白酒"},
        ])
        assert ms._limit_count_by_sector(df) == {"锂电池": 2, "白酒": 1}

    def test_empty(self):
        assert ms._limit_count_by_sector(pd.DataFrame()) == {}


class TestBuildHotSectionIntegration:
    def test_records_have_name_and_analysis(self, monkeypatch):
        today = "2026-07-31"
        limit_df = pd.DataFrame([
            {"trade_date": today, "symbol": "600000", "name": "测试A", "pool_type": "up",
             "change_pct": 10.0, "amount": 1e8, "lbc": 3, "sector": "锂电池"},
            {"trade_date": today, "symbol": "600222", "name": "测试C", "pool_type": "up",
             "change_pct": 10.0, "amount": 1e8, "lbc": 1, "sector": "锂电池"},
            {"trade_date": "2026-07-30", "symbol": "600111", "name": "测试B", "pool_type": "up",
             "change_pct": 10.0, "amount": 1e8, "lbc": 1, "sector": "锂电池"},
        ])
        pool_df = pd.DataFrame([
            {"trade_date": today, "symbol": "600000", "name": "测试A", "source": "连板",
             "sector": "锂电池", "change_pct": 10.0, "lbc": 3},
        ])
        monkeypatch.setattr(ms, "load_limit_pool", lambda trade_date=None: (
            limit_df[limit_df["trade_date"] == trade_date] if trade_date else limit_df))
        monkeypatch.setattr(ms, "load_hot_pool", lambda trade_date=None: pool_df)
        monkeypatch.setattr(ms, "load_daily_quotes", lambda symbol=None: _vol_df([100.0] * 20 + [300.0]))
        monkeypatch.setattr(ms, "scan_breakout_candidates", lambda data, period: pd.DataFrame([
            {"symbol": "600000", "close": 12.0, "channel_high": 11.5,
             "breakout_pct": 4.35, "atr_20": 0.5, "period": period},
        ]))
        monkeypatch.setattr(ms, "_analyze_catalysts_safe", lambda records: {})  # 禁网：⑧降级
        sector_rank = pd.DataFrame({"sector_name": ["锂电池", "白酒"], "rank": [1, 2]})

        out = ms._build_hot_section(today, sector_rank, "A")
        assert out["available"] is True
        assert len(out["hot_breakout"]) == 1
        rec = out["hot_breakout"][0]
        assert rec["name"] == "测试A"
        assert rec["rules_met"] == 5
        assert "符合 5 条" in rec["analysis"]
        assert "⑧人工核对" in rec["analysis"]

    def test_unavailable_when_no_data(self, monkeypatch):
        monkeypatch.setattr(ms, "load_limit_pool", lambda trade_date=None: pd.DataFrame())
        monkeypatch.setattr(ms, "load_hot_pool", lambda trade_date=None: pd.DataFrame())
        out = ms._build_hot_section("2026-07-31")
        assert out["available"] is False
        assert out["hot_breakout"] == []
