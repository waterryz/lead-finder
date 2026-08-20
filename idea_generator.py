"""Generate search-query ideas that tend to be good pentest targets."""
from llm import chat, parse_json

SYSTEM = """Ты генерируешь идеи поисковых запросов для поиска КОМПАНИЙ — хороших целей для пентеста.
Хорошая цель = сайт с реальной поверхностью атаки (логин, регистрация, личный кабинет, онлайн-оплата, API)
и чувствительными данными (персональные, финансовые, медицинские, образовательные).

Верни JSON-массив из N коротких поисковых запросов на русском (ниша, иногда с городом).
Разнообразь сектора: e-commerce, финтех, медицина, образование, доставка, недвижимость, страхование,
HR/рекрутинг, SaaS, туризм, логистика, юридические сервисы, фитнес, онлайн-записи.
Примеры формата: "интернет-магазин электроники москва", "онлайн-школа английского",
"клиника эстетической медицины", "сервис доставки еды", "страховая компания онлайн",
"crm для салонов красоты", "запись к врачу онлайн".
Только JSON-массив строк, без пояснений."""


async def generate_ideas(n: int = 6, avoid: list[str] | None = None) -> list[str]:
    try:
        avoid = avoid or []
        user = (
            f"Сгенерируй {n} разных идей. "
            f"Не повторяй эти: {', '.join(avoid) if avoid else '—'}"
        )
        text = await chat(SYSTEM, user, temperature=0.95, max_tokens=400, use_cache=False)
        data = parse_json(text)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()][:n]
        return []
    except Exception:
        return []
