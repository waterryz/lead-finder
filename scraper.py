import re
import time
import asyncio
import itertools
from urllib.parse import urljoin, urlparse
import aiohttp
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from config import SCRAPE_TIMEOUT, MAX_CONCURRENT_REQUESTS, PROXIES

ua = UserAgent()
semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

_proxy_cycle = itertools.cycle(PROXIES) if PROXIES else None


def next_proxy() -> str | None:
    """Round-robin proxy selection. Returns None when no proxies configured."""
    return next(_proxy_cycle) if _proxy_cycle else None


def _extract_contacts(text: str, html: str) -> dict:
    emails = set(re.findall(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text
    ))
    emails = [e for e in emails if not e.lower().endswith((".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"))]

    phones = set(re.findall(
        r"(?:\+7|8)[\s\-()]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}", text
    ))

    socials = {}
    social_patterns = {
        "telegram": r"(?:t\.me|telegram\.me)/([a-zA-Z0-9_]+)",
        "whatsapp": r"wa\.me/(\d+)",
        "vk": r"vk\.com/([a-zA-Z0-9_.]+)",
        "instagram": r"instagram\.com/([a-zA-Z0-9_.]+)",
        "facebook": r"facebook\.com/([a-zA-Z0-9_.]+)",
        "youtube": r"youtube\.com/(?:channel/|@|c/)([a-zA-Z0-9_\-]+)",
    }
    for name, pattern in social_patterns.items():
        matches = re.findall(pattern, html, re.IGNORECASE)
        if matches:
            socials[name] = list(set(matches))[:3]

    return {"emails": emails, "phones": list(phones)[:10], "socials": socials}


async def scrape_site(url: str) -> dict:
    """Scrape a company website. Returns text, contacts, tech signals, raw html+headers+timing."""
    async with semaphore:
        result = {
            "url": url,
            "final_url": url,
            "title": "",
            "text": "",
            "html": "",
            "status": 0,
            "headers": {},
            "tech_headers": {},
            "meta": {},
            "elapsed": 0.0,
            "emails": [],
            "phones": [],
            "socials": {},
            "rendered": False,   # set True later if JS-rendered
            "error": None,
        }

        headers = {
            "User-Agent": ua.random,
            "Accept-Language": "ru-RU,ru;q=0.9",
        }
        proxy = next_proxy()
        started = time.monotonic()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=headers, proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=SCRAPE_TIMEOUT),
                    ssl=False, allow_redirects=True, max_redirects=5,
                ) as resp:
                    result["status"] = resp.status
                    result["final_url"] = str(resp.url)
                    result["elapsed"] = round(time.monotonic() - started, 3)
                    result["headers"] = {k.lower(): v for k, v in resp.headers.items()}

                    for h in ("server", "x-powered-by", "x-generator", "x-cms"):
                        val = resp.headers.get(h)
                        if val:
                            result["tech_headers"][h] = val

                    if resp.status != 200:
                        result["error"] = f"HTTP {resp.status}"
                        try:
                            result["html"] = await resp.text(errors="replace")
                        except Exception:
                            pass
                        return result

                    html = await resp.text(errors="replace")

        except Exception as e:
            result["elapsed"] = round(time.monotonic() - started, 3)
            result["error"] = str(e)[:200]
            return result

        result["html"] = html
        soup = BeautifulSoup(html, "lxml")

        title_tag = soup.find("title")
        if title_tag:
            result["title"] = title_tag.get_text(strip=True)

        for meta in soup.find_all("meta"):
            name = meta.get("name", meta.get("property", "")).lower()
            content = meta.get("content", "")
            if name in ("description", "keywords", "generator", "author", "viewport"):
                result["meta"][name] = content[:500]

        for tag in soup(["script", "style", "noscript", "svg", "path"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        result["text"] = text[:8000]

        contacts = _extract_contacts(text, html)
        result["emails"] = contacts["emails"]
        result["phones"] = contacts["phones"]
        result["socials"] = contacts["socials"]

        return result


async def scrape_contacts_page(base_url: str) -> dict:
    """Try to find and scrape /contacts or similar pages for extra contacts + about text."""
    parsed = urlparse(base_url)
    contact_paths = ["/contacts", "/kontakty", "/contact", "/about", "/o-kompanii", "/company", "/team"]
    extra = {"emails": [], "phones": [], "socials": {}, "about_text": ""}

    for path in contact_paths:
        url = f"{parsed.scheme}://{parsed.netloc}{path}"
        data = await scrape_site(url)
        if not data["error"]:
            extra["emails"].extend(data["emails"])
            extra["phones"].extend(data["phones"])
            for k, v in data["socials"].items():
                extra["socials"].setdefault(k, []).extend(v)
            if len(data.get("text", "")) > len(extra["about_text"]):
                extra["about_text"] = data["text"][:4000]
            break

    extra["emails"] = list(set(extra["emails"]))
    extra["phones"] = list(set(extra["phones"]))
    return extra
