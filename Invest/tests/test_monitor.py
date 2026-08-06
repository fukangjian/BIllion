"""
持仓监控测试 — 临时 trades.json + 临时 SQLite 日线，不依赖网络
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.database import init_database, save_daily_quotes
from review.monitor import (
    _exit_channel_period,
    check_positions,
    derive_drawdown_state,
    run_monitor,
)
from review.trade_log import TradeLog


# ---------- 测试数据构造 ----------

def _make_log(tmp_path, trades: list[dict]) -> TradeLog:
    """写临时 trades.json 并返回 TradeLog"""
    f = tmp_path / "trades.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(trades, ensure_ascii=False, indent=2), encoding="utf-8")
    return TradeLog(f)


def _make_db(tmp_path, rows_by_symbol: dict[str, list[dict]]) -> Path:
    """建临时 SQLite 并写入日线"""
    db = tmp_path / "market.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    init_database(db)
    for sym, rows in rows_by_symbol.items():
        df = pd.DataFrame(rows)
        df["symbol"] = sym
        save_daily_quotes(
            df[["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]],
            db,
        )
    return db


def _quote_rows(days: int = 30, last_close: float | None = None) -> list[dict]:
    """
    构造递增多头日线：low 每根 +0.1，振幅 2（ATR≈2）。
    最后一根（2026-06-30）默认收盘 103.9；
    10 日通道下轨=101.9（前 10 根最低价最小值），20 日通道下轨=100.9。
    """
    dates = pd.date_range("2026-06-01", periods=days, freq="D").strftime("%Y-%m-%d")
    rows = []
    for i, d in enumerate(dates):
        low = 100.0 + i * 0.1
        rows.append({
            "trade_date": d,
            "open": low + 1.0,
            "high": low + 2.0,
            "low": low,
            "close": low + 1.0,
            "volume": 1000.0,
            "amount": 100000.0,
        })
    if last_close is not None:
        rows[-1]["close"] = last_close
        rows[-1]["low"] = min(rows[-1]["low"], last_close)
        rows[-1]["high"] = max(rows[-1]["high"], last_close)
    return rows


def _open_trade(**kw) -> dict:
    d = {
        "交易编号": "T20260720_t01",
        "日期": "2026-07-20",
        "股票代码": "600519",
        "股票名称": "测试股",
        "账户类型": "核心",
        "风险簇": "",
        "入场系统": "S1-A",
        "入场价": 102.0,
        "止损价": 95.0,
        "风险率": 0.5,
        "股数": 100,
        "仓位金额": 10200.0,
        "实际退出价": None,
        "退出日期": None,
        "是否系统内交易": True,
    }
    d.update(kw)
    return d


def _closed_trade(r: float, exit_date: str = "2026-07-25", risk: float = 0.5, trade_id: str = "") -> dict:
    """构造已平仓交易：入场 100 / 止损 90（每股风险 10），按目标 R 反推退出价"""
    entry, stop = 100.0, 90.0
    return {
        "交易编号": trade_id or f"T20260701_r{r}",
        "日期": "2026-07-01",
        "股票代码": "600519",
        "股票名称": "测试股",
        "账户类型": "核心",
        "入场系统": "S1-A",
        "入场价": entry,
        "止损价": stop,
        "风险率": risk,
        "股数": 100,
        "仓位金额": entry * 100,
        "实际退出价": entry + r * (entry - stop),
        "退出日期": exit_date,
        "退出原因": "测试",
        "是否系统内交易": True,
        "R倍数": r,
    }


def _loss_series(n_wins: int, n_losses: int) -> list[dict]:
    """先盈后亏的 R 序列（+2R × n_wins，随后 -1R × n_losses）"""
    trades = []
    for i in range(n_wins):
        trades.append(_closed_trade(2.0, exit_date=f"2026-06-{i + 1:02d}", trade_id=f"W{i}"))
    for i in range(n_losses):
        trades.append(_closed_trade(-1.0, exit_date=f"2026-06-{n_wins + i + 1:02d}", trade_id=f"L{i}"))
    return trades


# ---------- 通道周期选择 ----------

class TestExitChannelPeriod:
    def test_s1_uses_10(self):
        assert _exit_channel_period("S1-A") == 10
        assert _exit_channel_period("S1-A快速") == 10

    def test_s2_uses_20(self):
        assert _exit_channel_period("S2-A") == 20
        assert _exit_channel_period("S2-A慢速") == 20

    def test_other_systems_no_channel(self):
        assert _exit_channel_period("预埋") is None
        assert _exit_channel_period("") is None
        assert _exit_channel_period("手动") is None


# ---------- 持仓检查 ----------

class TestCheckPositions:
    def test_stop_alert(self, tmp_path):
        """收盘价 ≤ 止损价 → 止损警报（最高优先级）"""
        log = _make_log(tmp_path, [_open_trade(止损价=101.0)])
        db = _make_db(tmp_path, {"600519": _quote_rows(last_close=100.5)})
        result = check_positions(log, db_path=db)
        assert len(result["alerts"]) == 1
        a = result["alerts"][0]
        assert a["类型"] == "止损"
        assert a["现价"] == pytest.approx(100.5)
        assert a["建议动作"]
        assert result["positions_ok"] == []

    def test_exit_channel_alert(self, tmp_path):
        """S1-A 收盘跌破 10 日通道下轨（未触止损）→ 退出信号"""
        log = _make_log(tmp_path, [_open_trade(入场系统="S1-A", 止损价=95.0)])
        db = _make_db(tmp_path, {"600519": _quote_rows(last_close=100.5)})
        result = check_positions(log, db_path=db)
        assert len(result["alerts"]) == 1
        a = result["alerts"][0]
        assert a["类型"] == "退出"
        assert a["通道下轨"] == pytest.approx(101.9)
        assert "10" in a["建议动作"]

    def test_s1_breaks_but_s2_holds(self, tmp_path):
        """同一价格 101.5：跌破 10 日下轨(101.9) 但未跌破 20 日下轨(100.9)"""
        log_s1 = _make_log(tmp_path / "s1", [_open_trade(入场系统="S1-A", 止损价=95.0)])
        log_s2 = _make_log(tmp_path / "s2", [_open_trade(入场系统="S2-A", 止损价=95.0)])
        db = _make_db(tmp_path / "db", {"600519": _quote_rows(last_close=101.5)})

        r1 = check_positions(log_s1, db_path=db)
        assert [a["类型"] for a in r1["alerts"]] == ["退出"]

        r2 = check_positions(log_s2, db_path=db)
        assert r2["alerts"] == []
        assert len(r2["positions_ok"]) == 1
        assert r2["positions_ok"][0]["通道下轨"] == pytest.approx(100.9)

    def test_near_stop_alert(self, tmp_path):
        """距止损 < 1N（ATR 倍数）→ 接近止损提示"""
        # 默认收盘 103.9，ATR≈2，止损 102.5 → 距止损 0.7N
        log = _make_log(tmp_path, [_open_trade(入场价=103.0, 止损价=102.5)])
        db = _make_db(tmp_path, {"600519": _quote_rows()})
        result = check_positions(log, db_path=db)
        assert len(result["alerts"]) == 1
        a = result["alerts"][0]
        assert a["类型"] == "接近止损"
        assert a["距止损N"] == pytest.approx(0.7, abs=0.05)

    def test_ok_position(self, tmp_path):
        """远离止损且未破通道 → 正常持仓，含浮动R与数据日期"""
        log = _make_log(tmp_path, [_open_trade(入场价=102.0, 止损价=95.0)])
        db = _make_db(tmp_path, {"600519": _quote_rows()})
        result = check_positions(log, db_path=db)
        assert result["alerts"] == []
        assert len(result["positions_ok"]) == 1
        p = result["positions_ok"][0]
        assert p["现价"] == pytest.approx(103.9)
        assert p["浮动R"] == pytest.approx(round((103.9 - 102.0) / 7.0, 2))
        assert p["数据日期"] == "2026-06-30"
        assert result["data_date"] == "2026-06-30"

    def test_empty_positions(self, tmp_path):
        """无持仓 → 无警报，回撤状态 Normal 并标注样本为空"""
        log = _make_log(tmp_path, [])
        result = check_positions(log, db_path=tmp_path / "nonexistent.db")
        assert result["alerts"] == []
        assert result["positions_ok"] == []
        assert result["data_date"] is None
        dd = result["drawdown_state"]
        assert dd["state"] == "Normal"
        assert "样本为空" in dd["note"]

    def test_no_quote_data(self, tmp_path):
        """market.db 无该股行情 → 正常持仓中标注未检查"""
        log = _make_log(tmp_path, [_open_trade(股票代码="999999")])
        db = _make_db(tmp_path, {"600519": _quote_rows()})
        result = check_positions(log, db_path=db)
        assert result["alerts"] == []
        assert len(result["positions_ok"]) == 1
        assert "无该股行情" in result["positions_ok"][0]["备注"]

    def test_non_s_system_only_checks_stop(self, tmp_path):
        """预埋等非 S 系统：跌破通道位不报警，跌破止损才报警"""
        db = _make_db(tmp_path / "db", {"600519": _quote_rows(last_close=100.5)})

        log_ok = _make_log(tmp_path / "ok", [_open_trade(入场系统="预埋", 止损价=95.0)])
        result = check_positions(log_ok, db_path=db)
        assert result["alerts"] == []
        assert result["positions_ok"][0]["通道下轨"] is None

        log_stop = _make_log(tmp_path / "stop", [_open_trade(入场系统="预埋", 止损价=101.0)])
        result = check_positions(log_stop, db_path=db)
        assert [a["类型"] for a in result["alerts"]] == ["止损"]

    def test_result_structure(self, tmp_path):
        log = _make_log(tmp_path, [_open_trade()])
        db = _make_db(tmp_path, {"600519": _quote_rows()})
        result = check_positions(log, db_path=db)
        for key in ("date", "data_date", "alerts", "positions_ok", "drawdown_state"):
            assert key in result

    def test_run_monitor_writes_json(self, tmp_path):
        """run_monitor 输出 position_monitor_{date}.json"""
        log = _make_log(tmp_path, [_open_trade(止损价=101.0)])
        db = _make_db(tmp_path / "db", {"600519": _quote_rows(last_close=100.5)})
        out = tmp_path / "out"
        result = run_monitor(trade_log=log, db_path=db, output_dir=out)
        json_path = out / f"position_monitor_{result['date']}.json"
        assert json_path.exists()
        saved = json.loads(json_path.read_text(encoding="utf-8"))
        assert saved["alerts"][0]["类型"] == "止损"
        assert "drawdown_state" in saved


# ---------- 回撤状态推导 ----------

class TestDeriveDrawdownState:
    def test_empty_sample(self, tmp_path):
        log = _make_log(tmp_path, [])
        dd = derive_drawdown_state(log)
        assert dd["state"] == "Normal"
        assert "样本为空" in dd["note"]

    def test_normal_state(self, tmp_path):
        """+2R、-1R → 回撤 1R × 0.5% = 0.5% → Normal"""
        log = _make_log(tmp_path, [_closed_trade(2.0, trade_id="a"), _closed_trade(-1.0, trade_id="b")])
        dd = derive_drawdown_state(log)
        assert dd["state"] == "Normal"
        assert dd["drawdown_pct"] == pytest.approx(0.5)

    def test_caution_state(self, tmp_path):
        """峰值 +4R 后连亏 12R → 回撤 12R × 0.5% = 6% → Caution"""
        log = _make_log(tmp_path, _loss_series(2, 12))
        dd = derive_drawdown_state(log)
        assert dd["state"] == "Caution"
        assert dd["drawdown_pct"] == pytest.approx(6.0)

    def test_defensive_state(self, tmp_path):
        """回撤 16R × 0.5% = 8% → Defensive"""
        log = _make_log(tmp_path, _loss_series(2, 16))
        dd = derive_drawdown_state(log)
        assert dd["state"] == "Defensive"

    def test_review_state(self, tmp_path):
        """回撤 24R × 0.5% = 12% → Review"""
        log = _make_log(tmp_path, _loss_series(2, 24))
        dd = derive_drawdown_state(log)
        assert dd["state"] == "Review"

    def test_floating_r_counts(self, tmp_path):
        """无已平仓时，未平仓浮动 R 计入回撤（4 只各 -3R → 12R × 0.5% = 6% → Caution）"""
        trades = [
            _open_trade(交易编号=f"F{i}", 入场价=100.0, 止损价=90.0, 风险率=0.5)
            for i in range(4)
        ]
        log = _make_log(tmp_path, trades)
        db = _make_db(tmp_path / "db", {"600519": _quote_rows(last_close=70.0)})
        dd = derive_drawdown_state(log, db_path=db)
        assert dd["floating_r"] == pytest.approx(-12.0)
        assert dd["state"] == "Caution"

    def test_monthly_event_trade_halt(self, tmp_path):
        """当月已实现 -9R × 0.5% = -4.5% → 停事件交易，未触停止开仓"""
        this_month = datetime.now().strftime("%Y-%m-15")
        log = _make_log(tmp_path, [_closed_trade(-9.0, exit_date=this_month)])
        dd = derive_drawdown_state(log)
        assert dd["monthly_flags"]["event_trade_halt"] is True
        assert dd["monthly_flags"]["new_position_halt"] is False

    def test_monthly_new_position_halt(self, tmp_path):
        """当月已实现 -13R × 0.5% = -6.5% → 停事件交易 + 停止开仓"""
        this_month = datetime.now().strftime("%Y-%m-15")
        log = _make_log(tmp_path, [_closed_trade(-13.0, exit_date=this_month)])
        dd = derive_drawdown_state(log)
        assert dd["monthly_flags"]["event_trade_halt"] is True
        assert dd["monthly_flags"]["new_position_halt"] is True

    def test_env_conflict_uses_derived(self, tmp_path, monkeypatch):
        """环境变量与推导值冲突 → 以推导值为准并标注"""
        monkeypatch.setenv("DRAWDOWN_STATE", "Normal")
        log = _make_log(tmp_path, _loss_series(2, 12))
        dd = derive_drawdown_state(log)
        assert dd["state"] == "Caution"
        assert dd["env"] == "Normal"
        assert dd["conflict"] is True
        assert "人工确认" in dd["note"]

    def test_env_match_no_conflict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRAWDOWN_STATE", "Caution")
        log = _make_log(tmp_path, _loss_series(2, 12))
        dd = derive_drawdown_state(log)
        assert dd["conflict"] is False


# ---------- 移动止损建议（V5.0 §7.4 具体化：+1R 保本 / +2R 兑现 1/3 + 保护位上移） ----------

class TestTrailingStopAdvice:
    def test_r1_suggests_breakeven(self, tmp_path):
        """浮动R ≥ +1R 且止损仍低于成本 → 建议止损上移至成本价"""
        log = _make_log(tmp_path, [_open_trade(入场价=100.0, 止损价=96.0)])
        db = _make_db(tmp_path, {"600519": _quote_rows(last_close=105.0)})  # 浮动R=+1.25
        result = check_positions(log, db_path=db)
        assert len(result["alerts"]) == 1
        assert result["alerts"][0]["类型"] == "移动止损建议"
        assert "保本" in result["alerts"][0]["建议动作"]

    def test_r2_suggests_take_profit_and_raise(self, tmp_path):
        """浮动R ≥ +2R → 建议卖出 1/3，保护位上移至 +1R 位（入场价 + 每股风险）"""
        log = _make_log(tmp_path, [_open_trade(入场价=100.0, 止损价=96.0)])
        db = _make_db(tmp_path, {"600519": _quote_rows(last_close=109.0)})  # 浮动R=+2.25
        result = check_positions(log, db_path=db)
        assert len(result["alerts"]) == 1
        a = result["alerts"][0]
        assert a["类型"] == "移动止损建议"
        assert "卖出 1/3" in a["建议动作"]
        assert "104.00" in a["建议动作"]  # +1R 位 = 100 + 4

    def test_stop_above_entry_no_advice(self, tmp_path):
        """止损已在成本上方（加仓后统一上移过）→ 不再给移动止损建议"""
        log = _make_log(tmp_path, [_open_trade(入场价=100.0, 止损价=101.0)])
        db = _make_db(tmp_path, {"600519": _quote_rows(last_close=105.0)})
        result = check_positions(log, db_path=db)
        assert result["alerts"] == []
        assert len(result["positions_ok"]) == 1

    def test_hard_alert_not_overridden(self, tmp_path):
        """跌破止损时硬警报优先，不给移动止损建议"""
        log = _make_log(tmp_path, [_open_trade(入场价=100.0, 止损价=96.0)])
        db = _make_db(tmp_path, {"600519": _quote_rows(last_close=95.0)})
        result = check_positions(log, db_path=db)
        assert result["alerts"][0]["类型"] == "止损"
