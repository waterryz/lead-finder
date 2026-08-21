import os
import asyncio
import itertools
import tempfile
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from config import (
    QUALITY_MIN_SHOW, MIN_TARGETS, SEARCH_POOL, SEARCH_POOL_DEEP,
    SUPER_QUERIES, SUPER_POOL_PER_QUERY, HOTNESS_HOT_THRESHOLD,
)
from database import (
    init_db, get_companies_filtered, get_company_by_domain, get_dashboard, domain_exists,
    get_queries,
)
from collector import search_google
from idea_generator import generate_ideas
from exporter import export_to_excel
from bot import enrich_one  # reuse the full enrichment pipeline

CONCURRENCY = 4
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")

app = FastAPI(title="Pentest Lead Finder")

jobs: dict[str, dict] = {}
_jobc = itertools.count(1)


@app.on_event("startup")
async def _startup():
    init_db()


# ---------------- Scan runners ----------------

def _is_target(res: dict) -> bool:
    return (
        res.get("quality_score", 0) >= QUALITY_MIN_SHOW
        or res.get("surface_score", 0) >= 40
        or res.get("_is_hot")
    )


def _log(job, line: str):
    lg = job.setdefault("log", [])
    lg.append(line)
    if len(lg) > 400:
        del lg[:len(lg) - 400]


async def _enrich_pool(job, query, pool, stop_at, agg):
    _log(job, f"$ search :: {query}")
    urls = await search_google(query, num_results=pool)
    pending = [it for it in urls if not domain_exists(it["domain"])]
    agg["dup"] += len(urls) - len(pending)
    _log(job, f"> found {len(urls)} sites — {len(pending)} new, {len(urls) - len(pending)} in base")

    total = len(pending)
    idx = 0
    for i in range(0, total, CONCURRENCY):
        if job.get("cancel"):
            _log(job, "! cancelled")
            break
        batch = pending[i:i + CONCURRENCY]
        step_cb = lambda m: _log(job, m)
        results = await asyncio.gather(
            *[enrich_one(query, it, on_step=step_cb) for it in batch], return_exceptions=True
        )
        for item, res in zip(batch, results):
            idx += 1
            agg["done"] += 1
            dom = item["domain"]
            tag = f"[{idx}/{total}] {dom}"
            if isinstance(res, Exception) or not res:
                agg["errors"] += 1
                _log(job, f"{tag} :: error")
                continue
            skip = res.get("_skip")
            if skip == "landing":
                agg["landing"] += 1
                _log(job, f"{tag} :: no backend — skip")
            elif skip == "no_auth":
                agg["no_auth"] += 1
                _log(job, f"{tag} :: no auth surface — skip")
            elif skip:
                agg["errors"] += 1
                _log(job, f"{tag} :: {skip} — skip")
            elif _is_target(res):
                agg["good"] += 1
                agg["found"].append(dom)
                bb = "yes" if res.get("has_bb") else "no"
                _log(job, f"{tag} :: surface={res.get('surface_score',0)} "
                          f"pay={res.get('payout_score',0)} bb={bb} -> TARGET")
            else:
                _log(job, f"{tag} :: surface={res.get('surface_score',0)} weak — skip")
        job["progress"].update(agg, pending=total)
        if stop_at and agg["good"] >= stop_at:
            _log(job, f"> target reached: {agg['good']} leads")
            break


async def run_search(job, query, pool, stop_at):
    agg = {"good": 0, "done": 0, "landing": 0, "no_auth": 0, "dup": 0, "errors": 0, "found": []}
    job["progress"] = {"phase": "search", "query": query, "niche": None, **agg}
    try:
        await _enrich_pool(job, query, pool, stop_at, agg)
        job["progress"].update(agg)
        _log(job, f"> done :: targets={agg['good']} processed={agg['done']}")
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)[:300]
        _log(job, f"! fatal: {str(e)[:120]}")
    finally:
        job["done"] = True


async def run_superscan(job):
    job["progress"] = {"phase": "superscan", "niche": 0, "niche_total": SUPER_QUERIES,
                       "query": "", "good": 0, "done": 0, "landing": 0, "no_auth": 0,
                       "dup": 0, "errors": 0, "found": []}
    try:
        _log(job, f"$ super_scan :: generating {SUPER_QUERIES} niches...")
        ideas = await generate_ideas(SUPER_QUERIES)
        job["progress"]["niche_total"] = len(ideas)
        _log(job, f"> {len(ideas)} niches ready")
        agg = {"good": 0, "done": 0, "landing": 0, "no_auth": 0, "dup": 0, "errors": 0, "found": []}
        for qi, idea in enumerate(ideas, 1):
            if job.get("cancel"):
                break
            job["progress"].update(niche=qi, query=idea, **agg)
            _log(job, f"== niche {qi}/{len(ideas)} ==")
            await _enrich_pool(job, idea, SUPER_POOL_PER_QUERY, None, agg)
        job["progress"].update(agg)
        _log(job, f"> super_scan done :: targets={agg['good']} processed={agg['done']}")
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)[:300]
        _log(job, f"! fatal: {str(e)[:120]}")
    finally:
        job["done"] = True


def _new_job(kind: str, coro_factory) -> str:
    jid = str(next(_jobc))
    job = {"id": jid, "kind": kind, "status": "running", "done": False, "progress": {}, "cancel": False}
    jobs[jid] = job
    asyncio.create_task(coro_factory(job))
    return jid


# ---------------- API ----------------

@app.get("/api/dashboard")
async def api_dashboard():
    return get_dashboard()


@app.get("/api/queries")
async def api_queries():
    return {"folders": get_queries()}


@app.get("/api/companies")
async def api_companies(
    q: str = "", auth: int = 0, bb: int = 0, nobb: int = 0, rich: int = 0,
    hot: int = 0, email: int = 0, tg: int = 0, days: int = 0,
    sort: str = "surface_score", order: str = "desc",
):
    rows = get_companies_filtered(
        query=q or None,
        require_auth=bool(auth), require_bb=bool(bb), exclude_bb=bool(nobb),
        min_payout=7 if rich else 0,
        min_hotness=HOTNESS_HOT_THRESHOLD if hot else 0,
        has_email=bool(email), has_telegram=bool(tg), days=days,
    )
    numeric = {"surface_score", "payout_score", "quality_score", "hotness_score", "backend_score", "times_seen"}
    keyf = (lambda r: r.get(sort) or 0) if sort in numeric else (lambda r: str(r.get(sort) or "").lower())
    rows.sort(key=keyf, reverse=(order == "desc"))
    return {"total": len(rows), "items": rows}


@app.get("/api/company/{domain}")
async def api_company(domain: str):
    c = get_company_by_domain(domain)
    return c or JSONResponse({"error": "not found"}, status_code=404)


@app.get("/api/ideas")
async def api_ideas(n: int = 6):
    return {"ideas": await generate_ideas(n)}


@app.post("/api/search")
async def api_search(payload: dict):
    query = (payload or {}).get("query", "").strip()
    if not query:
        return JSONResponse({"error": "empty query"}, status_code=400)
    deep = bool((payload or {}).get("deep"))
    pool = SEARCH_POOL_DEEP if deep else SEARCH_POOL
    stop_at = None if deep else MIN_TARGETS
    jid = _new_job("search", lambda job: run_search(job, query, pool, stop_at))
    return {"job_id": jid}


@app.post("/api/superscan")
async def api_superscan():
    jid = _new_job("superscan", run_superscan)
    return {"job_id": jid}


@app.get("/api/job/{jid}")
async def api_job(jid: str):
    job = jobs.get(jid)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"id": job["id"], "kind": job["kind"], "status": job["status"],
            "done": job["done"], "progress": job["progress"], "error": job.get("error"),
            "log": job.get("log", [])[-90:]}


@app.post("/api/job/{jid}/cancel")
async def api_cancel(jid: str):
    job = jobs.get(jid)
    if job:
        job["cancel"] = True
    return {"ok": True}


@app.get("/api/export")
async def api_export(
    q: str = "", auth: int = 0, bb: int = 0, nobb: int = 0, rich: int = 0,
    hot: int = 0, email: int = 0, tg: int = 0, days: int = 0,
):
    rows = get_companies_filtered(
        query=q or None, require_auth=bool(auth), require_bb=bool(bb),
        exclude_bb=bool(nobb), min_payout=7 if rich else 0,
        min_hotness=HOTNESS_HOT_THRESHOLD if hot else 0,
        has_email=bool(email), has_telegram=bool(tg), days=days,
    )
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    export_to_excel(rows, path)
    return FileResponse(path, filename="leads.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/")
async def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


if os.path.isdir(WEB_DIR):
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
