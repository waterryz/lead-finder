import asyncio
import re
from urllib.parse import urlparse, quote_plus, unquote
import aiohttp
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from config import MAX_RESULTS_PER_QUERY, REQUEST_DELAY, SERPER_API_KEY

ua = UserAgent()

SKIP_DOMAINS = {
    "google.", "youtube.", "wikipedia.", "facebook.", "instagram.",
    "vk.com", "twitter.", "x.com", "reddit.", "duckduckgo.",
    "yandex.", "mail.ru", "ok.ru", "tiktok.", "pinterest.",
    "amazon.", "ebay.", "aliexpress.", "avito.ru", "hh.ru",
    "bing.com", "microsoft.com", "t.me", "zen.yandex", "dzen.ru",
    "2gis.", "otzovik.", "irecommend.", "flamp.", "spr.ru", "ya.ru",
}


def _should_skip(domain: str) -> bool:
    return (not domain) or any(s in domain for s in SKIP_DOMAINS)


def _clean(link: str) -> tuple[str, str] | None:
    if not link or not link.startswith("http"):
        return None
    link = unquote(link)
    domain = urlparse(link).netloc.lower().replace("www.", "")
    if _should_skip(domain):
        return None
    return link, domain


async def _fetch(session, url, headers, retries: int = 2):
    for attempt in range(retries):
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.text()
        except Exception:
            pass
        await asyncio.sleep(0.7 * (attempt + 1))
    return ""


async def _search_serper(session, query: str, max_results: int) -> list[dict]:
    """Google results via Serper.dev API (reliable, paginated). Needs SERPER_API_KEY."""
    if not SERPER_API_KEY:
        return []
    results, seen = [], set()
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    for page in range(1, 7):  # up to ~60 results
        payload = {"q": query, "gl": "ru", "hl": "ru", "num": 10, "page": page}
        try:
            async with session.post("https://google.serper.dev/search", json=payload,
                                    headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    break
                data = await r.json()
        except Exception:
            break
        organic = data.get("organic", [])
        if not organic:
            break
        for o in organic:
            cleaned = _clean(o.get("link", ""))
            if not cleaned:
                continue
            link, domain = cleaned
            if domain in seen:
                continue
            seen.add(domain)
            results.append({"url": link, "domain": domain, "name": (o.get("title") or domain)[:100]})
            if len(results) >= max_results:
                return results
    return results


async def _search_bing(session, query: str, max_results: int) -> list[dict]:
    """Bing HTML search with pagination via first= param."""
    results, seen = [], set()
    for page in range(0, 5):  # up to ~50 results
        first = page * 10 + 1
        url = f"https://www.bing.com/search?q={quote_plus(query)}&first={first}&setlang=ru-RU&mkt=ru-RU"
        html = await _fetch(session, url, {"User-Agent": ua.random, "Accept-Language": "ru-RU,ru;q=0.9"})
        if not html:
            break
        soup = BeautifulSoup(html, "lxml")
        page_hits = 0
        for h2 in soup.select("li.b_algo h2 a[href], h2 a[href]"):
            cleaned = _clean(h2.get("href", ""))
            if not cleaned:
                continue
            link, domain = cleaned
            if domain in seen:
                continue
            seen.add(domain)
            results.append({"url": link, "domain": domain, "name": h2.get_text(strip=True)[:100] or domain})
            page_hits += 1
            if len(results) >= max_results:
                return results
        if page_hits == 0:
            break
        await asyncio.sleep(0.5)
    return results


async def _search_duckduckgo(session, query: str, max_results: int) -> list[dict]:
    results, seen = [], set()
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    html = await _fetch(session, url, {"User-Agent": ua.random})
    if not html:
        return results
    soup = BeautifulSoup(html, "lxml")
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        m = re.search(r"uddg=(https?[^&]+)", href)
        cleaned = _clean(m.group(1) if m else href)
        if not cleaned:
            continue
        link, domain = cleaned
        if domain in seen:
            continue
        seen.add(domain)
        results.append({"url": link, "domain": domain, "name": a.get_text(strip=True) or domain})
        if len(results) >= max_results:
            break
    return results


async def _search_yandex(session, query: str, max_results: int) -> list[dict]:
    results, seen = [], set()
    url = f"https://yandex.ru/search/?text={quote_plus(query)}&numdoc=50"
    html = await _fetch(session, url, {"User-Agent": ua.random, "Accept-Language": "ru-RU,ru;q=0.9"})
    if not html:
        return results
    soup = BeautifulSoup(html, "lxml")
    for a in soup.select("a[href]"):
        cleaned = _clean(a.get("href", ""))
        if not cleaned:
            continue
        link, domain = cleaned
        if domain in seen:
            continue
        seen.add(domain)
        results.append({"url": link, "domain": domain, "name": a.get_text(strip=True)[:100] or domain})
        if len(results) >= max_results:
            break
    return results


async def serper_organic(query: str, num: int = 10) -> list[dict]:
    """Raw Serper organic results (unfiltered) — used e.g. to find a company's BB program."""
    if not SERPER_API_KEY:
        return []
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    payload = {"q": query, "gl": "ru", "hl": "ru", "num": num}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post("https://google.serper.dev/search", json=payload,
                              headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return []
                data = await r.json()
    except Exception:
        return []
    out = []
    for o in data.get("organic", []):
        link = o.get("link", "")
        out.append({
            "link": link,
            "title": o.get("title", ""),
            "snippet": o.get("snippet", ""),
            "domain": urlparse(link).netloc.lower().replace("www.", ""),
        })
    return out


async def search_google(query: str, num_results: int = MAX_RESULTS_PER_QUERY) -> list[dict]:
    """Aggregate candidates. Prefers Serper API; falls back to free scraping."""
    async with aiohttp.ClientSession() as session:
        # Reliable path: Serper (Google) if a key is configured.
        serper = await _search_serper(session, query, num_results)
        if len(serper) >= min(num_results, 8):
            return serper[:num_results]

        # Free fallback: combine scrapers.
        bing, ddg, yandex = await asyncio.gather(
            _search_bing(session, query, num_results),
            _search_duckduckgo(session, query, num_results),
            _search_yandex(session, query, num_results),
        )
        merged, seen = [], set()
        for src in (serper, ddg, bing, yandex):
            for r in src:
                if r["domain"] not in seen:
                    seen.add(r["domain"])
                    merged.append(r)
                if len(merged) >= num_results:
                    return merged

    return merged[:num_results]
