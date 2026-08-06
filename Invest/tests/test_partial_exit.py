"""
sell 子命令（分批退出拆单）与 add-position 加仓链路测试 — 临时 trades.json + mock 行情，不依赖网络
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import review.cli as cli
from review.trade_log import TradeLog


@pytest.fixture(autouse=True)
def _pin_market_state(monkeypatch):
    """固定市场状态为 A（建仓闸门的市场状态检查不依赖真实 market.db）"""
    monkeypatch.setattr("review.entry_gate.get_latest_market_state", lambda db_path=None: "A")


def _patch_log(tmp_path, monkeypatch, trades: list[dict]) -> Path:
    f = tmp_path / "trades.json"
    f.write_text(json.dumps(trades, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(cli, "TradeLog", lambda: TradeLog(f))
    return f


def _open_trade(**kw) -> dict:
    d = {
        "交易编号": "T20260801_root",
        "日期": "2026-08-01",
        "股票代码": "600519",
        "股票名称": "测试股",
        "账户类型": "产业",
        "风险簇": "",
        "入场系统": "S1-A",
        "入场价": 100.0,
        "止损价": 96.0,
        "风险率": 0.5,
        "股数": 1000,
        "仓位金额": 100000.0,
        "实际退出价": None,
        "退出日期": None,
        "是否系统内交易": True,
    }
    d.update(kw)
    return d


def _run(argv: list[str], monkeypatch):
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()


def _load(log_file: Path) -> list[dict]:
    return json.loads(log_file.read_text(encoding="utf-8"))


class TestSellFullClose:
    def test_full_close_sets_exit_and_r(self, tmp_path, monkeypatch):
        """卖出股数 = 持仓股数 → 全平，自动算 R = (108−100)/4 = +2.0"""
        log_file = _patch_log(tmp_path, monkeypatch, [_open_trade()])
        _run(["cli.py", "sell", "600519", "--shares", "1000", "--price", "108"], monkeypatch)
        trades = _load(log_file)
        assert len(trades) == 1
        t = trades[0]
        assert t["实际退出价"] == 108.0
        assert t["退出日期"]
        assert t["R倍数"] == pytest.approx(2.0)

    def test_oversell_treated_as_full_close(self, tmp_path, monkeypatch):
        """卖出股数 > 持仓股数 → 按全平处理"""
        log_file = _patch_log(tmp_path, monkeypatch, [_open_trade()])
        _run(["cli.py", "sell", "600519", "--shares", "2000", "--price", "108"], monkeypatch)
        assert _load(log_file)[0]["实际退出价"] == 108.0


class TestSellPartial:
    def test_partial_split(self, tmp_path, monkeypatch):
        """部分卖出拆单：原单减至 600 股仍持仓，子单 400 股已平仓、R=+2.0、关联原单"""
        log_file = _patch_log(tmp_path, monkeypatch, [_open_trade()])
        _run(["cli.py", "sell", "600519", "--shares", "400", "--price", "108"], monkeypatch)
        trades = _load(log_file)
        assert len(trades) == 2

        orig = next(t for t in trades if t["交易编号"] == "T20260801_root")
        assert orig["股数"] == 600
        assert orig["仓位金额"] == pytest.approx(600 * 100.0)
        assert orig["实际退出价"] is None  # 仍持仓
        assert "分批卖出" in orig["备注"]

        child = next(t for t in trades if t["交易编号"] != "T20260801_root")
        assert child["股数"] == 400
        assert child["实际退出价"] == 108.0
        assert child["退出原因"] == "分批止盈"
        assert child["R倍数"] == pytest.approx(2.0)
        assert child["关联单号"] == "T20260801_root"

    def test_partial_stats_consistency(self, tmp_path, monkeypatch):
        """拆单后统计口径：已平仓仅子单（+2R），原单仍持仓不计入已平仓"""
        log_file = _patch_log(tmp_path, monkeypatch, [_open_trade()])
        _run(["cli.py", "sell", "600519", "--shares", "400", "--price", "108"], monkeypatch)
        log = TradeLog(log_file)
        closed = log.list_all(closed_only=True)
        open_ = log.list_all(open_only=True)
        assert len(closed) == 1 and closed[0].股数 == 400
        assert len(open_) == 1 and open_[0].股数 == 600
        # 全链路总股数守恒
        assert closed[0].股数 + open_[0].股数 == 1000

    def test_sell_by_id(self, tmp_path, monkeypatch):
        """--id 指定单位卖出（加仓链中对特定单位减仓）"""
        trades = [_open_trade(),
                  _open_trade(交易编号="T20260802_u2", 入场价=101.0, 止损价=97.0,
                              股数=700, 仓位金额=70700.0, 关联单号="T20260801_root", 单位序号=2)]
        log_file = _patch_log(tmp_path, monkeypatch, trades)
        _run(["cli.py", "sell", "600519", "--id", "T20260802_u2",
              "--shares", "700", "--price", "104"], monkeypatch)
        data = _load(log_file)
        u2 = next(t for t in data if t["交易编号"] == "T20260802_u2")
        assert u2["实际退出价"] == 104.0
        assert u2["R倍数"] == pytest.approx((104 - 101) / 4)  # (104−101)/(101−97)=0.75
        root = next(t for t in data if t["交易编号"] == "T20260801_root")
        assert root["实际退出价"] is None  # 首仓不受影响


def _market_df(last_close: float = 101.2) -> pd.DataFrame:
    """30 根递增多头日线（振幅≈2.1 → ATR≈2.1；0.5N≈1.05）"""
    dates = pd.date_range("2026-07-01", periods=30, freq="D").strftime("%Y-%m-%d")
    lows = 100.0 + np.arange(30) * 0.1
    df = pd.DataFrame({
        "trade_date": dates,
        "open": lows + 1.0,
        "high": lows + 2.0,
        "low": lows,
        "close": lows + 1.0,
        "volume": 1_000_000.0,
        "amount": 1e8,
    })
    df.loc[df.index[-1], "close"] = last_close
    df.loc[df.index[-1], "high"] = max(df.iloc[-1]["high"], last_close)
    return df


class TestAddPosition:
    def _patch_market(self, monkeypatch, df: pd.DataFrame):
        """cli 与 monitor 的行情读取都指向同一 mock（不碰真实 market.db）"""
        monkeypatch.setattr(cli, "load_daily_quotes", lambda symbol=None, **kw: df)
        import review.monitor as monitor
        monkeypatch.setattr(monitor, "load_daily_quotes", lambda symbol=None, **kw: df)

    def test_add_position_full_flow(self, tmp_path, monkeypatch):
        """触发（现价≥首仓+0.5N）→ 子单落库（单位2）→ 首仓止损上移至 加仓价−2N（只上不下）"""
        log_file = _patch_log(tmp_path, monkeypatch, [_open_trade()])
        self._patch_market(monkeypatch, _market_df(101.2))

        _run(["cli.py", "add-position", "600519", "--equity", "500000"], monkeypatch)
        trades = _load(log_file)
        assert len(trades) == 2

        root = next(t for t in trades if t["交易编号"] == "T20260801_root")
        child = next(t for t in trades if t["交易编号"] != "T20260801_root")

        # ATR=2.0 → 统一止损 = 101.2 − 2×2.0 = 97.2 > 96.0 → 首仓止损上移
        assert root["止损价"] == pytest.approx(97.2)
        assert "止损上移" in root["备注"]
        assert child["单位序号"] == 2
        assert child["关联单号"] == "T20260801_root"
        assert child["入场价"] == pytest.approx(101.2)
        assert child["止损价"] == pytest.approx(97.2)
        # 加仓股数 = 首仓风险金额 4000 × 0.75 ÷ 每股风险 4.0 → 750 → 整手 700 股
        assert child["股数"] == 700

    def test_not_triggered_no_write(self, tmp_path, monkeypatch):
        """未达 0.5N 间距 → 不落库"""
        log_file = _patch_log(tmp_path, monkeypatch, [_open_trade()])
        self._patch_market(monkeypatch, _market_df(100.5))  # 仅 +0.5 < 0.5N=1.0
        _run(["cli.py", "add-position", "600519", "--equity", "500000"], monkeypatch)
        assert len(_load(log_file)) == 1

    def test_max_units_no_more_adds(self, tmp_path, monkeypatch):
        """已满 3 单位 → 拒绝加仓"""
        trades = [
            _open_trade(),
            _open_trade(交易编号="U2", 入场价=101.0, 股数=700, 仓位金额=70700.0,
                        关联单号="T20260801_root", 单位序号=2),
            _open_trade(交易编号="U3", 入场价=102.0, 股数=700, 仓位金额=71400.0,
                        关联单号="T20260801_root", 单位序号=3),
        ]
        log_file = _patch_log(tmp_path, monkeypatch, trades)
        self._patch_market(monkeypatch, _market_df(105.0))
        _run(["cli.py", "add-position", "600519", "--equity", "500000"], monkeypatch)
        assert len(_load(log_file)) == 3
