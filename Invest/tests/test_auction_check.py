"""
9:25 集合竞价判定测试 — mock 快照/分时/扫描 JSON + 临时 trades/db，不依赖网络、不碰真实数据
"""
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import (
    AUCTION_HOT_EXEC_OPEN,
    AUCTION_POS_LOW_OPEN_ALERT,
    AUCTION_SB_A_OPEN,
    AUCTION_SB_A_VOL,
    AUCTION_SB_S_OPEN,
    AUCTION_SB_S_VOL,
    AUCTION_VOL_RATIO_GOOD,
)
from pipeline.auction_check import (
    auction_metrics,
    build_watchlist,
    grade_hot_candidate,
    grade_position,
    grade_second_board,
    pre_min_trend_from_df,
    run_auction_check,
)
from review.trade_log import Trade, TradeLog


# ---------- 二板 S/A/B/C 分级 ----------

class TestGradeSecondBoard:
    @pytest.mark.parametrize("open_pct", [-3.0, -0.01, 0.0])
    def test_c_low_open(self, open_pct):
        grade, text = grade_second_board(open_pct, 20.0, True)
        assert grade == "C" and "放弃" in text

    def test_s_full(self):
        grade, text = grade_second_board(10.0, AUCTION_SB_S_VOL, True)
        assert grade == "S" and "挂涨停价" in text

    def test_s_boundary(self):
        grade, _ = grade_second_board(AUCTION_SB_S_OPEN[0], AUCTION_SB_S_VOL, True)
        assert grade == "S"

    def test_s_without_resonance_degrades_a(self):
        """高开 8-15% + 量足但板块无共振 → 降级 A"""
        grade, text = grade_second_board(10.0, AUCTION_SB_S_VOL, False)
        assert grade == "A" and "降级" in text

    def test_s_range_vol_short_to_a_or_b(self):
        grade, _ = grade_second_board(10.0, AUCTION_SB_A_VOL, False)  # 量 6% ≥5% → A
        assert grade == "A"
        grade2, _ = grade_second_board(10.0, AUCTION_SB_A_VOL - 1, False)  # 量 <5% → B
        assert grade2 == "B"

    def test_a(self):
        grade, text = grade_second_board(AUCTION_SB_A_OPEN[0], AUCTION_SB_A_VOL, False)
        assert grade == "A" and "秒板" in text

    def test_b(self):
        grade, text = grade_second_board(2.0, 20.0, True)
        assert grade == "B" and "回封" in text

    def test_overheat_open(self):
        grade, text = grade_second_board(AUCTION_SB_S_OPEN[1] + 1, 20.0, True)
        assert grade == "B" and "透支" in text


# ---------- 龙头 HOT-S 判定 ----------

class TestGradeHotCandidate:
    def test_yizi(self):
        verdict, text = grade_hot_candidate(10.0, 20.0, True)
        assert verdict == "不追" and "一字" in text

    @pytest.mark.parametrize("open_pct", [-2.0, 0.0])
    def test_low_open_out(self, open_pct):
        verdict, _ = grade_hot_candidate(open_pct, 20.0, False)
        assert verdict == "剔除"

    def test_execute(self):
        verdict, text = grade_hot_candidate(5.0, AUCTION_VOL_RATIO_GOOD, False)
        assert verdict == "执行" and "按计划执行" in text

    def test_execute_boundary(self):
        verdict, _ = grade_hot_candidate(AUCTION_HOT_EXEC_OPEN[0], AUCTION_VOL_RATIO_GOOD, False)
        assert verdict == "执行"

    def test_watch_low_volume(self):
        verdict, _ = grade_hot_candidate(5.0, AUCTION_VOL_RATIO_GOOD - 1, False)
        assert verdict == "观察"

    def test_watch_low_open(self):
        verdict, _ = grade_hot_candidate(1.0, 20.0, False)
        assert verdict == "观察"

    def test_overheat_no_chase(self):
        verdict, text = grade_hot_candidate(AUCTION_HOT_EXEC_OPEN[1] + 0.5, 20.0, False)
        assert verdict == "不追" and "高开" in text


# ---------- 持仓竞价风控 ----------

class TestGradePosition:
    def test_break_stop(self):
        alert = grade_position(-0.5, 9.8, 10.0)
        assert alert and "跌破止损" in alert

    def test_low_open_boundary(self):
        assert grade_position(AUCTION_POS_LOW_OPEN_ALERT, 10.5, 10.0) is not None
        assert grade_position(AUCTION_POS_LOW_OPEN_ALERT + 0.5, 10.5, 10.0) is None

    def test_normal(self):
        assert grade_position(1.0, 10.5, 10.0) is None

    def test_no_stop(self):
        assert grade_position(-5.0, 10.5, 0) is not None  # 无止损仍查低开
        assert grade_position(1.0, 10.5, 0) is None


# ---------- 竞价指标 ----------

class TestAuctionMetrics:
    def test_normal(self):
        spot = {"symbol": "600001", "open": 10.5, "prev_close": 10.0, "volume": 5000}
        op, vr, yizi = auction_metrics(spot, 100000)
        assert op == pytest.approx(5.0)
        assert vr == pytest.approx(5.0)
        assert yizi is False

    def test_yizi_main_board(self):
        spot = {"symbol": "600001", "open": 11.0, "prev_close": 10.0, "volume": 5000}
        _, _, yizi = auction_metrics(spot, 100000)
        assert yizi is True

    def test_yizi_20cm(self):
        spot = {"symbol": "688001", "open": 12.0, "prev_close": 10.0, "volume": 5000}
        _, _, yizi = auction_metrics(spot, 100000)
        assert yizi is True

    def test_missing_data(self):
        op, vr, yizi = auction_metrics({"symbol": "600001"}, 100000)
        assert (op, vr, yizi) == (0.0, 0.0, False)


# ---------- 竞价分时走向 ----------

def _pre_min_df(prices: list[float], vols: list[float]) -> pd.DataFrame:
    """09:15 起逐分钟分时"""
    times = [f"09:{15 + i:02d}" for i in range(len(prices))]
    return pd.DataFrame({"时间": [f"2026-08-24 {t}:00" for t in times],
                         "价格": prices, "成交量": vols})


class TestPreMinTrend:
    def test_rush(self):
        # 9:20 前平淡，9:20→9:25 价升量集中 → 抢筹
        df = _pre_min_df([10.0] * 5 + [10.0, 10.05, 10.10, 10.15, 10.20, 10.25],
                         [100] * 5 + [200, 300, 400, 500, 600, 800])
        r = pre_min_trend_from_df(df)
        assert r["trend"] == "抢筹" and r["price_chg"] > 0.5

    def test_pull_order(self):
        df = _pre_min_df([10.2] * 5 + [10.20, 10.15, 10.10, 10.05, 10.00, 9.95],
                         [100] * 11)
        r = pre_min_trend_from_df(df)
        assert r["trend"] == "撤单"

    def test_flat(self):
        df = _pre_min_df([10.0] * 11, [100] * 11)
        r = pre_min_trend_from_df(df)
        assert r["trend"] == "平淡"

    def test_degrade(self):
        assert pre_min_trend_from_df(pd.DataFrame()) is None
        assert pre_min_trend_from_df(pd.DataFrame({"a": [1]})) is None
        assert pre_min_trend_from_df(None) is None


# ---------- 编排（注入快照/扫描 JSON/临时库，无网络） ----------

def _scan() -> dict:
    return {
        "date": "2026-08-24",
        "hot_pool": {"second_board": {"candidates": [
            {"symbol": "600111", "name": "二板甲", "sector": "半导体", "close": 10.0},
            {"symbol": "600222", "name": "二板乙", "sector": "医药", "close": 20.0},
        ]}},
        "daily_plan": {"dragon_buys": [
            {"symbol": "600333", "name": "龙头甲", "sector": "半导体", "close": 30.0},
        ]},
    }


def _snapshot() -> dict:
    return {
        # 半导体板块共振：600111 高开 9% 量 9%（S 级要件之一）
        "600111": {"symbol": "600111", "name": "二板甲", "open": 10.9, "prev_close": 10.0, "volume": 9000, "amount": 1e8},
        # 低开 → C
        "600222": {"symbol": "600222", "name": "二板乙", "open": 19.6, "prev_close": 20.0, "volume": 4000, "amount": 1e8},
        # 高开 5% 量 6% → 执行；同时构成 600111 的板块共振（高开≥15？否，靠一字不足→另算）
        "600333": {"symbol": "600333", "name": "龙头甲", "open": 31.5, "prev_close": 30.0, "volume": 6000, "amount": 1e8},
        # 持仓：竞价 9.5 跌破止损 9.8
        "600519": {"symbol": "600519", "name": "贵州茅台", "open": 9.5, "prev_close": 10.0, "volume": 100, "amount": 1e6},
    }


def _tmp_env(tmp_path, monkeypatch):
    """临时 trades + db + 输出目录；昨日量统一 100000 手"""
    from pipeline.database import init_database, save_daily_quotes

    db = tmp_path / "market.db"
    init_database(db)
    rows = [{"symbol": s, "trade_date": "2026-08-21", "open": 1, "high": 1, "low": 1,
             "close": 1, "volume": 100000.0, "amount": 1e6}
            for s in ("600111", "600222", "600333", "600519")]
    save_daily_quotes(pd.DataFrame(rows), db)

    log = TradeLog(tmp_path / "trades.json")
    log.add(Trade(股票代码="600519", 股票名称="贵州茅台", 账户类型="核心", 入场系统="S1-A",
                  入场价=10.5, 止损价=9.8, 风险率=0.5, 股数=100, 仓位金额=1050))

    monkeypatch.setattr("pipeline.auction_check.MARKET_SCAN_OUTPUT_DIR", tmp_path)
    return db, log


class TestRunAuctionCheck:
    def test_full_flow(self, tmp_path, monkeypatch):
        db, log = _tmp_env(tmp_path, monkeypatch)
        monkeypatch.setattr("pipeline.auction_check.fetch_pre_min_trend",
                            lambda s: {"trend": "抢筹", "price_chg": 1.2, "late_vol_pct": 55.0})
        r = run_auction_check(db_path=db, trade_log=log, scan=_scan(), snapshot=_snapshot())
        assert r["available"] is True

        sb = {w["symbol"]: w for w in r["second_board"]}
        assert sb["600111"]["grade"] == "A"  # 高开 9% 量 9% 但板块无共振（600333 未≥15% 非一字）
        assert "抢筹" in sb["600111"]["text"]  # A 级附分时走向
        assert sb["600222"]["grade"] == "C"
        assert "pre_min" not in sb["600222"]  # C 级不做分时

        hot = r["hot"][0]
        assert hot["verdict"] == "执行" and "抢筹" in hot["text"]

        pos = r["positions"][0]
        assert "跌破止损" in pos["alert"]

        out = tmp_path / f"auction_check_{r['date']}.json"
        assert out.exists() and (tmp_path / f"auction_check_{r['date']}.md").exists()

    def test_sector_resonance_promotes_s(self, tmp_path, monkeypatch):
        """同板块有一字/高开≥15% → S 级"""
        db, log = _tmp_env(tmp_path, monkeypatch)
        snap = _snapshot()
        snap["600333"] = {**snap["600333"], "open": 34.5}  # 龙头甲高开 15% → 半导体共振
        monkeypatch.setattr("pipeline.auction_check.fetch_pre_min_trend", lambda s: None)
        r = run_auction_check(db_path=db, trade_log=log, scan=_scan(), snapshot=snap)
        sb = {w["symbol"]: w for w in r["second_board"]}
        assert sb["600111"]["grade"] == "S"

    def test_snapshot_empty_degrades(self, tmp_path, monkeypatch):
        db, log = _tmp_env(tmp_path, monkeypatch)
        r = run_auction_check(db_path=db, trade_log=log, scan=_scan(), snapshot={})
        assert r["available"] is False and "快照" in r["note"]

    def test_empty_watchlist(self, tmp_path, monkeypatch):
        db, log = _tmp_env(tmp_path, monkeypatch)
        log2 = TradeLog(tmp_path / "empty_trades.json")
        r = run_auction_check(db_path=db, trade_log=log2, scan={}, snapshot=_snapshot())
        assert r["available"] is False

    def test_missing_symbol_noted(self, tmp_path, monkeypatch):
        db, log = _tmp_env(tmp_path, monkeypatch)
        snap = _snapshot()
        del snap["600222"]
        monkeypatch.setattr("pipeline.auction_check.fetch_pre_min_trend", lambda s: None)
        r = run_auction_check(db_path=db, trade_log=log, scan=_scan(), snapshot=snap)
        assert r["available"] is True and "缺失" in r["note"]

    def test_vol_window_flag(self, tmp_path, monkeypatch):
        """9:26-9:31 窗口内 vol_in_window=True；窗口外 False 且备注竞昨比口径"""
        db, log = _tmp_env(tmp_path, monkeypatch)
        monkeypatch.setattr("pipeline.auction_check.fetch_pre_min_trend", lambda s: None)

        class _DT:
            t = datetime(2026, 8, 24, 9, 26, 30)

            @classmethod
            def now(cls):
                return cls.t

        monkeypatch.setattr("pipeline.auction_check.datetime", _DT)
        r = run_auction_check(db_path=db, trade_log=log, scan=_scan(), snapshot=_snapshot())
        assert r["vol_in_window"] is True and "竞昨比偏大" not in r["note"]

        _DT.t = datetime(2026, 8, 24, 11, 35, 0)
        r2 = run_auction_check(db_path=db, trade_log=log, scan=_scan(), snapshot=_snapshot())
        assert r2["vol_in_window"] is False and "竞昨比偏大" in r2["note"]


class TestBuildWatchlist:
    def test_groups(self, tmp_path):
        log = TradeLog(tmp_path / "trades.json")
        log.add(Trade(股票代码="600519", 账户类型="核心", 入场系统="S1-A",
                      入场价=10.5, 止损价=9.8, 风险率=0.5, 股数=100, 仓位金额=1050))
        w = build_watchlist(trade_log=log, scan=_scan())
        assert [x["symbol"] for x in w["second_board"]] == ["600111", "600222"]
        assert [x["symbol"] for x in w["hot"]] == ["600333"]
        assert [x["symbol"] for x in w["positions"]] == ["600519"]
        assert w["positions"][0]["stop"] == 9.8

    def test_hot_top5(self, tmp_path):
        scan = {"daily_plan": {"dragon_buys": [
            {"symbol": f"60000{i}", "name": f"龙{i}", "sector": "x", "close": 10}
            for i in range(8)]}}
        w = build_watchlist(trade_log=TradeLog(tmp_path / "t.json"), scan=scan)
        assert len(w["hot"]) == 5  # 与页面口径一致：只取前 5
