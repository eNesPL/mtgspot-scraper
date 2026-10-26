import logging
import requests
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger("mtgspot.discord")


def send_test_webhook(webhook_url: str) -> Tuple[bool, str]:
    """Send a test notification embed to verify Discord Webhook URL."""
    if not webhook_url or not webhook_url.startswith("https://discord.com/api/webhooks/"):
        return False, "Nieprawidłowy adres Discord Webhook URL (powinien zaczynać się od https://discord.com/api/webhooks/...)"

    payload = {
        "username": "MTG Spot Alert",
        "avatar_url": "https://mtgspot.pl/logo.png",
        "embeds": [
            {
                "title": "✅ Test powiadomień Discord – MTG Spot Watcher",
                "description": "Twój webhook Discord został pomyślnie połączony ze scraperem **MTG Spot**!\nOtrzymasz powiadomienie, gdy tylko poszukiwana karta pojawi się na stanie sklepu.",
                "color": 0xF59E0B,  # Amber / Gold
                "fields": [
                    {"name": "Status", "value": "🟢 Aktywny i gotowy do powiadomień", "inline": True},
                    {"name": "Sklep", "value": "mtgspot.pl", "inline": True}
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
            return True, "Wiadomość testowa została pomyślnie wysłana na Discord!"
        else:
            return False, f"Discord zwrócił błąd HTTP {resp.status_code}: {resp.text}"
    except Exception as e:
        logger.error(f"Failed to send test Discord webhook: {e}")
        return False, f"Błąd połączenia z Discordem: {e}"


def send_discord_card_alert(
    webhook_url: str,
    card: Dict[str, Any],
    rule: Optional[Dict[str, Any]] = None
) -> Tuple[bool, str]:
    """
    Send a rich Discord Embed when a watched card is in-stock on MTG Spot.
    """
    if not webhook_url:
        return False, "Brak skonfigurowanego Webhook URL"

    title = card.get("title", "Karta MTG")
    price = float(card.get("price", 0.0))
    stock = int(card.get("stock", 0))
    expansion = card.get("expansion_name", "Nieznany dodatek")
    condition = card.get("condition", "NM")
    is_foil = card.get("is_foil", False)
    rarity = card.get("rarity", "Common")
    image_url = card.get("image", "")
    card_url = card.get("card_url") or "https://mtgspot.pl/single"

    foil_text = "✨ Foil (Foil)" if is_foil else "Regular (Zwykła)"
    color = 0x10B981 if not is_foil else 0xF59E0B  # Emerald green or Gold for foil

    rule_note = ""
    if rule:
        max_p = rule.get("max_price")
        if max_p is not None:
            rule_note = f" *(Limit ceny: do {max_p:.2f} zł)*"

    fields = [
        {
            "name": "💰 Cena",
            "value": f"**{price:.2f} zł**",
            "inline": True
        },
        {
            "name": "📦 W magazynie",
            "value": f"**{stock} szt.**",
            "inline": True
        },
        {
            "name": "🔍 Stan karty",
            "value": f"**{condition}**",
            "inline": True
        },
        {
            "name": "✨ Wykończenie",
            "value": foil_text,
            "inline": True
        },
        {
            "name": "🏷️ Dodatek / Set",
            "value": expansion,
            "inline": True
        },
        {
            "name": "⚡ Rzadkość",
            "value": rarity.capitalize() if rarity else "Nieznana",
            "inline": True
        }
    ]

    if rule and rule.get("list_name"):
        fields.append({
            "name": "📋 Wishlista / Talia",
            "value": f"**{rule['list_name']}**",
            "inline": True
        })

    embed = {
        "title": f"🎯 Poszukiwana karta dostępna: {title}!",
        "url": card_url,
        "description": f"Właśnie pojawiła się na stanie w **MTG Spot** w cenie **{price:.2f} zł**!{rule_note}",
        "color": color,
        "thumbnail": {
            "url": image_url
        } if image_url else {},
        "fields": fields,
        "footer": {
            "text": "MTG Spot Watcher • Kliknij tytuł lub link powyżej, aby przejść do zakupu"
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
            return True, "Powiadomienie wysłane"
        else:
            msg = f"Discord webhook returned HTTP {resp.status_code}: {resp.text}"
            logger.warning(msg)
            return False, msg
    except Exception as e:
        msg = f"Błąd wysyłania powiadomienia Discord: {e}"
        logger.error(msg)
        return False, msg
