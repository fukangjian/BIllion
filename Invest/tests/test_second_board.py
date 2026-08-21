"""
二板战法观察池测试 — mock 首板数据 + 临时 SQLite，不依赖网络、不碰真实 data/market.db
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import (
    SECOND_BOARD_CAP_MAX_YI,
    SECOND_BOARD_CAP_MIN_YI,
    SECOND_BOARD_MIN_SCORE,
    SECOND_BOARD_PREV5_GAIN_MAX,
    SECOND_BOARD_PRICE_MAX,
    SECOND_BOARD_PRICE_MIN,
    SECOND_BOARD_SEAL_RATIO_MIN,
    SECOND_BOARD_STOP_PCT,
    SECOND_BOARD_TOP_N,
    SECOND_BOARD_TURNOVER_MAX,
)
from pipeline.second_board import (
    board_of,
    broke_prior_high,
    build_second_board_pool,
    fmt_fbt,
    hard_filter_reasons,
    limit_up_pct,
    norm_fbt,
    prev5_gain,
    score_candidate,
    screen_first_boards,
)


# ---------- 测试数据构造 ----------

def _daily(closes: list[float], high_offset: float = 0.5) -> pd.DataFrame:
    """按收盘价序列构造日线（high=close+offset，首板日为最后一行）"""
    dates = pd.date_range("2026-08-01", periods=len(closes), freq="D").strftime("%Y-%m-%d")
    return pd.DataFrame({
        "trade_date": dates,
        "open": closes,
        "high": [c + high_offset for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": 1000.0,
        "amount": 100000.0,
    })


def _flat_daily(bars: int = 25, close: float = 28.0) -> pd.DataFrame:
    """横盘日线：首板前涨幅≈0，且不突破前高（前高 28.5 > 首板收盘由调用方决定）"""
    return _daily([close] * bars)


def _pass_rec(**over) -> dict:
    """硬条件全过的首板记录（科创板 688001，可按需覆盖字段）"""
    rec = {
        "symbol": "688001", "name": "测试股份", "pool_type": "up", "lbc": 1,
        "sector": "半导体", "fbt": "093001",
        "seal_amount": 2e8, "turnover": 5.0,
        "latest": 30.0, "circular_cap": 50e8,  # 封单力度 4%
    }
    rec.update(over)
    return rec


# ---------- 基础判定 ----------

class TestBoardOf:
    @pytest.mark.parametrize("symbol,expected", [
        ("688001", "STAR"), ("300001", "GEM"), ("301001", "GEM"),
        ("600519", "MAIN"), ("000001", "MAIN"),
        ("830799", "BSE"), ("430047", "BSE"), ("920001", "BSE"),
    ])
    def test_board(self, symbol, expected):
        assert board_of(symbol) == expected

    def test_limit_up_pct(self):
        assert limit_up_pct("688001") == 0.20
        assert limit_up_pct("300001") == 0.20
        assert limit_up_pct("600519") == 0.10


class TestHelpers:
    def test_norm_fbt(self):
        assert norm_fbt("093001") == "093001"
        assert norm_fbt("09:30:01") == "093001"
        assert norm_fbt(93001) == "093001"
        assert norm_fbt("") == ""
        assert norm_fbt(None) == ""

    def test_fmt_fbt(self):
        assert fmt_fbt("093001") == "09:30:01"
        assert fmt_fbt("") == "-"

    def test_prev5_gain(self):
        # 首板前 5 日：28.0/27.0 - 1 ≈ 3.7%（不含首板当日 30.0）
        df = _daily([27.0, 27.2, 27.4, 27.6, 28.0, 30.0])
        assert prev5_gain(df) == pytest.approx(round((28.0 / 27.0 - 1) * 100, 2))

    def test_prev5_gain_insufficient(self):
        assert prev5_gain(_daily([27.0, 28.0, 30.0])) is None
        assert prev5_gain(None) is None
        assert prev5_gain(pd.DataFrame()) is None

    def test_broke_prior_high(self):
        df = _daily([28.0] * 24 + [30.0])  # 首板收盘 30 > 前高 28.5
        assert broke_prior_high(df) is True
        df2 = _daily([28.0] * 24 + [27.0])  # 未突破
        assert broke_prior_high(df2) is False

    def test_broke_prior_high_min_bars(self):
        assert broke_prior_high(_daily([28.0] * 9 + [30.0])) is False  # 不足 20 根不判


# ---------- 硬性条件 ----------

class TestHardFilters:
    def test_all_pass(self):
        reasons, info = hard_filter_reasons(_pass_rec(), _flat_daily())
        assert reasons == []
        assert info["seal_ratio"] == pytest.approx(4.0)
        assert info["cap_yi"] == pytest.approx(50.0)
        assert info["board"] == "STAR"

    @pytest.mark.parametrize("name", ["ST测试", "*ST测试", "N测试", "C测试"])
    def test_banned_names(self, name):
        reasons, _ = hard_filter_reasons(_pass_rec(name=name), _flat_daily())
        assert any("ST" in r or "新股" in r for r in reasons)

    def test_bse_excluded(self):
        reasons, _ = hard_filter_reasons(_pass_rec(symbol="830799"), _flat_daily())
        assert reasons and "北交所" in reasons[0]

    def test_seal_time_main(self):
        reasons, _ = hard_filter_reasons(_pass_rec(symbol="600001", fbt="100000"), _flat_daily())
        assert any("首封时间过晚" in r for r in reasons)
        reasons2, _ = hard_filter_reasons(_pass_rec(symbol="600001", fbt="095959"), _flat_daily())
        assert not any("首封" in r for r in reasons2)

    def test_seal_time_star_stricter(self):
        """科创板 9:45 阈值：09:44:59 过，09:45:00 不过（主板同刻可过）"""
        ok, _ = hard_filter_reasons(_pass_rec(fbt="094459"), _flat_daily())
        assert not any("首封" in r for r in ok)
        late, _ = hard_filter_reasons(_pass_rec(fbt="094500"), _flat_daily())
        assert any("首封时间过晚" in r for r in late)
        main, _ = hard_filter_reasons(_pass_rec(symbol="600001", fbt="094500"), _flat_daily())
        assert not any("首封" in r for r in main)

    def test_seal_ratio(self):
        weak = _pass_rec(seal_amount=1e8)  # 1亿/50亿 = 2% < 3%
        reasons, _ = hard_filter_reasons(weak, _flat_daily())
        assert any("封单力度不足" in r for r in reasons)

    def test_turnover(self):
        reasons, _ = hard_filter_reasons(_pass_rec(turnover=12.0), _flat_daily())
        assert any("换手率过高" in r for r in reasons)
        ok, _ = hard_filter_reasons(_pass_rec(turnover=11.9), _flat_daily())
        assert not any("换手" in r for r in ok)

    @pytest.mark.parametrize("cap_yi", [SECOND_BOARD_CAP_MIN_YI - 1, SECOND_BOARD_CAP_MAX_YI + 1])
    def test_cap_range(self, cap_yi):
        reasons, _ = hard_filter_reasons(_pass_rec(circular_cap=cap_yi * 1e8, seal_amount=cap_yi * 1e8 * 0.05), _flat_daily())
        assert any("流通市值超标" in r for r in reasons)

    @pytest.mark.parametrize("price", [SECOND_BOARD_PRICE_MIN - 1, SECOND_BOARD_PRICE_MAX + 1])
    def test_price_range(self, price):
        reasons, _ = hard_filter_reasons(_pass_rec(latest=price), _flat_daily())
        assert any("股价超标" in r for r in reasons)

    def test_prev5_gain_too_high(self):
        df = _daily([20.0, 22.0, 23.0, 23.5, 24.0, 30.0])  # 24/20-1=20% ≥ 15%
        reasons, _ = hard_filter_reasons(_pass_rec(), df)
        assert any("前 5 日涨幅过高" in r for r in reasons)

    def test_missing_daily(self):
        reasons, _ = hard_filter_reasons(_pass_rec(), None)
        assert "历史行情不足" in reasons

    def test_missing_cap(self):
        reasons, _ = hard_filter_reasons(_pass_rec(circular_cap=None, latest=30.0), _flat_daily())
        assert "价格/市值/换手数据缺失" in reasons


# ---------- 软性评分 ----------

class TestScore:
    def _info(self, **over):
        info = {"price": 25.5, "cap_yi": 60.0, "board": "MAIN"}
        info.update(over)
        return info

    def test_sector_rank(self):
        s2, _ = score_candidate(_pass_rec(), self._info(), sector_fbt_rank=1, broke_high=False, on_lhb=False)
        s1, _ = score_candidate(_pass_rec(), self._info(), sector_fbt_rank=3, broke_high=False, on_lhb=False)
        s0, _ = score_candidate(_pass_rec(), self._info(), sector_fbt_rank=4, broke_high=False, on_lhb=False)
        assert (s2, s1, s0) == (2, 1, 0)

    def test_breakout_lhb(self):
        s, notes = score_candidate(_pass_rec(), self._info(), sector_fbt_rank=9, broke_high=True, on_lhb=True)
        assert s == 3
        assert any("龙虎榜" in n for n in notes)

    def test_keyword_smallcap_integer(self):
        s, _ = score_candidate(
            _pass_rec(name="xx生物科技"), self._info(price=30.0, cap_yi=49.9),
            sector_fbt_rank=9, broke_high=False, on_lhb=False,
        )
        assert s == 3  # 关键词 +1，小市值 +1，整数关口 +1

    def test_no_points(self):
        s, notes = score_candidate(_pass_rec(), self._info(), sector_fbt_rank=9, broke_high=False, on_lhb=False)
        assert s == 0 and notes == []


# ---------- 筛选纯函数 ----------

class TestScreen:
    def test_end_to_end(self):
        # 688001：板块龙一(+2)+突破前高(+2)+龙虎榜(+1)+关键词(+1)+小市值(+1)+整数价(+1)=8 分
        star_daily = _daily([27.0, 27.2, 27.4, 27.6] + [28.0] * 19 + [30.0])
        records = [
            _pass_rec(name="测试科技", circular_cap=45e8),           # 入池 8 分
            _pass_rec(symbol="600002", name="测试制药", sector="医药", fbt="094500",
                      latest=25.5, circular_cap=60e8, seal_amount=3e8),  # 硬过但仅 2 分
            _pass_rec(symbol="600003", name="测试晚封", sector="医药", fbt="103000"),  # 硬过滤
        ]
        daily_map = {"688001": star_daily, "600002": _flat_daily(), "600003": _flat_daily()}
        cands, excluded, below = screen_first_boards(records, daily_map, {"688001": 1e7})
        assert [c["symbol"] for c in cands] == ["688001"]
        c = cands[0]
        assert c["score"] == 8
        assert c["board"] == "科创板"
        assert c["limit_up_price"] == pytest.approx(36.0)          # 30 × 1.2
        assert c["stop_price"] == pytest.approx(30.0 * (1 - SECOND_BOARD_STOP_PCT))
        assert c["prev5_gain"] == pytest.approx(round((28.0 / 28.0 - 1) * 100, 2))
        assert excluded == {"首封时间过晚": 1}
        assert below == 1

    def test_sector_rank_uses_all_first_boards(self):
        """板块名次按全部首板股排（含硬过滤淘汰者）：晚封股被淘，早封股仍是龙一"""
        records = [
            _pass_rec(symbol="600010", name="A", fbt="093000", latest=25.5),  # 板块第 2（600011 更早但换手超标）
            _pass_rec(symbol="600011", name="B", fbt="092000", turnover=20.0),
        ]
        daily_map = {"600010": _flat_daily(), "600011": _flat_daily()}
        # 600010 板块名次 2 → +1 而非 +2
        _, info = hard_filter_reasons(records[0], daily_map["600010"])
        s, notes = score_candidate(records[0], info, 2, False, False)
        assert s == 1 and "第 2" in notes[0]

    def test_sort_by_score_desc(self):
        """候选按评分降序；低于阈值不入池"""
        records, daily_map = [], {}
        for i in range(6):
            sym = f"6000{i:02d}"
            records.append(_pass_rec(symbol=sym, name=f"测试{i}"))
            daily_map[sym] = _flat_daily()
        # 同板块同首封时刻：名次分 2/1/1/…，均 <6 分
        cands, _, below = screen_first_boards(records, daily_map)
        assert cands == [] and below == 6
        # 加分项拉满后全部入池，验证降序
        lhb = {r["symbol"]: 1e7 for r in records}
        for r in records:
            r["name"] = "xx科技"
            r["circular_cap"] = 45e8
            r["latest"] = 30.0
        daily_map = {r["symbol"]: _daily([28.0] * 24 + [30.0]) for r in records}
        cands2, _, _ = screen_first_boards(records, daily_map, lhb)
        assert len(cands2) == 6
        assert [c["score"] for c in cands2] == sorted((c["score"] for c in cands2), reverse=True)


# ---------- DB 构建（临时库） ----------

class TestBuildPool:
    def _db(self, tmp_path) -> Path:
        from pipeline.database import init_database, save_daily_quotes, save_limit_pool

        db = tmp_path / "market.db"
        init_database(db)
        rows = [_pass_rec(name="测试科技", circular_cap=45e8),  # 龙一+突破+关键词+小市值+整数价，评分达标
                _pass_rec(symbol="600003", name="测试晚封", fbt="103000")]
        df = pd.DataFrame([{**r, "trade_date": "2026-08-20", "change_pct": 20.0, "amount": 1e8, "zbc": 0, "reason": ""}
                           for r in rows])
        save_limit_pool(df, db)
        for sym, closes in (("688001", [28.0] * 24 + [30.0]), ("600003", [28.0] * 25)):
            q = _daily(closes)
            q["symbol"] = sym
            save_daily_quotes(q, db)
        return db

    def test_build(self, tmp_path):
        db = self._db(tmp_path)
        pool = build_second_board_pool(trade_date="2026-08-20", db_path=db)
        assert pool["available"] is True
        assert pool["total_first_boards"] == 2
        assert [c["symbol"] for c in pool["candidates"]] == ["688001"]
        assert pool["excluded_count"] == 1
        assert pool["excluded_reasons"].get("首封时间过晚") == 1
        assert pool["params"]["min_score"] == SECOND_BOARD_MIN_SCORE

    def test_date_fallback(self, tmp_path):
        """当日无涨停池时回退最近一期（盘前取数与扫描日期错位兜底）"""
        db = self._db(tmp_path)
        pool = build_second_board_pool(trade_date="2026-08-21", db_path=db)
        assert pool["available"] is True
        assert pool["date"] == "2026-08-20"
        assert len(pool["candidates"]) == 1

    def test_empty_db_degrades(self, tmp_path):
        from pipeline.database import init_database

        db = tmp_path / "empty.db"
        init_database(db)
        pool = build_second_board_pool(trade_date="2026-08-20", db_path=db)
        assert pool["available"] is False
        assert pool["candidates"] == []
        assert "涨停池为空" in pool["note"]

    def test_top_n_cap(self, tmp_path, monkeypatch):
        """多只达标时按 SECOND_BOARD_TOP_N 截断"""
        from pipeline.database import init_database, save_daily_quotes, save_limit_pool

        monkeypatch.setattr("pipeline.second_board.SECOND_BOARD_TOP_N", 1)
        db = tmp_path / "market.db"
        init_database(db)
        rows = [_pass_rec(name="测试科技", circular_cap=45e8),
                _pass_rec(symbol="688002", name="测试智能", sector="软件", circular_cap=45e8)]
        df = pd.DataFrame([{**r, "trade_date": "2026-08-20", "change_pct": 20.0, "amount": 1e8, "zbc": 0, "reason": ""}
                           for r in rows])
        save_limit_pool(df, db)
        for sym in ("688001", "688002"):
            q = _daily([28.0] * 24 + [30.0])
            q["symbol"] = sym
            save_daily_quotes(q, db)
        pool = build_second_board_pool(trade_date="2026-08-20", db_path=db)
        assert pool["available"] is True
        assert pool["total_first_boards"] == 2
        assert len(pool["candidates"]) == 1

    def test_no_first_boards(self, tmp_path):
        from pipeline.database import init_database, save_limit_pool

        db = tmp_path / "market.db"
        init_database(db)
        df = pd.DataFrame([{**_pass_rec(lbc=3), "trade_date": "2026-08-20",
                            "change_pct": 20.0, "amount": 1e8, "zbc": 0, "reason": ""}])
        save_limit_pool(df, db)
        pool = build_second_board_pool(trade_date="2026-08-20", db_path=db)
        assert pool["available"] is False
        assert "无首板股" in pool["note"]


class TestConfigSanity:
    """阈值与 vault《二板打法》方案口径一致"""

    def test_thresholds(self):
        assert SECOND_BOARD_SEAL_RATIO_MIN == 3.0
        assert SECOND_BOARD_TURNOVER_MAX == 12.0
        assert (SECOND_BOARD_CAP_MIN_YI, SECOND_BOARD_CAP_MAX_YI) == (30.0, 120.0)
        assert (SECOND_BOARD_PRICE_MIN, SECOND_BOARD_PRICE_MAX) == (10.0, 60.0)
        assert SECOND_BOARD_PREV5_GAIN_MAX == 15.0
        assert SECOND_BOARD_MIN_SCORE == 6
        assert SECOND_BOARD_STOP_PCT == 0.10
        assert 5 <= SECOND_BOARD_TOP_N <= 10
