"""
LLM 客户端 — 多提供商路由（Kimi / DeepSeek / 自定义 OpenAI 兼容），含缓存与重试
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Optional

from config import (
    CUSTOM_LLM_API_KEY,
    CUSTOM_LLM_BASE_URL,
    CUSTOM_LLM_MODEL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_MODEL_PRO,
    KIMI_API_KEY,
    KIMI_BASE_URL,
    KIMI_MODEL,
    KIMI_MODEL_LONG,
    KIMI_MODEL_REASONING,
    LLM_CACHE_DIR,
    LLM_CACHE_TTL,
    LLM_MAX_RETRIES,
    LLM_PROVIDER_PRIORITY,
    LLM_RETRY_DELAY,
)

logger = logging.getLogger(__name__)

# 各提供商 API 密钥映射
_PROVIDER_KEYS = {
    "kimi": KIMI_API_KEY,
    "deepseek": DEEPSEEK_API_KEY,
    "custom": CUSTOM_LLM_API_KEY,
}


def has_llm_api_key() -> bool:
    """检查是否配置了任一 LLM API 密钥"""
    return any(_PROVIDER_KEYS.values())


def _resolve_provider(provider: Optional[str] = None) -> Optional[str]:
    """解析实际使用的 LLM 提供商"""
    if provider:
        key = _PROVIDER_KEYS.get(provider, "")
        if key:
            return provider
        logger.warning("指定提供商 %s 未配置 API 密钥，尝试自动选择", provider)
        return None

    for name in LLM_PROVIDER_PRIORITY:
        if _PROVIDER_KEYS.get(name):
            return name
    return None


def _model_for_task(provider: str, task_type: str) -> str:
    """按任务类型选择模型"""
    if task_type == "announcement":
        if provider == "kimi":
            return KIMI_MODEL_LONG
        if provider == "deepseek":
            return DEEPSEEK_MODEL_PRO
        if provider == "custom":
            return CUSTOM_LLM_MODEL or "gpt-4o-mini"
    if task_type == "reasoning":
        # 深度推理任务（龙头辨识等多维综合研判）：用旗舰推理模型
        if provider == "kimi":
            return KIMI_MODEL_REASONING  # 默认 kimi-k3
        if provider == "deepseek":
            return DEEPSEEK_MODEL_PRO
        if provider == "custom":
            return CUSTOM_LLM_MODEL or "gpt-4o-mini"
    if task_type == "summary":
        if provider == "kimi":
            return KIMI_MODEL  # 默认 8k
        if provider == "deepseek":
            return DEEPSEEK_MODEL  # v4-flash 快速响应
        if provider == "custom":
            return CUSTOM_LLM_MODEL or "gpt-4o-mini"
    # analysis 及其他：各提供商默认模型
    if provider == "kimi":
        return KIMI_MODEL
    if provider == "deepseek":
        return DEEPSEEK_MODEL
    return CUSTOM_LLM_MODEL or "gpt-4o-mini"


def _cache_file(cache_key: str) -> Path:
    LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return LLM_CACHE_DIR / f"{cache_key}.json"


def _load_cache(cache_key: str) -> Optional[str]:
    path = _cache_file(cache_key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cached_at = data.get("cached_at", 0)
        if time.time() - cached_at > LLM_CACHE_TTL:
            path.unlink(missing_ok=True)
            return None
        logger.debug("命中 LLM 缓存: %s", cache_key[:12])
        return data.get("response")
    except (json.JSONDecodeError, OSError, TypeError) as e:
        logger.warning("读取 LLM 缓存失败: %s", e)
        return None


def _save_cache(cache_key: str, response: str, provider: str, model: str) -> None:
    path = _cache_file(cache_key)
    payload = {
        "response": response,
        "provider": provider,
        "model": model,
        "cached_at": time.time(),
        "cached_at_iso": datetime.now().isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_cache_key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def call_llm(
    prompt: str,
    system_prompt: str = "你是一位专业的 A 股投资研究分析师，输出简洁、结构化的中文分析。",
    temperature: float = 0.3,
    max_tokens: int = 4096,
    provider: Optional[str] = None,
    use_cache: bool = True,
    task_type: str = "analysis",
) -> Optional[str]:
    """
    调用 LLM 生成文本，支持多提供商路由与 24 小时响应缓存。
    无 API key 或调用失败时返回 None（graceful degradation）。
    """
    if not has_llm_api_key():
        logger.warning("未配置任何 LLM API 密钥，跳过 AI 分析")
        return None

    resolved = _resolve_provider(provider)
    if not resolved:
        logger.warning("无可用 LLM 提供商")
        return None

    cache_key = _make_cache_key(prompt)
    if use_cache:
        cached = _load_cache(cache_key)
        if cached is not None:
            return cached

    model = _model_for_task(resolved, task_type)
    last_err = None

    for attempt in range(LLM_MAX_RETRIES):
        try:
            result = _call_provider(resolved, prompt, system_prompt, temperature, max_tokens, model)
            if use_cache and result:
                _save_cache(cache_key, result, resolved, model)
            return result
        except Exception as e:
            last_err = e
            logger.warning(
                "LLM 调用失败 [%s/%s] (尝试 %d/%d): %s",
                resolved, model, attempt + 1, LLM_MAX_RETRIES, e,
            )
            if attempt < LLM_MAX_RETRIES - 1:
                time.sleep(LLM_RETRY_DELAY * (attempt + 1))

    logger.error("LLM 调用最终失败: %s", last_err)
    return None


def _call_provider(
    provider: str,
    prompt: str,
    system_prompt: str,
    temperature: float,
    max_tokens: int,
    model: str,
) -> str:
    from openai import OpenAI

    if provider == "kimi":
        client = OpenAI(api_key=KIMI_API_KEY, base_url=KIMI_BASE_URL)
    elif provider == "deepseek":
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    elif provider == "custom":
        if not CUSTOM_LLM_BASE_URL:
            raise ValueError("CUSTOM_LLM_BASE_URL 未配置")
        client = OpenAI(api_key=CUSTOM_LLM_API_KEY, base_url=CUSTOM_LLM_BASE_URL)
    else:
        raise ValueError(f"未知 LLM 提供商: {provider}")

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""


# 保留旧函数名兼容（内部转发）
def _call_kimi(prompt: str, system_prompt: str, temperature: float, max_tokens: int) -> str:
    return _call_provider("kimi", prompt, system_prompt, temperature, max_tokens, KIMI_MODEL)
