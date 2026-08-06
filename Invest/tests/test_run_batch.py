"""
批量回测测试 — 汇总纯函数 + 真实 cerebro 离线冒烟（mock 数据），不依赖网络
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtest.run_batch as rb
from tests.test_backtest import _make_trend_df


def _stats_row(symbol: str, **kw) -> dict:
    row = {
        "symbol": symbol, "strategy": "S1-A", "start": "2020-01-01", "end": "2026-01-01",
        "total_return_pct": 10.0, "max_drawdown_pct": 5.0, "total_trades": 10,
        "won": 6, "lost": 4, "win_rate_pct": 60.0,
        "gross_profit": 6000.0, "gross_loss": 4000.0,
        "profit_factor": 1.5, "avg_r": 0.5, "r_count": 10,
    }
    row.update(kw)
    return row


class TestAggregateBatch:
    def test_summary_math(self):
        """整体胜率/PF/加权平均R/样本充足性"""
        rows = [
            _stats_row("600519", won=6, total_trades=10, gross_profit=6000, gross_loss=4000,
                       avg_r=0.5, r_count=10),
            _stats_row("000858", won=2, total_trades=10, gross_profit=2000, gross_loss=6000,
                       avg_r=-0.2, r_count=5),
        ]
        result = rb.aggregate_batch(rows, "S1-A", "2020-01-01", "2026-01-01")
        s = result["summary"]
        assert s["symbols_ok"] == 2
        assert s["total_trades"] == 20
        assert s["win_rate_pct"] == pytest.approx(40.0)          # (6+2)/20
        assert s["profit_factor"] == pytest.approx(0.8)          # 8000/10000
        assert s["avg_r"] == pytest.approx(0.27, abs=0.001)  # (0.5×10 − 0.2×5)/15 ≈ 0.267
        assert s["sample_sufficient"] is False                   # 20 < 50

    def test_error_rows_counted(self):
        rows = [_stats_row("600519"), {"symbol": "000858", "error": "数据不足"}]
        result = rb.aggregate_batch(rows, "S1-A", "2020-01-01", "2026-01-01")
        s = result["summary"]
        assert s["symbols_ok"] == 1
        assert s["symbols_failed"] == 1


class TestRunBatch:
    def test_writes_md_and_json(self, tmp_path, monkeypatch):
        """逐标的异常不中断；输出 MD + JSON 汇总"""
        def fake_run(strategy_name, symbol, **kw):
            if symbol == "000858":
                raise ValueError("数据不足")
            return _stats_row(symbol)

        monkeypatch.setattr(rb, "run_backtest", fake_run)
        result = rb.run_batch("S1-A", ["600519", "000858"], "2020-01-01",
                              "2026-01-01", output_dir=tmp_path)
        assert result["summary"]["symbols_ok"] == 1
        assert result["summary"]["symbols_failed"] == 1
        md_files = list(tmp_path.glob("batch_S1-A_*.md"))
        json_files = list(tmp_path.glob("batch_S1-A_*.json"))
        assert len(md_files) == 1 and len(json_files) == 1
        md = md_files[0].read_text(encoding="utf-8")
        assert "600519" in md and "失败" in md

    def test_real_cerebro_offline_smoke(self, tmp_path, monkeypatch):
        """真实回测链路（mock 行情注入 load_data_from_db）：两个标的共用同一合成趋势数据"""
        import backtest.run_backtest as single

        base = _make_trend_df()
        extra_dates = pd.date_range(base.index[-1] + pd.offsets.BDay(1), periods=10, freq="B")
        last = float(base["close"].iloc[-1])
        extra = pd.DataFrame(
            {"open": last, "high": last + 0.5, "low": last - 0.5, "close": last, "volume": 1e6},
            index=extra_dates,
        )
        df = pd.concat([base, extra])  # 65 根，满足 run_backtest 的 ≥60 条下限

        monkeypatch.setattr(single, "load_data_from_db", lambda *a, **kw: df)
        # run_batch 引用的是同一函数对象（模块级 from import 的是 run_backtest 函数，内部调 load_data_from_db）
        result = rb.run_batch("S1-A", ["600519", "000858"], "2025-01-01",
                              "2025-04-01", output_dir=tmp_path)
        s = result["summary"]
        assert s["symbols_ok"] == 2
        assert s["total_trades"] >= 2  # 每个标的至少 1 笔完整交易
        for row in result["rows"]:
            assert "error" not in row
            assert row["total_trades"] >= 1
            assert row["r_count"] >= 1
