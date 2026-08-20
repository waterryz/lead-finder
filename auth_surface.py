"""Detect a company's authentication / attack surface for pentest lead scoring.

Combines a free scan of the already-fetched HTML with light probing of common
auth-related paths. The richer the surface (login, registration, personal area,
admin, API auth, payments), the more valuable the pentest lead.
"""
import re
import asyncio
from urllib.parse import urlparse
import aiohttp
from fake_useragent import UserAgent
from scraper import next_proxy

ua = UserAgent()
_sem = asyncio.Semaphore(6)

# Curated high-signal paths (kept short — the HTML scan catches most cases for free).
LOGIN_PATHS = ["/login", "/signin", "/wp-login.php"]
REGISTER_PATHS = ["/register", "/signup"]
PERSONAL_PATHS = ["/lk", "/account", "/dashboard"]
RESET_PATHS = ["/password/reset"]
API_AUTH_PATHS = ["/api/auth", "/oauth"]
ADMIN_PATHS = ["/admin", "/wp-admin"]

_EXISTS_STATUSES = {200, 301, 302, 303, 307, 308, 401, 403, 405}


def _scan_html(html: str, text: str) -> dict:
    """Free signals from the already-fetched main page (no network)."""
    h = html.lower()
    t = text.lower()
    sig = {
        "login": False, "register": False, "personal": False, "reset": False,
        "api_auth": False, "oauth": False, "payments": False, "admin": False,
    }

    if re.search(r'<input[^>]*type=["\']password["\']', h):
        sig["login"] = True
    if re.search(r'action=["\'][^"\']*(?:login|signin|auth)', h) or \
       any(w in t for w in ("войти", "вход в", "log in", "sign in", "авторизац")):
        sig["login"] = True
    if any(w in t for w in ("регистрац", "зарегистрир", "sign up", "создать аккаунт", "create account")) or \
       re.search(r'action=["\'][^"\']*(?:register|signup)', h):
        sig["register"] = True
    if any(w in t for w in ("личный кабинет", "мой кабинет", "личного кабинета")):
        sig["personal"] = True
    if any(w in t for w in ("забыли пароль", "восстановить пароль", "forgot password", "сброс пароля")):
        sig["reset"] = True
    if any(w in h for w in ("bearer", "authorization:", "jwt", "/api/", "graphql")):
        sig["api_auth"] = True
    if any(w in h for w in ("accounts.google.com", "id.vk.com", "oauth.yandex", "oauth2",
                            "connect.ok.ru", "войти через", "sign in with")):
        sig["oauth"] = True
    if any(w in h for w in ("yookassa", "yoomoney", "cloudpayments", "robokassa",
                            "sberbank", "tinkoff", "stripe", "paypal")) or \
       any(w in t for w in ("оплатить", "оформить заказ", "checkout", "корзина")):
        sig["payments"] = True
    return sig


async def _probe(session, base, path):
    try:
        async with _sem:
            async with session.get(
                base + path, headers={"User-Agent": ua.random}, proxy=next_proxy(),
                timeout=aiohttp.ClientTimeout(total=6), ssl=False, allow_redirects=False,
            ) as r:
                return path, r.status
    except Exception:
        return path, None


async def detect_auth_surface(url: str, site_data: dict) -> dict:
    """Return the auth/attack surface of a site.

    {
      has_login, has_register, has_personal_area, has_password_reset,
      has_api_auth, has_oauth, has_admin, has_payments, has_auth (bool),
      labels: [RU labels], found_paths: {path: status}, score: 0-100
    }
    """
    result = {
        "has_login": False, "has_register": False, "has_personal_area": False,
        "has_password_reset": False, "has_api_auth": False, "has_oauth": False,
        "has_admin": False, "has_payments": False, "has_auth": False,
        "labels": [], "found_paths": {}, "score": 0,
    }

    try:
        html = site_data.get("html", "") or ""
        text = site_data.get("text", "") or ""
        sig = _scan_html(html, text)
        result["has_login"] = sig["login"]
        result["has_register"] = sig["register"]
        result["has_personal_area"] = sig["personal"]
        result["has_password_reset"] = sig["reset"]
        result["has_api_auth"] = sig["api_auth"]
        result["has_oauth"] = sig["oauth"]
        result["has_payments"] = sig["payments"]
        result["has_admin"] = sig["admin"]

        parsed = urlparse(site_data.get("final_url") or url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        groups = [
            ("login", LOGIN_PATHS), ("register", REGISTER_PATHS),
            ("personal", PERSONAL_PATHS), ("reset", RESET_PATHS),
            ("api_auth", API_AUTH_PATHS), ("admin", ADMIN_PATHS),
        ]
        async with aiohttp.ClientSession() as session:
            tasks = [_probe(session, base, p) for _, paths in groups for p in paths]
            probed = await asyncio.gather(*tasks)

        status_by_path = {p: s for p, s in probed if s is not None}
        result["found_paths"] = {p: s for p, s in status_by_path.items() if s in _EXISTS_STATUSES}

        def any_hit(paths):
            return any(status_by_path.get(p) in _EXISTS_STATUSES for p in paths)

        if any_hit(LOGIN_PATHS):
            result["has_login"] = True
        if any_hit(REGISTER_PATHS):
            result["has_register"] = True
        if any_hit(PERSONAL_PATHS):
            result["has_personal_area"] = True
        if any_hit(RESET_PATHS):
            result["has_password_reset"] = True
        # API auth: only 401/403/405 are a meaningful "endpoint exists but protected"
        if any(status_by_path.get(p) in {200, 401, 403, 405} for p in API_AUTH_PATHS):
            result["has_api_auth"] = True
        if any_hit(ADMIN_PATHS):
            result["has_admin"] = True

        weights = [
            ("has_login", 25, "Логин"),
            ("has_register", 25, "Регистрация"),
            ("has_personal_area", 15, "Личный кабинет"),
            ("has_api_auth", 20, "API-авторизация"),
            ("has_admin", 15, "Админка"),
            ("has_oauth", 10, "OAuth/соц-вход"),
            ("has_password_reset", 8, "Сброс пароля"),
            ("has_payments", 12, "Онлайн-оплата"),
        ]
        score = 0
        labels = []
        for key, w, label in weights:
            if result[key]:
                score += w
                labels.append(label)
        result["score"] = min(score, 100)
        result["labels"] = labels
        result["has_auth"] = any(result[k] for k in (
            "has_login", "has_register", "has_personal_area", "has_api_auth", "has_admin",
        ))
        return result

    except Exception:
        return result
