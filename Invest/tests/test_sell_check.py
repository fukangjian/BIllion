"""
卖出助手 sell-check 测试 — build_sell_check_lines 纯函数 + CLI 无持仓降级，不依赖网络
"""
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import review.cli as cli
from review.cli import build_sell_check_lines
from review.trade_log import Trade, TradeLog


# ---------- 测试数据构造 ----------

def _trade(**kw) -> Trade:
    d = dict(
        交易编号="T20260728_sell01", 日期="2026-07-28", 股票代码="605388",
        股票名称="均瑶健康", 账户类型="事件", 风险簇="未指定", 入场系统="HOT-S",
        入场价=5.96, 止损价=5.60, 风险率=1.0, 股数=3200, 仓位金额=19072.0,
    )
    d.update(kw)
    return Trade(**d)


def _pos_info(**kw) -> dict:
    """monitor._check_single_position 返回结构（含全部键）"""
    d = {
        "交易编号": "T20260728_sell01", "股票代码": "605388", "股票名称": "均瑶健康",
        "类型": None, "现价": 6.10, "止损价": 5.60, "通道下轨": None,
        "浮动R": 1.0, "距止损N": 2.5, "数据日期": "2026-07-30", "建议动作": "", "备注": "",
    }
    d.update(kw)
    return d


# ---------- build_sell_check_lines 纯函数 ----------

class TestBuildSellCheckLines:
    def test_hot_s_countdown_hint(self):
        """HOT-S 持仓 2 天 → 显示距 5 个交易日强制离场约剩 3 天（自然日近似）"""
        lines = build_sell_check_lines(
            _trade(), _pos_info(), in_hot_pool=True, close=6.10,
            today=date(2026, 7, 30),
        )
        text = "\n".join(lines)
        assert "持有天数: 2 天（自然日" in text
        assert "约剩 3 天" in text
        assert "5 个交易日强制离场" in text
        assert "自然日近似" in text

    def test_hot_s_overdue_warns_sell_today(self):
        """HOT-S 持仓超 5 天 → 提示已到期/超期，建议今日卖出"""
        lines = build_sell_check_lines(
            _trade(日期="2026-07-20"), _pos_info(), in_hot_pool=False, close=6.10,
            today=date(2026, 7, 30),
        )
        text = "\n".join(lines)
        assert "已到期/超期" in text
        assert "建议今日卖出" in text

    def test_non_hot_s_no_countdown(self):
        """非 HOT-S 系统（S1-A）不显示强制离场倒计时"""
        lines = build_sell_check_lines(
            _trade(入场系统="S1-A"), _pos_info(), in_hot_pool=None, close=6.10,
            today=date(2026, 7, 30),
        )
        text = "\n".join(lines)
        assert "强制离场" not in text
        assert "持有天数: 2 天" in text

    def test_hot_pool_annotations(self):
        """在池 / 不在池 / 池不可用 三种标注"""
        t = _trade()
        info = _pos_info()
        in_pool = "\n".join(build_sell_check_lines(t, info, True, 6.10, today=date(2026, 7, 30)))
        out_pool = "\n".join(build_sell_check_lines(t, info, False, 6.10, today=date(2026, 7, 30)))
        degraded = "\n".join(build_sell_check_lines(t, info, None, 6.10, today=date(2026, 7, 30)))
        assert "热点池: 仍在热点池" in in_pool
        assert "热点池: 已不在热点池" in out_pool
        assert "热点池: 不可用" in degraded and "已降级" in degraded

    def test_suggested_price_is_close_099(self):
        """建议挂单价 = 最新收盘 × 0.99（两位小数），注明挂低 1% 教训"""
        lines = build_sell_check_lines(
            _trade(), _pos_info(), in_hot_pool=True, close=6.10,
            today=date(2026, 7, 30),
        )
        text = "\n".join(lines)
        assert "建议挂单价: 6.04" in text  # 6.10 × 0.99 = 6.039 → 6.04
        assert "× 0.99" in text
        assert "挂低 1% 防挂高未成交" in text

    def test_no_quotes_degrades(self):
        """无行情（监控失败 + close None）→ 挂单价不可用并降级，不抛异常"""
        lines = build_sell_check_lines(
            _trade(), position_info=None, in_hot_pool=None, close=None,
            today=date(2026, 7, 30),
        )
        text = "\n".join(lines)
        assert "行情检查: 不可用" in text
        assert "建议挂单价: 不可用（无行情数据）" in text

    def test_stop_alert_rendered(self):
        """监控触发止损警报 → 检查单展示警报与建议动作"""
        info = _pos_info(类型="止损", 现价=5.50, 建议动作="收盘价已跌破止损价，按纪律立即退出")
        lines = build_sell_check_lines(
            _trade(), info, in_hot_pool=True, close=5.50,
            today=date(2026, 7, 30),
        )
        text = "\n".join(lines)
        assert "⚠️ 警报 [止损]" in text
        assert "按纪律立即退出" in text
        assert "建议挂单价: 5.45" in text  # 5.50 × 0.99 = 5.445 → 5.45


# ---------- CLI 层：无持仓降级 ----------

class TestSellCheckCli:
    def test_no_position_lists_holdings(self, tmp_path, monkeypatch, capsys):
        """目标代码无持仓 → 明确提示并列出当前持仓代码"""
        holding = {
            "交易编号": "T20260729_h01", "日期": "2026-07-29", "股票代码": "600519",
            "股票名称": "贵州茅台", "账户类型": "核心", "入场系统": "S2-A",
            "入场价": 100.0, "止损价": 95.0, "风险率": 1.0, "股数": 100,
            "仓位金额": 10000.0, "实际退出价": None, "退出日期": None,
        }
        closed = dict(holding, 交易编号="T20260701_c01", 股票代码="605388",
                      实际退出价=6.5, 退出日期="2026-07-25")
        log_file = tmp_path / "trades.json"
        log_file.write_text(json.dumps([holding, closed], ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(cli, "TradeLog", lambda: TradeLog(log_file))

        cli.cmd_sell_check(SimpleNamespace(symbol="605388"))
        out = capsys.readouterr().out
        assert "无持仓中交易" in out
        assert "600519" in out  # 列出当前持仓代码
        assert "605388" not in out.split("当前持仓代码:")[-1]  # 已平仓不列入持仓

    def test_no_position_no_holdings(self, tmp_path, monkeypatch, capsys):
        """无任何持仓 → 提示当前无任何持仓"""
        log_file = tmp_path / "trades.json"
        log_file.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(cli, "TradeLog", lambda: TradeLog(log_file))

        cli.cmd_sell_check(SimpleNamespace(symbol="605388"))
        out = capsys.readouterr().out
        assert "无持仓中交易" in out
        assert "当前无任何持仓" in out
