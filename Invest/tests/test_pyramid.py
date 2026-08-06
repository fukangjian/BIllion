"""
金字塔加仓规则测试 — 纯函数 + 临时数据，不依赖网络（V5.0 §5.5 三档）
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from review.pyramid import (
    add_unit_shares,
    check_add_trigger,
    get_unit_chain,
    unified_stop_after_add,
)
from review.trade_log import Trade


def _unit(tid: str, entry: float, unit: int, shares: int = 1000,
          stop: float | None = None, link: str = "", closed: bool = False,
          symbol: str = "600519") -> Trade:
    t = Trade(
        交易编号=tid, 股票代码=symbol, 账户类型="产业", 入场系统="S1-A",
        入场价=entry, 止损价=stop if stop is not None else entry - 4.0,
        风险率=0.5, 股数=shares, 仓位金额=entry * shares,
        关联单号=link, 单位序号=unit,
    )
    if closed:
        t.实际退出价 = entry + 1
        t.退出日期 = "2026-08-01"
    return t


class TestGetUnitChain:
    def test_chain_sorted_by_unit(self):
        """首仓 + 两个加仓单位按单位序号升序"""
        root = _unit("R1", 100.0, 1)
        u3 = _unit("U3", 103.0, 3, link="R1")
        u2 = _unit("U2", 101.0, 2, link="R1")
        chain = get_unit_chain([u3, root, u2], "600519")
        assert [t.交易编号 for t in chain] == ["R1", "U2", "U3"]

    def test_excludes_closed_and_other_symbols(self):
        root = _unit("R1", 100.0, 1)
        closed = _unit("C1", 101.0, 2, link="R1", closed=True)
        other = _unit("O1", 50.0, 1, symbol="000858")
        chain = get_unit_chain([root, closed, other], "600519")
        assert [t.交易编号 for t in chain] == ["R1"]

    def test_picks_latest_root(self):
        """同代码多条链（历史首仓已平仓）取最新未平仓首仓"""
        old_root = _unit("R0", 90.0, 1, closed=True)
        old_root.日期 = "2026-07-01"
        new_root = _unit("R1", 100.0, 1)
        new_root.日期 = "2026-08-01"
        chain = get_unit_chain([old_root, new_root], "600519")
        assert [t.交易编号 for t in chain] == ["R1"]

    def test_empty_when_no_position(self):
        assert get_unit_chain([], "600519") == []


class TestCheckAddTrigger:
    def test_trigger_at_half_n(self):
        """现价 = 上次入场 + 0.5N → 触发，下一单位 2，统一止损 = 现价 − 2N"""
        chain = [_unit("R1", 100.0, 1)]
        trig = check_add_trigger(chain, 101.0, 2.0)
        assert trig["可加仓"] is True
        assert trig["下一单位序号"] == 2
        assert trig["统一止损价"] == pytest.approx(97.0)

    def test_no_trigger_below_spacing(self):
        """间距 0.25N 不足 0.5N → 不触发，原因含触发价"""
        chain = [_unit("R1", 100.0, 1)]
        trig = check_add_trigger(chain, 100.5, 2.0)
        assert trig["可加仓"] is False
        assert "101.00" in trig["原因"]

    def test_gap_over_1n_advances_baseline(self):
        """跳空 1.3N 不追，基准推进至 +1.0N（跳过单位不补）"""
        chain = [_unit("R1", 100.0, 1)]
        trig = check_add_trigger(chain, 102.6, 2.0)
        assert trig["可加仓"] is False
        assert trig["新基准价"] == pytest.approx(102.0)
        assert "不追" in trig["原因"]

    def test_max_units_reached(self):
        """已满 MAX_UNITS=3（V5.0 三档）→ 不再加仓"""
        chain = [_unit("R1", 100.0, 1), _unit("U2", 101.0, 2, link="R1"),
                 _unit("U3", 102.0, 3, link="R1")]
        trig = check_add_trigger(chain, 104.0, 2.0)
        assert trig["可加仓"] is False
        assert "最大单位数" in trig["原因"]

    def test_atr_unavailable(self):
        chain = [_unit("R1", 100.0, 1)]
        assert check_add_trigger(chain, 105.0, 0.0)["可加仓"] is False

    def test_empty_chain(self):
        assert check_add_trigger([], 105.0, 2.0)["可加仓"] is False


class TestUnifiedStop:
    def test_stop_is_entry_minus_2n(self):
        assert unified_stop_after_add(101.0, 2.0) == pytest.approx(97.0)


class TestAddUnitShares:
    def test_budget_is_075_of_first_unit_risk(self):
        """加仓风险预算 = 首仓实际风险金额 × 30/40：4×1000×0.75 ÷ 4.2 → 700 股（整手）"""
        root = _unit("R1", 100.0, 1, shares=1000, stop=96.0)  # 每股风险 4
        assert add_unit_shares(root, 4.2) == 700

    def test_zero_risk_returns_zero(self):
        root = _unit("R1", 100.0, 1)
        assert add_unit_shares(root, 0.0) == 0
