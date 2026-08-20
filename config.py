import os
from dotenv import load_dotenv

_BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE, ".env"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
# Optional but strongly recommended for reliable volume: https://serper.dev (2500 free queries)
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

ADMIN_ID = 5348697217

DB_PATH = os.getenv("DB_PATH", os.path.join(_BASE, "companies.db"))
MAX_CONCURRENT_REQUESTS = 5
REQUEST_DELAY = 2.0
SCRAPE_TIMEOUT = 15
MAX_RESULTS_PER_QUERY = 50

# --- Feature thresholds ---
BACKEND_MIN_SCORE = 6          # detect_backend score to count as "has backend"
HOTNESS_HOT_THRESHOLD = 50     # hotness score >= => "hot" lead
QUALITY_MIN_SHOW = 5           # AI pentest_score >= => show card in chat

# --- Pentest focus ---
# We sell penetration-testing services, so we prioritize companies that expose a
# real attack surface: login, registration, personal cabinet, admin panel, API auth, payments.
AUTH_REQUIRED = True           # skip companies with NO auth/attack surface at all
                               # set False to keep every backend (surface still scored & shown)
BB_SEARCH_ENABLED = True       # extra search-engine query "{domain} bug bounty" to catch
                               # programs listed only on platforms (needs SERPER_API_KEY)
MIN_TARGETS = 5                # /search keeps digging until at least this many pentest targets
SEARCH_POOL = 45               # max candidate sites to pull for a normal /search
SEARCH_POOL_DEEP = 80          # candidate pool for /search50 (processes the whole pool)

# --- SUPER SCAN: AI generates N niches and scans them all ---
SUPER_QUERIES = 15             # how many niche ideas to generate and scan
SUPER_POOL_PER_QUERY = 12      # candidates pulled per niche

# --- AI cache ---
AI_CACHE_ENABLED = True
AI_CACHE_TTL_DAYS = 30

# --- JS rendering (Playwright) ---
# If a static fetch returns too little text, re-render with a headless browser.
JS_RENDER_ENABLED = True
JS_RENDER_MIN_TEXT = 400       # chars of visible text below which we try JS render
JS_RENDER_TIMEOUT = 25         # seconds

# --- Concurrency of background search jobs ---
MAX_CONCURRENT_JOBS = 3

# --- Proxy rotation ---
# Comma-separated proxy URLs in .env, e.g.:
#   PROXIES=http://user:pass@1.2.3.4:8000,http://user:pass@5.6.7.8:8000
# Leave empty to go direct (fine for hundreds of requests; needed at thousands).
PROXIES = [p.strip() for p in os.getenv("PROXIES", "").split(",") if p.strip()]
USE_PROXIES = bool(PROXIES)
