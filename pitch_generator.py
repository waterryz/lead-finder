"""Generate a personalized cold-outreach pitch offering penetration-testing services."""
from llm import chat

SYSTEM_PROMPT = """Ты пишешь короткое первое сообщение для B2B-аутрича от компании, оказывающей услуги
ПЕНТЕСТА (авторизованного тестирования на проникновение) и аудита безопасности.

Требования к сообщению:
- Русский язык, 3-5 предложений, максимум ~600 символов, обычный текст (без markdown).
- Обращение: если есть имя ЛПР — по имени; иначе "Здравствуйте".
- Сошлись на ОДНО конкретное наблюдение о поверхности атаки их сайта (логин/регистрация/личный кабинет/API/оплата)
  как на причину, почему стоит проверить безопасность. Это должно звучать как забота, а не угроза.
- Предложи авторизованный пентест / аудит безопасности, привязанный к их поверхности и типу данных.
- Тон: профессиональный, уважительный, без запугивания, без заявлений что их «взломали» или что у них «дыры».
  Не выдумывай уязвимости, факты, кейсы и цифры. Не давай технических деталей эксплуатации.
- Мягкий призыв: предложить короткий созвон или бесплатную первичную оценку/скоуп.
- Максимум 1 эмодзи (можно без).
Верни ТОЛЬКО текст сообщения."""


async def generate_pitch(company: dict) -> str:
    try:
        dms = company.get("decision_makers", []) or []
        first_name = ""
        if dms and isinstance(dms[0], dict):
            full = (dms[0].get("name") or "").strip()
            first_name = full.split()[0] if full else ""

        surface = company.get("attack_surface", []) or []
        ctx = f"""Компания: {company.get('name', company.get('domain', ''))}
Сайт: {company.get('domain', '')}
Чем занимается: {company.get('description', '')}
Размер: {company.get('size', '')}
Поверхность атаки: {', '.join(surface) if surface else 'не выявлена явно'}
Чувствительность данных: {company.get('data_sensitivity', 'unknown')}
Стек: {', '.join(company.get('stack', [])[:8])}
Что стоит проверить (аналитика): {company.get('needs', '')}
Имя ЛПР для обращения: {first_name or '—'}"""

        pitch = await chat(SYSTEM_PROMPT, ctx, temperature=0.6, max_tokens=400)
        return (pitch or "").strip()
    except Exception:
        return ""
