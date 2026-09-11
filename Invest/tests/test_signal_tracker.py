"""
信号追踪测试 — 临时 SQLite + mock 日线，不依赖网络、不碰真实 data/market.db
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.database import get_connection, init_database, save_daily_quotes
from pipeline.signal_tracker import (
    consecutive_stop_outs,
    last_signal_won,
    record_signals,
    settle_signals,
    signal_stats,
    signal_stats_to_markdown,
)


# ---------- 测试数据构造 ----------

def _make_db(tmp_path, rows_by_symbol: dict[str, list[dict]] | None = None) -> Path:
    """建临时 SQLite（含 signals 表）并写入日线"""
    db = tmp_path / "market.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    init_database(db)
    for sym, rows in (rows_by_symbol or {}).items():
        df = pd.DataFrame(rows)
        df["symbol"] = sym
        save_daily_quotes(
            df[["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]],
            db,
        )
    return db


def _q(d: str, low: float, close: float, high: float | None = None) -> dict:
    return {
        "trade_date": d,
        "open": (low + close) / 2,
        "high": high if high is not None else max(low, close) + 0.5,
        "low": low,
        "close": close,
        "volume": 1000.0,
        "amount": 100000.0,
    }


def _rising_rows(days: int = 30, step: float = 0.1, start: str = "2026-01-01") -> list[dict]:
    """递增多头日线：low 每根 +step，close=low+1"""
    dates = pd.date_range(start, periods=days, freq="D").strftime("%Y-%m-%d")
    return [_q(d, low=100.0 + i * step, close=100.0 + i * step + 1.0, high=100.0 + i * step + 2.0)
            for i, d in enumerate(dates)]


def _flat_rows(start: str, days: int) -> list[dict]:
    """横盘日线：low=99 close=100 high=101（不触止损、不破通道）"""
    dates = pd.date_range(start, periods=days, freq="D").strftime("%Y-%m-%d")
    return [_q(d, low=99.0, close=100.0, high=101.0) for d in dates]


def _candidate(close: float = 100.0, atr: float = 2.0, symbol: str = "600519", period: int = 20) -> dict:
    return {"symbol": symbol, "close": close, "channel_high": close * 0.99,
            "breakout_pct": 1.0, "atr_20": atr, "period": period}


def _fetch_signals(db: Path) -> list[dict]:
    with get_connection(db) as conn:
        cur = conn.execute("SELECT * FROM signals ORDER BY signal_date, system")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _insert_closed(db: Path, system: str, r: float, exit_date: str, signal_date: str = "2026-01-05"):
    """直接写入一条已关闭信号（统计测试用）：入场 100 / 止损 96（每股风险 4）"""
    with get_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO signals
            (signal_date, symbol, system, entry_price, stop_price, channel_period,
             status, exit_date, exit_price, exit_reason, r_multiple, created_at)
            VALUES (?, '600519', ?, 100.0, 96.0, 20, 'closed', ?, ?, '测试', ?, '2026-01-05 09:00:00')
            """,
            (signal_date, system, exit_date, round(100.0 + r * 4, 2), r),
        )


# ---------- 信号入库 ----------

class TestRecordSignals:
    def test_record_sets_fields(self, tmp_path):
        """入库字段：入场价=close，止损=close−2×ATR，状态 open"""
        db = _make_db(tmp_path)
        n = record_signals([_candidate(close=103.8, atr=2.0, period=20)], "S1-A", signal_date="2026-01-29", db_path=db)
        assert n == 1
        rows = _fetch_signals(db)
        assert len(rows) == 1
        r = rows[0]
        assert r["signal_date"] == "2026-01-29"
        assert r["symbol"] == "600519"
        assert r["system"] == "S1-A"
        assert r["entry_price"] == pytest.approx(103.8)
        assert r["stop_price"] == pytest.approx(99.8)
        assert r["channel_period"] == 20
        assert r["status"] == "open"
        assert r["exit_date"] is None
        assert r["created_at"]

    def test_dedup_while_open(self, tmp_path):
        """open 期间同 (symbol, system) 不重复入库；旧信号关闭后新日期信号可再入"""
        db = _make_db(tmp_path)
        assert record_signals([_candidate()], "S1-A", signal_date="2026-01-10", db_path=db) == 1
        # 连续突破日：open 期间再次入库被拒绝
        assert record_signals([_candidate()], "S1-A", signal_date="2026-01-11", db_path=db) == 0
        assert len(_fetch_signals(db)) == 1

        with get_connection(db) as conn:
            conn.execute("UPDATE signals SET status = 'closed', exit_date = '2026-01-12'")
        assert record_signals([_candidate()], "S1-A", signal_date="2026-01-13", db_path=db) == 1
        assert len(_fetch_signals(db)) == 2

    def test_different_systems_both_insert(self, tmp_path):
        """同股不同系统（S1-A / S2-A）各自独立入库"""
        db = _make_db(tmp_path)
        assert record_signals([_candidate(period=20)], "S1-A", signal_date="2026-01-10", db_path=db) == 1
        assert record_signals([_candidate(period=55)], "S2-A", signal_date="2026-01-10", db_path=db) == 1
        rows = _fetch_signals(db)
        assert len(rows) == 2
        assert {r["system"] for r in rows} == {"S1-A", "S2-A"}

    def test_atr_fallback_stop(self, tmp_path):
        """ATR 缺失/为 0 时止损降级为收盘价 ×0.95（同 from-scan 口径）"""
        db = _make_db(tmp_path)
        record_signals([_candidate(close=100.0, atr=0.0)], "S1-A", signal_date="2026-01-10", db_path=db)
        assert _fetch_signals(db)[0]["stop_price"] == pytest.approx(95.0)


# ---------- 每日结算 ----------

class TestSettleSignals:
    def test_stop_loss_settlement(self, tmp_path):
        """最低价 ≤ 止损价 → 按止损价关闭，R=−1；逐根回放，exit_date 取最早触发日"""
        rows = _flat_rows("2026-01-05", 6)          # 信号日前横盘
        rows.append(_q("2026-01-11", low=97.0, close=99.0))   # 第 1 根：未触止损
        rows.append(_q("2026-01-12", low=95.5, close=98.0))   # 第 2 根：low ≤ 96 触止损
        db = _make_db(tmp_path, {"600519": rows})
        record_signals([_candidate(close=100.0, atr=2.0)], "S1-A", signal_date="2026-01-10", db_path=db)

        result = settle_signals(db_path=db, settle_date="2026-01-13")
        assert result["checked"] == 1
        assert result["settled"] == 1
        assert result["by_reason"] == {"止损": 1, "通道退出": 0, "到期": 0}
        d = result["details"][0]
        assert d["exit_reason"] == "止损"
        assert d["exit_date"] == "2026-01-12"   # 最早触发日，而非最新行情日
        assert d["exit_price"] == pytest.approx(96.0)
        assert d["r_multiple"] == pytest.approx(-1.0)

        row = _fetch_signals(db)[0]
        assert row["status"] == "closed"
        assert row["r_multiple"] == pytest.approx(-1.0)

    def test_stop_priority_over_channel(self, tmp_path):
        """同日止损与通道同时触发 → 先判止损（保守）"""
        rows = _rising_rows(days=12, step=0.5)      # 10 日通道下轨=101.0
        rows.append(_q("2026-01-13", low=100.5, close=100.8, high=102.0))  # 止损+破通道
        db = _make_db(tmp_path, {"600519": rows})
        record_signals([_candidate(close=105.0, atr=2.0)], "S1-A", signal_date="2026-01-12", db_path=db)

        result = settle_signals(db_path=db, settle_date="2026-01-14")
        d = result["details"][0]
        assert d["exit_reason"] == "止损"
        assert d["exit_price"] == pytest.approx(101.0)
        assert d["r_multiple"] == pytest.approx(-1.0)

    def test_channel_exit_settlement(self, tmp_path):
        """收盘价 < 10 日通道下轨（未触止损）→ 通道退出，按收盘价结算"""
        rows = _rising_rows(days=30)                # 末日通道下轨=101.9
        rows[-1] = _q("2026-01-30", low=100.5, close=101.0, high=103.0)   # 收盘破下轨但未触止损
        db = _make_db(tmp_path, {"600519": rows})
        record_signals([_candidate(close=103.8, atr=2.0)], "S1-A", signal_date="2026-01-29", db_path=db)

        result = settle_signals(db_path=db, settle_date="2026-01-31")
        d = result["details"][0]
        assert d["exit_reason"] == "通道退出"
        assert d["exit_date"] == "2026-01-30"
        assert d["exit_price"] == pytest.approx(101.0)
        # R = (101.0 − 103.8) / (103.8 − 99.8) = −0.7
        assert d["r_multiple"] == pytest.approx(-0.7)

    def test_expiry_settlement(self, tmp_path):
        """满 20 个交易日未触发退出 → 到期，按第 20 根收盘价关闭"""
        rows = _flat_rows("2026-01-02", 30)
        db = _make_db(tmp_path, {"600519": rows})
        record_signals([_candidate(close=100.0, atr=2.0)], "S1-A", signal_date="2026-01-01", db_path=db)

        result = settle_signals(db_path=db, settle_date="2026-02-01")
        d = result["details"][0]
        assert d["exit_reason"] == "到期"
        assert d["exit_date"] == "2026-01-21"   # 信号日后第 20 个交易日，而非最新行情日
        assert d["exit_price"] == pytest.approx(100.0)
        assert d["r_multiple"] == pytest.approx(0.0)

    def test_same_day_signal_not_settled(self, tmp_path):
        """当天新记录的信号（signal_date == 结算日）不被当天结算"""
        rows = _flat_rows("2026-01-05", 6)
        rows.append(_q("2026-01-11", low=95.0, close=98.0))   # 若结算必触止损
        db = _make_db(tmp_path, {"600519": rows})
        record_signals([_candidate()], "S1-A", signal_date="2026-01-10", db_path=db)

        result = settle_signals(db_path=db, settle_date="2026-01-10")
        assert result["checked"] == 0
        assert result["settled"] == 0
        assert _fetch_signals(db)[0]["status"] == "open"

        # 下一结算日正常结算
        result = settle_signals(db_path=db, settle_date="2026-01-12")
        assert result["settled"] == 1

    def test_no_quotes_skipped(self, tmp_path):
        """market.db 无该股行情 → 跳过不关闭，计入 still_open"""
        db = _make_db(tmp_path, {"600519": _flat_rows("2026-01-02", 5)})
        record_signals([_candidate(symbol="999999")], "S1-A", signal_date="2026-01-01", db_path=db)
        result = settle_signals(db_path=db, settle_date="2026-01-10")
        assert result["checked"] == 1
        assert result["settled"] == 0
        assert result["still_open"] == 1
        assert _fetch_signals(db)[0]["status"] == "open"


# ---------- 分组统计 ----------

class TestSignalStats:
    def _build_stats_db(self, tmp_path) -> Path:
        db = _make_db(tmp_path)
        # S1-A：+2R ×3、−1R ×3（窗口内）；另有 1 条窗口外（2025-12-15）应被排除
        for i, r in enumerate([2.0, 2.0, 2.0, -1.0, -1.0, -1.0]):
            _insert_closed(db, "S1-A", r, exit_date=f"2026-02-{i + 1:02d}", signal_date=f"2026-01-{i + 5:02d}")
        _insert_closed(db, "S1-A", 5.0, exit_date="2025-12-15", signal_date="2025-12-01")
        # S2-A：3 条（样本不足）
        for i, r in enumerate([1.0, -1.0, -1.0]):
            _insert_closed(db, "S2-A", r, exit_date=f"2026-03-{i + 1:02d}", signal_date=f"2026-02-{i + 5:02d}")
        # 1 条 open 信号
        record_signals([_candidate()], "S1-A", signal_date="2026-03-20", db_path=db)
        return db

    def test_grouping_and_min_sample(self, tmp_path):
        db = self._build_stats_db(tmp_path)
        stats = signal_stats(db_path=db, days=90, as_of="2026-04-01")

        s1 = stats["systems"]["S1-A"]
        assert s1["closed"] == 6                    # 窗口外那条被排除
        assert s1["win_rate"] == pytest.approx(50.0)
        assert s1["avg_r"] == pytest.approx(0.5)
        assert s1["expectancy"] == pytest.approx(0.5)
        assert s1["profit_factor"] == pytest.approx(2.0)
        assert s1["sample_sufficient"] is True
        assert s1["note"] == ""

        s2 = stats["systems"]["S2-A"]
        assert s2["closed"] == 3
        assert s2["sample_sufficient"] is False
        assert s2["note"] == "样本不足"

        assert stats["open_count"] == 1
        assert stats["open_by_system"] == {"S1-A": 1}

    def test_markdown_output(self, tmp_path):
        db = self._build_stats_db(tmp_path)
        md = signal_stats_to_markdown(signal_stats(db_path=db, days=90, as_of="2026-04-01"))
        assert "| S1-A | 6 | 50.0% | +0.50 | +0.50 | 2.00 |" in md
        assert "S1-A 近 90 天期望值 +0.50R，样本 6" in md
        assert "S2-A 样本不足（3 < 5），继续观察" in md
        assert "open 信号 1 个" in md

    def test_empty_db(self, tmp_path):
        db = _make_db(tmp_path)
        stats = signal_stats(db_path=db, days=90, as_of="2026-04-01")
        assert stats["systems"] == {}
        assert stats["open_count"] == 0
        assert "_暂无已关闭信号_" in signal_stats_to_markdown(stats)


# ---------- 全流程顺序：记录 → 结算 → 再记录 ----------

class TestFullFlow:
    def test_settle_then_record_order(self, tmp_path):
        """旧信号关闭后才可再入库；当天新信号不会被当天结算"""
        rows = _flat_rows("2026-01-05", 4)
        rows.append(_q("2026-01-09", low=97.0, close=99.0))   # 未触止损
        rows.append(_q("2026-01-10", low=95.0, close=98.0))   # 触止损
        db = _make_db(tmp_path, {"600519": rows})

        # 第 1 天：扫描记录信号；同日重复扫描去重
        assert record_signals([_candidate()], "S1-A", signal_date="2026-01-08", db_path=db) == 1
        assert record_signals([_candidate()], "S1-A", signal_date="2026-01-08", db_path=db) == 0

        # 第 2 天：先结算旧信号（止损触发）→ 再记录当日新信号 → 当日新信号不被立即结算
        result = settle_signals(db_path=db, settle_date="2026-01-11")
        assert result["settled"] == 1
        assert result["details"][0]["exit_reason"] == "止损"

        assert record_signals([_candidate()], "S1-A", signal_date="2026-01-11", db_path=db) == 1
        result = settle_signals(db_path=db, settle_date="2026-01-11")
        assert result["checked"] == 0               # 当天新信号被 signal_date < 当天 过滤
        assert result["settled"] == 0

        rows_all = _fetch_signals(db)
        assert len(rows_all) == 2
        assert [r["status"] for r in rows_all] == ["closed", "open"]


# ---------- 结算数据链修复：补抓 / 数据缺失关闭 / 入场形态分层 ----------

def _insert_closed_sym(
    db: Path,
    symbol: str,
    system: str,
    r: float,
    exit_date: str,
    signal_date: str,
):
    """直接写入一条指定代码/系统的已关闭信号（入场形态分层统计用）"""
    with get_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO signals
            (signal_date, symbol, system, entry_price, stop_price, channel_period,
             status, exit_date, exit_price, exit_reason, r_multiple, created_at)
            VALUES (?, ?, ?, 100.0, 96.0, 20, 'closed', ?, ?, '测试', ?, '2026-01-01 09:00:00')
            """,
            (signal_date, symbol, system, exit_date, round(100.0 + r * 4, 2), r),
        )


class TestSettleRefetch:
    def test_refetch_fills_missing_then_settles(self, tmp_path):
        """缺K线信号：注入 fetch stub 补出后续日线 → 正常到期结算；fetch 参数=（代码，信号日）"""
        rows = _flat_rows("2026-01-01", 10)          # K线只到信号日 01-10
        db = _make_db(tmp_path, {"600519": rows})
        record_signals([_candidate()], "HOT-S", signal_date="2026-01-10", db_path=db)

        calls = []

        def fake_fetch(symbol, start_date):
            calls.append((symbol, start_date))
            df = pd.DataFrame([_q(d, 99.0, 100.0) for d in
                               ["2026-01-11", "2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15"]])
            df["symbol"] = symbol
            return df

        result = settle_signals(db_path=db, settle_date="2026-01-20",
                                refetch_missing=True, fetch_fn=fake_fetch)
        assert calls == [("600519", "2026-01-10")]
        assert result["refetched_symbols"] == 1
        assert result["settled"] == 1
        d = result["details"][0]
        assert d["exit_reason"] == "到期"            # HOT-S 第 5 根强制结算
        assert d["exit_date"] == "2026-01-15"

    def test_refetch_failure_degrades_to_open(self, tmp_path):
        """补抓抛异常 → 降级保持 open（不阻塞结算流程）"""
        rows = _flat_rows("2026-01-01", 10)
        db = _make_db(tmp_path, {"600519": rows})
        record_signals([_candidate()], "HOT-S", signal_date="2026-01-10", db_path=db)

        def boom(symbol, start_date):
            raise ConnectionError("network down")

        result = settle_signals(db_path=db, settle_date="2026-01-12",
                                refetch_missing=True, fetch_fn=boom)
        assert result["settled"] == 0
        assert result["still_open"] == 1
        assert _fetch_signals(db)[0]["status"] == "open"

    def test_refetch_cap_oldest_first(self, tmp_path, monkeypatch):
        """补抓上限：最老信号优先，超出上限的股票留待下一日"""
        from pipeline import signal_tracker as st

        monkeypatch.setattr(st, "SIGNAL_SETTLE_REFETCH_MAX", 1)
        db = _make_db(tmp_path, {"600519": _flat_rows("2026-01-01", 10),   # K线止于信号日 01-10
                                 "000001": _flat_rows("2026-01-01", 8)})   # K线止于信号日 01-08（更老）
        record_signals([_candidate(symbol="600519")], "HOT-S", signal_date="2026-01-10", db_path=db)
        record_signals([_candidate(symbol="000001")], "HOT-S", signal_date="2026-01-08", db_path=db)

        fetched = []

        def fake_fetch(symbol, start_date):
            fetched.append(symbol)
            return pd.DataFrame()

        settle_signals(db_path=db, settle_date="2026-01-12",
                       refetch_missing=True, fetch_fn=fake_fetch)
        assert fetched == ["000001"]                 # 01-08 比 01-10 老，上限 1 只


class TestStaleClose:
    def test_stale_signal_closed_as_data_missing(self, tmp_path):
        """超龄（> 最大持有×1.7+宽限 自然日）仍无信号日后K线 → 「数据缺失」关闭，R 置空"""
        rows = _flat_rows("2026-01-01", 10)
        db = _make_db(tmp_path, {"600519": rows})
        record_signals([_candidate()], "HOT-S", signal_date="2026-01-10", db_path=db)

        result = settle_signals(db_path=db, settle_date="2026-02-10")   # 31 天 > 18 天门槛
        assert result["settled"] == 0
        assert result["data_missing_closed"] == 1
        assert result["still_open"] == 0
        row = _fetch_signals(db)[0]
        assert row["status"] == "closed"
        assert row["exit_reason"] == "数据缺失"
        assert row["r_multiple"] is None

    def test_data_missing_excluded_from_stats(self, tmp_path):
        """数据缺失关闭不进胜率/R 统计，单独计数并写入 Markdown 披露"""
        rows = _flat_rows("2026-01-01", 10)
        db = _make_db(tmp_path, {"600519": rows})
        record_signals([_candidate()], "HOT-S", signal_date="2026-01-10", db_path=db)
        settle_signals(db_path=db, settle_date="2026-02-10")

        stats = signal_stats(db_path=db, days=90, as_of="2026-02-11")
        assert stats["systems"] == {}                # 无 R 统计
        assert stats["data_missing"] == {"HOT-S": 1}
        md = signal_stats_to_markdown(stats)
        assert "另有 1 条信号因数据缺失关闭" in md

    def test_stale_within_grace_stays_open(self, tmp_path):
        """宽限期内（HOT-S：5×1.7+10=18 自然日）缺数据保持 open，等待补抓"""
        rows = _flat_rows("2026-01-01", 10)
        db = _make_db(tmp_path, {"600519": rows})
        record_signals([_candidate()], "HOT-S", signal_date="2026-01-10", db_path=db)

        result = settle_signals(db_path=db, settle_date="2026-01-25")   # 15 天 ≤ 18
        assert result["data_missing_closed"] == 0
        assert result["still_open"] == 1
        assert _fetch_signals(db)[0]["status"] == "open"

    def test_s1a_grace_longer_than_hot(self, tmp_path):
        """S1-A（20 交易日持有）宽限门槛更宽：31 天缺数据仍 open（20×1.7+10=44）"""
        rows = _flat_rows("2026-01-01", 10)
        db = _make_db(tmp_path, {"600519": rows})
        record_signals([_candidate()], "S1-A", signal_date="2026-01-10", db_path=db)

        result = settle_signals(db_path=db, settle_date="2026-02-10")   # 31 天 ≤ 44
        assert result["data_missing_closed"] == 0
        assert result["still_open"] == 1


class TestEntryTypeStats:
    def test_by_entry_type_groups(self, tmp_path):
        """信号日形态分层：一字板（开=收=最高）/ 涨停收盘（收=最高）/ 非涨停 / 无K线→未知"""
        def _bar(d, o, h, l, c):
            return {"trade_date": d, "open": o, "high": h, "low": l, "close": c,
                    "volume": 1000.0, "amount": 100000.0}

        db = _make_db(tmp_path, {
            "600001": [_bar("2026-01-05", 11.0, 11.0, 11.0, 11.0)],   # 一字板
            "600002": [_bar("2026-01-06", 10.0, 11.0, 9.9, 11.0)],    # 涨停收盘
            "600003": [_bar("2026-01-07", 10.0, 10.8, 9.9, 10.5)],    # 非涨停
        })
        _insert_closed_sym(db, "600001", "HOT-S", 5.0, "2026-01-10", "2026-01-05")
        _insert_closed_sym(db, "600002", "HOT-S", 1.0, "2026-01-10", "2026-01-06")
        _insert_closed_sym(db, "600003", "HOT-S", -1.0, "2026-01-10", "2026-01-07")
        _insert_closed_sym(db, "600004", "HOT-S", 2.0, "2026-01-10", "2026-01-08")  # 无K线

        stats = signal_stats(db_path=db, days=90, as_of="2026-02-01")
        et = stats["by_entry_type"]
        assert set(et) == {"一字板", "涨停收盘", "非涨停", "未知"}
        assert et["一字板"]["HOT-S"]["closed"] == 1
        assert et["涨停收盘"]["HOT-S"]["closed"] == 1
        assert et["非涨停"]["HOT-S"]["closed"] == 1
        assert et["非涨停"]["HOT-S"]["avg_r"] == pytest.approx(-1.0)
        assert stats["board_locked_by_system"] == {"HOT-S": 2}

        md = signal_stats_to_markdown(stats)
        assert "按信号日入场形态分层" in md
        assert "| 一字板 | HOT-S |" in md
        assert "| 非涨停 | HOT-S |" in md
        assert "含一字/涨停收盘 2 条" in md


# ---------- S1-A 系统1过滤（V5.0 §4.3）：连续假突破冷却 / 上次盈利降仓 ----------

def _insert_closed_v2(
    db: Path,
    symbol: str,
    system: str,
    exit_reason: str,
    exit_date: str,
    r_multiple: float = -1.0,
    signal_date: str = "2026-07-01",
):
    """直接写入一条已关闭信号（指定代码/系统/退出原因）"""
    with get_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO signals
            (signal_date, symbol, system, entry_price, stop_price, channel_period,
             status, exit_date, exit_price, exit_reason, r_multiple, created_at)
            VALUES (?, ?, ?, 100.0, 96.0, 20, 'closed', ?, ?, ?, ?, '2026-07-01 09:00:00')
            """,
            (signal_date, symbol, system, exit_date,
             round(100.0 + r_multiple * 4, 2), exit_reason, r_multiple),
        )


class TestSystem1FilterQueries:
    def test_consecutive_stop_outs_counted(self, tmp_path):
        """连续 3 次止损退出 → 计数 3，最近一次退出日正确"""
        db = _make_db(tmp_path)
        for i, d in enumerate(["2026-07-28", "2026-07-30", "2026-08-01"]):
            _insert_closed_v2(db, "600519", "S1-A", "止损", d, signal_date=f"2026-07-2{i}")
        consec, last = consecutive_stop_outs("600519", "S1-A", db_path=db)
        assert consec == 3
        assert last == "2026-08-01"

    def test_consecutive_interrupted_by_profit_exit(self, tmp_path):
        """最近一次为通道退出（非止损）→ 连续止损计数中断为 0"""
        db = _make_db(tmp_path)
        _insert_closed_v2(db, "600519", "S1-A", "止损", "2026-07-28", signal_date="2026-07-20")
        _insert_closed_v2(db, "600519", "S1-A", "通道退出", "2026-08-01",
                          r_multiple=2.0, signal_date="2026-07-25")
        consec, last = consecutive_stop_outs("600519", "S1-A", db_path=db)
        assert consec == 0
        assert last is None

    def test_last_signal_won(self, tmp_path):
        """无历史 → None；最近 R>0 → True；最近止损 → False"""
        db = _make_db(tmp_path)
        assert last_signal_won("600519", "S1-A", db_path=db) is None
        _insert_closed_v2(db, "600519", "S1-A", "通道退出", "2026-08-01", r_multiple=1.5)
        assert last_signal_won("600519", "S1-A", db_path=db) is True
        _insert_closed_v2(db, "600519", "S1-A", "止损", "2026-08-03", signal_date="2026-08-02")
        assert last_signal_won("600519", "S1-A", db_path=db) is False


def _prepared_frame(high_early: float = 110.0, close_last: float = 100.0, days: int = 70):
    """构造带通道/ATR 的准备帧：前期高点 110（55 日通道高），近期回落至 100 横盘"""
    from pipeline.indicators import prepare_stock_indicators

    dates = pd.date_range("2026-05-01", periods=days, freq="B").strftime("%Y-%m-%d")
    n = days
    df = pd.DataFrame({
        "trade_date": dates,
        "open": [close_last] * n,
        "high": [high_early] * (n - 15) + [close_last + 1.0] * 15,
        "low": [close_last - 1.0] * n,
        "close": [close_last] * n,
        "volume": [1e6] * n,
        "amount": [1e8] * n,
    })
    return prepare_stock_indicators(df)


def _breakout_row(symbol: str = "600519", close: float = 100.0, atr: float = 2.0) -> pd.DataFrame:
    return pd.DataFrame([{
        "symbol": symbol, "trade_date": "2026-08-05", "close": close,
        "channel_high": 99.0, "breakout_pct": 1.0, "atr_20": atr, "period": 20,
    }])


class TestEnrichBreakout:
    def test_cooldown_blocks_recording(self, tmp_path):
        """连续 3 次假突破且最近在冷却期内 → record=False，备注「冷却中」"""
        from pipeline.market_scanner import _enrich_breakout

        db = _make_db(tmp_path)
        for i, d in enumerate(["2026-07-28", "2026-07-30", "2026-08-01"]):
            _insert_closed_v2(db, "600519", "S1-A", "止损", d, signal_date=f"2026-07-2{i}")
        out = _enrich_breakout(
            _breakout_row(), {"600519": _prepared_frame()}, {},
            {"600519": "半导体"}, "S1-A", "2026-08-05", db_path=db,
        )
        row = out.iloc[0]
        assert not row["record"]
        assert "冷却中" in row["note"]

    def test_cooldown_expired_records_normally(self, tmp_path):
        """连续止损但最近一次退出已超冷却期 → 照常入库"""
        from pipeline.market_scanner import _enrich_breakout

        db = _make_db(tmp_path)
        for i, d in enumerate(["2026-06-01", "2026-06-03", "2026-06-05"]):
            _insert_closed_v2(db, "600519", "S1-A", "止损", d, signal_date=f"2026-05-2{i}")
        out = _enrich_breakout(
            _breakout_row(), {"600519": _prepared_frame()}, {},
            {"600519": "半导体"}, "S1-A", "2026-08-05", db_path=db,
        )
        row = out.iloc[0]
        assert row["record"]
        assert "冷却中" not in row["note"]

    def test_last_won_far_from_55d_high_suggests_half_position(self, tmp_path):
        """上次突破盈利且现价距 55 日高点 >1×ATR → 备注「首仓建议降50%」，照常入库"""
        from pipeline.market_scanner import _enrich_breakout

        db = _make_db(tmp_path)
        _insert_closed_v2(db, "600519", "S1-A", "通道退出", "2026-08-01", r_multiple=2.0)
        out = _enrich_breakout(
            _breakout_row(close=100.0, atr=2.0), {"600519": _prepared_frame()}, {},
            {"600519": "半导体"}, "S1-A", "2026-08-05", db_path=db,
        )
        row = out.iloc[0]
        assert row["record"]
        assert "首仓建议降50%" in row["note"]

    def test_no_history_filters_note_only(self, tmp_path):
        """无信号历史 → 照常入库；滤网未全通过（周线数据不足/板块未知）标注观察级"""
        from pipeline.market_scanner import _enrich_breakout

        db = _make_db(tmp_path)
        out = _enrich_breakout(
            _breakout_row(), {"600519": _prepared_frame()}, {},
            {"600519": "半导体"}, "S1-A", "2026-08-05", db_path=db,
        )
        row = out.iloc[0]
        assert row["record"]
        assert "滤网未全通过" in row["note"]
        assert row["filters_passed"] < row["filters_required"]

    def test_s2a_skips_system1_checks(self, tmp_path):
        """S2-A 信号不做系统1过滤（冷却/降仓仅适用 S1 系列）"""
        from pipeline.market_scanner import _enrich_breakout

        db = _make_db(tmp_path)
        for i, d in enumerate(["2026-07-28", "2026-07-30", "2026-08-01"]):
            _insert_closed_v2(db, "600519", "S2-A", "止损", d, signal_date=f"2026-07-2{i}")
        out = _enrich_breakout(
            _breakout_row(), {"600519": _prepared_frame()}, {},
            {"600519": "半导体"}, "S2-A", "2026-08-05", db_path=db,
        )
        row = out.iloc[0]
        assert row["record"]
        assert "冷却中" not in row["note"]
