"""
数据获取模块 — 使用 AkShare 获取行情、板块、涨跌停、ETF、龙虎榜数据
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional

import akshare as ak
import pandas as pd

from config import FETCH_MAX_WORKERS, SECTOR_FETCH_LIMIT, WATCHLIST
from shared.utils import bypass_proxy, retry_fetch

logger = logging.getLogger(__name__)


def _normalize_date(d) -> str:
    """统一日期格式为 YYYY-MM-DD"""
    if d is None:
        return datetime.now().strftime("%Y-%m-%d")
    s = str(d).replace("/", "-")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def _safe_float(v, default: float = 0.0) -> float:
    """NaN 安全 float（数据源缺字段时 pd.to_numeric 产出 NaN，int(NaN) 会抛错）"""
    try:
        f = float(v)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def _safe_int(v, default: int = 0) -> int:
    """NaN 安全 int"""
    try:
        f = float(v)
        return default if f != f else int(f)
    except (TypeError, ValueError):
        return default


def _sina_symbol(symbol: str) -> str:
    """6位代码转新浪格式 (sh600519 / sz000001)"""
    if symbol.startswith(("6", "5", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def _fetch_ths_line(js_symbol: str, start_year: int, end_year: int) -> pd.DataFrame:
    """
    同花顺 d.10jqka v4/line 年度 K 线（01=前复权；分年抓取，单年失败跳过）。
    返回原始 DataFrame：trade_date/open/high/low/close/volume/amount
    """
    import json

    import requests

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36",
        "Referer": "http://stockpage.10jqka.com.cn/",
    }
    rows = []
    with bypass_proxy():
        for year in range(start_year, end_year + 1):
            url = f"https://d.10jqka.com.cn/v4/line/{js_symbol}/01/{year}.js"
            try:
                r = requests.get(url, headers=headers, timeout=10)
                if r.status_code != 200 or "(" not in r.text:
                    continue
                payload = json.loads(r.text[r.text.index("(") + 1: r.text.rindex(")")])
                for item in (payload.get("data") or "").split(";"):
                    parts = item.split(",")
                    if len(parts) < 7 or not parts[0].isdigit():
                        continue
                    rows.append({
                        "trade_date": _normalize_date(parts[0]),
                        "open": _safe_float(parts[1]),
                        "high": _safe_float(parts[2]),
                        "low": _safe_float(parts[3]),
                        "close": _safe_float(parts[4]),
                        "volume": _safe_float(parts[5]),
                        "amount": _safe_float(parts[6]),
                    })
            except Exception as e:
                logger.warning("同花顺日线 %s %d 年失败（跳过该年）: %s", js_symbol, year, e)
            time.sleep(0.2)
    return pd.DataFrame(rows)


def fetch_stock_daily(
    symbol: str,
    start_date: str = "20200101",
    end_date: Optional[str] = None,
    adjust: str = "qfq",
) -> pd.DataFrame:
    """
    获取个股日线行情（同花顺 d.10jqka v4/line，01 前复权）。

    2026-08-07 起替换原「新浪优先、东财备用」链路——新浪/东财在本机持续被风控重置，
    重试超时拖慢全量抓取（热点池/趋势池数百只）。
    """
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    start_date = start_date.replace("-", "")
    end_date = end_date.replace("-", "")

    df = _fetch_ths_line(f"hs_{symbol}", int(start_date[:4]), int(end_date[:4]))
    if df.empty:
        logger.warning("同花顺源 %s 无数据", symbol)
        return df
    df["symbol"] = symbol
    s, e = _normalize_date(start_date), _normalize_date(end_date)
    df = df[(df["trade_date"] >= s) & (df["trade_date"] <= e)]
    return df[["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)


def fetch_index_daily(
    symbol: str = "000300",
    start_date: str = "20200101",
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """获取指数日线（同花顺 v4/line，如沪深300；2026-08 起替代新浪/东财链路）"""
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    start_date = start_date.replace("-", "")
    end_date = end_date.replace("-", "")

    # 同花顺指数代码映射（目前仅沪深300在用；未映射的代码降级空表）
    ths_map = {"000300": "zs_1B0300"}
    js_symbol = ths_map.get(symbol)
    if not js_symbol:
        logger.warning("指数 %s 无同花顺代码映射，返回空表", symbol)
        return pd.DataFrame()

    df = _fetch_ths_line(js_symbol, int(start_date[:4]), int(end_date[:4]))
    if df.empty:
        return df
    df["symbol"] = symbol
    s, e = _normalize_date(start_date), _normalize_date(end_date)
    df = df[(df["trade_date"] >= s) & (df["trade_date"] <= e)]
    return df[["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)


def fetch_sector_list() -> pd.DataFrame:
    """获取行业板块列表 — 优先同花顺，东方财富备用"""
    try:
        raw = retry_fetch(ak.stock_board_industry_name_ths)
        if raw is not None and not raw.empty:
            return raw
    except Exception as e:
        logger.warning("同花顺板块列表失败: %s", e)

    try:
        raw = retry_fetch(ak.stock_board_industry_name_em)
        if raw is not None and not raw.empty:
            return raw
    except Exception as e:
        logger.warning("东方财富板块列表也失败: %s", e)

    return pd.DataFrame()


def fetch_sector_daily(
    sector_name: str,
    start_date: str = "20240101",
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """获取板块指数日线 — 优先同花顺，东方财富备用"""
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    start_date = start_date.replace("-", "")
    end_date = end_date.replace("-", "")

    try:
        raw = retry_fetch(
            ak.stock_board_industry_index_ths,
            symbol=sector_name,
            start_date=f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}",
            end_date=f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}",
        )
        if raw is not None and not raw.empty:
            date_col = "日期" if "日期" in raw.columns else raw.columns[0]
            open_col = "开盘价" if "开盘价" in raw.columns else ("开盘" if "开盘" in raw.columns else raw.columns[1])
            high_col = "最高价" if "最高价" in raw.columns else ("最高" if "最高" in raw.columns else raw.columns[2])
            low_col = "最低价" if "最低价" in raw.columns else ("最低" if "最低" in raw.columns else raw.columns[3])
            close_col = "收盘价" if "收盘价" in raw.columns else ("收盘" if "收盘" in raw.columns else raw.columns[4])
            vol_col = "成交量" if "成交量" in raw.columns else None
            amt_col = "成交额" if "成交额" in raw.columns else None
            chg_col = "涨跌幅" if "涨跌幅" in raw.columns else None

            return pd.DataFrame({
                "sector_name": sector_name,
                "trade_date": raw[date_col].apply(_normalize_date),
                "open": pd.to_numeric(raw[open_col], errors="coerce").fillna(0),
                "high": pd.to_numeric(raw[high_col], errors="coerce").fillna(0),
                "low": pd.to_numeric(raw[low_col], errors="coerce").fillna(0),
                "close": pd.to_numeric(raw[close_col], errors="coerce").fillna(0),
                "volume": pd.to_numeric(raw[vol_col], errors="coerce").fillna(0) if vol_col else 0.0,
                "amount": pd.to_numeric(raw[amt_col], errors="coerce").fillna(0) if amt_col else 0.0,
                "change_pct": pd.to_numeric(raw[chg_col], errors="coerce").fillna(0) if chg_col else 0.0,
            })
    except Exception as e:
        logger.warning("同花顺板块 %s 失败: %s", sector_name, e)

    try:
        raw = retry_fetch(
            ak.stock_board_industry_hist_em,
            symbol=sector_name,
            start_date=start_date,
            end_date=end_date,
            period="日k",
            adjust="",
        )
        if raw is not None and not raw.empty:
            return pd.DataFrame({
                "sector_name": sector_name,
                "trade_date": raw["日期"].apply(_normalize_date),
                "open": raw["开盘"].astype(float),
                "high": raw["最高"].astype(float),
                "low": raw["最低"].astype(float),
                "close": raw["收盘"].astype(float),
                "volume": raw["成交量"].astype(float),
                "amount": raw["成交额"].astype(float),
                "change_pct": raw.get("涨跌幅", pd.Series([0] * len(raw))).astype(float),
            })
    except Exception as e:
        logger.warning("东方财富板块 %s 也失败: %s", sector_name, e)

    return pd.DataFrame()


def _ths_request_headers() -> dict:
    """同花顺 hexin-v 反爬 cookie（akshare 自带 ths.js + py_mini_racer 计算，与行业一览同机制）"""
    import akshare
    from py_mini_racer import MiniRacer

    js = MiniRacer()
    ths_js = Path(akshare.__file__).parent / "stock_feature" / "ths.js"
    js.eval(ths_js.read_text(encoding="utf-8"))
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36",
        "Cookie": f"v={js.call('v')}",
    }


def _fetch_sector_constituents_ths(sector_name: str) -> pd.DataFrame:
    """
    同花顺行业成分股（直连 q.10jqka.com.cn；板块名与 sector_quotes 同为同花顺口径，无需映射）。
    返回列: symbol / name；任何一步失败返回空 DataFrame（由调用方降级到东财）。
    """
    import re

    import requests

    try:
        names = retry_fetch(ak.stock_board_industry_name_ths)
        match = names[names["name"] == sector_name]
        if match.empty:
            cand = names[names["name"].astype(str).apply(
                lambda n: sector_name in n or n in sector_name)]
            if cand.empty:
                logger.warning("同花顺行业名录无 %s（跳过）", sector_name)
                return pd.DataFrame(columns=["symbol", "name"])
            match = cand.head(1)
        code = str(match.iloc[0]["code"])
        headers = _ths_request_headers()

        frames = []
        with bypass_proxy():
            # 第 1 页：详情页 HTML（含成分股表格与 page_info 总页数）
            r = requests.get(f"https://q.10jqka.com.cn/thshy/detail/code/{code}/",
                             headers=headers, timeout=10)
            tables = pd.read_html(r.text)
            if not tables:
                return pd.DataFrame(columns=["symbol", "name"])
            frames.append(tables[0])
            m = re.search(r"page_info[^>]*>\s*\d+/(\d+)", r.text)
            total_pages = min(int(m.group(1)), 15) if m else 1  # 15 页≈300 只，对齐 TREND_POOL_MAX
            for page in range(2, total_pages + 1):
                url = (f"http://q.10jqka.com.cn/thshy/detail/code/{code}/"
                       f"field/199112/order/desc/page/{page}/ajax/1/")
                # 逐页容错：THS 偶发断连/cookie 中途失效（401），重试前重建 cookie
                for attempt in range(2):
                    if attempt > 0:
                        time.sleep(1)
                        try:
                            headers = _ths_request_headers()
                        except Exception:
                            pass
                    try:
                        rp = requests.get(url, headers=headers, timeout=10)
                        if rp.status_code == 200:
                            tp = pd.read_html(rp.text)
                            if tp and not tp[0].empty:
                                frames.append(tp[0])
                            break
                        logger.warning("同花顺成分 %s 第 %d 页 HTTP %s", sector_name, page, rp.status_code)
                    except Exception as e:
                        logger.warning("同花顺成分 %s 第 %d 页失败（尝试 %d/2）: %s",
                                       sector_name, page, attempt + 1, e)
                time.sleep(0.5)  # 页间限速，降低断连概率

        if not frames:
            return pd.DataFrame(columns=["symbol", "name"])
        raw = pd.concat(frames, ignore_index=True)
        out = pd.DataFrame({
            "symbol": raw["代码"].astype(str).str.extract(r"(\d{6})", expand=False),
            "name": raw["名称"].astype(str),
        })
        out = out.dropna(subset=["symbol"]).drop_duplicates("symbol").reset_index(drop=True)
        logger.info("同花顺成分股 %s: %d 只", sector_name, len(out))
        return out
    except Exception as e:
        logger.warning("同花顺成分股 %s 获取失败: %s", sector_name, e)
        return pd.DataFrame(columns=["symbol", "name"])


def _fetch_sector_constituents_em(sector_name: str) -> pd.DataFrame:
    """
    东财行业成分股（push2 接口，曾被 IP 风控时不可用；作为同花顺的备用源）。
    板块名为同花顺口径，东财行业名存在差异（如 白酒→酿酒行业），
    先按原名直查，失败/为空时用东财行业列表做包含式模糊匹配后重试。
    """

    def _query(name: str) -> pd.DataFrame:
        raw = retry_fetch(ak.stock_board_industry_cons_em, symbol=name)
        return raw if raw is not None else pd.DataFrame()

    try:
        raw = _query(sector_name)
        if raw.empty:
            # 名称对齐：同花顺板块名 → 东财行业名（包含式模糊匹配）
            em_names = retry_fetch(ak.stock_board_industry_name_em)
            col = next((c for c in em_names.columns if "名称" in str(c)), em_names.columns[0])
            candidates = [
                n for n in em_names[col].astype(str)
                if sector_name in n or n in sector_name
            ]
            for cand in candidates[:2]:
                raw = _query(cand)
                if not raw.empty:
                    logger.info("板块 %s 按东财行业名 %s 匹配成功", sector_name, cand)
                    break
    except Exception as e:
        logger.warning("东财成分股 %s 获取失败: %s", sector_name, e)
        return pd.DataFrame(columns=["symbol", "name"])

    if raw is None or raw.empty:
        return pd.DataFrame(columns=["symbol", "name"])

    code_col = next((c for c in raw.columns if "代码" in str(c)), None)
    name_col = next((c for c in raw.columns if "名称" in str(c)), None)
    if code_col is None:
        logger.warning("东财成分股 %s 返回缺少代码列（实际列: %s）", sector_name, list(raw.columns))
        return pd.DataFrame(columns=["symbol", "name"])

    out = pd.DataFrame({
        "symbol": raw[code_col].astype(str).str.extract(r"(\d{6})", expand=False),
        "name": raw[name_col].astype(str) if name_col else "",
    })
    return out.dropna(subset=["symbol"]).reset_index(drop=True)


def fetch_sector_constituents(sector_name: str) -> pd.DataFrame:
    """
    行业板块成分股（趋势动态池原料），双源 fallback：
    同花顺直连优先（板块名同口径无需映射），东财备用（push2，风控期不可用）。
    仍失败返回空 DataFrame（调用方降级跳过该板块）。
    返回列: symbol / name（6 位代码 + 名称）。
    """
    df = _fetch_sector_constituents_ths(sector_name)
    if not df.empty:
        return df
    return _fetch_sector_constituents_em(sector_name)


def fetch_limit_reasons_ths(trade_date: Optional[str] = None) -> pd.DataFrame:
    """
    同花顺涨停池「涨停归因」（龙头逻辑维度证据，data.10jqka.com.cn 直连）。

    返回列: symbol / name / reason（涨停原因，如「短剧+游戏+摘帽」）/ open_num（开板次数）
    / turnover_rate（换手率，与东财字段互备）。失败返回空 DataFrame（调用方降级）。
    """
    import requests

    trade_date = (trade_date or datetime.now().strftime("%Y%m%d")).replace("-", "")
    url_tpl = ("https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
               "?page={page}&limit=200&field=199112,9001,9002,1968584"
               f"&filter=HS,GEM2STAR&date={trade_date}&order_field=330324&order_type=0")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36",
    }
    records = []
    try:
        with bypass_proxy():
            for page in range(1, 4):  # 200/页 × 3 足够覆盖全市场涨停
                try:
                    r = requests.get(url_tpl.format(page=page), headers=headers, timeout=10)
                    data = r.json().get("data") or {}
                    items = data.get("info") or []
                    if not items:
                        break
                    for it in items:
                        records.append({
                            "symbol": str(it.get("code", "")).zfill(6)[-6:],
                            "name": str(it.get("name", "")),
                            "reason": str(it.get("reason_type", "") or ""),
                            "open_num": _safe_int(it.get("open_num")),
                            "turnover_rate": _safe_float(it.get("turnover_rate")),
                        })
                    total = int((data.get("page") or {}).get("total", 0))
                    if page * 200 >= total:
                        break
                except Exception as e:
                    logger.warning("THS 涨停归因第 %d 页失败（保留已抓部分）: %s", page, e)
                    break
                time.sleep(0.3)
    except Exception as e:
        logger.warning("THS 涨停归因获取失败: %s", e)
        return pd.DataFrame(columns=["symbol", "name", "reason", "open_num", "turnover_rate"])
    return pd.DataFrame(records)


def fetch_limit_stats(trade_date: Optional[str] = None) -> pd.DataFrame:
    """获取涨跌停及市场宽度统计"""
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y%m%d")
    date_str = trade_date.replace("-", "")

    limit_up_count = limit_down_count = broken_count = 0

    try:
        zt = retry_fetch(ak.stock_zt_pool_em, date=date_str)
        limit_up_count = len(zt) if zt is not None and not zt.empty else 0
    except Exception as e:
        logger.warning("涨停池获取失败: %s", e)

    try:
        dt = retry_fetch(ak.stock_zt_pool_dtgc_em, date=date_str)
        limit_down_count = len(dt) if dt is not None and not dt.empty else 0
    except Exception as e:
        logger.warning("跌停池获取失败: %s", e)

    try:
        zb = retry_fetch(ak.stock_zt_pool_zbgc_em, date=date_str)
        broken_count = len(zb) if zb is not None and not zb.empty else 0
    except Exception:
        broken_count = 0

    up_count = down_count = flat_count = 0
    total_amount = 0.0
    try:
        spot = retry_fetch(ak.stock_zh_a_spot)
        if spot is not None and not spot.empty:
            change_col = None
            for col in ("changepercent", "涨跌幅", "change"):
                if col in spot.columns:
                    change_col = col
                    break
            if change_col is None:
                change_col = spot.columns[-1]
            changes = pd.to_numeric(spot[change_col], errors="coerce")
            up_count = int((changes > 0).sum())
            down_count = int((changes < 0).sum())
            flat_count = int((changes == 0).sum())
            for amt_col in ("amount", "成交额"):
                if amt_col in spot.columns:
                    total_amount = float(pd.to_numeric(spot[amt_col], errors="coerce").sum())
                    break
    except Exception as e:
        logger.warning("全市场行情获取失败(新浪): %s", e)
        try:
            spot = retry_fetch(ak.stock_zh_a_spot_em)
            if spot is not None and not spot.empty:
                changes = pd.to_numeric(spot.get("涨跌幅", spot.iloc[:, -1]), errors="coerce")
                up_count = int((changes > 0).sum())
                down_count = int((changes < 0).sum())
                flat_count = int((changes == 0).sum())
                if "成交额" in spot.columns:
                    total_amount = float(pd.to_numeric(spot["成交额"], errors="coerce").sum())
        except Exception as e2:
            logger.warning("全市场行情获取失败(东方财富): %s", e2)

    return pd.DataFrame([{
        "trade_date": _normalize_date(date_str),
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "broken_count": broken_count,
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "total_amount": total_amount,
    }])


def fetch_limit_pools(trade_date: Optional[str] = None) -> pd.DataFrame:
    """
    获取涨停/炸板个股名单（超短热点原料；东财 push2ex 主机，与行情 push2 不同，风控互不影响）。

    返回列: symbol / name / pool_type(up=涨停, broken=炸板) / change_pct / amount / lbc(连板数) / sector
    / fbt(首次封板时间) / seal_amount(封板资金) / turnover(换手率) / zbc(炸板次数)。
    单个池失败降级跳过，不阻塞另一个池；全失败返回空 DataFrame。
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y%m%d")
    date_str = trade_date.replace("-", "")

    records = []

    def _append(df: pd.DataFrame, pool_type: str) -> None:
        for _, row in df.iterrows():
            lbc_val = row.get("连板数", 1)
            fbt_val = row.get("首次封板时间", "")
            records.append({
                "symbol": str(row.get("代码", "")).zfill(6)[-6:],
                "name": str(row.get("名称", "")),
                "pool_type": pool_type,
                "change_pct": float(pd.to_numeric(row.get("涨跌幅", 0), errors="coerce") or 0),
                "amount": float(pd.to_numeric(row.get("成交额", 0), errors="coerce") or 0),
                "lbc": int(lbc_val) if pool_type == "up" and pd.notna(lbc_val) else 0,
                "sector": str(row.get("所属行业", "")),
                # 龙头评分数据（2026-08 扩列）：首次封板时间/封板资金/换手率/炸板次数
                "fbt": str(fbt_val) if pd.notna(fbt_val) else "",
                "seal_amount": _safe_float(row.get("封板资金")),
                "turnover": _safe_float(row.get("换手率")),
                "zbc": _safe_int(row.get("炸板次数")),
            })

    try:
        zt = retry_fetch(ak.stock_zt_pool_em, date=date_str)
        if zt is not None and not zt.empty:
            _append(zt, "up")
    except Exception as e:
        logger.warning("涨停池名单获取失败: %s", e)

    try:
        zb = retry_fetch(ak.stock_zt_pool_zbgc_em, date=date_str)
        if zb is not None and not zb.empty:
            _append(zb, "broken")
    except Exception as e:
        logger.warning("炸板池名单获取失败: %s", e)

    return pd.DataFrame(records)


def fetch_etf_flow(trade_date: Optional[str] = None) -> pd.DataFrame:
    """获取 ETF 资金流向"""
    records = []
    try:
        etf_list = retry_fetch(ak.fund_etf_category_sina, symbol="ETF基金")
        if etf_list is None or etf_list.empty:
            return pd.DataFrame()

        for _, row in etf_list.head(20).iterrows():
            raw_code = str(row.get("代码", ""))
            code = raw_code.replace("sh", "").replace("sz", "")
            name = str(row.get("名称", ""))
            if not code:
                continue
            try:
                sina_sym = raw_code if raw_code.startswith(("sh", "sz")) else f"sh{code}"
                hist = retry_fetch(ak.fund_etf_hist_sina, symbol=sina_sym)
                if hist is None or hist.empty:
                    continue
                latest = hist.iloc[-1]
                prev = hist.iloc[-2] if len(hist) > 1 else latest
                close_val = float(latest.get("close", latest.get("收盘", 0)))
                vol_now = float(latest.get("volume", latest.get("成交量", 0)))
                vol_prev = float(prev.get("volume", prev.get("成交量", 0)))
                date_val = latest.get("date", latest.get("日期", trade_date))
                records.append({
                    "etf_code": code,
                    "etf_name": name,
                    "trade_date": _normalize_date(date_val),
                    "close": close_val,
                    "volume": vol_now,
                    "amount": float(latest.get("amount", latest.get("成交额", 0))),
                    "net_flow": (vol_now - vol_prev) * close_val,
                })
                time.sleep(0.3)
            except Exception as e:
                logger.debug("ETF %s 跳过: %s", code, e)
    except Exception as e:
        logger.warning("ETF 数据获取失败: %s", e)

    return pd.DataFrame(records)


def fetch_dragon_tiger(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """获取龙虎榜数据"""
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")

    start_date = start_date.replace("-", "")
    end_date = end_date.replace("-", "")

    def _fetch():
        return ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)

    try:
        raw = retry_fetch(_fetch)
    except Exception as e:
        logger.warning("龙虎榜获取失败: %s", e)
        return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    records = []
    for _, row in raw.iterrows():
        records.append({
            "symbol": str(row.get("代码", "")).zfill(6),
            "trade_date": _normalize_date(row.get("上榜日", row.get("日期", ""))),
            "name": str(row.get("名称", "")),
            "close": float(row.get("收盘价", 0) or 0),
            "change_pct": float(row.get("涨跌幅", 0) or 0),
            "turnover": float(row.get("换手率", 0) or 0),
            "reason": str(row.get("解读", row.get("上榜原因", ""))),
            "buy_amount": float(row.get("买入额", row.get("龙虎榜买入额", 0)) or 0),
            "sell_amount": float(row.get("卖出额", row.get("龙虎榜卖出额", 0)) or 0),
            "net_amount": float(row.get("净买额", row.get("龙虎榜净买额", 0)) or 0),
        })
    return pd.DataFrame(records)


def fetch_and_save_all(
    symbols: list[str],
    start_date: str = "20230101",
    sector_limit: int = SECTOR_FETCH_LIMIT,
) -> dict:
    """批量获取并保存所有数据，返回各模块写入行数统计"""
    from pipeline.database import (
        init_database,
        save_daily_quotes,
        save_dragon_tiger,
        save_etf_flow,
        save_limit_stats,
        save_sector_quotes,
    )

    init_database()
    stats = {"daily": 0, "sector": 0, "limit": 0, "etf": 0, "lhb": 0}

    for sym in symbols:
        try:
            df = fetch_stock_daily(sym, start_date=start_date)
            stats["daily"] += save_daily_quotes(df)
            logger.info("已保存 %s 日线 %d 条", sym, len(df))
            time.sleep(0.5)
        except Exception as e:
            logger.error("股票 %s 失败: %s", sym, e)

    try:
        idx_df = fetch_index_daily("000300", start_date=start_date)
        stats["daily"] += save_daily_quotes(idx_df)
    except Exception as e:
        logger.error("沪深300 失败: %s", e)

    try:
        sectors = fetch_sector_list()
        for _, srow in sectors.head(sector_limit).iterrows():
            name = srow.get("name", srow.get("板块名称", ""))
            if not name and len(srow) > 1:
                name = str(srow.iloc[0])
            if not name:
                continue
            df = fetch_sector_daily(str(name), start_date=start_date)
            stats["sector"] += save_sector_quotes(df)
            time.sleep(0.5)
    except Exception as e:
        logger.error("板块数据失败: %s", e)

    try:
        limit_df = fetch_limit_stats()
        stats["limit"] += save_limit_stats(limit_df)
    except Exception as e:
        logger.error("涨跌停统计失败: %s", e)

    try:
        etf_df = fetch_etf_flow()
        stats["etf"] += save_etf_flow(etf_df)
    except Exception as e:
        logger.error("ETF 失败: %s", e)

    try:
        lhb_df = fetch_dragon_tiger()
        stats["lhb"] += save_dragon_tiger(lhb_df)
    except Exception as e:
        logger.error("龙虎榜失败: %s", e)

    return stats


def _fetch_and_save_single_stock(sym: str, start_date: str) -> tuple[str, int, Optional[str]]:
    """并行 worker：获取并保存单只股票日线，返回 (代码, 行数, 错误信息)"""
    from pipeline.database import save_daily_quotes

    try:
        df = fetch_stock_daily(sym, start_date=start_date)
        rows = save_daily_quotes(df)
        return sym, rows, None
    except Exception as e:
        return sym, 0, str(e)


def fetch_and_save_all_parallel(
    symbols: list[str],
    start_date: str = "20230101",
    sector_limit: int = SECTOR_FETCH_LIMIT,
    max_workers: int = FETCH_MAX_WORKERS,
) -> dict:
    """并行批量获取并保存数据（日线并行，全局数据串行），返回各模块写入行数统计"""
    from pipeline.database import (
        init_database,
        save_daily_quotes,
        save_dragon_tiger,
        save_etf_flow,
        save_limit_stats,
        save_sector_quotes,
    )

    init_database()
    stats = {"daily": 0, "sector": 0, "limit": 0, "etf": 0, "lhb": 0}

    # 日线数据：ThreadPoolExecutor 并行获取
    logger.info("开始并行获取 %d 只股票日线 (workers=%d)...", len(symbols), max_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_and_save_single_stock, sym, start_date): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            sym, rows, err = future.result()
            if err:
                logger.error("股票 %s 失败: %s", sym, err)
            else:
                stats["daily"] += rows
                logger.info("已保存 %s 日线 %d 条", sym, rows)

    # 沪深300 指数（串行）
    try:
        idx_df = fetch_index_daily("000300", start_date=start_date)
        stats["daily"] += save_daily_quotes(idx_df)
        logger.info("已保存沪深300 日线 %d 条", len(idx_df))
    except Exception as e:
        logger.error("沪深300 失败: %s", e)

    # 板块数据（串行，全局数据不宜并行）
    try:
        sectors = fetch_sector_list()
        for _, srow in sectors.head(sector_limit).iterrows():
            name = srow.get("name", srow.get("板块名称", ""))
            if not name and len(srow) > 1:
                name = str(srow.iloc[0])
            if not name:
                continue
            df = fetch_sector_daily(str(name), start_date=start_date)
            stats["sector"] += save_sector_quotes(df)
            time.sleep(0.5)
    except Exception as e:
        logger.error("板块数据失败: %s", e)

    # 涨跌停统计（串行）
    try:
        limit_df = fetch_limit_stats()
        stats["limit"] += save_limit_stats(limit_df)
    except Exception as e:
        logger.error("涨跌停统计失败: %s", e)

    # ETF 资金流向（串行）
    try:
        etf_df = fetch_etf_flow()
        stats["etf"] += save_etf_flow(etf_df)
    except Exception as e:
        logger.error("ETF 失败: %s", e)

    # 龙虎榜（串行）
    try:
        lhb_df = fetch_dragon_tiger()
        stats["lhb"] += save_dragon_tiger(lhb_df)
    except Exception as e:
        logger.error("龙虎榜失败: %s", e)

    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("开始获取数据...")
    result = fetch_and_save_all(WATCHLIST[:3], start_date="20240101")
    print("完成:", result)
