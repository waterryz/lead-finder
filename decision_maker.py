"""
decision_maker.py

Извлечение ЛПР (лиц, принимающих решения) со страницы компании.

Экспортирует единственную корутину:
    async def find_decision_makers(site_data: dict, about_text: str = "") -> list[dict]

Функция скармливает LLM видимый текст сайта + about_text и просит вернуть
ТОЛЬКО реальных людей (основатель, CEO, гендиректор, директор, owner,
руководитель, управляющий, head of X, ...), которые буквально присутствуют
в тексте. Ничего не выдумывает. Всегда возвращает Python list[dict]
(максимум 5 элементов); при любой ошибке — пустой список.
"""

from __future__ import annotations

import llm

# Максимальная длина текста, отдаваемого модели (символов).
_MAX_INPUT_CHARS = 4000

# Максимум людей в ответе.
_MAX_PEOPLE = 5

# Максимальная длина отдельного строкового поля в результате.
_MAX_FIELD_LEN = 300

_SYSTEM_PROMPT = (
    "Ты — строгий экстрактор данных. На вход тебе дают ТЕКСТ со страницы "
    "сайта компании. Твоя задача: найти реальных людей — лиц, принимающих "
    "решения (ЛПР): основатель, CEO, гендиректор, генеральный директор, "
    "директор, владелец, owner, founder, руководитель, управляющий, "
    "head of / глава подразделения, коммерческий/технический директор и т.п.\n"
    "\n"
    "ЖЁСТКИЕ ПРАВИЛА:\n"
    "1. Извлекай ТОЛЬКО то, что буквально написано в тексте. "
    "НИКОГДА не выдумывай имена, должности или контакты, которых нет в тексте.\n"
    "2. name — это имя КОНКРЕТНОГО человека (например 'Иван Петров', "
    "'John Smith'). Не бренды, не названия компаний, не отделы.\n"
    "3. role — должность/роль этого человека, дословно из текста.\n"
    "4. contact — персональный email / телефон / telegram, указанный рядом с "
    "этим человеком в тексте. Если рядом нет — пустая строка \"\".\n"
    "5. Если в тексте нет ни одного реального ЛПР — верни пустой массив [].\n"
    "6. Выводи ТОЛЬКО валидный JSON-массив объектов вида "
    "{\"name\": \"...\", \"role\": \"...\", \"contact\": \"...\"}. "
    "Без пояснений, без markdown, без текста до или после массива.\n"
    "Максимум 5 человек."
)


def _clean_str(value, limit: int = _MAX_FIELD_LEN) -> str:
    """Безопасно привести значение к обрезанной строке."""
    try:
        if value is None:
            return ""
        s = str(value).strip()
        if len(s) > limit:
            s = s[:limit].strip()
        return s
    except Exception:
        return ""


def _build_user_prompt(site_data: dict, about_text: str) -> str:
    """Собрать входной текст для модели (site_data['text'] + about_text, cap)."""
    parts: list[str] = []
    try:
        site_text = site_data.get("text") or ""
    except Exception:
        site_text = ""
    if not isinstance(site_text, str):
        site_text = str(site_text or "")
    if not isinstance(about_text, str):
        about_text = str(about_text or "")

    if site_text.strip():
        parts.append(site_text.strip())
    if about_text.strip():
        parts.append(about_text.strip())

    combined = "\n\n".join(parts)
    if len(combined) > _MAX_INPUT_CHARS:
        combined = combined[:_MAX_INPUT_CHARS]

    return (
        "ТЕКСТ СО СТРАНИЦЫ КОМПАНИИ (извлеки ЛПР строго по тексту):\n"
        "---\n"
        f"{combined}\n"
        "---\n"
        "Верни ТОЛЬКО JSON-массив."
    )


def _normalize_people(data) -> list[dict]:
    """Привести ответ модели к list[dict] с полями name/role/contact."""
    if not isinstance(data, list):
        # Иногда модель заворачивает массив в объект.
        if isinstance(data, dict):
            for key in ("people", "decision_makers", "lpr", "result", "items", "data"):
                inner = data.get(key)
                if isinstance(inner, list):
                    data = inner
                    break
            else:
                return []
        else:
            return []

    result: list[dict] = []
    seen: set[str] = set()

    for item in data:
        if not isinstance(item, dict):
            continue
        name = _clean_str(item.get("name"))
        if not name:
            # Без имени элемент бесполезен — это не конкретный человек.
            continue
        role = _clean_str(item.get("role"))
        contact = _clean_str(item.get("contact"))

        dedup_key = name.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        result.append({"name": name, "role": role, "contact": contact})
        if len(result) >= _MAX_PEOPLE:
            break

    return result


async def find_decision_makers(site_data: dict, about_text: str = "") -> list[dict]:
    """
    Извлечь реальных ЛПР со страницы компании.

    Возвращает list[dict], каждый: {"name": str, "role": str, "contact": str}.
    Максимум 5 элементов. При любой ошибке или нелистовом ответе модели — [].
    """
    try:
        if not isinstance(site_data, dict):
            return []

        user_prompt = _build_user_prompt(site_data, about_text)

        # Если совсем нет содержимого — незачем дёргать модель.
        # (Проверяем по фактическому тексту, не по обёртке.)
        try:
            has_site_text = bool((site_data.get("text") or "").strip())
        except Exception:
            has_site_text = False
        has_about = bool(isinstance(about_text, str) and about_text.strip())
        if not has_site_text and not has_about:
            return []

        try:
            raw = await llm.chat(
                system=_SYSTEM_PROMPT,
                user=user_prompt,
                temperature=0.0,
                max_tokens=800,
                use_cache=True,
            )
        except Exception:
            return []

        if not raw or not isinstance(raw, str):
            return []

        try:
            parsed = llm.parse_json(raw)
        except Exception:
            # parse_json бросает ValueError, если JSON не найден.
            return []

        return _normalize_people(parsed)
    except Exception:
        # Никогда не поднимаем исключение наверх.
        return []
