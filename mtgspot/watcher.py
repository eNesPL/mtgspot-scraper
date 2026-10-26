import time
import threading
import logging
import urllib.parse
from typing import Dict, Any, List, Optional
from .db import Database
from .scraper import MTGSpotScraper
from .discord import send_discord_card_alert

logger = logging.getLogger("mtgspot.watcher")


class WatchService:
    def __init__(self, db: Database, scraper: MTGSpotScraper):
        self.db = db
        self.scraper = scraper
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.last_run_time: Optional[float] = None
        self.is_checking: bool = False

    def check_watchlist_item(
        self,
        item: Dict[str, Any],
        default_webhook: Optional[str] = None,
        force_notify: bool = False
    ) -> Dict[str, Any]:
        """
        Check MTG Spot live store for a single watchlist item.
        If in-stock cards match the criteria, send consolidated Discord alert.
        Alert is only sent on restock events (stock went from 0 to >0, or card was never notified),
        preventing continuous repeat spam for cards that remain in stock.
        """
        watchlist_id = item["id"]
        card_name = item["card_name"]
        max_price = item.get("max_price")
        req_condition = item.get("condition")
        req_foil = item.get("is_foil")
        req_expansion = item.get("expansion")
        webhook = item.get("webhook_url") or item.get("list_webhook_url") or default_webhook
        prev_stock = int(item.get("last_stock") or 0)
        last_notified_at = item.get("last_notified_at")

        # Query MTG Spot API for in-stock cards with this name
        url = f"https://gateway.mtgspot.pl/api/shop/articles?page[limit]=50&filter[is_in_channel]=1&filter[name]={urllib.parse.quote(card_name)}"
        try:
            resp = self.scraper.session.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json().get("response", {}).get("data", [])
        except Exception as e:
            logger.error(f"Error querying MTG Spot for watchlist card '{card_name}': {e}")
            return {"card_name": card_name, "error": str(e), "matches": []}

        matching_cards = []
        total_in_stock = 0

        for raw_item in data:
            card = self.scraper.parse_card_item(raw_item)
            title = card["title"]

            # Exact or substring match check
            if card_name.lower() not in title.lower():
                continue

            # Price constraint
            if max_price is not None and card["price"] > max_price:
                continue

            # Foil constraint
            if req_foil is not None and card["is_foil"] != req_foil:
                continue

            # Condition constraint (e.g. NM, EX)
            if req_condition and card["condition"]:
                allowed_conds = [c.strip().upper() for c in req_condition.split(",") if c.strip()]
                if card["condition"].upper() not in allowed_conds:
                    continue

            # Expansion constraint
            if req_expansion and card["expansion_name"]:
                if req_expansion.lower() not in card["expansion_name"].lower():
                    continue

            # Must have stock
            if card["stock"] <= 0:
                continue

            matching_cards.append(card)
            total_in_stock += card["stock"]

        alerts_sent = 0

        # Sort matching cards by price ascending so the best offer is first
        matching_cards.sort(key=lambda c: (c.get("price", float("inf")), -c.get("stock", 0)))

        if matching_cards and webhook:
            best_offer = matching_cards[0]

            # Restock event logic:
            # Send alert only if:
            # 1. Card was previously out of stock (prev_stock == 0)
            # 2. Or card was newly added and never alerted yet (last_notified_at is None)
            # 3. Or forced explicitly
            # If the card was ALREADY in stock and notified, do NOT send repeat alerts!
            is_restock_event = (prev_stock == 0) or (last_notified_at is None) or force_notify

            # Anti-spam check: prevent re-sending if notified very recently (e.g. across lists or rapid clicks)
            already_notified = self.db.has_card_been_notified(card_name, hours=6)

            if is_restock_event and (not already_notified or force_notify):
                success, msg = send_discord_card_alert(
                    webhook,
                    best_offer,
                    rule=item,
                    total_stock=total_in_stock,
                    matching_offers_count=len(matching_cards)
                )
                if success:
                    self.db.record_notification(
                        watchlist_id,
                        best_offer["id"],
                        card_name,
                        best_offer["price"],
                        total_in_stock
                    )
                    alerts_sent = 1

        # Update stock count in database
        # If alerts_sent > 0, notified=True updates last_notified_at
        self.db.update_watchlist_stock(watchlist_id, total_in_stock, notified=(alerts_sent > 0))

        return {
            "watchlist_id": watchlist_id,
            "card_name": card_name,
            "in_stock_count": total_in_stock,
            "matching_offers": len(matching_cards),
            "alerts_sent": alerts_sent,
            "cards": matching_cards
        }

    def check_all_watchlist(
        self,
        default_webhook: Optional[str] = None,
        list_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Run check on all active watchlist items (or items in a specific wishlist)."""
        self.is_checking = True
        self.last_run_time = time.time()
        results = []

        if not default_webhook:
            default_webhook = self.db.get_setting("discord_webhook")

        items = self.db.get_watchlist(list_id=list_id, active_only=True)
        list_label = f"wishlist_id={list_id}" if list_id else "all wishlists"
        logger.info(f"Checking {len(items)} watchlist items ({list_label}) for MTG Spot store stock...")

        for item in items:
            res = self.check_watchlist_item(item, default_webhook)
            results.append(res)
            # Polite pause between queries
            time.sleep(0.3)

        self.is_checking = False
        return results

    def _loop(self):
        logger.info("WatchService background loop started.")
        while not self._stop_event.is_set():
            try:
                interval_str = self.db.get_setting("watch_interval_seconds", "300")
                interval = max(60, int(interval_str))
                default_webhook = self.db.get_setting("discord_webhook")

                self.check_all_watchlist(default_webhook)

                # Wait with periodic check for stop event
                for _ in range(interval):
                    if self._stop_event.is_set():
                        break
                    time.sleep(1)

            except Exception as e:
                logger.exception(f"Unexpected error in WatchService loop: {e}")
                time.sleep(10)

        logger.info("WatchService background loop stopped.")

    def start(self):
        """Start the background monitoring thread."""
        if self._thread and self._thread.is_alive():
            logger.info("WatchService thread is already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="MTGSpotWatcherThread")
        self._thread.start()
        logger.info("WatchService started.")

    def stop(self):
        """Stop the background monitoring thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
            logger.info("WatchService stopped.")
