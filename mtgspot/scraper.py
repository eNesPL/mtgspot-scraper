import time
import re
import logging
import requests
from typing import Dict, Any, List, Optional, Callable
from .db import Database, DEFAULT_DB_PATH

logger = logging.getLogger("mtgspot.scraper")

GATEWAY_API_URL = "https://gateway.mtgspot.pl"
DEFAULT_API_KEY = "b3d39321-5dc4-4298-98c6-0399432a948b"
DEFAULT_GAME_ID = "1"  # Magic: The Gathering
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def slugify(text: Optional[str]) -> str:
    """Generate URL slug from set or card name."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


class MTGSpotScraper:
    def __init__(
        self,
        db: Optional[Database] = None,
        api_key: str = DEFAULT_API_KEY,
        game_id: str = DEFAULT_GAME_ID,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: int = 30
    ):
        self.db = db or Database()
        self.api_key = api_key
        self.game_id = game_id
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "X-Api-Key": self.api_key,
            "X-Game-Id": self.game_id,
            "Accept": "application/json"
        })

    def fetch_expansions(self) -> List[Dict[str, Any]]:
        """Fetch all official MTG expansions/sets from MTG Spot."""
        url = f"{GATEWAY_API_URL}/api/expansions"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            expansions = data.get("response", {}).get("data", [])
            logger.info(f"Fetched {len(expansions)} expansions from MTG Spot")
            if self.db:
                self.db.upsert_expansions(expansions)
            return expansions
        except Exception as e:
            logger.error(f"Failed to fetch expansions: {e}")
            return []

    def fetch_articles_page(
        self,
        offset: int = 0,
        limit: int = 2000,
        only_in_stock: bool = True,
        extra_filters: Optional[Dict[str, Any]] = None,
        retries: int = 3
    ) -> Dict[str, Any]:
        """Fetch a single page of card articles from MTG Spot."""
        url = f"{GATEWAY_API_URL}/api/shop/articles"
        params: Dict[str, Any] = {
            "page[limit]": limit,
            "page[offset]": offset
        }
        if only_in_stock:
            params["filter[is_in_channel]"] = 1

        if extra_filters:
            for k, v in extra_filters.items():
                if v is not None:
                    params[f"filter[{k}]"] = v

        for attempt in range(1, retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.json().get("response", {})
                elif resp.status_code == 429:
                    wait_time = attempt * 3
                    logger.warning(f"Rate limited (429). Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.warning(f"HTTP {resp.status_code} on attempt {attempt}: {resp.text[:100]}")
                    time.sleep(attempt * 1.5)
            except Exception as e:
                logger.warning(f"Request error (attempt {attempt}/{retries}): {e}")
                if attempt == retries:
                    raise
                time.sleep(attempt * 1.5)

        return {"data": [], "total": 0}

    def parse_card_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize raw MTG Spot card JSON into structured dictionary."""
        card_id = item.get("id")
        attrs = item.get("attributes", {})

        id_article = attrs.get("id_article")
        id_product = attrs.get("id_product")
        id_expansion = attrs.get("id_expansion")
        title = attrs.get("title") or "Unknown Card"
        expansion_name = attrs.get("expansion_name") or ""

        # Construct store URL on mtgspot.pl
        card_url = "https://mtgspot.pl/single"
        if id_expansion and expansion_name:
            exp_slug = slugify(expansion_name)
            title_slug = slugify(title)
            if id_article and id_product:
                card_url = f"https://mtgspot.pl/single/{exp_slug}/{id_expansion}/{title_slug}/{id_product}/{id_article}"
            else:
                card_url = f"https://mtgspot.pl/single/{exp_slug}/{id_expansion}"

        try:
            price = float(attrs.get("price") or 0.0)
        except (ValueError, TypeError):
            price = 0.0

        try:
            stock = int(attrs.get("stock") or 0)
        except (ValueError, TypeError):
            stock = 0

        return {
            "id": card_id,
            "id_article": id_article,
            "id_product": id_product,
            "title": title,
            "description": attrs.get("description", ""),
            "language": attrs.get("language", "English"),
            "condition": attrs.get("condition", "NM"),
            "is_foil": bool(attrs.get("is_foil", False)),
            "is_signed": bool(attrs.get("is_signed", False)),
            "is_playset": bool(attrs.get("is_playset", False)),
            "is_altered": bool(attrs.get("is_altered", False)),
            "id_expansion": id_expansion,
            "expansion_name": expansion_name,
            "rarity": attrs.get("rarity", ""),
            "stock": stock,
            "price": price,
            "image": attrs.get("image", ""),
            "card_url": card_url
        }

    def scrape_all_available_cards(
        self,
        batch_size: int = 2000,
        max_cards: Optional[int] = None,
        progress_callback: Optional[Callable[[int, int, float, str], None]] = None
    ) -> Dict[str, Any]:
        """
        Scrape all available (in-stock) cards from MTG Spot.
        Fetches cards in batches and upserts them directly into the database.
        
        progress_callback signature:
            callback(fetched_count, total_count, percent, message)
        """
        start_time = time.time()
        logger.info("Starting scrape of all available cards from MTG Spot...")

        if self.db:
            self.db.update_scrape_meta(status="running")

        # First, ensure expansions are synced
        try:
            self.fetch_expansions()
        except Exception as e:
            logger.warning(f"Could not sync expansions before card scrape: {e}")

        # Fetch initial page to determine total available cards
        initial_resp = self.fetch_articles_page(offset=0, limit=batch_size, only_in_stock=True)
        total_in_store = initial_resp.get("total", 0)
        first_batch_data = initial_resp.get("data", [])

        target_total = total_in_store
        if max_cards is not None and max_cards < target_total:
            target_total = max_cards

        logger.info(f"Total available cards reported by MTG Spot: {total_in_store}. Target to fetch: {target_total}")

        fetched_count = 0
        total_upserted = 0

        # Process first batch
        parsed_batch = [self.parse_card_item(item) for item in first_batch_data]
        if max_cards and len(parsed_batch) > max_cards:
            parsed_batch = parsed_batch[:max_cards]

        if self.db and parsed_batch:
            self.db.upsert_cards_batch(parsed_batch)
            total_upserted += len(parsed_batch)

        fetched_count += len(parsed_batch)

        if progress_callback:
            pct = (fetched_count / target_total * 100.0) if target_total else 100.0
            progress_callback(fetched_count, target_total, pct, f"Fetched {fetched_count}/{target_total} cards...")

        offset = batch_size
        while fetched_count < target_total:
            batch_limit = min(batch_size, target_total - fetched_count)
            try:
                page_resp = self.fetch_articles_page(
                    offset=offset,
                    limit=batch_limit,
                    only_in_stock=True
                )
                items = page_resp.get("data", [])
                if not items:
                    logger.info("No more items returned from MTG Spot API.")
                    break

                parsed_items = [self.parse_card_item(item) for item in items]
                if self.db and parsed_items:
                    self.db.upsert_cards_batch(parsed_items)
                    total_upserted += len(parsed_items)

                fetched_count += len(parsed_items)
                offset += len(items)

                if progress_callback:
                    pct = (fetched_count / target_total * 100.0) if target_total else 100.0
                    progress_callback(fetched_count, target_total, pct, f"Fetched {fetched_count}/{target_total} cards...")

                # Small polite throttle
                time.sleep(0.08)

            except Exception as e:
                logger.error(f"Error fetching batch at offset {offset}: {e}")
                if self.db:
                    self.db.update_scrape_meta(status="error", error=str(e))
                raise

        duration = time.time() - start_time
        logger.info(f"Finished scraping! Total cards upserted: {total_upserted} in {duration:.2f} seconds.")

        if self.db:
            self.db.update_scrape_meta(
                status="success",
                total_available=total_upserted,
                duration=duration
            )

        return {
            "status": "success",
            "total_available_in_store": total_in_store,
            "cards_scraped": total_upserted,
            "duration_seconds": round(duration, 2)
        }

    def live_search(
        self,
        name: Optional[str] = None,
        is_foil: Optional[int] = None,
        condition: Optional[str] = None,
        id_expansion: Optional[int] = None,
        sort: Optional[str] = "price",
        limit: int = 20,
        offset: int = 0
    ) -> Dict[str, Any]:
        """Live pass-through search directly against MTG Spot API."""
        url = f"{GATEWAY_API_URL}/api/shop/articles"
        params: Dict[str, Any] = {
            "page[limit]": limit,
            "page[offset]": offset,
            "filter[is_in_channel]": 1
        }
        if name:
            params["filter[name]"] = name
        if is_foil is not None:
            params["filter[is_foil]"] = is_foil
        if condition:
            params["filter[condition]"] = condition
        if id_expansion:
            params["filter[id_expansion]"] = id_expansion
        if sort:
            params["sort"] = sort

        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        raw = resp.json().get("response", {})
        data = raw.get("data", [])
        return {
            "total": raw.get("total", 0),
            "cards": [self.parse_card_item(item) for item in data]
        }
