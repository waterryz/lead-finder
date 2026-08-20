# -*- coding: utf-8 -*-
"""
js_renderer.py — headless-рендеринг JS/SPA-сайтов через Playwright.

Модуль спроектирован так, чтобы безопасно импортироваться даже без установленного
Playwright: сам импорт обёрнут в try/except, а публичные функции никогда не
пробрасывают исключения наружу — они возвращают документированные безопасные
значения по умолчанию.
"""

from __future__ import annotations

# --- Guard the Playwright import: модуль обязан импортироваться даже если
#     playwright не установлен (тогда render() отдаёт ok=False). ---
try:
    from playwright.async_api import async_playwright  # type: ignore
    _PLAYWRIGHT_AVAILABLE = True
except Exception:  # ImportError и любые прочие проблемы окружения
    async_playwright = None  # type: ignore
    _PLAYWRIGHT_AVAILABLE = False

# Конфиг-флаги. Импортируем аккуратно — если что-то отсутствует, ставим разумные
# дефолты, чтобы модуль оставался работоспособным.
try:
    from config import (
        JS_RENDER_ENABLED,
        JS_RENDER_MIN_TEXT,
        JS_RENDER_TIMEOUT,
    )
except Exception:  # pragma: no cover - защитный дефолт
    JS_RENDER_ENABLED = False
    JS_RENDER_MIN_TEXT = 200
    JS_RENDER_TIMEOUT = 30

# Функции скрапера. Импортируем через модуль, чтобы не падать, если чего-то нет.
try:
    from scraper import next_proxy as _next_proxy
except Exception:  # pragma: no cover - защитный дефолт
    _next_proxy = None  # type: ignore

try:
    from scraper import _extract_contacts as _scraper_extract_contacts
except Exception:  # pragma: no cover - защитный дефолт
    _scraper_extract_contacts = None  # type: ignore


# Реалистичный desktop User-Agent.
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _safe_next_proxy():
    """Вернуть proxy-строку из scraper.next_proxy() либо None (не бросая)."""
    if _next_proxy is None:
        return None
    try:
        proxy = _next_proxy()
        if proxy:
            return proxy
    except Exception:
        return None
    return None


async def render(url: str) -> dict:
    """
    Отрендерить страницу в headless Chromium и вернуть HTML/текст/статус.

    Возвращает dict строго вида:
        {"ok": bool, "html": str, "text": str, "status": int, "error": str|None}

    Никогда не бросает исключений — при любой ошибке отдаёт
    {"ok": False, "html": "", "text": "", "status": 0, "error": <str>}.
    """
    fail = {"ok": False, "html": "", "text": "", "status": 0, "error": None}

    if not _PLAYWRIGHT_AVAILABLE or async_playwright is None:
        fail["error"] = "playwright not installed"
        return fail

    if not url:
        fail["error"] = "empty url"
        return fail

    browser = None
    context = None
    try:
        # Опции запуска: при наличии proxy добавляем его на уровень launch.
        launch_kwargs = {"headless": True}
        proxy = _safe_next_proxy()
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}

        timeout_ms = int(JS_RENDER_TIMEOUT) * 1000

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(user_agent=_DESKTOP_UA)
                page = await context.new_page()

                status = 0
                try:
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    if response is not None:
                        try:
                            status = int(response.status)
                        except Exception:
                            status = 0
                except Exception as nav_err:
                    # Навигация не удалась — возвращаем безопасный дефолт.
                    return {
                        "ok": False,
                        "html": "",
                        "text": "",
                        "status": 0,
                        "error": str(nav_err),
                    }

                # Best-effort ожидание networkidle — таймаут глотаем.
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=timeout_ms
                    )
                except Exception:
                    pass

                # Извлекаем HTML.
                try:
                    html = await page.content()
                except Exception:
                    html = ""

                # Извлекаем видимый текст body.
                try:
                    text = await page.inner_text("body")
                except Exception:
                    text = ""

                return {
                    "ok": True,
                    "html": html or "",
                    "text": text or "",
                    "status": status,
                    "error": None,
                }
            finally:
                # Гарантированно закрываем context и browser.
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        pass
    except Exception as e:
        return {
            "ok": False,
            "html": "",
            "text": "",
            "status": 0,
            "error": str(e),
        }


def _merge_contacts(site_data: dict, extracted: dict) -> None:
    """Слить контакты из extracted в site_data: union списков, merge socials."""
    if not isinstance(extracted, dict):
        return

    # emails / phones — объединение с сохранением порядка и уникальности.
    for key in ("emails", "phones"):
        try:
            existing = site_data.get(key)
            if not isinstance(existing, list):
                existing = []
            new_items = extracted.get(key) or []
            if not isinstance(new_items, list):
                continue
            seen = set(existing)
            merged = list(existing)
            for item in new_items:
                if item and item not in seen:
                    seen.add(item)
                    merged.append(item)
            site_data[key] = merged
        except Exception:
            continue

    # socials — merge словарей (не затираем уже найденное).
    try:
        new_socials = extracted.get("socials")
        if isinstance(new_socials, dict):
            existing_socials = site_data.get("socials")
            if not isinstance(existing_socials, dict):
                existing_socials = {}
            for k, v in new_socials.items():
                if v and not existing_socials.get(k):
                    existing_socials[k] = v
            site_data["socials"] = existing_socials
    except Exception:
        pass


async def enrich_with_js(site_data: dict) -> dict:
    """
    Дорендерить сайт через JS, если статического текста оказалось мало.

    Условие: JS_RENDER_ENABLED и len(site_data['text']) < JS_RENDER_MIN_TEXT.
    Если рендер успешен и дал больше текста — обновляем html/text, ставим
    rendered=True и переизвлекаем контакты, объединяя их с уже найденными.

    Всегда возвращает site_data (возможно, без изменений). Никогда не бросает.
    """
    try:
        if not isinstance(site_data, dict):
            return site_data

        if not JS_RENDER_ENABLED:
            return site_data

        current_text = site_data.get("text", "") or ""
        if len(current_text) >= JS_RENDER_MIN_TEXT:
            return site_data

        target_url = site_data.get("final_url") or site_data.get("url")
        if not target_url:
            return site_data

        result = await render(target_url)
        if not result or not result.get("ok"):
            return site_data

        rendered_text = result.get("text", "") or ""
        rendered_html = result.get("html", "") or ""

        # Обновляем только если рендер реально дал больше текста.
        if len(rendered_text) > len(current_text):
            site_data["html"] = rendered_html
            site_data["text"] = rendered_text[:8000]
            site_data["rendered"] = True

            # Переизвлечение контактов из свежего html/text.
            if _scraper_extract_contacts is not None:
                try:
                    extracted = _scraper_extract_contacts(
                        rendered_text, rendered_html
                    )
                    _merge_contacts(site_data, extracted)
                except Exception:
                    pass

        return site_data
    except Exception:
        # Любая непредвиденная ошибка — возвращаем исходные данные без изменений.
        return site_data
