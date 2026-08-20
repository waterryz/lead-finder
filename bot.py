import os
import asyncio
import itertools
import logging
from functools import wraps
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from config import (
    TELEGRAM_BOT_TOKEN, ADMIN_ID, HOTNESS_HOT_THRESHOLD, QUALITY_MIN_SHOW, AUTH_REQUIRED,
    MIN_TARGETS, SEARCH_POOL, SEARCH_POOL_DEEP, SUPER_QUERIES, SUPER_POOL_PER_QUERY,
)
from database import (
    init_db, save_company, get_companies_by_query, get_stats,
    domain_exists, get_company_by_domain, set_crm_status, get_by_crm_status,
    get_dashboard, get_companies_filtered, CRM_STATUSES,
)
from access import (
    init_access_db, get_status, is_approved, create_request,
    set_status, get_user, list_users,
)
from collector import search_google
from scraper import scrape_site, scrape_contacts_page
from backend_detector import detect_backend
from auth_surface import detect_auth_surface
from bb_checker import check_bug_bounty
from stack_detector import detect_stack
from hotness_scorer import score_hotness
from contact_validator import validate_contacts
from decision_maker import find_decision_makers
from pitch_generator import generate_pitch
from idea_generator import generate_ideas
from js_renderer import enrich_with_js
from analyzer import analyze_company
from exporter import export_to_excel
from task_queue import manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

active_tasks: dict[int, bool] = {}

# Generated search ideas, keyed by batch id (so old inline buttons stay valid)
idea_batches: dict[str, list[str]] = {}
last_ideas: dict[int, list[str]] = {}
_batch_counter = itertools.count(1)

# Paginated result viewers: token -> ordered list of domains found in a search
result_views: dict[str, list[str]] = {}
_view_counter = itertools.count(1)


def progress_bar(current: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▱" * width
    filled = min(width, max(0, round(current / total * width)))
    return "▰" * filled + "▱" * (width - filled)

# How many companies to enrich concurrently within one search
SEARCH_CONCURRENCY = 4


async def safe_edit(msg, text):
    """Edit a message, swallowing Telegram 'not modified' / transient 400s."""
    try:
        await msg.edit_text(text)
    except Exception:
        pass


# --- Persistent bottom keyboard ---
BTN_SEARCH = "🔍 Поиск"
BTN_SUPER = "⚡ SUPER SCAN"
BTN_DASH = "📊 Дашборд"
BTN_STATS = "📈 Статистика"
BTN_CRM = "📋 CRM"
BTN_EXPORT = "📥 Экспорт"
BTN_HELP = "❓ Помощь"
BTN_STOP = "⏹ Стоп"

MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_SEARCH), KeyboardButton(BTN_SUPER)],
        [KeyboardButton(BTN_DASH), KeyboardButton(BTN_STATS)],
        [KeyboardButton(BTN_EXPORT), KeyboardButton(BTN_HELP)],
        [KeyboardButton(BTN_STOP)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Введи запрос или нажми кнопку…",
)


def export_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 С авторизацией", callback_data="export:auth"),
         InlineKeyboardButton("🏴 С bug bounty", callback_data="export:bb")],
        [InlineKeyboardButton("🎯 Без BB (для оффера)", callback_data="export:nobb"),
         InlineKeyboardButton("💰 Платёжеспособные", callback_data="export:rich")],
        [InlineKeyboardButton("🔥 Горячие", callback_data="export:hot"),
         InlineKeyboardButton("📧 С email", callback_data="export:email")],
        [InlineKeyboardButton("📦 Всё", callback_data="export:all")],
    ])


def require_access(func):
    """Decorator: only approved users may run the wrapped handler."""
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        status = get_status(user.id)
        if status == "approved":
            return await func(update, ctx, *args, **kwargs)
        if status == "pending":
            await update.message.reply_text("⏳ Твоя заявка на рассмотрении. Дождись одобрения.")
        elif status == "rejected":
            await update.message.reply_text("🚫 Доступ отклонён.")
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📝 Подать заявку", callback_data="register")
            ]])
            await update.message.reply_text(
                "🔒 Доступ к боту только по заявке.\nНажми кнопку, чтобы подать заявку.",
                reply_markup=kb,
            )
    return wrapper


HELP_TEXT = (
    "Привет! Я ищу компании как *цели для пентеста* — с реальной поверхностью атаки "
    "(логин, регистрация, личный кабинет, API, оплата).\n\n"
    "Отправь запрос, например:\n"
    "• `интернет-магазин Москва`\n"
    "• `онлайн-банк`\n"
    "• `образовательная платформа`\n\n"
    "Команды:\n"
    "/search <запрос> — поиск + анализ (20)\n"
    "/search50 <запрос> — до 50 компаний\n"
    "/superscan — ⚡ AI придумает 15 ниш и прочешет их все, в конце — экспорт по фильтрам\n"
    "/stats — статистика базы\n"
    "/dashboard — сводка (авторизация, бэкенд, воронка)\n"
    "/crm <статус> — лиды по статусу (new/contacted/replied/rejected/client)\n"
    "/export <запрос> [флаги] — Excel-выгрузка\n"
    "   флаги: `--auth --bb --nobb --hot --email --score=7 --crm=client`\n"
    "/stop — остановить парсинг\n\n"
    "🏴 Для целей с поверхностью атаки проверяю, есть ли у них своя bug bounty / VDP "
    "(security.txt, HackerOne, Bugcrowd, Standoff365, BI.ZONE…). `--bb` = есть программа "
    "(можно хантить), `--nobb` = нет (можно предлагать пентест).\n"
    "Под каждой карточкой — CRM-кнопки и «✍️ Питч»."
)

ADMIN_HELP = (
    "\n\n👑 Админ:\n"
    "/users, /pending, /approve <id>, /reject <id>, /jobs"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    status = get_status(user.id)

    if status == "approved":
        text = HELP_TEXT + (ADMIN_HELP if user.id == ADMIN_ID else "")
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KB)
    elif status == "pending":
        await update.message.reply_text("⏳ Твоя заявка на рассмотрении. Дождись одобрения.")
    elif status == "rejected":
        await update.message.reply_text("🚫 Доступ отклонён.")
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 Подать заявку", callback_data="register")
        ]])
        await update.message.reply_text(
            "🔒 Привет! Я парсер компаний, но доступ только по заявке.\n\n"
            "Нажми кнопку ниже, чтобы отправить заявку администратору.",
            reply_markup=kb,
        )


# ---------------- Registration / access callbacks ----------------

async def send_access_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    full_name = user.full_name or "—"
    username = f"@{user.username}" if user.username else "—"

    status = create_request(user.id, user.username or "", full_name)
    if status == "approved":
        await ctx.bot.send_message(user.id, "✅ У тебя уже есть доступ. Отправь запрос для поиска.")
        return
    if status == "rejected":
        await ctx.bot.send_message(user.id, "🚫 Твоя заявка была отклонена ранее.")
        return

    await ctx.bot.send_message(
        user.id, "✅ Заявка отправлена администратору. Ожидай одобрения.",
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{user.id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{user.id}"),
    ]])
    await ctx.bot.send_message(
        ADMIN_ID,
        f"🔔 *Новая заявка на доступ*\n\n"
        f"👤 Имя: {full_name}\n🔗 Username: {username}\n🆔 ID: `{user.id}`",
        parse_mode="Markdown", reply_markup=kb,
    )


CRM_LABELS = {
    "contacted": "✉️ Написал", "replied": "💬 Ответили",
    "rejected": "❌ Отказ", "client": "🤝 Клиент",
}


def crm_keyboard(domain: str) -> InlineKeyboardMarkup | None:
    """CRM + pitch buttons. Returns None if domain too long for Telegram callback (64 bytes)."""
    longest = f"crm:rejected:{domain}"
    if len(longest.encode("utf-8")) > 64:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Питч", callback_data=f"pitch:{domain}")],
        [
            InlineKeyboardButton("✉️ Написал", callback_data=f"crm:contacted:{domain}"),
            InlineKeyboardButton("💬 Ответили", callback_data=f"crm:replied:{domain}"),
        ],
        [
            InlineKeyboardButton("❌ Отказ", callback_data=f"crm:rejected:{domain}"),
            InlineKeyboardButton("🤝 Клиент", callback_data=f"crm:client:{domain}"),
        ],
    ])


def results_keyboard(token: str, idx: int, total: int, domain: str) -> InlineKeyboardMarkup:
    """Nav (◀ n/N ▶) + pitch button for the paginated result viewer."""
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"nav:{token}:{idx - 1}"))
    nav.append(InlineKeyboardButton(f"{idx + 1}/{total}", callback_data="noop"))
    if idx < total - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"nav:{token}:{idx + 1}"))
    rows = [nav]
    if len(f"pitch:{domain}".encode("utf-8")) <= 64:
        rows.append([InlineKeyboardButton("✍️ Питч (готовое письмо)", callback_data=f"pitch:{domain}")])
    return InlineKeyboardMarkup(rows)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""

    # --- registration (anyone) ---
    if data == "register":
        await query.answer()
        await send_access_request(update, ctx)
        return

    # --- admin approve/reject ---
    if data.startswith("approve:") or data.startswith("reject:"):
        if query.from_user.id != ADMIN_ID:
            await query.answer("Только для администратора", show_alert=True)
            return
        await query.answer()
        action, uid_str = data.split(":", 1)
        uid = int(uid_str)
        user = get_user(uid)
        name = user["full_name"] if user else uid
        if action == "approve":
            set_status(uid, "approved")
            await query.edit_message_text(f"✅ Одобрено: {name} (`{uid}`)", parse_mode="Markdown")
            try:
                await ctx.bot.send_message(uid, "🎉 Твоя заявка одобрена! Отправь поисковый запрос.")
            except Exception:
                pass
        else:
            set_status(uid, "rejected")
            await query.edit_message_text(f"❌ Отклонено: {name} (`{uid}`)", parse_mode="Markdown")
            try:
                await ctx.bot.send_message(uid, "🚫 Твоя заявка отклонена.")
            except Exception:
                pass
        return

    # --- CRM / pitch / idea (approved users only) ---
    if not is_approved(query.from_user.id):
        await query.answer("Нет доступа", show_alert=True)
        return

    if data == "noop":
        await query.answer()
        return

    if data.startswith("nav:"):
        await query.answer()
        try:
            _, token, idx = data.split(":", 2)
            domains = result_views.get(token, [])
            idx = max(0, min(int(idx), len(domains) - 1))
            domain = domains[idx]
        except (ValueError, IndexError):
            await ctx.bot.send_message(query.message.chat_id, "Список результатов устарел — запусти поиск заново.")
            return
        company = get_company_by_domain(domain)
        if not company:
            await query.answer("Компания не найдена", show_alert=True)
            return
        card = build_card(company, company.get("backend_type", ""))
        try:
            await query.edit_message_text(
                card, reply_markup=results_keyboard(token, idx, len(domains), domain),
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        return

    if data == "gen_idea":
        await query.answer("Генерирую идеи…")
        uid = query.from_user.id
        ideas = await generate_ideas(6, avoid=last_ideas.get(uid, []))
        if not ideas:
            await ctx.bot.send_message(query.message.chat_id, "Не удалось сгенерировать идеи, попробуй ещё раз.")
            return
        last_ideas[uid] = ideas
        bid = str(next(_batch_counter))
        idea_batches[bid] = ideas
        rows = [[InlineKeyboardButton(f"🔍 {idea}", callback_data=f"si:{bid}:{i}")]
                for i, idea in enumerate(ideas)]
        rows.append([InlineKeyboardButton("🔄 Ещё идеи", callback_data="gen_idea")])
        await ctx.bot.send_message(
            query.message.chat_id,
            "💡 Идеи для поиска (нажми — сразу запущу):",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data.startswith("si:"):
        await query.answer()
        try:
            _, bid, idx = data.split(":", 2)
            ideas = idea_batches.get(bid, [])
            q = ideas[int(idx)]
        except (ValueError, IndexError):
            await ctx.bot.send_message(query.message.chat_id, "Эти идеи устарели — сгенерируй заново.")
            return
        await start_search_job(ctx.bot, query.message.chat_id, query.from_user.id,
                               q, SEARCH_POOL, MIN_TARGETS)
        return

    if data.startswith("crm:"):
        _, status, domain = data.split(":", 2)
        set_crm_status(domain, status)
        await query.answer(f"Статус: {CRM_LABELS.get(status, status)}")
        return

    if data.startswith("pitch:"):
        domain = data.split(":", 1)[1]
        company = get_company_by_domain(domain)
        await query.answer()
        pitch = (company or {}).get("pitch", "")
        if pitch:
            await ctx.bot.send_message(
                query.message.chat_id,
                f"✍️ *Питч для {domain}:*\n\n{pitch}",
                parse_mode="Markdown",
            )
        else:
            await ctx.bot.send_message(query.message.chat_id, "Питч для этой компании не сгенерирован.")
        return

    if data.startswith("crmview:"):
        status = data.split(":", 1)[1]
        await query.answer()
        rows = get_by_crm_status(status)
        if not rows:
            await ctx.bot.send_message(query.message.chat_id, f"Нет лидов «{status}».")
            return
        lines = [f"📋 Лиды «{status}» ({len(rows)}):"]
        for r in rows[:30]:
            lines.append(f"• {r.get('name') or r['domain']} — {r['domain']}")
        await ctx.bot.send_message(
            query.message.chat_id, "\n".join(lines), disable_web_page_preview=True
        )
        return

    if data.startswith("export:"):
        preset = data.split(":", 1)[1]
        await query.answer("Готовлю файл…")
        kwargs = {}
        if preset == "auth":
            kwargs["require_auth"] = True
        elif preset == "bb":
            kwargs["require_bb"] = True
        elif preset == "nobb":
            kwargs["require_auth"] = True
            kwargs["exclude_bb"] = True
        elif preset == "rich":
            kwargs["min_payout"] = 7
        elif preset == "hot":
            kwargs["min_hotness"] = HOTNESS_HOT_THRESHOLD
        elif preset == "email":
            kwargs["has_email"] = True
        elif preset == "tg":
            kwargs["has_telegram"] = True
        comps = get_companies_filtered(**kwargs)
        if not comps:
            await ctx.bot.send_message(query.message.chat_id, "Под фильтр ничего не подошло.")
            return
        filepath = f"export_{preset}.xlsx"
        export_to_excel(comps, filepath)
        with open(filepath, "rb") as f:
            await ctx.bot.send_document(
                query.message.chat_id, document=f, filename=filepath,
                caption=f"Выгружено {len(comps)} компаний.",
            )
        os.remove(filepath)
        return

    await query.answer()


# ---------------- Core pipeline ----------------

def _dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_card(company: dict, backend_label: str) -> str:
    contacts = company.get("contacts", {})
    channels = company.get("channels", {})
    emails = ", ".join(contacts.get("emails", [])[:3]) or "—"
    phones = ", ".join(contacts.get("phones", [])[:2]) or "—"
    tg = ", ".join(channels.get("telegram", [])[:2]) or "—"
    stack_str = ", ".join(company.get("stack", [])[:6]) or "—"

    dms = company.get("decision_makers", [])
    if dms:
        d0 = dms[0]
        dm_str = d0.get("name", "")
        if d0.get("role"):
            dm_str += f" ({d0['role']})"
        if d0.get("contact"):
            dm_str += f" — {d0['contact']}"
    else:
        dm_str = "—"

    surface = company.get("attack_surface", [])
    surface_str = ", ".join(surface) if surface else "—"
    sens = {"high": "🔴 высокая", "medium": "🟡 средняя", "low": "🟢 низкая"}.get(
        company.get("data_sensitivity", ""), company.get("data_sensitivity", "?")
    )
    hot = company.get("hotness_score", 0)

    pay = int(company.get("payout_score", 0) or 0)
    if pay >= 7:
        pay_label = "🟢 высокая"
    elif pay >= 4:
        pay_label = "🟡 средняя"
    elif pay >= 1:
        pay_label = "🔴 низкая"
    else:
        pay_label = "—"
    pay_reason = company.get("payout_reason", "")
    pay_line = f"💰 Платёжеспособность: {pay_label} ({pay}/10)"
    if pay_reason:
        pay_line += f" · {pay_reason}"

    if company.get("has_bb"):
        plats = ", ".join(company.get("bb_platforms", [])) or "есть"
        bb_line = f"🏴 Bug bounty: ЕСТЬ ({plats}) — можно хантить\n"
        bb_url = company.get("bb_url", "")
        if bb_url:
            bb_line += f"   ↳ {bb_url}\n"
    else:
        bb_line = "🏴 Bug bounty: нет — только пентест-оффер\n"

    return (
        f"🏢 {company.get('name', company['domain'])}\n"
        f"🌐 {company['domain']}\n"
        f"📝 {company.get('description', '—')}\n"
        f"🔐 Поверхность атаки: {surface_str} ({company.get('surface_score', 0)}/100)\n"
        f"{bb_line}"
        f"🗄 Чувствительность данных: {sens}\n"
        f"{pay_line}\n"
        f"⚙️ Стек: {stack_str}\n"
        f"🔧 Бэкенд: {backend_label}\n"
        f"📏 Размер: {company.get('size', '?')}\n"
        f"👤 ЛПР: {dm_str}\n"
        f"📧 Email: {emails}\n"
        f"📞 Тел: {phones}\n"
        f"✈️ Telegram: {tg}\n"
        f"🎯 Что проверить: {company.get('needs', '—')}\n"
        f"⭐ Оценка пентест-лида: {company.get('quality_score', 0)}/10  |  🔥 {hot}/100"
    )


async def enrich_one(query: str, item: dict) -> dict | None:
    """Full enrichment for one company URL. Returns saved company dict or None if skipped."""
    backend = await detect_backend(item["url"])
    if not backend["has_backend"]:
        return {"_skip": "landing"}

    site_data = await scrape_site(item["url"])
    if site_data.get("error"):
        return {"_skip": "error"}

    site_data = await enrich_with_js(site_data)

    # --- Pentest focus: gate on real auth/attack surface ---
    auth = await detect_auth_surface(item["url"], site_data)
    if AUTH_REQUIRED and not auth["has_auth"]:
        return {"_skip": "no_auth"}
    site_data["auth_labels"] = auth["labels"]

    # Independent steps run concurrently (incl. bug-bounty check for passed sites)
    stack_info, hot, contacts_extra, bb = await asyncio.gather(
        detect_stack(site_data),
        score_hotness(site_data),
        scrape_contacts_page(item["url"]),
        check_bug_bounty(item["url"], site_data),
    )

    emails = _dedup(site_data.get("emails", []) + contacts_extra.get("emails", []))
    phones = _dedup(site_data.get("phones", []) + contacts_extra.get("phones", []))
    socials = dict(site_data.get("socials", {}))
    for k, v in contacts_extra.get("socials", {}).items():
        socials[k] = _dedup(socials.get(k, []) + v)

    site_data["detected_stack"] = stack_info.get("flat", [])
    # Contact validation, decision-maker search and AI analysis in parallel
    validated, dms, ai = await asyncio.gather(
        validate_contacts(emails, phones),
        find_decision_makers(site_data, contacts_extra.get("about_text", "")),
        analyze_company(site_data),
    )
    if "error" in ai:
        return {"_skip": "ai_error"}

    backend_label = stack_info.get("backend_hint") or backend.get("backend_type") or "unknown"

    valid_emails = [e["value"] for e in validated.get("emails", [])] or emails
    valid_phones = [p["e164"] or p["raw"] for p in validated.get("phones", [])] or phones

    company = {
        "query": query,
        "domain": item["domain"],
        "name": ai.get("name", item.get("name", item["domain"])),
        "url": item["url"],
        "description": ai.get("description", ""),
        "stack": _dedup(list(ai.get("stack", [])) + stack_info.get("flat", [])),
        "size": ai.get("size", ""),
        "contacts": {
            "emails": valid_emails,
            "phones": valid_phones,
            "primary_email": validated.get("primary_email", ""),
            "primary_phone": validated.get("primary_phone", ""),
        },
        "channels": socials,
        "needs": ai.get("needs", ""),
        "backend_type": backend_label,
        "backend_score": backend.get("score", 0),
        "hotness_score": hot.get("score", 0),
        "hotness_reasons": hot.get("reasons", []),
        "quality_score": ai.get("pentest_score", ai.get("quality_score", 0)),
        "decision_makers": dms,
        "attack_surface": auth.get("labels", []),
        "surface_score": auth.get("score", 0),
        "data_sensitivity": ai.get("data_sensitivity", ""),
        "has_bb": bb.get("has_bb", False),
        "bb_platforms": bb.get("platforms", []),
        "bb_url": bb.get("policy_url", ""),
        "payout_score": ai.get("payout_score", 0),
        "payout_reason": ai.get("payout_reason", ""),
        "ai_summary": f"surface={auth.get('score',0)}; backend={backend_label}; hot={hot.get('score',0)}; bb={bb.get('has_bb')}",
        "raw_html_length": len(site_data.get("text", "")),
        "status": "done",
    }

    company["pitch"] = await generate_pitch(company)
    company["_saved"] = save_company(company)
    company["_backend_label"] = backend_label
    company["_is_hot"] = hot.get("is_hot", False)
    return company


async def process_search(bot, chat_id: int, uid: int, query: str, max_results: int, msg,
                         stop_at: int | None = None):
    active_tasks[uid] = True
    try:
        await safe_edit(msg, f"🔍 Ищу компании по запросу: «{query}»...")
        urls = await search_google(query, num_results=max_results)
        if not urls:
            await safe_edit(msg, "Ничего не нашёл. Попробуй другой запрос.")
            return

        # Skip domains already in the base up front
        pending = []
        dup = 0
        for item in urls:
            if domain_exists(item["domain"]):
                dup += 1
            else:
                pending.append(item)

        await safe_edit(msg, f"Найдено {len(urls)} сайтов ({dup} уже в базе). Анализирую...")

        total = len(pending)
        done = good = landing = no_auth = errors = 0
        found_domains: list[str] = []

        def render_progress():
            if stop_at:
                bar = progress_bar(good, stop_at)
                goal = f"🎯 Целей: {good}/{stop_at}"
            else:
                bar = progress_bar(done, total)
                goal = f"🎯 Найдено целей: {good}"
            return (
                f"🔍 «{query}»\n{goal}\n{bar}\n"
                f"⚙️ Обработано {done}/{total} · без авторизации {no_auth} · "
                f"лендинги {landing} · в базе {dup}"
            )

        for i in range(0, total, SEARCH_CONCURRENCY):
            if not active_tasks.get(uid, False):
                await safe_edit(msg, f"⏹ Остановлено. Обработано {done}/{total}, целей: {good}")
                break

            batch = pending[i:i + SEARCH_CONCURRENCY]
            results = await asyncio.gather(
                *[enrich_one(query, it) for it in batch], return_exceptions=True
            )

            for item, result in zip(batch, results):
                done += 1
                if isinstance(result, Exception) or not result:
                    errors += 1
                    if isinstance(result, Exception):
                        logger.error(f"enrich error {item['url']}: {result}")
                    continue
                skip = result.get("_skip")
                if skip == "landing":
                    landing += 1
                elif skip == "no_auth":
                    no_auth += 1
                elif skip:
                    errors += 1
                else:
                    show = (
                        result["quality_score"] >= QUALITY_MIN_SHOW
                        or result.get("surface_score", 0) >= 40
                        or result.get("_is_hot")
                    )
                    if show:
                        good += 1
                        found_domains.append(item["domain"])

            await safe_edit(msg, render_progress())

            if stop_at and good >= stop_at:
                break

        # --- Paginated result viewer ---
        if found_domains:
            token = str(next(_view_counter))
            result_views[token] = found_domains
            first = get_company_by_domain(found_domains[0])
            await safe_edit(msg, f"✅ Найдено целей: {len(found_domains)} по «{query}» 👇")
            if first:
                await bot.send_message(
                    chat_id, build_card(first, first.get("backend_type", "")),
                    reply_markup=results_keyboard(token, 0, len(found_domains), found_domains[0]),
                    disable_web_page_preview=True,
                )
        else:
            note = f"🔎 По «{query}» целей не нашёл."
            if stop_at:
                note += "\n💡 Уточни нишу/город или запусти /search50 (глубокий поиск)."
            await safe_edit(msg, note)

        # --- Excel export ---
        companies = get_companies_by_query(query)
        if companies:
            caption = (
                f"📊 Экспорт по «{query}»\n"
                f"🎯 Целей: {good} · без авторизации: {no_auth} · "
                f"лендинги: {landing} · в базе: {dup} · ошибки: {errors}"
            )
            filepath = f"export_{query[:30].replace(' ', '_')}.xlsx"
            export_to_excel(companies, filepath)
            with open(filepath, "rb") as f:
                await bot.send_document(chat_id, document=f, filename=filepath,
                                        caption=caption, reply_markup=MAIN_KB)
            os.remove(filepath)
    finally:
        active_tasks.pop(uid, None)


async def start_search_job(bot, chat_id: int, uid: int, query: str,
                           pool: int, stop_at: int | None):
    """Kick off a background search job (usable from commands, text, or callbacks)."""
    # NOTE: no reply_markup here — a message carrying a ReplyKeyboardMarkup can't be
    # edited (editMessageText 400), which would break the live progress bar.
    msg = await bot.send_message(chat_id, f"🔎 «{query}» — старт…")
    manager.submit(
        str(uid),
        lambda: process_search(bot, chat_id, uid, query, pool, msg, stop_at=stop_at),
        meta={"query": query, "uid": uid},
    )


async def launch_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                        pool: int, stop_at: int | None):
    uid = update.effective_user.id
    query = " ".join(ctx.args) if ctx.args else ""
    if not query:
        await update.message.reply_text("Укажи запрос: `/search автосервис Москва`", parse_mode="Markdown")
        return
    await start_search_job(ctx.bot, update.effective_chat.id, uid, query, pool, stop_at)


async def super_scan(bot, chat_id: int, uid: int, msg):
    """AI generates SUPER_QUERIES niches, scans them all, then offers filtered export."""
    active_tasks[uid] = True
    try:
        await safe_edit(msg, f"⚡ SUPER SCAN\n🧠 Генерирую {SUPER_QUERIES} ниш…")
        ideas = await generate_ideas(SUPER_QUERIES)
        if not ideas:
            await safe_edit(msg, "Не удалось сгенерировать идеи. Попробуй ещё раз.")
            return

        n = len(ideas)
        total_good = total_done = total_landing = total_noauth = total_dup = 0

        def render(qi, idea):
            return (
                f"⚡ SUPER SCAN — ниша {qi}/{n}\n"
                f"🔍 «{idea}»\n{progress_bar(qi, n)}\n"
                f"🎯 Целей всего: {total_good} · обработано: {total_done} · "
                f"без авторизации: {total_noauth} · лендинги: {total_landing} · в базе: {total_dup}"
            )

        for qi, idea in enumerate(ideas, 1):
            if not active_tasks.get(uid, False):
                await safe_edit(msg, f"⏹ Остановлено. Ниш: {qi - 1}/{n}, целей: {total_good}")
                break

            urls = await search_google(idea, num_results=SUPER_POOL_PER_QUERY)
            pending = []
            for it in urls:
                if domain_exists(it["domain"]):
                    total_dup += 1
                else:
                    pending.append(it)

            for i in range(0, len(pending), SEARCH_CONCURRENCY):
                if not active_tasks.get(uid, False):
                    break
                batch = pending[i:i + SEARCH_CONCURRENCY]
                results = await asyncio.gather(
                    *[enrich_one(idea, it) for it in batch], return_exceptions=True
                )
                for item, result in zip(batch, results):
                    total_done += 1
                    if isinstance(result, Exception) or not result:
                        continue
                    skip = result.get("_skip")
                    if skip == "landing":
                        total_landing += 1
                    elif skip == "no_auth":
                        total_noauth += 1
                    elif not skip and (
                        result["quality_score"] >= QUALITY_MIN_SHOW
                        or result.get("surface_score", 0) >= 40
                        or result.get("_is_hot")
                    ):
                        total_good += 1
                await safe_edit(msg, render(qi, idea))
            await safe_edit(msg, render(qi, idea))

        await safe_edit(
            msg,
            f"✅ SUPER SCAN завершён\n🎯 Пентест-целей: {total_good} · обработано: {total_done}",
        )
        await bot.send_message(
            chat_id,
            f"📥 Найдено целей: {total_good}. Выгрузить по фильтру:",
            reply_markup=export_menu_kb(),
        )
    finally:
        active_tasks.pop(uid, None)


async def launch_super_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("⚡ SUPER SCAN запускаю…")
    manager.submit(
        str(uid),
        lambda: super_scan(ctx.bot, chat_id, uid, msg),
        meta={"query": "SUPER SCAN", "uid": uid},
    )


@require_access
async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # keep digging until at least MIN_TARGETS pentest targets are found
    await launch_search(update, ctx, SEARCH_POOL, stop_at=MIN_TARGETS)


@require_access
async def cmd_search50(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # process the whole (larger) pool, no early stop
    await launch_search(update, ctx, SEARCH_POOL_DEEP, stop_at=None)


@require_access
async def cmd_superscan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await launch_super_scan(update, ctx)


@require_access
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == BTN_SEARCH:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💡 Сгенерировать идею", callback_data="gen_idea")
        ]])
        await update.message.reply_text(
            "🔍 Введи поисковый запрос, например: `интернет-магазин Москва`\n\n"
            "…или нажми кнопку, и я подскажу ниши с хорошей поверхностью атаки 👇",
            parse_mode="Markdown", reply_markup=kb,
        )
        return
    if text == BTN_DASH:
        return await cmd_dashboard(update, ctx)
    if text == BTN_STATS:
        return await cmd_stats(update, ctx)
    if text == BTN_HELP:
        return await cmd_start(update, ctx)
    if text == BTN_STOP:
        return await cmd_stop(update, ctx)
    if text == BTN_SUPER:
        await launch_super_scan(update, ctx)
        return
    if text == BTN_EXPORT:
        await update.message.reply_text("Что выгрузить?", reply_markup=export_menu_kb())
        return

    # Anything else = search query
    ctx.args = text.split()
    await launch_search(update, ctx, SEARCH_POOL, stop_at=MIN_TARGETS)


@require_access
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if active_tasks.get(uid):
        active_tasks[uid] = False
        await update.message.reply_text("⏹ Останавливаю парсинг...")
    else:
        await update.message.reply_text("Нет активного парсинга.")


# ---------------- Stats / dashboard / CRM ----------------

@require_access
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_stats()
    await update.message.reply_text(
        f"📊 Всего компаний: {s['total']}\nОбработано: {s['done']}\n"
        f"Уникальных запросов: {s['queries']}"
    )


@require_access
async def cmd_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = get_dashboard()
    lines = [
        "📈 *Дашборд*",
        f"Всего: {d['total']} | 🔐 С авторизацией: {d.get('with_auth', 0)} | "
        f"🏴 С bug bounty: {d.get('with_bb', 0)} | 🔥 Горячих: {d['hot']}",
        f"📧 С email: {d['with_email']} | ✈️ С Telegram: {d['with_telegram']}",
        "",
        "*По бэкенду:*",
    ]
    for bt, c in d["by_backend"][:10]:
        lines.append(f"  • {bt}: {c}")
    lines.append("\n*Воронка (CRM):*")
    for cs, c in d["by_crm"]:
        lines.append(f"  • {cs}: {c}")
    lines.append("\n*Топ запросов:*")
    for q, c in d["by_query"][:8]:
        lines.append(f"  • {q}: {c}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@require_access
async def cmd_crm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    status = ctx.args[0].lower() if ctx.args else ""
    if status not in CRM_STATUSES:
        await update.message.reply_text(
            "Использование: `/crm <статус>`\n"
            f"Статусы: {', '.join(CRM_STATUSES)}",
            parse_mode="Markdown",
        )
        return
    rows = get_by_crm_status(status)
    if not rows:
        await update.message.reply_text(f"Нет лидов со статусом «{status}».")
        return
    lines = [f"📋 Лиды «{status}» ({len(rows)}):"]
    for r in rows[:30]:
        lines.append(f"• {r.get('name') or r['domain']} — {r['domain']}")
    await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)


@require_access
async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    min_quality = min_hotness = min_payout = 0
    has_email = has_tg = require_auth = require_bb = exclude_bb = False
    crm_status = None
    qparts = []
    for a in args:
        al = a.lower()
        if al == "--auth":
            require_auth = True
        elif al == "--bb":
            require_bb = True
        elif al == "--nobb":
            exclude_bb = True
        elif al == "--rich":
            min_payout = 7
        elif al == "--hot":
            min_hotness = HOTNESS_HOT_THRESHOLD
        elif al in ("--email", "--mail"):
            has_email = True
        elif al in ("--tg", "--telegram"):
            has_tg = True
        elif al.startswith("--score="):
            try:
                min_quality = int(al.split("=", 1)[1])
            except ValueError:
                pass
        elif al.startswith("--crm="):
            crm_status = al.split("=", 1)[1]
        elif al.startswith("--"):
            pass
        else:
            qparts.append(a)
    query = " ".join(qparts) or None

    companies = get_companies_filtered(
        query=query, min_quality=min_quality, min_hotness=min_hotness,
        has_email=has_email, has_telegram=has_tg, crm_status=crm_status,
        require_auth=require_auth, require_bb=require_bb, exclude_bb=exclude_bb,
        min_payout=min_payout,
    )
    if not companies:
        await update.message.reply_text("Под фильтры ничего не подошло.")
        return

    label = (query or "all").replace(" ", "_")[:30]
    filepath = f"export_{label}.xlsx"
    export_to_excel(companies, filepath)
    with open(filepath, "rb") as f:
        await update.message.reply_document(
            document=f, filename=filepath,
            caption=f"Выгружено {len(companies)} компаний.",
        )
    os.remove(filepath)


# ---------------- Admin ----------------

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *a, **kw):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("🚫 Команда только для администратора.")
            return
        return await func(update, ctx, *a, **kw)
    return wrapper


@admin_only
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    users = list_users()
    if not users:
        await update.message.reply_text("Пользователей нет.")
        return
    lines = ["👥 *Пользователи:*"]
    icons = {"approved": "✅", "pending": "⏳", "rejected": "🚫"}
    for u in users:
        icon = icons.get(u["status"], "❓")
        uname = f"@{u['username']}" if u["username"] else "—"
        lines.append(f"{icon} {u['full_name']} ({uname}) `{u['user_id']}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    users = list_users("pending")
    if not users:
        await update.message.reply_text("Заявок на рассмотрении нет.")
        return
    for u in users:
        uname = f"@{u['username']}" if u["username"] else "—"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{u['user_id']}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{u['user_id']}"),
        ]])
        await update.message.reply_text(
            f"⏳ {u['full_name']} ({uname})\n🆔 `{u['user_id']}`",
            parse_mode="Markdown", reply_markup=kb,
        )


@admin_only
async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Использование: `/approve <user_id>`", parse_mode="Markdown")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Неверный ID.")
        return
    set_status(uid, "approved")
    await update.message.reply_text(f"✅ Пользователь `{uid}` одобрен.", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(uid, "🎉 Твоя заявка одобрена! Отправь поисковый запрос.")
    except Exception:
        pass


@admin_only
async def cmd_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Использование: `/reject <user_id>`", parse_mode="Markdown")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Неверный ID.")
        return
    set_status(uid, "rejected")
    await update.message.reply_text(f"🚫 Пользователь `{uid}` отклонён.", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(uid, "🚫 Твоя заявка отклонена.")
    except Exception:
        pass


@admin_only
async def cmd_jobs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = manager.stats()
    active = manager.list_active()
    lines = [f"⚙️ Задачи: {st}"]
    for j in active:
        lines.append(f"  • #{j['id']} {j.get('meta', {}).get('query', '')} [{j['status']}]")
    await update.message.reply_text("\n".join(lines))


def main():
    init_db()
    init_access_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("crm", cmd_crm))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("search50", cmd_search50))
    app.add_handler(CommandHandler("superscan", cmd_superscan))
    # admin
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    # callbacks
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
