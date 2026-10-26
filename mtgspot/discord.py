import logging
import requests
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger("mtgspot.discord")


def send_test_webhook(webhook_url: str) -> Tuple[bool, str]:
    """Send a test notification embed to verify Discord Webhook URL."""
    if not webhook_url or not webhook_url.startswith("https://discord.com/api/webhooks/"):
        return False, "Invalid Discord Webhook URL (must start with https://discord.com/api/webhooks/...)"

    payload = {
        "username": "MTG Spot Alert",
        "avatar_url": "https://mtgspot.pl/logo.png",
        "embeds": [
            {
                "title": "✅ Discord Notifications Test – MTG Spot Watcher",
                "description": "Your Discord webhook is successfully connected to the **MTG Spot** watcher!\nYou will receive an alert whenever a watched card is restocked.",
                "color": 0xF59E0B,  # Amber / Gold
                "fields": [
                    {"name": "Status", "value": "🟢 Active & ready for alerts", "inline": True},
                    {"name": "Store", "value": "mtgspot.pl", "inline": True}
                ],
                "footer": {
                    "text": "MTG Spot Card Watcher"
                },
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        ]
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            return True, "Test message successfully sent to Discord!"
        else:
            return False, f"Discord returned HTTP error {resp.status_code}: {resp.text}"
    except Exception as e:
        logger.error(f"Failed to send test Discord webhook: {e}")
        return False, f"Error connecting to Discord: {e}"


def send_discord_card_alert(
    webhook_url: str,
    card: Dict[str, Any],
    rule: Optional[Dict[str, Any]] = None,
    total_stock: Optional[int] = None,
    matching_offers_count: Optional[int] = None
) -> Tuple[bool, str]:
    """
    Send a rich Discord Embed when a watched card is in-stock on MTG Spot.
    Consolidates multiple offers into a single notification.
    """
    if not webhook_url:
        return False, "No Discord Webhook URL configured"

    title = card.get("title", "MTG Card")
    price = float(card.get("price", 0.0))
    stock = int(card.get("stock", 0))
    display_stock = total_stock if total_stock is not None else stock
    expansion = card.get("expansion_name", "Unknown Set")
    condition = card.get("condition", "NM")
    is_foil = card.get("is_foil", False)
    rarity = card.get("rarity", "Common")
    image_url = card.get("image", "")
    card_url = card.get("card_url") or "https://mtgspot.pl/single"

    foil_text = "✨ Foil" if is_foil else "Regular"
    color = 0x10B981 if not is_foil else 0xF59E0B  # Emerald green or Gold for foil

    rule_note = ""
    if rule:
        max_p = rule.get("max_price")
        if max_p is not None:
            rule_note = f" *(Price limit: up to {max_p:.2f} PLN)*"

    price_label = "💰 Price" if not matching_offers_count or matching_offers_count <= 1 else "💰 Best Price"
    stock_value = f"**{display_stock} pcs**"
    if matching_offers_count and matching_offers_count > 1:
        stock_value = f"**{display_stock} pcs** ({matching_offers_count} offers from {price:.2f} PLN)"

    desc = f"Just restocked on **MTG Spot** at **{price:.2f} PLN**!{rule_note}"
    if matching_offers_count and matching_offers_count > 1:
        desc = f"Just restocked on **MTG Spot** from **{price:.2f} PLN** ({matching_offers_count} offers available, {display_stock} pcs total)!{rule_note}"

    fields = [
        {
            "name": price_label,
            "value": f"**{price:.2f} PLN**",
            "inline": True
        },
        {
            "name": "📦 In Stock",
            "value": stock_value,
            "inline": True
        },
        {
            "name": "🔍 Condition",
            "value": f"**{condition}**",
            "inline": True
        },
        {
            "name": "✨ Finish",
            "value": foil_text,
            "inline": True
        },
        {
            "name": "🏷️ Set / Expansion",
            "value": expansion,
            "inline": True
        },
        {
            "name": "⚡ Rarity",
            "value": rarity.capitalize() if rarity else "Unknown",
            "inline": True
        }
    ]

    if rule and rule.get("list_name"):
        fields.append({
            "name": "📋 Wishlist / Deck",
            "value": f"**{rule['list_name']}**",
            "inline": True
        })

    embed = {
        "title": f"🎯 Watched Card In Stock: {title}!",
        "url": card_url,
        "description": desc,
        "color": color,
        "thumbnail": {
            "url": image_url
        } if image_url else {},
        "fields": fields,
        "footer": {
            "text": "MTG Spot Watcher • Click the title or link above to purchase"
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    payload = {
        "username": "MTG Spot Alert",
        "avatar_url": "https://mtgspot.pl/logo.png",
        "embeds": [embed]
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            logger.info(f"Discord alert successfully sent for '{title}' to webhook")
            return True, "Alert sent"
        else:
            msg = f"Discord webhook returned HTTP {resp.status_code}: {resp.text}"
            logger.warning(msg)
            return False, msg
    except Exception as e:
        msg = f"Failed to send Discord alert: {e}"
        logger.error(msg)
        return False, msg
