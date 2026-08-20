# Pentest Lead Finder

Ищет компании с реальной поверхностью атаки (логин, регистрация, личный кабинет, API, онлайн-оплата),
оценивает их как цели для пентеста и находит их bug bounty программы. Тёмный веб-дашборд + Telegram-бот.

## Возможности

- **Поиск по нишам** (Serper/Google) + генератор идей запросов через AI
- **SUPER SCAN** — AI придумывает 15 ниш и прочёсывает их все
- По каждой компании: поверхность атаки (0–100), тип бэкенда и стек, чувствительность данных,
  bug bounty (HackerOne / Bugcrowd / Standoff365 / BI.ZONE + ссылка на программу),
  платёжеспособность (1–10), контакты (email c MX-проверкой / телефоны / соцсети), ЛПР,
  что проверять на пентесте, готовый питч
- **Веб-дашборд** (тёмный, терминальный): таблица, фильтры, карточки, экспорт в Excel
- **Telegram-бот** с тем же движком

## Стек

Python · FastAPI · SQLite (WAL) · Playwright · DeepSeek API · Serper API · python-telegram-bot

## Локальный запуск

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # заполни ключи
python -m uvicorn webapp:app --host 127.0.0.1 --port 8000   # дашборд → http://localhost:8000
python bot.py                                                # (опционально) telegram-бот
```

## Переменные окружения (.env)

| Переменная | Назначение |
|---|---|
| `DEEPSEEK_API_KEY` | AI-анализ компаний (обязательно) |
| `SERPER_API_KEY` | Поиск через Google, 2500 запросов бесплатно (обязательно для объёма) |
| `TELEGRAM_BOT_TOKEN` | Токен бота (только если нужен Telegram) |
| `RUN_BOT` | `1` — запускать бота вместе с вебом в одном контейнере |
| `DB_PATH` | Путь к SQLite (в Docker: `/data/companies.db`) |

## Деплой на Railway

1. Запушь этот репозиторий на GitHub.
2. Railway → **New Project → Deploy from GitHub repo** → выбери репу. Railway соберёт по `Dockerfile`.
3. **Variables** — добавь `DEEPSEEK_API_KEY`, `SERPER_API_KEY` (и `TELEGRAM_BOT_TOKEN` + `RUN_BOT=1`, если нужен бот).
4. **Volumes** — добавь Volume, mount path `/data` (там будет `companies.db`, данные переживут редеплой).
5. Railway сам даст домен — открывай дашборд по нему.

> SQLite работает, потому что данные лежат на Volume. Веб и бот запускаются в **одном** контейнере
> (`RUN_BOT=1`), чтобы делить один SQLite-файл — на Railway один Volume нельзя примонтировать к двум сервисам.

> ⚠️ Если запустишь бота и на Railway, и локально одновременно — Telegram будет конфликтовать
> (один токен = один поллер). Держи запущенным что-то одно.
