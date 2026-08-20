import json
import hashlib
from openai import AsyncOpenAI
from config import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, AI_CACHE_ENABLED,
)

_client = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    return _client


def _cache_key(system: str, user: str, temperature: float) -> str:
    raw = f"{DEEPSEEK_MODEL}|{temperature}|{system}|{user}"
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


async def chat(system: str, user: str, temperature: float = 0.3,
               max_tokens: int = 800, use_cache: bool = True) -> str:
    """Single-turn completion against DeepSeek. Transparently caches by content hash."""
    key = _cache_key(system, user, temperature)

    if use_cache and AI_CACHE_ENABLED:
        from database import get_ai_cache
        cached = get_ai_cache(key)
        if cached is not None:
            return cached

    response = await get_client().chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    text = (response.choices[0].message.content or "").strip()

    if use_cache and AI_CACHE_ENABLED and text:
        from database import set_ai_cache
        set_ai_cache(key, text)

    return text


def parse_json(text: str) -> dict | list:
    """Robustly parse a JSON object/array possibly wrapped in ```json fences or prose."""
    t = text.strip()
    t = t.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # Fallback: grab the outermost {...} or [...] block.
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = t.find(open_c)
        end = t.rfind(close_c)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no valid JSON found")
