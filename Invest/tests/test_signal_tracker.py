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
