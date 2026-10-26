import os
import time
import threading
import logging
from typing import Optional
from fastapi import FastAPI, Query, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .db import Database, DEFAULT_DB_PATH
from .scraper import MTGSpotScraper
from .watcher import WatchService
from .discord import send_test_webhook

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("mtgspot.api")

app = FastAPI(
    title="MTG Spot Cards Scraper & Search API",
    description="High-performance card scraper, indexing engine, and search API for mtgspot.pl",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Enable CORS for all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared database and scraper instances
db = Database()
scraper = MTGSpotScraper(db=db)
watcher = WatchService(db=db, scraper=scraper)


@app.on_event("startup")
def on_startup():
    # Start the Discord Watcher background monitoring thread
    watcher.start()


@app.on_event("shutdown")
def on_shutdown():
    watcher.stop()

# Global scrape job state
scrape_state = {
    "status": "idle",       # "idle", "running", "success", "error"
    "current": 0,
    "total": 0,
    "percent": 0.0,
    "message": "Ready to scrape",
    "start_time": 0.0,
    "duration": 0.0,
    "error": None
}
scrape_lock = threading.Lock()


class ScrapeRequest(BaseModel):
    batch_size: Optional[int] = 2000
    max_cards: Optional[int] = None
    clear_existing: Optional[bool] = False


def _run_scrape_job(batch_size: int, max_cards: Optional[int], clear_existing: bool):
    global scrape_state
    with scrape_lock:
        scrape_state["status"] = "running"
        scrape_state["current"] = 0
        scrape_state["total"] = 0
        scrape_state["percent"] = 0.0
        scrape_state["message"] = "Initializing scrape job..."
        scrape_state["start_time"] = time.time()
        scrape_state["duration"] = 0.0
        scrape_state["error"] = None

    def on_progress(cur, tot, pct, msg):
        global scrape_state
        scrape_state["current"] = cur
        scrape_state["total"] = tot
        scrape_state["percent"] = round(pct, 1)
        scrape_state["message"] = msg
        scrape_state["duration"] = round(time.time() - scrape_state["start_time"], 1)

    try:
        if clear_existing:
            logger.info("Clearing existing cards before full scrape...")
            db.clear_cards()

        res = scraper.scrape_all_available_cards(
            batch_size=batch_size,
            max_cards=max_cards,
            progress_callback=on_progress
        )

        with scrape_lock:
            scrape_state["status"] = "success"
            scrape_state["message"] = f"Successfully scraped {res['cards_scraped']} available cards!"
            scrape_state["duration"] = res["duration_seconds"]
            scrape_state["percent"] = 100.0

    except Exception as e:
        logger.exception("Scraper job failed with error:")
        with scrape_lock:
            scrape_state["status"] = "error"
            scrape_state["error"] = str(e)
            scrape_state["message"] = f"Scrape failed: {e}"


@app.get("/api/cards", summary="Search and filter cards")
def search_cards(
    q: Optional[str] = Query(None, description="Full-text search keyword (title, expansion, rarity, condition)"),
    title: Optional[str] = Query(None, description="Substring search on card title"),
    expansion: Optional[str] = Query(None, description="Set/Expansion name filter"),
    id_expansion: Optional[int] = Query(None, description="Set/Expansion ID"),
    condition: Optional[str] = Query(None, description="Card condition (e.g. 'NM', 'EX', or comma-separated 'NM,EX')"),
    is_foil: Optional[bool] = Query(None, description="Foil filter (true or false)"),
    rarity: Optional[str] = Query(None, description="Rarity filter (e.g. 'Rare', 'Mythic', 'Uncommon', 'Common', 'Token')"),
    language: Optional[str] = Query(None, description="Language filter (e.g. 'English', 'Japanese')"),
    min_price: Optional[float] = Query(None, description="Minimum price in PLN", ge=0),
    max_price: Optional[float] = Query(None, description="Maximum price in PLN", ge=0),
    min_stock: Optional[int] = Query(1, description="Minimum in-stock quantity (default 1 for available cards)", ge=0),
    sort: Optional[str] = Query("price_asc", description="Sort order: 'price_asc', 'price_desc', 'title_asc', 'title_desc', 'stock_desc', 'newest'"),
    page: int = Query(1, description="Page number", ge=1),
    page_size: int = Query(24, description="Results per page", ge=1, le=200)
):
    offset = (page - 1) * page_size
    items, total = db.search_cards(
        q=q,
        title=title,
        expansion=expansion,
        id_expansion=id_expansion,
        condition=condition,
        is_foil=is_foil,
        rarity=rarity,
        language=language,
        min_price=min_price,
        max_price=max_price,
        min_stock=min_stock,
        sort_by=sort,
        limit=page_size,
        offset=offset
    )

    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1
    }


@app.get("/api/cards/{card_id}", summary="Get card by ID")
def get_card(card_id: str):
    card = db.get_card_by_id(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    return card


@app.get("/api/filters", summary="Get filter options and aggregate statistics")
def get_filters():
    return db.get_filter_options()


@app.get("/api/expansions", summary="List sets and expansions")
def get_expansions():
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM expansions ORDER BY name ASC")
        return [dict(r) for r in cursor.fetchall()]


@app.get("/api/stats", summary="Get store index statistics")
def get_stats():
    meta = db.get_scrape_meta()
    filter_opts = db.get_filter_options()
    stats = filter_opts.get("stats", {})
    return {
        "index_stats": stats,
        "scrape_meta": meta
    }


@app.post("/api/scrape", summary="Trigger cards scrape/refresh")
def trigger_scrape(req: Optional[ScrapeRequest] = None, background_tasks: BackgroundTasks = None):
    global scrape_state
    if scrape_state["status"] == "running":
        return {
            "status": "already_running",
            "message": "Scrape job is currently in progress",
            "progress": scrape_state
        }

    batch_size = req.batch_size if req and req.batch_size else 2000
    max_cards = req.max_cards if req else None
    clear_existing = req.clear_existing if req and req.clear_existing else False

    # Launch background thread for non-blocking execution
    thread = threading.Thread(
        target=_run_scrape_job,
        args=(batch_size, max_cards, clear_existing),
        daemon=True
    )
    thread.start()

    return {
        "status": "started",
        "message": "Scrape task started in background",
        "batch_size": batch_size,
        "max_cards": max_cards
    }


@app.get("/api/scrape/status", summary="Get scrape progress status")
def get_scrape_status():
    global scrape_state
    return scrape_state


@app.get("/api/live-search", summary="Live pass-through search to MTG Spot API")
def live_search(
    name: Optional[str] = Query(None, description="Card name"),
    is_foil: Optional[int] = Query(None, description="1 for foil, 0 for regular"),
    condition: Optional[str] = Query(None, description="Condition: NM, EX, GD, etc."),
    id_expansion: Optional[int] = Query(None, description="Set ID"),
    sort: Optional[str] = Query("price", description="Sort parameter"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0)
):
    try:
        return scraper.live_search(
            name=name,
            is_foil=is_foil,
            condition=condition,
            id_expansion=id_expansion,
            sort=sort,
            limit=limit,
            offset=offset
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"MTG Spot gateway error: {e}")


# ==================== Discord Alerts & Watchlist Models & Endpoints ====================

class WatchlistCreateRequest(BaseModel):
    card_name: str
    max_price: Optional[float] = None
    condition: Optional[str] = None
    is_foil: Optional[bool] = None
    expansion: Optional[str] = None
    webhook_url: Optional[str] = None


class DiscordSettingsRequest(BaseModel):
    webhook_url: str
    interval_seconds: Optional[int] = 300


class DiscordTestRequest(BaseModel):
    webhook_url: str


@app.get("/api/watchlist", summary="Get all watchlisted cards")
def get_watchlist():
    return db.get_watchlist()


@app.post("/api/watchlist", summary="Add card to Discord alert watchlist")
def add_to_watchlist(req: WatchlistCreateRequest):
    if not req.card_name or not req.card_name.strip():
        raise HTTPException(status_code=400, detail="Nazwa karty jest wymagana")

    item_id = db.add_watchlist_item(
        card_name=req.card_name,
        max_price=req.max_price,
        condition=req.condition,
        is_foil=req.is_foil,
        expansion=req.expansion,
        webhook_url=req.webhook_url
    )

    # Immediately check this item in background or return
    item = db.get_watchlist_item(item_id)
    default_webhook = db.get_setting("discord_webhook")
    check_result = watcher.check_watchlist_item(item, default_webhook)

    return {
        "status": "success",
        "message": f"Karta '{req.card_name}' została dodana do listy obserwowanych!",
        "item": item,
        "initial_check": check_result
    }


@app.delete("/api/watchlist/{item_id}", summary="Delete card from watchlist")
def delete_from_watchlist(item_id: int):
    success = db.delete_watchlist_item(item_id)
    if not success:
        raise HTTPException(status_code=404, detail="Element listy nie został znaleziony")
    return {"status": "success", "message": "Usunięto z listy obserwowanych"}


@app.post("/api/watchlist/check", summary="Manually trigger immediate stock check for all watched cards")
def trigger_watchlist_check():
    default_webhook = db.get_setting("discord_webhook")
    results = watcher.check_all_watchlist(default_webhook)
    total_alerts = sum(r.get("alerts_sent", 0) for r in results)
    return {
        "status": "success",
        "checked_count": len(results),
        "alerts_sent": total_alerts,
        "details": results
    }


@app.get("/api/settings/discord", summary="Get Discord Webhook settings")
def get_discord_settings():
    webhook_url = db.get_setting("discord_webhook", "")
    interval = int(db.get_setting("watch_interval_seconds", "300"))
    return {
        "webhook_url": webhook_url,
        "interval_seconds": interval,
        "is_watcher_running": watcher._thread.is_alive() if watcher._thread else False,
        "last_run_time": watcher.last_run_time
    }


@app.post("/api/settings/discord", summary="Update Discord Webhook settings")
def save_discord_settings(req: DiscordSettingsRequest):
    db.set_setting("discord_webhook", req.webhook_url.strip())
    if req.interval_seconds:
        db.set_setting("watch_interval_seconds", str(max(60, req.interval_seconds)))
    return {"status": "success", "message": "Ustawienia Discord zostały zapisane!"}


@app.post("/api/discord/test", summary="Send test message to Discord Webhook")
def test_discord_webhook(req: DiscordTestRequest):
    success, message = send_test_webhook(req.webhook_url.strip())
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return {"status": "success", "message": message}


@app.get("/", response_class=HTMLResponse, summary="Web Dashboard UI")
def serve_ui():
    static_file = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(static_file):
        with open(static_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>MTG Spot Scraper & Search API is running. UI file missing.</h1>")
