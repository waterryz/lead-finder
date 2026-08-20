"""Detect whether a company runs a bug bounty / vulnerability disclosure program.

Runs only for sites that already passed the auth-surface check. Sources:
- /.well-known/security.txt and /security.txt (RFC 9116)
- common policy paths (/bug-bounty, /vdp, /security, ...)
- links to known BB platforms + disclosure keywords in the HTML.
"""
import re
import asyncio
from urllib.parse import urlparse
import aiohttp
from fake_useragent import UserAgent
from scraper import next_proxy
from collector import serper_organic
from config import BB_SEARCH_ENABLED, SERPER_API_KEY

ua = UserAgent()
_sem = asyncio.Semaphore(6)

SECURITY_TXT_PATHS = ["/.well-known/security.txt", "/security.txt"]
BB_PATHS = ["/bug-bounty", "/bugbounty", "/bug_bounty", "/security",
            "/responsible-disclosure", "/vulnerability-disclosure", "/vdp"]

PLATFORMS = {
    "HackerOne": r"hackerone\.com",
    "Bugcrowd": r"bugcrowd\.com",
    "Intigriti": r"intigriti\.com",
    "YesWeHack": r"yeswehack\.com",
    "Standoff365": r"standoff365\.com",
    "BI.ZONE": r"bugbounty\.bi\.zone|bi\.zone/(?:bug|bb)",
    "BugBounty.ru": r"bugbounty\.ru",
    "HackenProof": r"hackenproof\.com",
    "Synack": r"synack\.com",
    "Immunefi": r"immunefi\.com",
}

BB_KEYWORDS = [
    "bug bounty", "bugbounty", "responsible disclosure", "vulnerability disclosure",
    "vulnerability reward", "security researcher", "report a vulnerability",
    "багбаунти", "баг-баунти", "программа поиска уязвимостей", "ответственное раскрытие",
    "раскрытие уязвимост", "вознаграждение за уязвимост", "сообщить об уязвимост",
]

# Strong phrases that a REAL disclosure/BB page has — but a soft-404 that merely
# echoes the requested path (e.g. "/bugbounty") does not. Used to validate policy pages.
STRONG_KEYWORDS = [
    "responsible disclosure", "vulnerability disclosure", "vulnerability reward",
    "security researcher", "report a vulnerability", "security.txt",
    "программа поиска уязвимостей", "ответственное раскрытие", "раскрытие уязвимост",
    "вознаграждение за уязвимост", "сообщить об уязвимост", "программа багбаунти",
]

_SECTXT_MARKERS = ("contact:", "policy:", "expires:", "encryption:", "acknowledgments:")

# Markers of an error/"soft 404" page (returns HTTP 200 but is really not-found).
_404_MARKERS = (
    "ошибка 404", "error 404", "404 not found", "page not found", "not found",
    "страница не найдена", "страница не существует", "не найдена", "не существует",
    "попали не туда", "больше нет или никогда", "ничего не найдено", "такой страницы",
)


def _find_platforms(corpus: str) -> list[str]:
    found = []
    for name, pattern in PLATFORMS.items():
        if re.search(pattern, corpus, re.IGNORECASE):
            found.append(name)
    return found


def _has_keyword(corpus: str) -> bool:
    low = corpus.lower()
    return any(k in low for k in BB_KEYWORDS)


def _has_strong(body: str) -> bool:
    low = body.lower()
    return any(k in low for k in STRONG_KEYWORDS)


def _looks_404(body: str) -> bool:
    low = body.lower()
    return any(m in low for m in _404_MARKERS)


def _is_plaintext_sectxt(body: str) -> bool:
    """A real security.txt is text/plain, not an HTML page."""
    low = body.lower()
    return "<html" not in low and "<!doctype" not in low and "<body" not in low


async def _get(session, url):
    try:
        async with _sem:
            async with session.get(
                url, headers={"User-Agent": ua.random}, proxy=next_proxy(),
                timeout=aiohttp.ClientTimeout(total=6), ssl=False, allow_redirects=True,
            ) as r:
                if r.status != 200:
                    return url, r.status, ""
                body = await r.text(errors="replace")
                return url, r.status, body[:30000]
    except Exception:
        return url, None, ""


async def check_bug_bounty(url: str, site_data: dict) -> dict:
    """Return {has_bb, platforms:[...], security_txt:bool, policy_url:str, signals:[...]}."""
    result = {
        "has_bb": False, "platforms": [], "security_txt": False,
        "policy_url": "", "signals": [],
    }

    try:
        parsed = urlparse(site_data.get("final_url") or url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        html = site_data.get("html", "") or ""

        # Corpus starts with the already-fetched main page.
        corpus = html
        platforms = set(_find_platforms(html))
        if platforms:
            result["signals"].append("platform-link-on-homepage")

        netloc = parsed.netloc.lower().replace("www.", "")
        label = netloc.split(".")[0] if netloc else ""

        async with aiohttp.ClientSession() as session:
            jobs = [
                asyncio.gather(*[_get(session, base + p) for p in SECURITY_TXT_PATHS]),
                asyncio.gather(*[_get(session, base + p) for p in BB_PATHS]),
            ]
            do_search = BB_SEARCH_ENABLED and SERPER_API_KEY
            if do_search:
                jobs.append(serper_organic(f"{netloc} bug bounty vulnerability disclosure", 10))
            gathered = await asyncio.gather(*jobs)

        sectxt_res, path_res = gathered[0], gathered[1]
        organic = gathered[2] if do_search and len(gathered) > 2 else []

        for u, status, body in sectxt_res:
            # A real security.txt is plain text with the RFC fields — not an HTML/404 page.
            if (status == 200 and any(m in body.lower() for m in _SECTXT_MARKERS)
                    and _is_plaintext_sectxt(body) and not _looks_404(body)):
                result["security_txt"] = True
                result["signals"].append("security.txt")
                corpus += "\n" + body
                m = re.search(r"(?im)^\s*policy:\s*(\S+)", body)
                if m and not result["policy_url"]:
                    result["policy_url"] = m.group(1).strip()
                elif not result["policy_url"]:
                    result["policy_url"] = u  # fallback: link to the security.txt itself

        for u, status, body in path_res:
            if status != 200 or _looks_404(body):
                continue
            # Strip the requested path from the body so a soft-404 that merely echoes
            # "/bugbounty" doesn't match the "bugbounty" keyword — real pages keep other wording.
            token = urlparse(u).path.strip("/").lower()
            stripped = body.lower().replace(token, " ").replace(token.replace("-", ""), " ")
            if _has_keyword(stripped):
                result["signals"].append(f"policy-page:{urlparse(u).path}")
                corpus += "\n" + body
                if not result["policy_url"]:
                    result["policy_url"] = u

        # Search-engine lookup: trust a platform result only if the company's slug is in the
        # program URL itself (e.g. hackerone.com/gitlab, standoff365.com/programs/ozon).
        for o in organic:
            link_l = o["link"].lower()
            if not label or label not in link_l:
                continue
            for name, pattern in PLATFORMS.items():
                if re.search(pattern, o["link"], re.IGNORECASE):
                    platforms.add(name)
                    if f"search:{name}" not in result["signals"]:
                        result["signals"].append(f"search:{name}")
                    if not result["policy_url"]:
                        result["policy_url"] = o["link"]

        platforms |= set(_find_platforms(corpus))
        result["platforms"] = sorted(platforms)

        has_policy_page = any(s.startswith("policy-page:") for s in result["signals"])
        result["has_bb"] = bool(platforms) or result["security_txt"] or has_policy_page

        # Weak homepage mention (only informational if nothing stronger found).
        if not result["has_bb"] and _has_keyword(html):
            result["signals"].append("keyword-on-homepage")

        return result

    except Exception:
        return result
