# -*- coding: utf-8 -*-
"""
contact_validator.py

Валидация контактов (email + телефоны), собранных парсером.

Публичный интерфейс:
    async def validate_contacts(emails: list[str], phones: list[str]) -> dict

Функция НИКОГДА не бросает исключение наружу: любая внутренняя ошибка
логируется в результат как невалидный контакт, а фатальная ошибка приводит
к возврату безопасного значения по умолчанию.
"""

from __future__ import annotations

import re

# Роль-адреса (generic mailboxes) — не считаются персональными контактами.
ROLE_LOCALPARTS = {
    "info", "admin", "support", "office", "sales", "mail", "hello", "hi",
    "contact", "noreply", "no-reply", "mailer", "webmaster", "help", "order",
    "zakaz", "shop", "reception",
}

# Достаточно строгий, но практичный синтаксический чек email.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+$"
)

_SAFE_DEFAULT = {
    "emails": [],
    "phones": [],
    "primary_email": "",
    "primary_phone": "",
}


async def _lookup_mx(domain: str, cache: dict) -> bool:
    """
    Асинхронная проверка наличия MX-записи у домена.

    Результат кэшируется в переданном dict в рамках одного вызова
    validate_contacts. При любой ошибке DNS -> False.
    """
    if not domain:
        return False
    key = domain.lower()
    if key in cache:
        return cache[key]

    result = False
    try:
        import dns.asyncresolver  # локальный импорт — если dnspython нет, mx=False

        resolver = dns.asyncresolver.Resolver()
        # Короткий общий таймаут на весь lookup.
        resolver.lifetime = 5
        try:
            resolver.timeout = 5
        except Exception:
            pass

        answers = await resolver.resolve(key, "MX")
        # Наличие хотя бы одной записи считаем успехом.
        result = bool(answers) and len(answers) > 0
    except Exception:
        # NXDOMAIN, NoAnswer, Timeout, отсутствие пакета, что угодно — mx=False.
        result = False

    cache[key] = result
    return result


def _validate_one_phone(raw: str, phonenumbers) -> dict:
    """Разбор и валидация одного телефона. Никогда не бросает."""
    entry = {"raw": raw, "e164": "", "national": "", "valid": False}
    try:
        num = phonenumbers.parse(raw, "RU")
    except Exception:
        return entry

    try:
        entry["valid"] = bool(phonenumbers.is_valid_number(num))
    except Exception:
        entry["valid"] = False

    try:
        entry["e164"] = phonenumbers.format_number(
            num, phonenumbers.PhoneNumberFormat.E164
        )
    except Exception:
        entry["e164"] = ""

    try:
        entry["national"] = phonenumbers.format_number(
            num, phonenumbers.PhoneNumberFormat.NATIONAL
        )
    except Exception:
        entry["national"] = ""

    return entry


async def validate_contacts(emails: list[str], phones: list[str]) -> dict:
    """
    Валидирует списки email и телефонов.

    Возвращает:
    {
      "emails": [{"value":str, "valid_syntax":bool, "mx":bool, "role":bool}],
      "phones": [{"raw":str, "e164":str, "national":str, "valid":bool}],
      "primary_email": str,
      "primary_phone": str
    }

    При фатальной ошибке возвращает безопасный дефолт со всеми пустыми полями.
    """
    try:
        emails = emails or []
        phones = phones or []

        # ---------- EMAILS ----------
        email_results: list[dict] = []
        mx_cache: dict[str, bool] = {}
        seen_emails: set[str] = set()

        for raw in emails:
            try:
                if not isinstance(raw, str):
                    continue
                value = raw.strip()
                if not value:
                    continue

                low = value.lower()
                if low in seen_emails:
                    continue
                seen_emails.add(low)

                valid_syntax = bool(_EMAIL_RE.match(value))

                local = ""
                domain = ""
                if "@" in value:
                    local, _, domain = value.rpartition("@")

                role = local.lower() in ROLE_LOCALPARTS if local else False

                mx = False
                if valid_syntax and domain:
                    mx = await _lookup_mx(domain, mx_cache)

                email_results.append({
                    "value": value,
                    "valid_syntax": valid_syntax,
                    "mx": mx,
                    "role": role,
                })
            except Exception:
                # Один битый адрес не должен рушить всю проверку.
                continue

        # ---------- PHONES ----------
        phone_results: list[dict] = []
        seen_e164: set[str] = set()
        try:
            import phonenumbers
        except Exception:
            phonenumbers = None

        for raw in phones:
            try:
                if not isinstance(raw, str):
                    continue
                value = raw.strip()
                if not value:
                    continue

                if phonenumbers is None:
                    entry = {"raw": value, "e164": "", "national": "", "valid": False}
                else:
                    entry = _validate_one_phone(value, phonenumbers)

                e164 = entry.get("e164", "")
                # Дедуп по e164 (включая пустой для непарсящихся номеров).
                if e164 in seen_e164:
                    continue
                seen_e164.add(e164)

                phone_results.append(entry)
            except Exception:
                continue

        # ---------- PRIMARY EMAIL ----------
        primary_email = ""
        for e in email_results:
            if not e.get("role", False) and e.get("mx", False):
                primary_email = e["value"]
                break
        if not primary_email:
            for e in email_results:
                if e.get("valid_syntax", False):
                    primary_email = e["value"]
                    break

        # ---------- PRIMARY PHONE ----------
        primary_phone = ""
        for p in phone_results:
            if p.get("valid", False) and p.get("e164", ""):
                primary_phone = p["e164"]
                break

        return {
            "emails": email_results,
            "phones": phone_results,
            "primary_email": primary_email,
            "primary_phone": primary_phone,
        }
    except Exception:
        # Фатальная ошибка — безопасный дефолт (свежая копия).
        return {
            "emails": [],
            "phones": [],
            "primary_email": "",
            "primary_phone": "",
        }
