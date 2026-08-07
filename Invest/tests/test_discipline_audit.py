"""
纪律自动审计测试 — 纯函数，不依赖网络
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from review.discipline_audit import (
    RULE_BANNED,
    RULE_CHASE,
    RULE_NO_STOP,
    RULE_OFF_SYSTEM,
    RULE_SWITCH,
    audit_discipline,
    audit_to_markdown,
)
from review.trade_log import Trade


def _make_trade(**kwargs) -> Trade:
    defaults = {
        "交易编号": "T1",
        "日期": "2026-07-20",
        "股票代码": "600519",
        "股票名称": "贵州茅台",
        "账户类型": "事件",
        "入场系统": "STR-B",
        "入场价": 100.0,
        "止损价": 95.0,
        "风险率": 0.5,
        "股数": 100,
        "仓位金额": 10000.0,
        "是否系统内交易": True,
    }
    defaults.update(kwargs)
    return Trade(**defaults)


def _find(findings, rule):
    return [f for f in findings if f.规则 == rule]


class TestChaseBack:
    def _sell_buy(self, **overrides):
        sell = _make_trade(交易编号="T_sell", 股票代码="300164", 日期="2026-07-19",
                           实际退出价=12.05, 退出日期="2026-07-20", 退出时间="14:20:34")
        buy = _make_trade(交易编号="T_buy", 股票代码="300164", 日期="2026-07-20",
                          入场时间="14:33:56", 入场价=12.20)
        for k, v in overrides.items():
            setattr(sell if k.startswith("sell_") else buy, k.split("_", 1)[1], v)
        return sell, buy

    def test_hit_same_day_sell_then_higher_buy(self):
        sell, buy = self._sell_buy()
        chase = _find(audit_discipline([sell, buy]), RULE_CHASE)
        assert len(chase) == 1
        assert chase[0].严重程度 == "高"
        assert chase[0].交易编号 == "T_buy"
        assert "无时间数据" not in chase[0].描述  # 两笔都有时间 → 精确判定

    def test_no_hit_when_buy_price_lower(self):
        sell, buy = self._sell_buy(buy_入场价=11.00)
        assert _find(audit_discipline([sell, buy]), RULE_CHASE) == []

    def test_no_hit_when_buy_before_sell(self):
        """时间字段齐备但买在卖前 → 非接回"""
        sell, buy = self._sell_buy(buy_入场时间="09:30:00")
        assert _find(audit_discipline([sell, buy]), RULE_CHASE) == []

    def test_degraded_to_date_when_time_missing(self):
        """无时间字段 → 按日期+价格降级判定，描述注明"""
        sell, buy = self._sell_buy(sell_退出时间="", buy_入场时间="")
        chase = _find(audit_discipline([sell, buy]), RULE_CHASE)
        assert len(chase) == 1
        assert "无时间数据，按日期判定" in chase[0].描述


class TestFastSwitch:
    def _sell_buy(self, buy_time="09:56:55", sell_time="09:56:46"):
        sell = _make_trade(交易编号="T_sell", 股票代码="300164",
                           实际退出价=11.91, 退出日期="2026-07-22", 退出时间=sell_time)
        buy = _make_trade(交易编号="T_buy", 股票代码="600722", 日期="2026-07-22",
                          入场时间=buy_time)
        return sell, buy

    def test_hit_within_threshold(self):
        sell, buy = self._sell_buy()  # 间隔 9 秒
        switch = _find(audit_discipline([sell, buy]), RULE_SWITCH)
        assert len(switch) == 1
        assert switch[0].严重程度 == "中"
        assert switch[0].交易编号 == "T_buy"

    def test_no_hit_beyond_threshold(self):
        sell, buy = self._sell_buy(buy_time="11:00:00")  # 间隔超阈值
        assert _find(audit_discipline([sell, buy]), RULE_SWITCH) == []

    def test_skip_when_time_missing(self):
        """任一笔缺时间 → 跳过（不误报）"""
        sell, buy = self._sell_buy(buy_time="")
        assert _find(audit_discipline([sell, buy]), RULE_SWITCH) == []
        sell, buy = self._sell_buy(sell_time="")
        assert _find(audit_discipline([sell, buy]), RULE_SWITCH) == []

    def test_no_hit_same_symbol(self):
        """同一代码的卖→买不算换仓（属追高接回规则的管辖）"""
        sell, buy = self._sell_buy()
        buy.股票代码 = sell.股票代码
        assert _find(audit_discipline([sell, buy]), RULE_SWITCH) == []


class TestSimpleRules:
    def test_banned_board_default_off_after_lift(self):
        """2026-08-07 放开创业板后：默认空前缀，301 代码不再命中禁买板块"""
        assert _find(audit_discipline([_make_trade(股票代码="301631")]), RULE_BANNED) == []
        assert _find(audit_discipline([_make_trade(股票代码="600519")]), RULE_BANNED) == []

    def test_banned_board_mechanism_intact(self, monkeypatch):
        """机制保留：重新配置前缀即恢复命中"""
        import review.discipline_audit as da

        monkeypatch.setattr(da, "BANNED_BOARD_PREFIXES", ("300", "301"))
        hit = _find(audit_discipline([_make_trade(股票代码="301631")]), RULE_BANNED)
        assert len(hit) == 1 and hit[0].严重程度 == "高"

    def test_no_stop_hit_and_miss(self):
        hit = _find(audit_discipline([_make_trade(止损价=0.0)]), RULE_NO_STOP)
        assert len(hit) == 1 and hit[0].严重程度 == "中"
        assert _find(audit_discipline([_make_trade(止损价=95.0)]), RULE_NO_STOP) == []

    def test_off_system_hit_and_miss(self):
        hit = _find(audit_discipline([_make_trade(是否系统内交易=False)]), RULE_OFF_SYSTEM)
        assert len(hit) == 1 and hit[0].严重程度 == "低"
        assert _find(audit_discipline([_make_trade(是否系统内交易=True)]), RULE_OFF_SYSTEM) == []

    def test_clean_trade_no_findings(self):
        assert audit_discipline([_make_trade()]) == []
        assert audit_discipline([]) == []


class TestAuditToMarkdown:
    def test_empty_renders_pass(self):
        assert audit_to_markdown([]) == "[PASS] 未发现纪律问题"

    def test_summary_then_detail(self):
        findings = audit_discipline([
            _make_trade(交易编号="T1", 股票代码="600519", 止损价=0.0, 是否系统内交易=False),
        ])
        md = audit_to_markdown(findings)
        assert "| 规则 | 严重程度 | 命中数 |" in md  # 先汇总表
        assert md.index("汇总") < md.index("明细")  # 汇总在明细前
        assert "| 禁买板块 | 高 | 0 |" in md  # 2026-08-07 放开创业板后默认不触发
        assert "| 无止损 | 中 | 1 |" in md
        assert "| 非系统交易 | 低 | 1 |" in md
        assert "| 闪电换仓 | 中 | 0 |" in md  # 未命中规则也列计数
        assert "T1" in md and "600519" in md
