"""
通用工具 — 代理绕过、AkShare 重试、Markdown 格式化、股票代码规范化
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Optional

import pandas as pd

from config import FETCH_RETRY, FETCH_RETRY_DELAY

logger = logging.getLogger(__name__)

_PROXY_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
_PROXY_PATCHED = False


def patch_bypass_proxy():
    """
    绕过 Windows 系统代理（V2Ray/Clash TUN 模式等），直连国内金融数据源。
    """
    global _PROXY_PATCHED
    if _PROXY_PATCHED:
        return
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    for key in _PROXY_KEYS:
        os.environ.pop(key, None)

    import requests as _req
    _original_init = _req.Session.__init__

    def _patched_init(self, *a, **kw):
        _original_init(self, *a, **kw)
        self.trust_env = False
        self.proxies = {"http": "", "https": ""}

    _req.Session.__init__ = _patched_init
    _PROXY_PATCHED = True
    logger.info("已绕过系统代理，直连国内数据源")


@contextmanager
def bypass_proxy():
    """上下文管理器：确保代理已绕过"""
    patch_bypass_proxy()
    yield


# 模块加载时立即绕过代理
patch_bypass_proxy()


def normalize_symbol(symbol: str) -> str:
    """规范化股票代码为 6 位数字"""
    s = str(symbol).strip().upper()
    if "." in s:
        s = s.split(".")[0]
    return s.zfill(6)[-6:]


def retry_fetch(func: Callable, *args, **kwargs):
    """带重试的数据获取，自动绕过代理直连国内数据源"""
    last_err = None
    with bypass_proxy():
        for attempt in range(FETCH_RETRY):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_err = e
                logger.warning("数据获取失败 (尝试 %d/%d): %s", attempt + 1, FETCH_RETRY, e)
                if attempt < FETCH_RETRY - 1:
                    time.sleep(FETCH_RETRY_DELAY * (attempt + 1))
    raise last_err


def safe_fetch(func: Callable, *args, default=None, **kwargs):
    """安全获取数据，失败时返回 default"""
    try:
        return retry_fetch(func, *args, **kwargs)
    except Exception as e:
        logger.error("数据获取最终失败: %s", e)
        return default


def df_to_markdown_table(df: pd.DataFrame, float_fmt: str = ".2f") -> str:
    """DataFrame 转 Obsidian 兼容 Markdown 表格"""
    if df is None or df.empty:
        return "_暂无数据_"

    display_df = df.copy()
    for col in display_df.select_dtypes(include=["float", "float64"]).columns:
        display_df[col] = display_df[col].apply(
            lambda x: f"{x:{float_fmt}}" if pd.notna(x) else "-"
        )

    headers = "| " + " | ".join(str(c) for c in display_df.columns) + " |"
    sep = "| " + " | ".join("---" for _ in display_df.columns) + " |"
    rows = []
    for _, row in display_df.iterrows():
        rows.append("| " + " | ".join(str(v) for v in row.values) + " |")
    return "\n".join([headers, sep] + rows)


def write_markdown(content: str, output_path: Path) -> Path:
    """写入 Markdown 文件"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    logger.info("已写入: %s", output_path)
    return output_path


def obsidian_frontmatter(tags: list[str], **extra) -> str:
    """生成 Obsidian YAML frontmatter"""
    lines = ["---"]
    lines.append(f"tags: [{', '.join(tags)}]")
    lines.append(f"created: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    for k, v in extra.items():
        lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


_NAME_CACHE: dict[str, str] = {}


def get_stock_name(symbol: str) -> str:
    """获取股票名称（使用 stock_info_a_code_name 并缓存）"""
    symbol = normalize_symbol(symbol)
    if symbol in _NAME_CACHE:
        return _NAME_CACHE[symbol]

    try:
        import akshare as ak

        if not _NAME_CACHE:
            df = safe_fetch(ak.stock_info_a_code_name, default=pd.DataFrame())
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    _NAME_CACHE[str(row["code"])] = str(row["name"])
                if symbol in _NAME_CACHE:
                    return _NAME_CACHE[symbol]
    except Exception:
        pass

    return symbol
