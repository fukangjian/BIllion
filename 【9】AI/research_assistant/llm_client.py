"""
LLM 客户端 — 统一调用 OpenAI / Anthropic，含重试与 graceful degradation
"""
import logging
import time
from typing import Optional

from config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    LLM_MAX_RETRIES,
    LLM_PROVIDER,
    LLM_RETRY_DELAY,
    OPENAI_API_KEY,
    OPENAI_MODEL,
)

logger = logging.getLogger(__name__)


def has_llm_api_key() -> bool:
    """检查是否配置了可用的 API 密钥"""
    if LLM_PROVIDER == "anthropic":
        return bool(ANTHROPIC_API_KEY)
    return bool(OPENAI_API_KEY)


def call_llm(
    prompt: str,
    system_prompt: str = "你是一位专业的 A 股投资研究分析师，输出简洁、结构化的中文分析。",
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> Optional[str]:
    """
    调用 LLM 生成文本。
    无 API key 或调用失败时返回 None（graceful degradation）。
    """
    if not has_llm_api_key():
        logger.warning("未配置 LLM API 密钥，跳过 AI 分析")
        return None

    last_err = None
    for attempt in range(LLM_MAX_RETRIES):
        try:
            if LLM_PROVIDER == "anthropic":
                return _call_anthropic(prompt, system_prompt, temperature, max_tokens)
            return _call_openai(prompt, system_prompt, temperature, max_tokens)
        except Exception as e:
            last_err = e
            logger.warning("LLM 调用失败 (尝试 %d/%d): %s", attempt + 1, LLM_MAX_RETRIES, e)
            if attempt < LLM_MAX_RETRIES - 1:
                time.sleep(LLM_RETRY_DELAY * (attempt + 1))

    logger.error("LLM 调用最终失败: %s", last_err)
    return None


def _call_openai(prompt: str, system_prompt: str, temperature: float, max_tokens: int) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""


def _call_anthropic(prompt: str, system_prompt: str, temperature: float, max_tokens: int) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    return response.content[0].text if response.content else ""
