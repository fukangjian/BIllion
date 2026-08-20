"""
六条硬规则组合级行为门禁测试 — 持仓只数 / 连亏停手 / 周频率（纯函数，不依赖网络）
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from review.compliance_check import check_behavior_guards
from review.trade_log import Trade

TODAY = date(2026, 8, 20)  # 周四，本周一 = 2026-08-17


def _make_trade(**kwargs) -> Trade:
    defaults = {
        "交易编号": "T_new",
        "日期": "2026-08-20",
        "股票代码": "600519",
        "股票名称": "贵州茅台",
        "账户类型": "事件",
        "入场系统": "HOT-S",
        "核心逻辑": "测试理由",
        "入场价": 100.0,
        "止损价": 97.0,
        "风险率": 0.5,
        "股数": 100,
        "仓位金额": 10000.0,
        "目标价": 110.0,
        "是否系统内交易": True,
    }
    defaults.update(kwargs)
    return Trade(**defaults)


def _open(symbol: str, trade_id: str, entry_date: str = "2026-08-10", **kw) -> Trade:
    return _make_trade(交易编号=trade_id, 股票代码=symbol, 日期=entry_date, **kw)


def _closed(r: float, exit_date: str, trade_id: str, entry_date: str = "2026-08-10") -> Trade:
    entry, stop = 100.0, 90.0
    return _make_trade(
        交易编号=trade_id, 日期=entry_date, 入场价=entry, 止损价=stop,
        实际退出价=entry + r * (entry - stop), 退出日期=exit_date, R倍数=r,
    )


def _types(violations):
    return [v.违规类型 for v in violations]


class TestPositionCountGuard:
    def test_third_symbol_rejected(self):
        new = _make_trade(股票代码="000858")
        open_trades = [_open("600519", "T1"), _open("300750", "T2")]
        v = check_behavior_guards(new, open_trades, [], today=TODAY)
        assert any(x.违规类型 == "持仓数量超限" and x.严重程度 == "高" for x in v)

    def test_second_symbol_allowed(self):
        new = _make_trade(股票代码="000858")
        open_trades = [_open("600519", "T1")]
        v = check_behavior_guards(new, open_trades, [], today=TODAY)
        assert "持仓数量超限" not in _types(v)

    def test_add_to_existing_symbol_not_counted(self):
        # 已有 2 只持仓，对其中一只加仓（同代码）不触发只数上限
        new = _make_trade(股票代码="600519", 关联单号="T1")
        open_trades = [_open("600519", "T1"), _open("300750", "T2")]
        v = check_behavior_guards(new, open_trades, [], today=TODAY)
        assert "持仓数量超限" not in _types(v)

    def test_pyramid_units_deduped_by_symbol(self):
        # 同一代码的首仓 + 加仓子单只算 1 只
        new = _make_trade(股票代码="000858")
        open_trades = [_open("600519", "T1"), _open("600519", "T1_u2", 关联单号="T1")]
        v = check_behavior_guards(new, open_trades, [], today=TODAY)
        assert "持仓数量超限" not in _types(v)


class TestConsecutiveLossHalt:
    def test_two_losses_same_day_rejected(self):
        new = _make_trade()
        closed = [_closed(-1.0, "2026-08-19", "L1"), _closed(-1.0, "2026-08-20", "L2")]
        v = check_behavior_guards(new, [], closed, today=TODAY)
        assert any(x.违规类型 == "连亏停手" and x.严重程度 == "高" for x in v)

    def test_two_losses_yesterday_rejected(self):
        new = _make_trade()
        closed = [_closed(-1.0, "2026-08-18", "L1"), _closed(-1.0, "2026-08-19", "L2")]
        v = check_behavior_guards(new, [], closed, today=TODAY)
        assert "连亏停手" in _types(v)

    def test_halt_window_passed_allowed(self):
        # 最近退出在 2 天前，停手期已过
        new = _make_trade()
        closed = [_closed(-1.0, "2026-08-17", "L1"), _closed(-1.0, "2026-08-18", "L2")]
        v = check_behavior_guards(new, [], closed, today=TODAY)
        assert "连亏停手" not in _types(v)

    def test_one_win_breaks_streak(self):
        new = _make_trade()
        closed = [_closed(-1.0, "2026-08-19", "L1"), _closed(1.5, "2026-08-20", "W1")]
        v = check_behavior_guards(new, [], closed, today=TODAY)
        assert "连亏停手" not in _types(v)

    def test_single_closed_trade_no_halt(self):
        new = _make_trade()
        closed = [_closed(-1.0, "2026-08-20", "L1")]
        v = check_behavior_guards(new, [], closed, today=TODAY)
        assert "连亏停手" not in _types(v)

    def test_loss_without_r_multiple_uses_exit_price(self):
        # 无 R倍数 时按退出价 < 入场价判定亏损
        new = _make_trade()
        closed = [
            _closed(-1.0, "2026-08-19", "L1"),
            _closed(-1.0, "2026-08-20", "L2"),
        ]
        for t in closed:
            t.R倍数 = None
        v = check_behavior_guards(new, [], closed, today=TODAY)
        assert "连亏停手" in _types(v)


class TestWeeklyFrequencyGuard:
    def test_third_entry_this_week_rejected(self):
        new = _make_trade()
        open_trades = [_open("600519", "T1", "2026-08-18"), _open("300750", "T2", "2026-08-19")]
        v = check_behavior_guards(new, open_trades, [], today=TODAY)
        assert any(x.违规类型 == "交易频率超限" and x.严重程度 == "高" for x in v)

    def test_last_week_entries_not_counted(self):
        new = _make_trade()
        open_trades = [_open("600519", "T1", "2026-08-10"), _open("300750", "T2", "2026-08-12")]
        v = check_behavior_guards(new, open_trades, [], today=TODAY)
        assert "交易频率超限" not in _types(v)

    def test_pyramid_child_not_counted(self):
        # 加仓子单（有关联单号）不计入新开仓频率
        new = _make_trade(股票代码="000858")
        open_trades = [
            _open("600519", "T1", "2026-08-18"),
            _open("600519", "T1_u2", "2026-08-19", 关联单号="T1"),
        ]
        v = check_behavior_guards(new, open_trades, [], today=TODAY)
        assert "交易频率超限" not in _types(v)

    def test_second_entry_this_week_allowed(self):
        new = _make_trade()
        open_trades = [_open("600519", "T1", "2026-08-18")]
        v = check_behavior_guards(new, open_trades, [], today=TODAY)
        assert "交易频率超限" not in _types(v)
