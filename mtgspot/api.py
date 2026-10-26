import os
import time
import re
import urllib.parse
import threading
import logging
from typing import Optional, List, Tuple, Dict, Any
from fastapi import FastAPI, Query, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .db import Database, DEFAULT_DB_PATH
from .scraper import MTGSpotScraper, parse_card_list_text
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

    # Live fallback if local database has 0 matches for a query
    keyword = (q or title or "").strip()
    if total == 0 and len(keyword) >= 2:
        try:
            live_res = scraper.live_search(
                name=keyword,
                sort="price" if "price" in (sort or "") else None,
                limit=page_size,
                offset=offset,
                only_in_stock=(min_stock is not None and min_stock > 0)
            )
            live_items = live_res.get("cards", [])
            if is_foil is not None:
                live_items = [c for c in live_items if c.get("is_foil") == is_foil]
            if condition:
                cond_list = [x.strip().upper() for x in condition.split(",") if x.strip()]
                live_items = [c for c in live_items if c.get("condition", "").upper() in cond_list]
            if min_price is not None:
                live_items = [c for c in live_items if c.get("price", 0) >= min_price]
            if max_price is not None:
                live_items = [c for c in live_items if c.get("price", 0) <= max_price]
            if min_stock is not None and min_stock > 0:
                live_items = [c for c in live_items if c.get("stock", 0) >= min_stock]

            if live_items:
                live_total = live_res.get("total", len(live_items))
                total_pages = (live_total + page_size - 1) // page_size if live_total > 0 else 1
                return {
                    "items": live_items,
                    "total": live_total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": total_pages,
                    "has_next": page < total_pages,
                    "has_prev": page > 1,
                    "is_live": True
                }
        except Exception as e:
            logger.warning(f"Live search fallback in /api/cards failed: {e}")

    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
        "is_live": False
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
    offset: int = Query(0, ge=0),
    only_in_stock: bool = Query(False, description="Filter only cards with stock > 0")
):
    try:
        return scraper.live_search(
            name=name,
            is_foil=is_foil,
            condition=condition,
            id_expansion=id_expansion,
            sort=sort,
            limit=limit,
            offset=offset,
            only_in_stock=only_in_stock
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"MTG Spot gateway error: {e}")


# ==================== Bulk Paste & Decklist Search ====================

class BulkSearchRequest(BaseModel):
    text: Optional[str] = None
    cards: Optional[List[str]] = None
    live_check: Optional[bool] = True


@app.post("/api/cards/bulk", summary="Bulk search cards from pasted list (Decklist checker)")
def bulk_search_cards(req: BulkSearchRequest):
    card_entries = []
    if req.text:
        card_entries.extend(parse_card_list_text(req.text))
    if req.cards:
        for c in req.cards:
            parsed = parse_card_list_text(c)
            if parsed:
                card_entries.extend(parsed)
            elif c.strip():
                card_entries.append((1, c.strip()))

    if not card_entries:
        raise HTTPException(status_code=400, detail="No cards provided for search")

    # Limit to 100 cards per request
    card_entries = card_entries[:100]

    found_cards = []
    missing_cards = []
    total_cost = 0.0

    for qty, card_name in card_entries:
        clean_target = card_name.lower().replace("’", "'").replace("`", "'").replace("//", "/").strip()
        matching_offers = []

        # 1. Check live in-stock store if live_check is enabled (ALWAYS with only_in_stock=True)
        if req.live_check:
            try:
                live_res = scraper.live_search(name=card_name, limit=50, sort="price", only_in_stock=True)
                live_items = live_res.get("cards", [])
                matching_offers = [
                    c for c in live_items
                    if (
                        clean_target in c.get("title", "").lower().replace("’", "'").replace("//", "/")
                        or c.get("title", "").lower().replace("’", "'").replace("//", "/") in clean_target
                    )
                    and c.get("stock", 0) > 0
                ]
            except Exception as e:
                logger.warning(f"Live search fallback failed for {card_name}: {e}")

        # 2. If no live matches or live_check disabled, check local database
        if not matching_offers:
            db_items, _ = db.search_cards(title=card_name, min_stock=1, limit=50, sort_by="price_asc")
            matching_offers = [
                c for c in db_items
                if (
                    clean_target in c.get("title", "").lower().replace("’", "'").replace("//", "/")
                    or c.get("title", "").lower().replace("’", "'").replace("//", "/") in clean_target
                )
                and c.get("stock", 0) > 0
            ]

        if matching_offers:
            matching_offers.sort(key=lambda x: (float(x.get("price") or 999999), -int(x.get("stock") or 0)))
            cheapest = matching_offers[0]
            price = float(cheapest.get("price", 0.0))
            subtotal = round(price * qty, 2)
            total_cost += subtotal
            total_stock_all_offers = sum(int(c.get("stock", 0)) for c in matching_offers)

            found_cards.append({
                "requested_name": card_name,
                "quantity": qty,
                "is_available": True,
                "cheapest_price": price,
                "subtotal_price": subtotal,
                "total_stock": total_stock_all_offers,
                "best_offer": cheapest,
                "all_offers": matching_offers
            })
        else:
            # Card is out of stock. Fetch catalog match for display in missing list.
            catalog_match = None
            try:
                cat_res = scraper.live_search(name=card_name, limit=1, sort="price", only_in_stock=False)
                cat_cards = cat_res.get("cards", [])
                if cat_cards:
                    catalog_match = cat_cards[0]
                else:
                    db_cat, _ = db.search_cards(title=card_name, min_stock=0, limit=1)
                    if db_cat:
                        catalog_match = db_cat[0]
            except Exception:
                pass

            missing_cards.append({
                "requested_name": card_name,
                "quantity": qty,
                "is_available": False,
                "catalog_offer": catalog_match
            })

    return {
        "summary": {
            "total_requested": len(card_entries),
            "available_count": len(found_cards),
            "missing_count": len(missing_cards),
            "total_estimated_cost": round(total_cost, 2)
        },
        "available": found_cards,
        "missing": missing_cards
    }


# ==================== Discord Alerts & Watchlist Models & Endpoints ====================

class WishlistCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    webhook_url: Optional[str] = None


class WishlistUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    webhook_url: Optional[str] = None
    is_active: Optional[bool] = None


class WatchlistCreateRequest(BaseModel):
    card_name: str
    list_id: Optional[int] = 1
    max_price: Optional[float] = None
    condition: Optional[str] = None
    is_foil: Optional[bool] = None
    expansion: Optional[str] = None
    webhook_url: Optional[str] = None


class WatchlistUpdateRequest(BaseModel):
    card_name: Optional[str] = None
    max_price: Optional[float] = None
    condition: Optional[str] = None
    is_foil: Optional[bool] = None
    expansion: Optional[str] = None
    list_id: Optional[int] = None
    is_active: Optional[bool] = None


class BulkWatchlistRequest(BaseModel):
    list_id: Optional[int] = 1
    text: Optional[str] = None
    cards: Optional[List[str]] = None
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


# ==================== Wishlists Endpoints ====================

@app.get("/api/wishlists", summary="Get all wishlists / decklists with card counts")
def get_wishlists():
    return db.get_wishlists()


@app.post("/api/wishlists", summary="Create a new wishlist / decklist")
def create_wishlist(req: WishlistCreateRequest):
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Wishlist name is required")
    try:
        list_id = db.create_wishlist(
            name=req.name,
            description=req.description,
            webhook_url=req.webhook_url
        )
        return {
            "status": "success",
            "message": f"Created wishlist '{req.name.strip()}'!",
            "list": db.get_wishlist(list_id)
        }
    except Exception as e:
        if "UNIQUE" in str(e).upper():
            raise HTTPException(status_code=400, detail="A wishlist with this name already exists")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/wishlists/{list_id}", summary="Update a wishlist / decklist")
def update_wishlist(list_id: int, req: WishlistUpdateRequest):
    success = db.update_wishlist(
        list_id=list_id,
        name=req.name,
        description=req.description,
        webhook_url=req.webhook_url,
        is_active=req.is_active
    )
    if not success:
        raise HTTPException(status_code=404, detail="Wishlist not found or no changes made")
    return {
        "status": "success",
        "message": "Wishlist updated successfully",
        "list": db.get_wishlist(list_id)
    }


@app.delete("/api/wishlists/{list_id}", summary="Delete a wishlist and its watched cards")
def delete_wishlist(list_id: int):
    try:
        success = db.delete_wishlist(list_id)
        if not success:
            raise HTTPException(status_code=404, detail="Wishlist not found")
        return {"status": "success", "message": "Wishlist and its cards deleted successfully"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==================== Watchlist Endpoints ====================

@app.get("/api/watchlist", summary="Get watchlisted cards (optionally filtered by list_id)")
def get_watchlist(list_id: Optional[int] = None):
    return db.get_watchlist(list_id=list_id)


@app.post("/api/watchlist", summary="Add card to Discord alert watchlist")
def add_to_watchlist(req: WatchlistCreateRequest):
    if not req.card_name or not req.card_name.strip():
        raise HTTPException(status_code=400, detail="Card name is required")

    target_list_id = req.list_id or 1
    item_id, is_new = db.add_or_update_watchlist_item(
        card_name=req.card_name,
        list_id=target_list_id,
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

    list_name = item.get("list_name", "Main Wishlist") if item else "Main Wishlist"
    message = (
        f"Card '{req.card_name}' was added to '{list_name}'!"
        if is_new else
        f"Card '{req.card_name}' was already on '{list_name}' (rules updated)!"
    )

    return {
        "status": "success",
        "is_new": is_new,
        "message": message,
        "item": item,
        "initial_check": check_result
    }


@app.put("/api/watchlist/{item_id}", summary="Update a watchlist item's alert rules or list")
def update_watchlist_item(item_id: int, req: WatchlistUpdateRequest):
    success = db.update_watchlist_item(
        item_id=item_id,
        card_name=req.card_name,
        max_price=req.max_price,
        condition=req.condition,
        is_foil=req.is_foil,
        expansion=req.expansion,
        list_id=req.list_id,
        is_active=req.is_active
    )
    if not success:
        raise HTTPException(status_code=404, detail="Watchlist item not found or no changes made")
    return {
        "status": "success",
        "message": "Alert rules updated successfully",
        "item": db.get_watchlist_item(item_id)
    }


@app.post("/api/watchlist/bulk", summary="Add multiple cards to a wishlist at once")
def add_to_watchlist_bulk(req: BulkWatchlistRequest):
    card_entries = []
    if req.text:
        card_entries.extend(parse_card_list_text(req.text))
    if req.cards:
        for c in req.cards:
            parsed = parse_card_list_text(c)
            if parsed:
                card_entries.extend(parsed)
            elif c.strip():
                card_entries.append((1, c.strip()))

    if not card_entries:
        raise HTTPException(status_code=400, detail="No cards provided to add")

    # Limit to 200 cards
    card_entries = card_entries[:200]
    target_list_id = req.list_id or 1

    records = []
    for _, name in card_entries:
        records.append({
            "card_name": name,
            "max_price": req.max_price,
            "condition": req.condition,
            "is_foil": req.is_foil,
            "expansion": req.expansion,
            "webhook_url": req.webhook_url
        })

    inserted_ids, existing_ids = db.add_watchlist_items_bulk(records, list_id=target_list_id)

    default_webhook = db.get_setting("discord_webhook")
    checked = []
    for item_id in (inserted_ids + existing_ids):
        item = db.get_watchlist_item(item_id)
        if item:
            res = watcher.check_watchlist_item(item, default_webhook)
            checked.append(res)

    total_in_stock = sum(1 for r in checked if r.get("in_stock_count", 0) > 0)
    alerts_sent = sum(r.get("alerts_sent", 0) for r in checked)

    list_obj = db.get_wishlist(target_list_id)
    list_name = list_obj["name"] if list_obj else f"ID {target_list_id}"

    if inserted_ids and existing_ids:
        msg = f"Added {len(inserted_ids)} new cards to '{list_name}' ({len(existing_ids)} were already on the list)."
    elif existing_ids:
        msg = f"All provided cards ({len(existing_ids)}) were already on '{list_name}'!"
    else:
        msg = f"Added {len(inserted_ids)} cards to '{list_name}'!"

    return {
        "status": "success",
        "message": msg,
        "total_added": len(inserted_ids),
        "total_existing": len(existing_ids),
        "currently_in_stock": total_in_stock,
        "alerts_sent": alerts_sent,
        "results": checked
    }


@app.delete("/api/watchlist/{item_id}", summary="Delete card from watchlist")
def delete_from_watchlist(item_id: int):
    success = db.delete_watchlist_item(item_id)
    if not success:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    return {"status": "success", "message": "Removed from watchlist"}


@app.post("/api/watchlist/check", summary="Manually trigger immediate stock check (all or specific list)")
def trigger_watchlist_check(list_id: Optional[int] = None):
    default_webhook = db.get_setting("discord_webhook")
    results = watcher.check_all_watchlist(default_webhook, list_id=list_id)
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
    return {"status": "success", "message": "Discord settings saved successfully!"}


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
