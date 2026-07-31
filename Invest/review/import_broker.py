"""
券商成交导入 — 解析券商成交明细（Markdown 表 / CSV），FIFO 配对为 Trade 落库

数据源示例（Markdown 表，日期无年份、金额含千分位逗号）：
    | 07-20 14:20:34 | 通源石油 | 卖出 | 12.050 | 1400 | 16,870 |

导入口径：
- 名称→代码：表内无代码时走 shared.utils.get_symbol_by_name，可用 symbol_map 手动映射覆盖；
  解析不出的名称收集在返回值的「未解析名称」中报给调用方
- 按代码 FIFO 配对买卖 → 已平仓 Trade；未配对买入 → 持仓 Trade；
  窗口前建立的持仓导致的未配对卖出（如通源石油首笔卖出）只列出报告、不落库
- 导入字段：是否系统内交易=False、止损价=0.0、备注="券商导入"（历史事实落库，不走入场闸门）
- 幂等：同 日期+股票代码+入场价+股数 已存在则跳过

用法:
    python review/import_broker.py                       # 空跑自检（不落库）
    python review/import_broker.py --file 成交.md --year 2026 --dry-run
    python review/import_broker.py --file 成交.csv --symbol-map 通源石油=300164,壹连科技=301631
"""
import argparse
import csv
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from review.trade_log import Trade, TradeLog
from shared.utils import get_symbol_by_name

logger = logging.getLogger(__name__)

# 方向归一化（券商导出可能是 买/卖 或 买入/卖出）
_DIRECTIONS = {"买入": "买入", "卖出": "卖出", "买": "买入", "卖": "卖出"}

# Markdown 行首单元格：'07-20 14:20:34' 或 '2026-07-20 14:20:34'
_MD_FIRST_CELL = re.compile(r"^\d{1,4}[-/]\d{1,2}(?:[-/]\d{1,4})?\s+\d{1,2}:\d{2}")

# CSV 表头别名（各券商导出口径不一，统一归一到内部键）
_CSV_HEADER_ALIASES = {
    "日期": "日期", "成交日期": "日期",
    "时间": "时间", "成交时间": "时间",
    "名称": "名称", "股票": "名称", "股票名称": "名称", "证券名称": "名称",
    "代码": "代码", "股票代码": "代码", "证券代码": "代码",
    "方向": "方向", "操作": "方向", "买卖方向": "方向", "交易方向": "方向",
    "价格": "价格", "成交价": "价格", "成交价格": "价格", "成交均价": "价格",
    "股数": "股数", "数量": "股数", "成交数量": "股数", "数量(股)": "股数",
    "金额": "金额", "成交额": "金额", "成交金额": "金额", "成交额(元)": "金额",
}


def _normalize_date(date_str: str, year: int) -> str:
    """'07-20'/'7-20' → 'YYYY-07-20'（year 补全年份）；'2026-07-20' 原样规范化"""
    parts = date_str.strip().replace("/", "-").split("-")
    if len(parts) == 3:
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    if len(parts) == 2:
        return f"{year:04d}-{int(parts[0]):02d}-{int(parts[1]):02d}"
    raise ValueError(f"无法解析日期: {date_str!r}")


def _normalize_time(time_str: str) -> str:
    """'14:20'/'14:20:34' → 'HH:MM:SS'（缺秒补 00）；无法解析返回空串"""
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", time_str.strip())
    if not m:
        return ""
    return f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3) or '00'}"


def _parse_float(value) -> float:
    """解析数字（支持千分位逗号）"""
    return float(str(value).replace(",", "").strip())


def _parse_int(value) -> int:
    """解析整数（支持千分位逗号与 '1400.0' 形式）"""
    return int(float(str(value).replace(",", "").strip()))


def _normalize_code(code) -> str:
    """提取 6 位数字代码（容忍 'SH600519'/'600519.XSHG' 等前后缀）；空返回 ''"""
    digits = re.sub(r"\D", "", str(code or ""))
    return digits.zfill(6)[-6:] if digits else ""


def parse_broker_markdown(text: str, year: int) -> list[dict]:
    """
    解析 Markdown 成交明细表（行形如 `| 07-20 14:20:34 | 通源石油 | 卖出 | 12.050 | 1400 | 16,870 |`）。

    日期无年份时用 year 补齐；金额支持千分位逗号；表头/分隔行与其他表格自动跳过。
    返回记录列表: {日期, 时间, 名称, 代码, 方向, 价格, 股数, 金额}（代码留空，由 resolve_symbols 补）
    """
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5 or not _MD_FIRST_CELL.match(cells[0]):
            continue  # 表头/分隔行/非成交明细表格
        direction = _DIRECTIONS.get(cells[2], "")
        if not direction:
            continue
        date_part, time_part = cells[0].split(None, 1)
        price = _parse_float(cells[3])
        shares = _parse_int(cells[4])
        records.append({
            "日期": _normalize_date(date_part, year),
            "时间": _normalize_time(time_part),
            "名称": cells[1],
            "代码": "",
            "方向": direction,
            "价格": price,
            "股数": shares,
            "金额": _parse_float(cells[5]) if len(cells) > 5 and cells[5] else round(price * shares, 2),
        })
    return records


def parse_broker_csv(path: str | Path, year: int) -> list[dict]:
    """
    解析券商成交 CSV（与 Markdown 表同列口径）。

    表头支持别名（见 _CSV_HEADER_ALIASES）；「时间」列可为 '07-20 14:20:34' 合并形式，
    也可由「日期」+「时间」两列分开给出；「名称」或「代码」至少其一。
    """
    records = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return records
    header = [_CSV_HEADER_ALIASES.get(h.strip(), "") for h in rows[0]]
    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        data = {key: row[i].strip() for i, key in enumerate(header) if key and i < len(row)}
        datetime_str = data.get("时间", "")
        if data.get("日期"):
            date_str, time_str = data["日期"], datetime_str
        elif " " in datetime_str:
            date_str, time_str = datetime_str.split(None, 1)
        else:
            date_str, time_str = datetime_str, ""
        direction = _DIRECTIONS.get(data.get("方向", ""), "")
        if not direction or not date_str:
            continue
        price = _parse_float(data.get("价格") or 0)
        shares = _parse_int(data.get("股数") or 0)
        if price <= 0 or shares <= 0:
            continue
        records.append({
            "日期": _normalize_date(date_str, year),
            "时间": _normalize_time(time_str),
            "名称": data.get("名称", ""),
            "代码": _normalize_code(data.get("代码", "")),
            "方向": direction,
            "价格": price,
            "股数": shares,
            "金额": _parse_float(data["金额"]) if data.get("金额") else round(price * shares, 2),
        })
    return records


def resolve_symbols(records: list[dict], symbol_map: dict | None = None) -> list[str]:
    """
    为缺代码的记录解析 名称→代码（symbol_map 手动映射优先，其次 get_symbol_by_name）。

    原地写回 record["代码"]；返回未能解析的名称清单（去重、保序），由调用方报告。
    """
    symbol_map = symbol_map or {}
    unresolved: list[str] = []
    for r in records:
        if r.get("代码"):
            r["代码"] = _normalize_code(r["代码"])
            continue
        name = str(r.get("名称", "")).strip()
        code = _normalize_code(symbol_map.get(name, ""))
        if not code and name:
            code = _normalize_code(get_symbol_by_name(name))
        if code:
            r["代码"] = code
        elif name and name not in unresolved:
            unresolved.append(name)
    return unresolved


def pair_trades(records: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """
    按代码 FIFO 配对买入/卖出（支持部分成交拆分）。

    返回 (closed_roundtrips, open_positions, unpaired_sells)：
    - closed_roundtrips: [{"买入": 买入记录, "卖出": 卖出记录, "股数": 配对股数}]
    - open_positions: 未配对买入（剩余股数），导入为持仓 Trade
    - unpaired_sells: 未配对卖出（导入窗口前建立的持仓所致），只报告不落库
    """
    by_symbol: dict[str, list[dict]] = {}
    for r in records:
        by_symbol.setdefault(r.get("代码", ""), []).append(r)

    closed: list[dict] = []
    open_positions: list[dict] = []
    unpaired_sells: list[dict] = []

    for recs in by_symbol.values():
        recs.sort(key=lambda r: (r["日期"], r["时间"]))
        buy_queue: list[dict] = []
        for r in recs:
            if r["方向"] == "买入":
                buy_queue.append({**r})
                continue
            remaining = r["股数"]
            while remaining > 0 and buy_queue:
                buy = buy_queue[0]
                take = min(remaining, buy["股数"])
                closed.append({"买入": {**buy, "股数": take}, "卖出": {**r, "股数": take}, "股数": take})
                buy["股数"] -= take
                remaining -= take
                if buy["股数"] <= 0:
                    buy_queue.pop(0)
            if remaining > 0:
                unpaired_sells.append({**r, "股数": remaining})
        open_positions.extend(buy_queue)

    closed.sort(key=lambda c: (c["买入"]["日期"], c["买入"]["时间"]))
    open_positions.sort(key=lambda r: (r["日期"], r["时间"]))
    return closed, open_positions, unpaired_sells


def build_trades(
    closed: list[dict],
    open_positions: list[dict],
    account_type: str = "事件",
    entry_system: str = "",
    cluster: str = "",
) -> list[Trade]:
    """
    配对结果 → Trade 列表（先平仓笔后持仓笔）。

    平仓笔：日期/入场时间=买入日/时，实际退出价/退出日期/退出时间=卖出价/日/时；
    持仓笔无退出字段。统一 是否系统内交易=False、止损价=0.0、备注="券商导入"。
    """
    def _base(buy: dict) -> dict:
        return {
            "日期": buy["日期"],
            "入场时间": buy.get("时间", ""),
            "股票代码": buy.get("代码", ""),
            "股票名称": buy.get("名称", ""),
            "账户类型": account_type,
            "风险簇": cluster,
            "入场系统": entry_system,
            "入场价": buy["价格"],
            "止损价": 0.0,
            "风险率": 0.0,
            "是否系统内交易": False,
            "备注": "券商导入",
        }

    trades: list[Trade] = []
    for c in closed:
        buy, sell, shares = c["买入"], c["卖出"], c["股数"]
        trades.append(Trade(
            **_base(buy),
            股数=shares,
            仓位金额=round(buy["价格"] * shares, 2),
            实际退出价=sell["价格"],
            退出日期=sell["日期"],
            退出时间=sell.get("时间", ""),
        ))
    for buy in open_positions:
        trades.append(Trade(
            **_base(buy),
            股数=buy["股数"],
            仓位金额=round(buy["价格"] * buy["股数"], 2),
        ))
    return trades


def import_records(
    records: list[dict],
    trade_log: TradeLog,
    symbol_map: dict | None = None,
    account_type: str = "事件",
    entry_system: str = "",
    dry_run: bool = False,
) -> dict:
    """
    成交记录 → 名称解析 → FIFO 配对 → Trade 落库（幂等，不过入场闸门）。

    幂等键：日期+股票代码+入场价+股数（同键已存在则跳过）。
    dry_run=True 只返回统计不落库。
    返回 {新增, 跳过, 未配对卖出, 未解析名称, trades}（trades 为本次新增/拟新增的 Trade 列表）
    """
    unresolved = resolve_symbols(records, symbol_map)
    coded = [r for r in records if r.get("代码")]
    closed, open_positions, unpaired_sells = pair_trades(coded)
    trades = build_trades(closed, open_positions,
                          account_type=account_type, entry_system=entry_system)

    existing = {
        (t.日期, t.股票代码, round(t.入场价, 4), t.股数)
        for t in trade_log.list_all()
    }
    new_trades: list[Trade] = []
    skipped = 0
    for t in trades:
        key = (t.日期, t.股票代码, round(t.入场价, 4), t.股数)
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        new_trades.append(t)
        if not dry_run:
            trade_log.add(t)
            logger.info("已导入交易: %s %s %s", t.交易编号, t.股票代码, t.日期)

    return {
        "新增": len(new_trades),
        "跳过": skipped,
        "未配对卖出": unpaired_sells,
        "未解析名称": unresolved,
        "trades": new_trades,
    }


# ---------- CLI 辅助（模块 __main__ 与 review/cli.py import 子命令共用） ----------

def parse_symbol_map(text: str) -> dict:
    """'通源石油=300164,壹连科技=301631' → {'通源石油': '300164', ...}"""
    mapping = {}
    for part in (text or "").split(","):
        part = part.strip()
        if "=" in part:
            name, code = part.split("=", 1)
            mapping[name.strip()] = code.strip()
    return mapping


def load_records_from_file(path: str | Path, year: int) -> list[dict]:
    """按扩展名选择 Markdown / CSV 解析"""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown"):
        return parse_broker_markdown(path.read_text(encoding="utf-8"), year)
    if suffix == ".csv":
        return parse_broker_csv(path, year)
    raise ValueError(f"不支持的文件类型: {suffix}（仅支持 .md/.csv）")


def print_import_summary(result: dict, dry_run: bool = False):
    """打印导入汇总（新增/跳过/未配对卖出/未解析名称）"""
    tag = "[DRY-RUN] " if dry_run else ""
    print(f"{tag}新增 {result['新增']} 笔，跳过 {result['跳过']} 笔（已存在）")
    for t in result["trades"]:
        status = "已平仓" if t.is_closed else "持仓"
        print(f"  · {t.日期} {t.股票代码} {t.股票名称} {t.股数}股 @{t.入场价} [{status}]")
    for s in result["未配对卖出"]:
        print(f"[提示] 卖出无对应买入（窗口前建立的持仓），未落库: "
              f"{s['日期']} {s.get('名称', '')} {s['股数']}股 @{s['价格']}")
    if result["未解析名称"]:
        print(f"[警告] {len(result['未解析名称'])} 个名称未能解析代码，相关记录已跳过: "
              f"{'、'.join(result['未解析名称'])}")
        print("       可用 --symbol-map 名称=代码,... 手动指定")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="券商成交导入 — Markdown 表 / CSV → FIFO 配对 → trades.json")
    parser.add_argument("--file", default=None, help="成交明细文件（.md/.csv）；缺省时空跑自检（不落库）")
    parser.add_argument("--year", type=int, default=datetime.now().year, help="日期补全年份（默认当前年）")
    parser.add_argument("--symbol-map", default="", dest="symbol_map",
                        help="名称=代码 手动映射，逗号分隔（如 通源石油=300164,壹连科技=301631）")
    parser.add_argument("--account", default="事件", help="账户类型（默认 事件）")
    parser.add_argument("--system", default="", help="入场系统（默认空，导入历史多为非系统交易）")
    parser.add_argument("--dry-run", action="store_true", help="只打印配对结果，不落库")
    args = parser.parse_args()

    symbol_map = parse_symbol_map(args.symbol_map)
    if not args.file:
        print("[说明] 未指定 --file，空跑自检（不落库）")
        result = import_records([], TradeLog(), symbol_map=symbol_map, dry_run=True)
        print_import_summary(result, dry_run=True)
        return

    records = load_records_from_file(args.file, args.year)
    print(f"[OK] 解析成交记录 {len(records)} 条（年份 {args.year}）")
    result = import_records(records, TradeLog(), symbol_map=symbol_map,
                            account_type=args.account, entry_system=args.system,
                            dry_run=args.dry_run)
    print_import_summary(result, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
