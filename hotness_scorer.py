"""hotness_scorer.py

Оценка "горячести" лида (hotness) — насколько компании СРОЧНО нужны
веб/IT-услуги. Заброшенный/устаревший сайт = горячий лид.

Скоринг выполняется ТОЛЬКО по уже полученным данным site_data
(никаких сетевых запросов). Начинаем с 0, прибавляем баллы, клампим 0..100.
"""

import re
import datetime

try:
    from config import HOTNESS_HOT_THRESHOLD
except Exception:  # pragma: no cover - на случай отсутствия/битого конфига
    HOTNESS_HOT_THRESHOLD = 50


# Регэксп для поиска 4-значных годов вида 20xx.
_YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Маркеры "сайт в разработке" / заглушки.
_UNDER_CONSTRUCTION_MARKERS = (
    "under construction",
    "в разработке",
    "сайт в разработке",
    "coming soon",
    "скоро открытие",
    "placeholder",
)

# Маркеры аналитики/трекинга в html.
_ANALYTICS_MARKERS = (
    "google-analytics",
    "gtag",
    "googletagmanager",
    "mc.yandex",
    "metrika",
    "ym(",
)


def _clamp(value: int, low: int = 0, high: int = 100) -> int:
    """Ограничить значение диапазоном [low, high]."""
    if value < low:
        return low
    if value > high:
        return high
    return value


async def score_hotness(site_data: dict) -> dict:
    """Оценить "горячесть" лида по данным site_data.

    Возвращает {"score": int(0..100), "reasons": list[str], "is_hot": bool}.
    Никогда не бросает исключение наружу — при любой ошибке возвращает
    безопасный дефолт {"score": 0, "reasons": [], "is_hot": False}.
    """
    try:
        if not isinstance(site_data, dict):
            return {"score": 0, "reasons": [], "is_hot": False}

        score = 0
        reasons: list[str] = []

        # Безопасно достаём поля.
        text = site_data.get("text") or ""
        if not isinstance(text, str):
            text = str(text)
        html = site_data.get("html") or ""
        if not isinstance(html, str):
            html = str(html)
        final_url = site_data.get("final_url") or site_data.get("url") or ""
        if not isinstance(final_url, str):
            final_url = str(final_url)
        meta = site_data.get("meta")
        if not isinstance(meta, dict):
            meta = {}

        text_lower = text.lower()
        html_lower = html.lower()

        current_year = datetime.datetime.now().year

        # --- Старый копирайт / год ---
        try:
            years = [int(y) for y in _YEAR_RE.findall(text)]
            # Отсекаем нереалистично будущие годы (мусор), оставляем <= current_year+1.
            years = [y for y in years if y <= current_year + 1]
            if years:
                max_year = max(years)
                if max_year <= current_year - 4:
                    score += 25
                    reasons.append(
                        "Очень старая дата на сайте (копирайт %d)" % max_year
                    )
                elif max_year <= current_year - 2:
                    score += 15
                    reasons.append(
                        "Устаревшая дата на сайте (копирайт %d)" % max_year
                    )
        except Exception:
            pass

        # --- Нет HTTPS ---
        try:
            if final_url.strip().lower().startswith("http://"):
                score += 15
                reasons.append("Сайт без HTTPS (небезопасное соединение)")
        except Exception:
            pass

        # --- Нет viewport meta (не адаптивен под мобильные) ---
        try:
            if "viewport" not in meta or not meta.get("viewport"):
                score += 12
                reasons.append("Нет мета viewport — сайт не адаптирован под мобильные")
        except Exception:
            pass

        # --- Медленная загрузка ---
        try:
            elapsed = site_data.get("elapsed")
            if elapsed is not None:
                elapsed = float(elapsed)
                if elapsed > 6:
                    score += 20
                    reasons.append("Очень медленная загрузка (%.1f сек)" % elapsed)
                elif elapsed > 3:
                    score += 10
                    reasons.append("Медленная загрузка (%.1f сек)" % elapsed)
        except Exception:
            pass

        # --- Тонкий контент ---
        try:
            text_len = len(text)
            if text_len < 250:
                score += 25
                reasons.append("Очень мало контента на сайте (%d симв.)" % text_len)
            elif text_len < 600:
                score += 15
                reasons.append("Мало контента на сайте (%d симв.)" % text_len)
        except Exception:
            pass

        # --- Нет аналитики ---
        try:
            if not any(m in html_lower for m in _ANALYTICS_MARKERS):
                score += 10
                reasons.append("Не установлена веб-аналитика (нет GA/Метрики)")
        except Exception:
            pass

        # --- "В разработке" / заглушка ---
        try:
            if any(m in text_lower for m in _UNDER_CONSTRUCTION_MARKERS):
                score += 30
                reasons.append("Сайт-заглушка / в разработке")
        except Exception:
            pass

        # --- Устаревшие технические маркеры (по 8 баллов, но с общим капом) ---
        try:
            outdated_hits: list[str] = []
            if "jquery-1." in html_lower:
                outdated_hits.append("старый jQuery 1.x")
            # Верстка таблицами (много тегов <table>).
            if html_lower.count("<table") >= 5:
                outdated_hits.append("верстка таблицами")
            if "flash" in html_lower:
                outdated_hits.append("устаревший Flash")
            # Старый Bitrix (эвристика по характерным старым путям/меткам).
            if ("bitrix" in html_lower) and (
                "/bitrix/templates/" in html_lower
                or "bx-" in html_lower
                or "bitrix24" not in html_lower
            ):
                outdated_hits.append("старый Bitrix")
            if "frameset" in html_lower or "<frame" in html_lower:
                outdated_hits.append("устаревшие фреймы (frameset)")

            if outdated_hits:
                # Общий кап вклада устаревших маркеров — 24 балла (3 * 8).
                contribution = min(len(outdated_hits) * 8, 24)
                score += contribution
                reasons.append("Устаревшие технологии: " + ", ".join(outdated_hits))
        except Exception:
            pass

        # --- Нет favicon / нет meta description ---
        try:
            has_favicon = ("favicon" in html_lower) or ('rel="icon"' in html_lower) or (
                "rel='icon'" in html_lower
            ) or ("shortcut icon" in html_lower)
            has_description = bool(meta.get("description"))
            if (not has_favicon) or (not has_description):
                score += 5
                if not has_favicon and not has_description:
                    reasons.append("Нет favicon и мета-описания")
                elif not has_favicon:
                    reasons.append("Нет favicon")
                else:
                    reasons.append("Нет мета-описания (description)")
        except Exception:
            pass

        score = _clamp(score, 0, 100)

        try:
            is_hot = score >= int(HOTNESS_HOT_THRESHOLD)
        except Exception:
            is_hot = score >= 50

        return {"score": score, "reasons": reasons, "is_hot": bool(is_hot)}

    except Exception:
        return {"score": 0, "reasons": [], "is_hot": False}
