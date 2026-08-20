import re
import asyncio
from urllib.parse import urljoin, urlparse
import aiohttp
from fake_useragent import UserAgent
from config import SCRAPE_TIMEOUT

ua = UserAgent()

BACKEND_PATHS = [
    "/api", "/api/v1", "/api/v2", "/graphql",
    "/admin", "/login", "/signin", "/auth",
    "/register", "/signup",
    "/dashboard", "/account", "/profile",
    "/wp-admin", "/wp-login.php",
    "/swagger", "/docs", "/redoc",
]

SESSION_COOKIE_PATTERNS = [
    "session", "sess", "sid", "token", "jwt", "auth",
    "phpsessid", "jsessionid", "asp.net_sessionid",
    "csrftoken", "csrf", "_csrf",
    "laravel_session", "rails", "rack.session",
    "connect.sid",
]

BACKEND_HEADERS = {
    "x-powered-by": None,
    "x-request-id": None,
    "x-trace-id": None,
    "x-correlation-id": None,
    "x-runtime": None,
    "x-frame-options": None,
    "content-security-policy": None,
    "strict-transport-security": None,
    "x-ratelimit-limit": None,
    "x-ratelimit-remaining": None,
}


async def detect_backend(url: str) -> dict:
    """Detect if a site has a real backend vs just a landing page."""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    result = {
        "has_backend": False,
        "score": 0,
        "signals": [],
        "backend_type": "unknown",
        "details": {},
    }

    headers_req = {"User-Agent": ua.random, "Accept-Language": "ru-RU,ru;q=0.9"}

    async with aiohttp.ClientSession() as session:
        # 1. Check main page headers and cookies
        try:
            async with session.get(
                url, headers=headers_req,
                timeout=aiohttp.ClientTimeout(total=SCRAPE_TIMEOUT),
                ssl=False, allow_redirects=True,
            ) as resp:
                for h in BACKEND_HEADERS:
                    val = resp.headers.get(h)
                    if val:
                        result["signals"].append(f"header:{h}={val[:80]}")
                        result["score"] += 2

                cookies = resp.cookies
                for cookie_name in cookies:
                    name_lower = cookie_name.lower()
                    for pattern in SESSION_COOKIE_PATTERNS:
                        if pattern in name_lower:
                            result["signals"].append(f"cookie:{cookie_name}")
                            result["score"] += 3
                            break

                set_cookies = resp.headers.getall("set-cookie", [])
                for sc in set_cookies:
                    sc_lower = sc.lower()
                    for pattern in SESSION_COOKIE_PATTERNS:
                        if pattern in sc_lower:
                            if f"set-cookie:{pattern}" not in [s for s in result["signals"]]:
                                result["signals"].append(f"set-cookie:{pattern}")
                                result["score"] += 3
                            break

                html = await resp.text(errors="replace")

        except Exception:
            return result

        # 2. Analyze HTML for backend signals
        html_lower = html.lower()

        login_patterns = [
            (r'<form[^>]*action=["\'][^"\']*(?:login|signin|auth)[^"\']*["\']', "login_form", 5),
            (r'<input[^>]*type=["\']password["\']', "password_field", 4),
            (r'<input[^>]*name=["\'](?:csrf|_token|authenticity_token)["\']', "csrf_token", 4),
            (r'(?:window\.__NEXT_DATA__|__NUXT__|__APP_STATE__|__INITIAL_STATE__)', "ssr_hydration", 3),
            (r'(?:/_next/|/static/chunks/|/webpack-)', "nextjs_assets", 2),
            (r'/api/(?:v\d|auth|users?|graphql)', "api_refs", 4),
            (r'(?:swagger|openapi|redoc)', "api_docs_ref", 3),
            (r'(?:websocket|wss?://|socket\.io)', "websocket", 4),
            (r'(?:localStorage|sessionStorage)\.(?:setItem|getItem)\(["\'](?:token|auth|jwt)', "client_auth", 4),
            (r'bearer\s+', "bearer_auth", 4),
            (r'authorization["\s]*:["\s]*["\']bearer', "auth_header", 4),
        ]

        for pattern, name, points in login_patterns:
            if re.search(pattern, html, re.IGNORECASE):
                result["signals"].append(f"html:{name}")
                result["score"] += points

        # 3. Check meta generator for CMS
        gen_match = re.search(r'<meta[^>]*name=["\']generator["\'][^>]*content=["\']([^"\']+)', html, re.I)
        if gen_match:
            gen = gen_match.group(1).lower()
            result["details"]["generator"] = gen_match.group(1)
            if "wordpress" in gen:
                result["signals"].append("cms:wordpress")
                result["score"] += 2
            elif "joomla" in gen:
                result["signals"].append("cms:joomla")
                result["score"] += 2
            elif "drupal" in gen:
                result["signals"].append("cms:drupal")
                result["score"] += 3
            elif "bitrix" in gen or "1c-bitrix" in gen:
                result["signals"].append("cms:bitrix")
                result["score"] += 3

        # 4. Probe known backend paths
        probed = []
        paths_to_check = ["/api", "/api/v1", "/graphql", "/login", "/admin", "/swagger", "/docs"]

        async def probe(path):
            try:
                probe_url = base + path
                async with session.get(
                    probe_url, headers=headers_req,
                    timeout=aiohttp.ClientTimeout(total=8),
                    ssl=False, allow_redirects=False,
                ) as r:
                    if r.status in (200, 301, 302, 401, 403, 405):
                        return (path, r.status)
            except Exception:
                pass
            return None

        tasks = [probe(p) for p in paths_to_check]
        probe_results = await asyncio.gather(*tasks)

        for pr in probe_results:
            if pr:
                path, status = pr
                result["signals"].append(f"probe:{path}={status}")
                if path in ("/api", "/api/v1", "/graphql"):
                    result["score"] += 5
                elif path in ("/login", "/admin"):
                    result["score"] += 3 if status in (200, 301, 302) else 2
                else:
                    result["score"] += 2

        # 5. Detect static-only (negative signals)
        static_builders = [
            "tilda", "wix.com", "squarespace", "weebly", "webflow",
            "readymag", "lpgenerator", "platformlp", "getcourse",
            "landingi", "instapage", "leadpages", "unbounce",
        ]
        for sb in static_builders:
            if sb in html_lower:
                result["signals"].append(f"static_builder:{sb}")
                result["score"] -= 5

        # 6. Determine backend type
        signals_str = " ".join(result["signals"]).lower()
        if "nextjs" in signals_str or "__next_data__" in signals_str:
            result["backend_type"] = "Next.js"
        elif "nuxt" in signals_str:
            result["backend_type"] = "Nuxt.js"
        elif "bitrix" in signals_str:
            result["backend_type"] = "1C-Bitrix"
        elif "wordpress" in signals_str:
            result["backend_type"] = "WordPress"
        elif "laravel" in signals_str:
            result["backend_type"] = "Laravel"
        elif "rails" in signals_str:
            result["backend_type"] = "Ruby on Rails"
        elif "django" in signals_str or "csrftoken" in signals_str:
            result["backend_type"] = "Django"
        elif "phpsessid" in signals_str:
            result["backend_type"] = "PHP"
        elif "jsessionid" in signals_str:
            result["backend_type"] = "Java"
        elif "asp.net" in signals_str:
            result["backend_type"] = "ASP.NET"

        # 7. Final verdict
        result["has_backend"] = result["score"] >= 6
        result["details"]["total_signals"] = len(result["signals"])

    return result
