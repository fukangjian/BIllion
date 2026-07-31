"""
券商成交导入测试 — symbol_map/mock 名称解析 + 临时 trades.json，不依赖网络
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from review.import_broker import (
    build_trades,
    import_records,
    pair_trades,
    parse_broker_csv,
    parse_broker_markdown,
    parse_symbol_map,
    resolve_symbols,
)
from review.trade_log import TradeLog

# 真实成交明细样本（E:/Billion/交易日志与心得整理_2026年7-8月.md §二，07-19~07-31 窗口）
SYMBOL_MAP = {
    "通源石油": "300164",
    "金牛化工": "600722",
    "山东墨龙": "002490",
    "长城军工": "601606",
    "创新医疗": "002173",
    "壹连科技": "301631",
    "均瑶健康": "605388",
}

SAMPLE_MD = """
### 成交明细（按时间顺序）

| 时间 | 股票 | 方向 | 成交价 | 数量(股) | 成交额(元) |
|---|---|---|---|---|---|
| 07-20 14:20:34 | 通源石油 | 卖出 | 12.050 | 1400 | 16,870 |
| 07-20 14:33:56 | 通源石油 | 买入 | 12.200 | 1400 | 17,080 |
| 07-22 09:56:46 | 通源石油 | 卖出 | 11.910 | 1400 | 16,674 |
| 07-22 09:56:55 | 金牛化工 | 买入 | 9.760 | 1700 | 16,592 |
| 07-23 09:30:53 | 金牛化工 | 卖出 | 10.130 | 1700 | 17,221 |
| 07-23 09:32:03 | 山东墨龙 | 买入 | 7.890 | 2200 | 17,358 |
| 07-24 09:33:41 | 山东墨龙 | 卖出 | 8.530 | 2200 | 18,766 |
| 07-24 09:33:56 | 长城军工 | 买入 | 31.900 | 500 | 15,950 |
| 07-27 09:31:07 | 长城军工 | 卖出 | 33.000 | 500 | 16,500 |
| 07-27 09:33:26 | 创新医疗 | 买入 | 20.690 | 900 | 18,621 |
| 07-29 09:37:36 | 创新医疗 | 卖出 | 22.040 | 900 | 19,836 |
| 07-29 09:38:49 | 壹连科技 | 买入 | 60.997 | 300 | 18,299 |
| 07-30 09:25:00 | 壹连科技 | 卖出 | 58.590 | 300 | 17,577 |
| 07-30 09:30:05 | 均瑶健康 | 买入 | 5.960 | 3200 | 19,072 |

### 波段盈亏核算（未含佣金、印花税）

| 股票 | 买入→卖出 | 盈亏(元) | 幅度 | 结果 |
|---|---|---|---|---|
| 通源石油（7/20接回段） | 12.20 → 11.91 | **-406** | -2.4% | 亏 |
"""


def _make_log(tmp_path) -> TradeLog:
    f = tmp_path / "trades.json"
    f.write_text("[]", encoding="utf-8")
    return TradeLog(f)


def _record(**kw) -> dict:
    r = {"日期": "2026-07-20", "时间": "14:33:56", "名称": "通源石油", "代码": "300164",
         "方向": "买入", "价格": 12.2, "股数": 1400, "金额": 17080.0}
    r.update(kw)
    return r


class TestParseMarkdown:
    def test_fills_year_and_thousands_separator(self):
        records = parse_broker_markdown(SAMPLE_MD, year=2026)
        assert len(records) == 14
        r = records[0]
        assert r["日期"] == "2026-07-20"      # 无年份 → 用 year 补
        assert r["时间"] == "14:20:34"
        assert r["名称"] == "通源石油"
        assert r["方向"] == "卖出"
        assert r["价格"] == pytest.approx(12.05)
        assert r["股数"] == 1400
        assert r["金额"] == pytest.approx(16870.0)  # 千分位逗号已去除

    def test_skips_headers_and_other_tables(self):
        text = (
            "| 时间 | 股票 | 方向 | 成交价 | 数量(股) | 成交额(元) |\n"
            "|---|---|---|---|---|---|\n"
            "| 07-20 14:20:34 | 通源石油 | 卖出 | 12.050 | 1400 | 16,870 |\n"
            "| 股票 | 买入→卖出 | 盈亏(元) | 幅度 | 结果 |\n"
            "| 通源石油 | 12.20 → 11.91 | **-406** | -2.4% | 亏 |\n"
            "| 笔记自述 | 成交记录印证 |\n"
        )
        records = parse_broker_markdown(text, year=2026)
        assert len(records) == 1  # 表头/分隔行/其他表格全部跳过


class TestParseCsv:
    def test_split_date_time_and_thousands(self, tmp_path):
        csv_path = tmp_path / "fills.csv"
        csv_path.write_text(
            "日期,时间,名称,方向,价格,股数,金额\n"
            "2026-07-20,14:20:34,通源石油,卖出,12.050,1400,\"16,870\"\n"
            "07-20,14:33:56,通源石油,买入,12.200,1400,17080\n",
            encoding="utf-8",
        )
        records = parse_broker_csv(csv_path, year=2026)
        assert len(records) == 2
        assert records[0]["日期"] == "2026-07-20"
        assert records[0]["金额"] == pytest.approx(16870.0)
        assert records[1]["日期"] == "2026-07-20"  # 无年份 → 用 year 补

    def test_combined_datetime_and_code_column(self, tmp_path):
        csv_path = tmp_path / "fills2.csv"
        csv_path.write_text(
            "时间,代码,方向,成交价,数量\n"
            "07-20 14:20:34,300164,卖出,12.050,1400\n",
            encoding="utf-8",
        )
        records = parse_broker_csv(csv_path, year=2026)
        assert len(records) == 1
        r = records[0]
        assert r["代码"] == "300164"          # 代码列直接生效，无需名称解析
        assert r["日期"] == "2026-07-20"      # 合并时间列拆出日期
        assert r["时间"] == "14:20:34"
        assert r["金额"] == pytest.approx(round(12.05 * 1400, 2))  # 无金额列 → 价格×股数


class TestResolveSymbols:
    def test_manual_map_covers_all_no_network(self, monkeypatch):
        """symbol_map 全覆盖时不得调用 get_symbol_by_name（防网络）"""
        def _boom(name):
            raise AssertionError("不应触发网络名称解析")
        monkeypatch.setattr("review.import_broker.get_symbol_by_name", _boom)
        records = [_record(代码="")]
        unresolved = resolve_symbols(records, {"通源石油": "300164"})
        assert unresolved == []
        assert records[0]["代码"] == "300164"

    def test_unresolved_names_collected(self, monkeypatch):
        """解析失败的名称收集上报，记录不赋代码"""
        monkeypatch.setattr("review.import_broker.get_symbol_by_name", lambda name: "")
        records = [_record(代码="", 名称="未知股A"), _record(代码="", 名称="未知股A"),
                   _record(代码="", 名称="未知股B")]
        unresolved = resolve_symbols(records)
        assert unresolved == ["未知股A", "未知股B"]  # 去重保序
        assert all(r["代码"] == "" for r in records)

    def test_parse_symbol_map_text(self):
        assert parse_symbol_map("通源石油=300164, 壹连科技=301631") == {
            "通源石油": "300164", "壹连科技": "301631",
        }
        assert parse_symbol_map("") == {}


class TestPairTrades:
    def test_closed_roundtrip(self):
        records = [
            _record(方向="买入", 日期="2026-07-20", 时间="14:33:56", 价格=12.2),
            _record(方向="卖出", 日期="2026-07-22", 时间="09:56:46", 价格=11.91),
        ]
        closed, opened, unpaired = pair_trades(records)
        assert len(closed) == 1 and not opened and not unpaired
        assert closed[0]["买入"]["价格"] == pytest.approx(12.2)
        assert closed[0]["卖出"]["价格"] == pytest.approx(11.91)
        assert closed[0]["股数"] == 1400

    def test_open_position_leftover(self):
        closed, opened, unpaired = pair_trades([_record(方向="买入")])
        assert not closed and not unpaired
        assert len(opened) == 1 and opened[0]["股数"] == 1400

    def test_unpaired_sell_before_window(self):
        """窗口前建立的持仓：卖出无对应买入 → 未配对卖出，只报告不落库"""
        closed, opened, unpaired = pair_trades([_record(方向="卖出", 价格=12.05)])
        assert not closed and not opened
        assert len(unpaired) == 1 and unpaired[0]["股数"] == 1400


class TestBuildTrades:
    def test_imported_trade_fields(self):
        closed = [{
            "买入": _record(方向="买入", 日期="2026-07-20", 时间="14:33:56", 价格=12.2),
            "卖出": _record(方向="卖出", 日期="2026-07-22", 时间="09:56:46", 价格=11.91),
            "股数": 1400,
        }]
        opened = [_record(方向="买入", 日期="2026-07-30", 时间="09:30:05",
                          名称="均瑶健康", 代码="605388", 价格=5.96, 股数=3200)]
        trades = build_trades(closed, opened, account_type="事件", entry_system="")
        assert len(trades) == 2

        t = trades[0]
        assert t.是否系统内交易 is False
        assert t.止损价 == 0.0
        assert t.备注 == "券商导入"
        assert t.账户类型 == "事件" and t.入场系统 == ""
        assert t.日期 == "2026-07-20" and t.入场时间 == "14:33:56"
        assert t.实际退出价 == pytest.approx(11.91)
        assert t.退出日期 == "2026-07-22" and t.退出时间 == "09:56:46"
        assert t.is_closed

        o = trades[1]
        assert not o.is_closed
        assert o.实际退出价 is None and o.退出日期 is None and o.退出时间 == ""
        assert o.股票代码 == "605388" and o.股数 == 3200


class TestImportRecords:
    def test_full_sample_flow(self, tmp_path, monkeypatch):
        """真实样本：6 笔平仓 + 1 笔持仓（均瑶健康）+ 1 笔未配对卖出（通源窗口前持仓）"""
        monkeypatch.setattr("review.import_broker.get_symbol_by_name", lambda name: "")
        log = _make_log(tmp_path)
        records = parse_broker_markdown(SAMPLE_MD, year=2026)
        result = import_records(records, log, symbol_map=SYMBOL_MAP)
        assert result["新增"] == 7
        assert result["跳过"] == 0
        assert result["未解析名称"] == []
        assert len(result["未配对卖出"]) == 1
        assert result["未配对卖出"][0]["名称"] == "通源石油"
        assert result["未配对卖出"][0]["价格"] == pytest.approx(12.05)

        trades = log.list_all()
        closed = [t for t in trades if t.is_closed]
        opened = [t for t in trades if not t.is_closed]
        assert len(closed) == 6
        assert len(opened) == 1
        assert opened[0].股票代码 == "605388"
        assert opened[0].股数 == 3200
        assert opened[0].入场价 == pytest.approx(5.96)

    def test_idempotent_skip_existing(self, tmp_path, monkeypatch):
        """幂等：同 日期+代码+入场价+股数 已存在则跳过"""
        monkeypatch.setattr("review.import_broker.get_symbol_by_name", lambda name: "")
        log = _make_log(tmp_path)
        records = parse_broker_markdown(SAMPLE_MD, year=2026)
        import_records(records, log, symbol_map=SYMBOL_MAP)
        result = import_records(records, log, symbol_map=SYMBOL_MAP)
        assert result["新增"] == 0
        assert result["跳过"] == 7
        assert log.count() == 7

    def test_dry_run_no_write(self, tmp_path, monkeypatch):
        """dry_run 只返回统计不落库"""
        monkeypatch.setattr("review.import_broker.get_symbol_by_name", lambda name: "")
        log_file = tmp_path / "trades.json"
        log = _make_log(tmp_path)
        records = parse_broker_markdown(SAMPLE_MD, year=2026)
        result = import_records(records, log, symbol_map=SYMBOL_MAP, dry_run=True)
        assert result["新增"] == 7
        assert log.count() == 0
        assert json.loads(log_file.read_text(encoding="utf-8")) == []
