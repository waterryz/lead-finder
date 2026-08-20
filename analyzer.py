import json
from llm import chat, parse_json

SYSTEM_PROMPT = """Ты — аналитик для компании, которая оказывает услуги пентеста (тестирования на проникновение).
Твоя задача — оценить компанию как ПОТЕНЦИАЛЬНОГО КЛИЕНТА на пентест по её сайту.
Верни ТОЛЬКО валидный JSON (без markdown):

{
  "name": "Название компании",
  "description": "Чем занимается (1-2 предложения)",
  "stack": ["технология1", "технология2"],
  "size": "micro/small/medium/large",
  "data_sensitivity": "low/medium/high",
  "pentest_score": 1-10,
  "payout_score": 1-10,
  "payout_reason": "Короткая оценка платёжеспособности (почему)",
  "needs": "Что стоит проверить на пентесте и почему (1-2 предложения)"
}

Правила оценки:
- Смотри на ПОВЕРХНОСТЬ АТАКИ: есть ли логин, регистрация, личный кабинет, API, админка, онлайн-оплата.
- data_sensitivity: high — если обрабатывают персональные данные, платежи, мед/фин/юр-данные, аккаунты пользователей;
  medium — есть аккаунты/формы, но чувствительных данных мало; low — статичный сайт-визитка без аккаунтов.
- pentest_score (1=не интересно, 10=идеальный клиент на пентест): выше, если есть аутентификация + чувствительные данные
  + признаки самописного бэкенда/API/личного кабинета. Ниже — если это лендинг/визитка без входа и данных.
- payout_score (1=денег нет, 10=крупная состоятельная компания) — ПРОСТАЯ, НО КАЧЕСТВЕННАЯ оценка, есть ли у компании
  деньги реально заплатить (за баг-баунти или пентест). Оценивай по сектору и масштабу:
  ВЫСОКО (7-10): банки, финтех, страхование, телеком, крупный e-commerce/маркетплейс, enterprise-SaaS, госкорпорации,
  компании с платными тарифами/подписками, с корпоративными клиентами и известным брендом.
  СРЕДНЕ (4-6): средний бизнес, региональные сети, растущие стартапы с продуктом и оплатой.
  НИЗКО (1-3): малый локальный бизнес, сайты-визитки, самозанятые, НКО, бюджетные учреждения без своих средств.
  Учитывай признаки достатка: enterprise-клиенты/логотипы, филиалы, масштаб, платный продукт, дорогой сегмент.
- payout_reason: 3-6 слов, почему (например "крупный банк, есть бюджет" / "малый локальный бизнес").
- needs: конкретно назови вероятные направления проверки (IDOR, обход авторизации, BOLA/broken auth и т.п.).
  Не выдумывай уязвимости — говори о КЛАССАХ проверок исходя из наблюдаемой поверхности.
- Если поверхности атаки почти нет — ставь pentest_score низким.
Отвечай ТОЛЬКО JSON."""


async def analyze_company(site_data: dict) -> dict:
    """Assess the company as a penetration-testing lead via DeepSeek."""
    detected_stack = site_data.get("detected_stack", {})
    auth_surface = site_data.get("auth_labels", [])
    user_msg = f"""URL: {site_data.get('url', '')}
Заголовок: {site_data.get('title', '')}
Meta: {json.dumps(site_data.get('meta', {}), ensure_ascii=False)}
Технические заголовки: {json.dumps(site_data.get('tech_headers', {}), ensure_ascii=False)}
Обнаруженные технологии: {json.dumps(detected_stack, ensure_ascii=False)}
ПОВЕРХНОСТЬ АТАКИ (детектор): {json.dumps(auth_surface, ensure_ascii=False)}
Email: {', '.join(site_data.get('emails', []))}
Телефоны: {', '.join(site_data.get('phones', []))}

Текст сайта (первые 3000 символов):
{site_data.get('text', '')[:3000]}"""

    try:
        text = await chat(SYSTEM_PROMPT, user_msg, temperature=0.3, max_tokens=800)
        return parse_json(text)
    except ValueError:
        return {"error": "Failed to parse AI response"}
    except Exception as e:
        return {"error": str(e)[:200]}
