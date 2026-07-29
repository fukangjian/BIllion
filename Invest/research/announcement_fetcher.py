"""
巨潮信息网公告全文抓取 — 搜索、HTML 解析、本地缓存、长文分块摘要
"""
import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from config import ANNOUNCEMENT_CACHE_DIR, ANNOUNCEMENT_MAX_CHARS
from shared.utils import bypass_proxy, normalize_symbol

logger = logging.getLogger(__name__)

CNINFO_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_BASE = "https://static.cninfo.com.cn/"
CNINFO_DETAIL_BASE = "https://www.cninfo.com.cn/new/disclosure/detail"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "http://www.cninfo.com.cn/",
}

# orgId 本地缓存（巨潮 API 需要 symbol,orgId 格式）
_org_id_cache: dict[str, str] = {}


def _get_org_id(symbol: str) -> str:
    """获取巨潮 orgId（复用 AkShare 内部映射表）"""
    symbol = normalize_symbol(symbol)
    if symbol in _org_id_cache:
        return _org_id_cache[symbol]
    try:
        import akshare as ak

        stock_map = ak.stock_zh_a_disclosure_report_cninfo.__globals__["__get_stock_json"]("沪深京")
        org_id = stock_map.get(symbol, "")
        if org_id:
            _org_id_cache[symbol] = org_id
        return org_id
    except Exception as e:
        logger.warning("获取 orgId 失败 (%s): %s", symbol, e)
        return ""


def _default_se_date(start: str = "2020-01-01") -> str:
    """默认查询较宽日期范围，避免遗漏历史公告"""
    end = datetime.now().strftime("%Y-%m-%d")
    return f"{start}~{end}"


def _query_cninfo(
    symbol: str,
    searchkey: str = "",
    se_date: str = "",
    org_id: str = "",
) -> list[dict]:
    """调用巨潮 hisAnnouncement/query API"""
    symbol = normalize_symbol(symbol)
    org_id = org_id or _get_org_id(symbol)
    stock_param = f"{symbol},{org_id}" if org_id else symbol

    payload = {
        "pageNum": "1",
        "pageSize": "30",
        "column": _cninfo_column(symbol),
        "tabName": "fulltext",
        "plate": "",
        "stock": stock_param,
        "searchkey": searchkey,
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": se_date or _default_se_date(),
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }

    with bypass_proxy():
        resp = requests.post(
            CNINFO_QUERY_URL,
            data=payload,
            headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()

    return data.get("announcements") or []


def _parse_detail_url(url: str) -> dict:
    """解析巨潮详情页 URL 参数"""
    qs = parse_qs(urlparse(url).query)
    return {
        "stock_code": qs.get("stockCode", [""])[0],
        "announcement_id": qs.get("announcementId", [""])[0],
        "org_id": qs.get("orgId", [""])[0],
        "announcement_time": qs.get("announcementTime", [""])[0],
    }


def _announcement_to_result(item: dict) -> dict:
    """将巨潮 API 单条公告转为统一结果结构"""
    result: dict = {
        "url": "",
        "content_type": "unknown",
        "adjunct_url": "",
        "announcement_id": str(item.get("announcementId", "")),
    }
    adjunct = item.get("adjunctUrl") or ""
    if adjunct:
        result["adjunct_url"] = adjunct
        result["url"] = urljoin(CNINFO_STATIC_BASE, adjunct)
        lower = adjunct.lower()
        result["content_type"] = "pdf" if lower.endswith(".pdf") else "html"
    else:
        sec_code = item.get("secCode", "")
        org_id = item.get("orgId", "")
        ann_id = result["announcement_id"]
        ann_time = item.get("announcementTime", "")
        result["url"] = (
            f"{CNINFO_DETAIL_BASE}?stockCode={sec_code}"
            f"&announcementId={ann_id}&orgId={org_id}&announcementTime={ann_time}"
        )
        result["content_type"] = "html"
    return result


def _resolve_url_to_content_source(
    url: str,
    symbol: str = "",
    title: str = "",
    date: str = "",
) -> dict:
    """
    将详情页 / 搜索链接解析为实际 PDF/HTML 静态资源 URL。
    巨潮详情页为 SPA，需通过 API 获取 adjunctUrl。
    """
    if not url:
        return {"url": "", "content_type": "unknown"}

    lower = url.lower()
    if lower.endswith(".pdf"):
        return {"url": url, "content_type": "pdf"}
    if lower.endswith((".htm", ".html")) and "static.cninfo.com.cn" in lower:
        return {"url": url, "content_type": "html"}

    if "disclosure/detail" in url:
        detail = _parse_detail_url(url)
        sym = detail["stock_code"] or normalize_symbol(symbol)
        ann_id = detail["announcement_id"]
        ann_time = detail["announcement_time"] or date
        se_date = f"{ann_time}~{ann_time}" if ann_time else _default_se_date()

        announcements = _query_cninfo(
            sym,
            searchkey=title[:40] if title else "",
            se_date=se_date,
            org_id=detail["org_id"],
        )
        for item in announcements:
            if str(item.get("announcementId")) == ann_id:
                return _announcement_to_result(item)
        if announcements:
            return _announcement_to_result(announcements[0])

    if "static.cninfo.com.cn" in lower:
        return {"url": url, "content_type": "html"}

    return {"url": url, "content_type": "html"}


def _is_valid_extracted_text(text: str) -> bool:
    """过滤 SPA 模板残留等无效正文"""
    if not text or len(text) < 80:
        return False
    if "{{" in text and "}}" in text:
        return False
    return True


def _cache_path(symbol: str, date: str, title: str) -> Path:
    """生成公告缓存文件路径（同一公告只抓取一次）"""
    raw = f"{normalize_symbol(symbol)}|{date}|{title}"
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)[:40]
    return ANNOUNCEMENT_CACHE_DIR / f"{normalize_symbol(symbol)}_{date}_{safe_title}_{key}.json"


def _load_cache(symbol: str, date: str, title: str) -> Optional[dict]:
    path = _cache_path(symbol, date, title)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        content = data.get("content", "")
        # 跳过无效的 SPA 模板缓存，触发重新抓取
        if content and "PDF 公告需手动查看" not in content and not _is_valid_extracted_text(content):
            logger.info("忽略无效公告缓存，将重新抓取: %s", path.name)
            return None
        logger.info("命中公告缓存: %s", path.name)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读取公告缓存失败: %s", e)
        return None


def cache_announcement(symbol: str, date: str, title: str, content: str, **extra) -> Path:
    """缓存公告到 data/announcements/ 目录"""
    ANNOUNCEMENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol, date, title)
    payload = {
        "symbol": normalize_symbol(symbol),
        "date": date,
        "title": title,
        "content": content,
        "cached_at": datetime.now().isoformat(timespec="seconds"),
        **extra,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("公告已缓存: %s", path.name)
    return path


def _cninfo_column(symbol: str) -> str:
    """根据股票代码判断巨潮 column 参数"""
    s = normalize_symbol(symbol)
    if s.startswith(("0", "3")):
        return "szse"
    if s.startswith("6"):
        return "sse"
    if s.startswith(("8", "4")):
        return "bj"
    return "szse"


def fetch_cninfo_announcement(symbol: str, title: str) -> dict:
    """
    在巨潮信息网搜索匹配公告，返回 URL 及内容类型。
    返回字段: url, content_type (html/pdf/unknown), adjunct_url, announcement_id
    """
    symbol = normalize_symbol(symbol)
    result: dict = {"url": "", "content_type": "unknown", "adjunct_url": "", "announcement_id": ""}

    try:
        announcements = _query_cninfo(symbol, searchkey=title[:50])
    except Exception as e:
        logger.warning("巨潮公告搜索失败 (%s): %s", symbol, e)
        return result

    if not announcements:
        logger.info("巨潮未找到匹配公告: %s — %s", symbol, title[:30])
        return result

    # 优先标题完全匹配（去除 HTML 高亮标签），否则取第一条
    title_clean = re.sub(r"<[^>]+>", "", title.strip())
    matched = announcements[0]
    for item in announcements:
        item_title = re.sub(r"<[^>]+>", "", item.get("announcementTitle", "")).strip()
        if item_title == title_clean or title_clean in item_title:
            matched = item
            break

    return _announcement_to_result(matched)


def extract_html_content(url: str) -> str:
    """从 HTML 公告页面提取正文文本"""
    if not url:
        return ""

    with bypass_proxy():
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=30)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            html = resp.text
        except Exception as e:
            logger.warning("获取公告 HTML 失败: %s — %s", url[:60], e)
            return ""

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("未安装 bs4，无法解析 HTML 公告")
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # 移除脚本与样式
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # 巨潮常见正文容器
    selectors = [
        "div.detail-body",
        "div#detail-body",
        "div.notice-content",
        "div.content",
        "div.main-content",
        "div#noticeDetail",
        "div.detail-page",
        "body",
    ]
    text = ""
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            text = node.get_text(separator="\n", strip=True)
            if len(text) > 100:
                break

    if not text:
        text = soup.get_text(separator="\n", strip=True)

    # 清理多余空行
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


def fetch_announcement_full_text(
    url: str,
    symbol: str = "",
    title: str = "",
    date: str = "",
) -> str:
    """
    获取公告全文：优先读缓存，再尝试 URL 或巨潮搜索。
    PDF 公告返回提示信息并保留链接。
    """
    if symbol and title and date:
        cached = _load_cache(symbol, date, title)
        if cached and cached.get("content"):
            return cached["content"]

    fetch_url = url or ""
    content_type = "unknown"

    if fetch_url:
        resolved = _resolve_url_to_content_source(fetch_url, symbol=symbol, title=title, date=date)
        fetch_url = resolved.get("url", fetch_url)
        content_type = resolved.get("content_type", "unknown")
    elif symbol and title:
        info = fetch_cninfo_announcement(symbol, title)
        fetch_url = info.get("url", "")
        content_type = info.get("content_type", "unknown")

    if not fetch_url:
        return "（未能获取公告链接，请手动查阅巨潮资讯网）"

    if content_type == "pdf" or fetch_url.lower().endswith(".pdf"):
        content = f"公告链接: {fetch_url}\n（PDF 公告需手动查看）"
        if symbol and title and date:
            cache_announcement(symbol, date, title, content, url=fetch_url, content_type="pdf")
        return content

    body = extract_html_content(fetch_url)
    if not _is_valid_extracted_text(body):
        content = f"公告链接: {fetch_url}\n（HTML 正文解析失败，请手动查阅）"
    else:
        content = body

    if symbol and title and date and body and _is_valid_extracted_text(body):
        cache_announcement(symbol, date, title, content, url=fetch_url, content_type="html")

    return content


def chunk_text(text: str, max_chars: int = 2000, overlap: int = 200) -> list[str]:
    """将长文本分块，块间保留 overlap 字符重叠"""
    if not text or len(text) <= max_chars:
        return [text] if text else []

    chunks: list[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + max_chars, text_len)
        chunk = text[start:end]

        # 尽量在段落边界切分
        if end < text_len:
            break_at = chunk.rfind("\n\n")
            if break_at > max_chars // 2:
                end = start + break_at
                chunk = text[start:end]

        chunks.append(chunk.strip())
        if end >= text_len:
            break
        start = max(end - overlap, start + 1)

    return [c for c in chunks if c]


def summarize_long_announcement(
    text: str,
    symbol: str = "",
    title: str = "",
    max_chars: int = ANNOUNCEMENT_MAX_CHARS,
) -> str:
    """
    长公告 RAG 管道：分块 → 每块独立摘要 → 合并为最终摘要文本。
    短于 max_chars 的文本直接截断返回。
    """
    if not text:
        return ""
    if len(text) <= max_chars:
        return text[:max_chars]

    # 延迟导入，避免模块加载时的循环依赖
    from shared.llm_client import call_llm

    chunks = chunk_text(text, max_chars=2000, overlap=200)
    if len(chunks) <= 1:
        return text[:max_chars]

    chunk_summaries: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        prompt = (
            f"以下是上市公司公告正文第 {i}/{len(chunks)} 段，请提取关键信息摘要"
            f"（财务数字、日期、比例、重要事项），200 字以内：\n\n{chunk}"
        )
        summary = call_llm(
            prompt,
            system_prompt="你是 A 股公告分析助手，只基于给定文本摘要，不编造信息。",
            temperature=0.2,
            max_tokens=512,
            task_type="announcement",
        )
        chunk_summaries.append(summary or chunk[:500])

    merged = "\n\n".join(f"[段 {i+1}] {s}" for i, s in enumerate(chunk_summaries))
    header = f"（长公告分 {len(chunks)} 段摘要合并，原文约 {len(text)} 字）\n\n"
    result = header + merged
    return result[:max_chars]
