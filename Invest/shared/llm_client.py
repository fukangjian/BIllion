"""
LLM 客户端 — 调用 Kimi (Moonshot) API，兼容 OpenAI 协议，含重试与 graceful degradation
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
import time
from typing import Optional

from config import (
    KIMI_API_KEY,
    KIMI_BASE_URL,
    KIMI_MODEL,
    LLM_MAX_RETRIES,
    LLM_RETRY_DELAY,
)

logger = logging.getLogger(__name__)


def has_llm_api_key() -> bool:
    """检查是否配置了 Kimi API 密钥"""
    return bool(KIMI_API_KEY)


def call_llm(
    prompt: str,
    system_prompt: str = "你是一位专业的 A 股投资研究分析师，输出简洁、结构化的中文分析。",
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> Optional[str]:
    """
    调用 Kimi LLM 生成文本。
    无 API key 或调用失败时返回 None（graceful degradation）。
    """
    if not has_llm_api_key():
        logger.warning("未配置 KIMI_API_KEY，跳过 AI 分析")
        return None

    last_err = None
    for attempt in range(LLM_MAX_RETRIES):
        try:
            return _call_kimi(prompt, system_prompt, temperature, max_tokens)
        except Exception as e:
            last_err = e
            logger.warning("LLM 调用失败 (尝试 %d/%d): %s", attempt + 1, LLM_MAX_RETRIES, e)
            if attempt < LLM_MAX_RETRIES - 1:
                time.sleep(LLM_RETRY_DELAY * (attempt + 1))

    logger.error("LLM 调用最终失败: %s", last_err)
    return None


def _call_kimi(prompt: str, system_prompt: str, temperature: float, max_tokens: int) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=KIMI_API_KEY, base_url=KIMI_BASE_URL)
    response = client.chat.completions.create(
        model=KIMI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""
