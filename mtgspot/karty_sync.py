import hashlib
import hmac
import logging
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from .db import Database
from .scraper import GATEWAY_API_URL, MTGSpotScraper

logger = logging.getLogger("mtgspot.karty_sync")

DEFAULT_SYNC_INTERVAL_SECONDS = 300

# Condition hierarchy from strictest (NM) to broadest
CONDITION_ACCEPTANCE_MAP: Dict[str, Set[str]] = {
    "M": {"M", "MINT", "NM", "NEAR MINT"},
    "MINT": {"M", "MINT", "NM", "NEAR MINT"},
    "NM": {"M", "MINT", "NM", "NEAR MINT"},
    "NEAR MINT": {"M", "MINT", "NM", "NEAR MINT"},
    "EX": {"M", "MINT", "NM", "NEAR MINT", "EX", "EXCELLENT", "LP/EX"},
    "EXCELLENT": {"M", "MINT", "NM", "NEAR MINT", "EX", "EXCELLENT", "LP/EX"},
    "LP": {"M", "MINT", "NM", "NEAR MINT", "EX", "EXCELLENT", "LP", "LP/EX", "LIGHTLY PLAYED"},
    "LP/EX": {"M", "MINT", "NM", "NEAR MINT", "EX", "EXCELLENT", "LP", "LP/EX", "LIGHTLY PLAYED"},
    "LIGHTLY PLAYED": {"M", "MINT", "NM", "NEAR MINT", "EX", "EXCELLENT", "LP", "LP/EX", "LIGHTLY PLAYED"},
    "GD": {"M", "MINT", "NM", "NEAR MINT", "EX", "EXCELLENT", "LP", "LP/EX", "LIGHTLY PLAYED", "GD", "GOOD"},
    "GOOD": {"M", "MINT", "NM", "NEAR MINT", "EX", "EXCELLENT", "LP", "LP/EX", "LIGHTLY PLAYED", "GD", "GOOD"},
    "MP": {
        "M", "MINT", "NM", "NEAR MINT", "EX", "EXCELLENT",
        "LP", "LP/EX", "LIGHTLY PLAYED", "GD", "GOOD",
        "MP", "MODERATELY PLAYED", "PL", "PLAYED",
    },
    "PL": {
        "M", "MINT", "NM", "NEAR MINT", "EX", "EXCELLENT",
        "LP", "LP/EX", "LIGHTLY PLAYED", "GD", "GOOD",
        "MP", "MODERATELY PLAYED", "PL", "PLAYED",
    },
    "HP": {
        "M", "MINT", "NM", "NEAR MINT", "EX", "EXCELLENT",
        "LP", "LP/EX", "LIGHTLY PLAYED", "GD", "GOOD",
        "MP", "MODERATELY PLAYED", "PL", "PLAYED", "HP", "HEAVILY PLAYED",
    },
}

LANGUAGE_ALIASES: Dict[str, str] = {
    "en": "en",
    "eng": "en",
    "english": "en",
    "angielski": "en",
    "pl": "pl",
    "pol": "pl",
    "polish": "pl",
    "polski": "pl",
    "de": "de",
    "ger": "de",
    "german": "de",
    "niemiecki": "de",
    "fr": "fr",
    "fre": "fr",
    "french": "fr",
    "it": "it",
    "ita": "it",
    "italian": "it",
    "es": "es",
    "spa": "es",
    "spanish": "es",
    "pt": "pt",
    "por": "pt",
    "portuguese": "pt",
    "ja": "ja",
    "jp": "ja",
    "jpn": "ja",
    "japanese": "ja",
    "ru": "ru",
    "rus": "ru",
    "russian": "ru",
    "ko": "ko",
    "kor": "ko",
    "korean": "ko",
    "zhs": "zhs",
    "simplified chinese": "zhs",
    "chinese simplified": "zhs",
    "zht": "zht",
    "traditional chinese": "zht",
    "chinese traditional": "zht",
    "ph": "ph",
    "phyrexian": "ph",
}

VARIANT_KEYWORDS = (
    "borderless",
    "showcase",
    "extended art",
    "extended-art",
    "retro frame",
    "retro",
    "etched",
    "foil etched",
    "textured",
    "surge foil",
    "halo foil",
    "gilded foil",
    "step-and-compleat",
    "serialized",
    "serial",
    "poster",
    "anime",
    "prerelease",
    "promo",
)


def redact_secrets(text: str, token: Optional[str] = None) -> str:
    """Ensure sync token or Bearer tokens are never exposed in logs or error strings."""
    if not text:
        return ""
    sanitized = str(text)
    if token and len(token.strip()) >= 3:
        sanitized = sanitized.replace(token.strip(), "[REDACTED]")
    sanitized = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", sanitized, flags=re.IGNORECASE)
    return sanitized


@dataclass
class KartySyncConfig:
    api_url: str
    sync_token: str
    interval_seconds: int = DEFAULT_SYNC_INTERVAL_SECONDS
    enabled: bool = True
    timeout_seconds: int = 20
    max_retries: int = 3
    require_https: bool = False

    def __repr__(self) -> str:
        return (
            f"KartySyncConfig(api_url={self.api_url!r}, sync_token='***', "
            f"interval_seconds={self.interval_seconds}, enabled={self.enabled})"
        )

    def validate(self) -> None:
        if not self.enabled:
            return

        if not self.api_url or not self.sync_token:
            raise ValueError(
                "Karty integration requires both KARTY_API_URL and KARTY_SYNC_TOKEN to be configured."
            )

        parsed = urllib.parse.urlparse(self.api_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Invalid KARTY_API_URL: {self.api_url!r} (must be a valid HTTP/HTTPS URL).")

        hostname = (parsed.hostname or "").lower()
        is_local = hostname in ("localhost", "127.0.0.1", "::1")
        if self.require_https and parsed.scheme != "https" and not is_local:
            raise ValueError("KARTY_API_URL must use HTTPS in production environment.")

        if len(self.sync_token.strip()) < 8:
            raise ValueError("KARTY_SYNC_TOKEN must be at least 8 characters long.")

        if not isinstance(self.interval_seconds, int) or self.interval_seconds < 1:
            raise ValueError("MTGSPOT_KARTY_SYNC_INTERVAL_SECONDS must be a positive integer.")

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "KartySyncConfig":
        source = env if env is not None else os.environ
        enabled_raw = source.get("MTGSPOT_KARTY_SYNC_ENABLED", "").strip().lower()
        api_url = source.get("KARTY_API_URL", "").strip().rstrip("/")
        sync_token = source.get("KARTY_SYNC_TOKEN", "").strip()
        interval_raw = source.get(
            "MTGSPOT_KARTY_SYNC_INTERVAL_SECONDS",
            str(DEFAULT_SYNC_INTERVAL_SECONDS),
        ).strip()

        try:
            interval_seconds = int(interval_raw) if interval_raw else DEFAULT_SYNC_INTERVAL_SECONDS
        except ValueError as exc:
            raise ValueError(
                f"Invalid MTGSPOT_KARTY_SYNC_INTERVAL_SECONDS value: {interval_raw!r}"
            ) from exc

        if enabled_raw in ("0", "false", "no", "off"):
            return cls(
                api_url=api_url,
                sync_token=sync_token,
                interval_seconds=max(1, interval_seconds),
                enabled=False,
            )

        # If neither KARTY_API_URL nor KARTY_SYNC_TOKEN is set and not explicitly forced on, disable cleanly
        if not api_url and not sync_token and enabled_raw not in ("1", "true", "yes", "on"):
            return cls(
                api_url="",
                sync_token="",
                interval_seconds=max(1, interval_seconds),
                enabled=False,
            )

        env_mode = source.get("MTGSPOT_ENV", source.get("NODE_ENV", "")).strip().lower()
        allow_http = source.get("KARTY_ALLOW_HTTP", "").strip().lower() in ("1", "true", "yes")
        require_https = (env_mode == "production") and not allow_http

        cfg = cls(
            api_url=api_url,
            sync_token=sync_token,
            interval_seconds=interval_seconds,
            enabled=True,
            require_https=require_https,
        )
        cfg.validate()
        return cfg


def normalize_card_text(text: Optional[str]) -> str:
    if not text:
        return ""
    s = text.lower().replace("’", "'").replace("`", "'")
    s = re.sub(r"\s*//\s*", " / ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_title_parts(raw_title: str) -> Tuple[str, Set[str], Optional[str]]:
    """
    Extract (base_card_name, variant_tags, collector_number) from an MTGSpot card title.
    Example:
      "Sheoldred, the Apocalypse (Showcase) #290" ->
        ("sheoldred, the apocalypse", {"showcase"}, "290")
    """
    if not raw_title:
        return "", set(), None

    working = raw_title.strip()
    variants: Set[str] = set()
    collector_num: Optional[str] = None

    # Extract #123 collector number patterns
    hash_match = re.search(r"#\s*([0-9]+[a-zA-Z]?)\b", working)
    if hash_match:
        collector_num = hash_match.group(1).lower()
        working = (working[:hash_match.start()] + " " + working[hash_match.end():]).strip()

    # Extract parenthetical or bracketed annotations e.g. (Borderless), (107), [Showcase]
    for match in re.finditer(r"[\(\[]([^\)\]]+)[\)\]]", working):
        inner = match.group(1).strip().lower()
        if re.fullmatch(r"#?\s*([0-9]+[a-z]?)", inner):
            if not collector_num:
                collector_num = inner.lstrip("#").strip()
        else:
            for kw in VARIANT_KEYWORDS:
                if kw in inner:
                    variants.add(kw)
            variants.add(inner)

    # Strip parenthetical/bracketed suffixes from base title
    base = re.sub(r"\s*[\(\[][^\)\]]+[\)\]]", "", working)
    # Also check trailing " - Borderless" / " - Showcase"
    dash_parts = re.split(r"\s+-\s+", base)
    if len(dash_parts) > 1:
        tail = dash_parts[-1].strip().lower()
        for kw in VARIANT_KEYWORDS:
            if kw in tail:
                variants.add(kw)
                base = " - ".join(dash_parts[:-1])
                break

    norm_base = normalize_card_text(base)
    for kw in VARIANT_KEYWORDS:
        if kw in raw_title.lower():
            variants.add(kw)

    return norm_base, variants, collector_num


def extract_collector_number_from_description(description: Optional[str]) -> Optional[str]:
    if not description:
        return None
    m = re.search(r"(?:#|nr\.?\s*|collector\s*number[:\s]*)([0-9]+[a-zA-Z]?)\b", description, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return None


def extract_set_code_from_image_url(image_url: Optional[str]) -> Optional[str]:
    if not image_url:
        return None
    # e.g. https://gateway.mtgspot.pl/images/singles/DMU/67890.jpg
    m = re.search(r"/images/singles/([A-Za-z0-9]{2,6})/", image_url)
    if m:
        return m.group(1).lower()
    return None


def normalize_expansion_name(name: Optional[str]) -> Tuple[str, bool]:
    """
    Normalize expansion name and detect whether it is an ': Extras' / 'Promos' sub-set.
    Returns (normalized_base_name, is_extras_or_promo).
    """
    if not name:
        return "", False
    lower = name.lower().replace("’", "'").strip()
    is_extras = False
    if re.search(r"(:|\s+-)?\s*(extras|promos|variants)\b", lower):
        is_extras = True
        lower = re.sub(r"(:|\s+-)?\s*(extras|promos|variants)\b.*$", "", lower).strip()
    lower = re.sub(r"[^a-z0-9]+", " ", lower).strip()
    return lower, is_extras


def build_offer_external_id(offer: Dict[str, Any]) -> str:
    """
    Build a stable, deterministic identifier for an MTGSpot offer.
    Uses MTGSpot's native offer/article ID when available, or a deterministic SHA-256 fallback.
    """
    raw_id = offer.get("id")
    if raw_id is not None and str(raw_id).strip():
        return str(raw_id).strip()

    id_article = offer.get("id_article")
    if id_article is not None and str(id_article).strip():
        return f"article_{str(id_article).strip()}"

    canonical = "|".join([
        normalize_card_text(offer.get("title", "")),
        (offer.get("expansion_name") or "").strip().lower(),
        (offer.get("condition") or "NM").strip().upper(),
        "foil" if offer.get("is_foil") else "nonfoil",
        (offer.get("language") or "English").strip().lower(),
        str(offer.get("id_product") or ""),
        (offer.get("card_url") or "").strip().lower(),
    ])
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"det_{digest}"


def is_condition_acceptable(offer_condition: Optional[str], required_condition: Optional[str]) -> bool:
    if not required_condition or not required_condition.strip():
        return True
    req_clean = required_condition.strip().upper()
    if req_clean in ("ANY", "ALL", "*"):
        return True

    offer_clean = (offer_condition or "NM").strip().upper()

    # If comma-separated explicit list (e.g. "NM,EX")
    if "," in req_clean:
        allowed = {c.strip() for c in req_clean.split(",") if c.strip()}
        return offer_clean in allowed

    if req_clean in CONDITION_ACCEPTANCE_MAP:
        return offer_clean in CONDITION_ACCEPTANCE_MAP[req_clean]

    return offer_clean == req_clean


def is_language_acceptable(offer_language: Optional[str], required_language: Optional[str]) -> bool:
    if not required_language or not required_language.strip():
        return True
    req_norm = LANGUAGE_ALIASES.get(required_language.strip().lower(), required_language.strip().lower())
    if req_norm in ("any", "all", "*"):
        return True

    if not offer_language or not offer_language.strip():
        return True

    offer_norm = LANGUAGE_ALIASES.get(offer_language.strip().lower(), offer_language.strip().lower())
    return offer_norm == req_norm


def match_offer_to_buylist_item(
    offer: Dict[str, Any],
    wantlist_item: Dict[str, Any],
    expansion_code_map: Optional[Dict[int, str]] = None,
    expansion_name_to_code: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Evaluate whether a parsed MTGSpot offer matches the given Karty buylist item.
    Returns a formatted Karty offer payload dict if matched, or None if it does not match.
    Never merges distinct printings/expansions merely because they share a card name.
    """
    stock = int(offer.get("stock") or 0)
    price = float(offer.get("price") or 0.0)
    if stock <= 0 or price <= 0:
        return None

    target_name_raw = wantlist_item.get("card_name") or ""
    target_base, target_variants, target_title_num = extract_title_parts(target_name_raw)
    if not target_base:
        return None

    offer_title = offer.get("title") or ""
    offer_base, offer_variants, offer_title_num = extract_title_parts(offer_title)
    if not offer_base:
        return None

    # 1. Exact base card name match (or front-face match for double-faced/split cards)
    name_matches = (offer_base == target_base)
    if not name_matches and " / " in offer_base and " / " not in target_base:
        front_face = offer_base.split(" / ")[0].strip()
        if front_face == target_base:
            name_matches = True
    elif not name_matches and " / " in target_base and " / " not in offer_base:
        front_face = target_base.split(" / ")[0].strip()
        if front_face == offer_base:
            name_matches = True

    if not name_matches:
        return None

    # 2. Foil check
    req_foil = wantlist_item.get("is_foil")
    offer_foil = bool(offer.get("is_foil", False))
    if req_foil is True and not offer_foil:
        return None
    if req_foil is False and offer_foil:
        return None

    # 3. Condition check
    req_cond = wantlist_item.get("condition")
    if not is_condition_acceptable(offer.get("condition"), req_cond):
        return None

    # 4. Language check
    req_lang = wantlist_item.get("language")
    if not is_language_acceptable(offer.get("language"), req_lang):
        return None

    # 5. Expansion / Set & Printing check
    req_set_code = (wantlist_item.get("set_code") or "").strip().lower() or None
    req_set_name = (wantlist_item.get("set_name") or "").strip() or None
    req_expansion = (wantlist_item.get("expansion") or "").strip() or None
    req_collector_num = (
        (wantlist_item.get("collector_number") or "").strip().lower()
        or target_title_num
        or None
    )
    req_scryfall_id = (wantlist_item.get("scryfall_id") or "").strip() or None

    offer_exp_name = (offer.get("expansion_name") or "").strip()
    offer_exp_id = offer.get("id_expansion")
    offer_set_code = extract_set_code_from_image_url(offer.get("image"))
    if not offer_set_code and expansion_code_map and offer_exp_id is not None:
        try:
            offer_set_code = expansion_code_map.get(int(offer_exp_id))
        except (ValueError, TypeError):
            pass
    if not offer_set_code and expansion_name_to_code and offer_exp_name:
        offer_set_code = expansion_name_to_code.get(offer_exp_name.lower())

    offer_exp_norm, offer_is_extras = normalize_expansion_name(offer_exp_name)

    has_expansion_requirement = bool(req_set_code or req_set_name or req_expansion)
    if has_expansion_requirement:
        exp_matched = False

        # Match by set code if both sides have set_code
        if req_set_code and offer_set_code:
            if req_set_code == offer_set_code.lower():
                exp_matched = True
            else:
                # Explicit set code mismatch -> different expansion, reject!
                return None

        # Match by expansion name
        if not exp_matched:
            candidate_names = [n for n in (req_set_name, req_expansion) if n]
            for cand in candidate_names:
                cand_norm, _ = normalize_expansion_name(cand)
                if cand_norm and offer_exp_norm and cand_norm == offer_exp_norm:
                    exp_matched = True
                    break
                # Also check if candidate was passed as a short set code in `expansion`
                if offer_set_code and cand.strip().lower() == offer_set_code.lower():
                    exp_matched = True
                    break

        if not exp_matched:
            return None

    # 6. Collector number & variant printing check
    offer_collector_num = offer_title_num or extract_collector_number_from_description(
        offer.get("description")
    )

    if req_collector_num and offer_collector_num:
        if req_collector_num != offer_collector_num:
            # Explicit collector number mismatch -> different printing within the set!
            return None

    # Check if offer has special variant tags (Showcase, Borderless, Extended Art, Retro) or ': Extras'
    # when the buylist item did NOT request those variants
    unrequested_variants = offer_variants - target_variants
    if (offer_is_extras or unrequested_variants) and (req_collector_num or req_scryfall_id):
        # If the offer doesn't explicitly match the requested collector number, do not merge
        # an ': Extras' / Showcase / Borderless printing into a standard printing request!
        if not (req_collector_num and offer_collector_num and req_collector_num == offer_collector_num):
            return None

    requires_verification = False
    verification_note: Optional[str] = None

    if (req_collector_num or req_scryfall_id) and not offer_collector_num:
        # MTGSpot does not provide collector_number or scryfall_id in this article payload
        requires_verification = True
        verification_note = (
            "MTGSpot nie udostępnia numeru kolekcjonerskiego ani Scryfall ID — "
            "potwierdzono nazwę i dodatek, zweryfikuj dokładny print przed zakupem."
        )

    scraped_at = offer.get("scraped_at")
    if not scraped_at:
        scraped_at = datetime.now(timezone.utc).isoformat()

    return {
        "external_id": build_offer_external_id(offer),
        "buylist_item_id": int(wantlist_item["id"]),
        "card_name": offer_title or wantlist_item["card_name"],
        "price": round(price, 2),
        "stock": stock,
        "condition": offer.get("condition") or "NM",
        "is_foil": offer_foil,
        "expansion": offer_exp_name or req_set_name or req_expansion,
        "card_url": offer.get("card_url") or None,
        "image_url": offer.get("image") or None,
        "language": offer.get("language") or None,
        "requires_verification": requires_verification,
        "verification_note": verification_note,
        "scraped_at": str(scraped_at),
    }


class KartySyncService:
    """
    Background synchronizer between Karty (buylist source of truth) and MTGSpot Scraper.
    Completely isolated from manual Wishlists/Watchlist and Discord notifications.
    """

    def __init__(
        self,
        db: Database,
        scraper: MTGSpotScraper,
        config: Optional[KartySyncConfig] = None,
    ):
        self.db = db
        self.scraper = scraper
        self.config = config or KartySyncConfig.from_env()
        self._sync_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.session = requests.Session()

        self.status: str = "disabled" if not self.config.enabled else "idle"
        self.last_attempt_at: Optional[str] = None
        self.last_success_at: Optional[str] = None
        self.last_items_fetched: int = 0
        self.last_items_checked: int = 0
        self.last_offers_found: int = 0
        self.last_errors_count: int = 0
        self.last_error: Optional[str] = None

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.sync_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def verify_request_token(self, auth_header: Optional[str]) -> bool:
        if not self.config.sync_token or not auth_header:
            return False
        m = re.match(r"^Bearer\s+(.+)$", auth_header.strip(), flags=re.IGNORECASE)
        if not m:
            return False
        provided = m.group(1).strip()
        return hmac.compare_digest(
            provided.encode("utf-8"),
            self.config.sync_token.strip().encode("utf-8"),
        )

    def _request_karty_api(
        self,
        method: str,
        path: str,
        json_payload: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        url = f"{self.config.api_url.rstrip('/')}/{path.lstrip('/')}"
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = self.session.request(
                    method=method,
                    url=url,
                    headers=self._auth_headers(),
                    json=json_payload,
                    timeout=self.config.timeout_seconds,
                )
                # Do NOT retry on auth or validation errors (400, 401, 403, 404, 422)
                if resp.status_code in (400, 401, 403, 404, 422):
                    return resp

                if resp.status_code in (200, 201):
                    return resp

                # Transient HTTP error (e.g. 429, 500, 502, 503, 504)
                logger.warning(
                    "Transient HTTP %s from Karty API (%s %s) on attempt %d/%d",
                    resp.status_code,
                    method,
                    path,
                    attempt,
                    self.config.max_retries,
                )
                if attempt == self.config.max_retries:
                    return resp
                time.sleep(min(5.0, attempt * 1.0))

            except (requests.Timeout, requests.ConnectionError, requests.RequestException) as exc:
                last_exc = exc
                safe_err = redact_secrets(str(exc), self.config.sync_token)
                logger.warning(
                    "Network error calling Karty API (%s %s) on attempt %d/%d: %s",
                    method,
                    path,
                    attempt,
                    self.config.max_retries,
                    safe_err,
                )
                if attempt == self.config.max_retries:
                    raise RuntimeError(f"Karty API request failed: {safe_err}") from exc
                time.sleep(min(5.0, attempt * 1.0))

        if last_exc:
            raise RuntimeError(redact_secrets(str(last_exc), self.config.sync_token))
        raise RuntimeError("Karty API request failed after retries")

    def fetch_wantlist(self) -> List[Dict[str, Any]]:
        resp = self._request_karty_api("GET", "/api/integrations/mtgspot/wantlist")
        if resp.status_code in (401, 403):
            raise PermissionError(
                f"Authorization failed when fetching Karty wantlist (HTTP {resp.status_code})"
            )
        if resp.status_code != 200:
            safe_body = redact_secrets(resp.text[:200], self.config.sync_token)
            raise RuntimeError(
                f"Failed to fetch Karty wantlist: HTTP {resp.status_code} ({safe_body})"
            )

        try:
            payload = resp.json()
        except Exception as exc:
            raise ValueError("Invalid JSON response from Karty wantlist endpoint") from exc

        if isinstance(payload, list):
            raw_items = payload
        elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
            raw_items = payload["items"]
        else:
            raise ValueError("Unexpected Karty wantlist payload structure (missing 'items' list)")

        active_items: List[Dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                raise ValueError("Invalid wantlist item format (expected object)")
            if "id" not in item or "card_name" not in item:
                raise ValueError("Wantlist item is missing required 'id' or 'card_name' field")
            if item.get("active", True) is False:
                continue
            active_items.append(item)

        return active_items

    def _load_expansion_maps(self) -> Tuple[Dict[int, str], Dict[str, str]]:
        id_to_code: Dict[int, str] = {}
        name_to_code: Dict[str, str] = {}
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute("SELECT id, name, code FROM expansions").fetchall()
                for row in rows:
                    code = (row["code"] or "").strip().lower()
                    name = (row["name"] or "").strip().lower()
                    if code:
                        if row["id"] is not None:
                            id_to_code[int(row["id"])] = code
                        if name:
                            name_to_code[name] = code
        except Exception:
            pass
        return id_to_code, name_to_code

    def search_offers_for_buylist_item(
        self,
        wantlist_item: Dict[str, Any],
        expansion_code_map: Optional[Dict[int, str]] = None,
        expansion_name_to_code: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
        """
        Query MTGSpot for a single Karty buylist item and parse/match all offers.
        Returns: (scan_succeeded, matched_offers, error_message)
        CRITICAL: scan_succeeded is True ONLY if the HTTP request, JSON response structure,
        and card parsing all completed without any error.
        """
        card_name = (wantlist_item.get("card_name") or "").strip()
        if not card_name:
            return False, [], "Empty card_name in wantlist item"

        url = f"{GATEWAY_API_URL}/api/shop/articles"
        params: Dict[str, Any] = {
            "page[limit]": 100,
            "page[offset]": 0,
            "filter[is_in_channel]": 1,
            "filter[name]": card_name,
            "sort": "price",
        }
        req_foil = wantlist_item.get("is_foil")
        if req_foil is True:
            params["filter[is_foil]"] = 1
        elif req_foil is False:
            params["filter[is_foil]"] = 0

        raw_articles: List[Dict[str, Any]] = []
        last_err: Optional[str] = None

        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = self.scraper.session.get(
                    url,
                    params=params,
                    timeout=self.scraper.timeout or self.config.timeout_seconds,
                )
                if resp.status_code in (401, 403, 400, 422):
                    return (
                        False,
                        [],
                        f"MTGSpot returned HTTP {resp.status_code} for '{card_name}'",
                    )

                if resp.status_code != 200:
                    last_err = f"MTGSpot returned HTTP {resp.status_code} for '{card_name}'"
                    if attempt == self.config.max_retries:
                        return False, [], last_err
                    time.sleep(min(3.0, attempt * 1.0))
                    continue

                payload = resp.json()
                if not isinstance(payload, dict) or "response" not in payload:
                    return False, [], f"Incomplete MTGSpot JSON response for '{card_name}' (missing 'response')"

                response_obj = payload.get("response")
                if not isinstance(response_obj, dict) or "data" not in response_obj:
                    return False, [], f"Incomplete MTGSpot JSON response for '{card_name}' (missing 'data')"

                data_list = response_obj.get("data")
                if not isinstance(data_list, list):
                    return False, [], f"Invalid MTGSpot 'data' field for '{card_name}' (expected list)"

                raw_articles = data_list
                last_err = None
                break

            except (ValueError, KeyError) as parse_exc:
                return False, [], f"Failed to decode MTGSpot response for '{card_name}': {parse_exc}"
            except Exception as req_exc:
                last_err = f"Error querying MTGSpot for '{card_name}': {req_exc}"
                if attempt == self.config.max_retries:
                    return False, [], last_err
                time.sleep(min(3.0, attempt * 1.0))

        if last_err is not None:
            return False, [], last_err

        # Parse and match each article; if parsing fails on malformed item, abort scan_succeeded!
        matched_offers: List[Dict[str, Any]] = []
        seen_external_ids: Set[str] = set()
        parsed_cards_list: List[Dict[str, Any]] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            for raw_item in raw_articles:
                if not isinstance(raw_item, dict) or not isinstance(raw_item.get("attributes"), dict):
                    return False, [], f"Malformed article item in MTGSpot response for '{card_name}'"

                parsed_card = self.scraper.parse_card_item(raw_item)
                parsed_card["scraped_at"] = now_iso
                parsed_cards_list.append(parsed_card)

                matched = match_offer_to_buylist_item(
                    parsed_card,
                    wantlist_item,
                    expansion_code_map=expansion_code_map,
                    expansion_name_to_code=expansion_name_to_code,
                )
                if matched is not None:
                    ext_id = matched["external_id"]
                    if ext_id not in seen_external_ids:
                        seen_external_ids.add(ext_id)
                        matched_offers.append(matched)

            # Fallback: if Scryfall populated default set/collector_number on the buylist item
            # and MTGSpot only has other printings/expansions of this card in stock, include them
            # with requires_verification=True so the admin sees all available printings.
            if not matched_offers and parsed_cards_list and not wantlist_item.get("strict_expansion"):
                relaxed_item = dict(wantlist_item)
                orig_exp = (
                    wantlist_item.get("set_name")
                    or wantlist_item.get("expansion")
                    or (wantlist_item.get("set_code") or "").upper()
                )
                relaxed_item["set_code"] = None
                relaxed_item["set_name"] = None
                relaxed_item["expansion"] = None
                relaxed_item["collector_number"] = None
                relaxed_item["scryfall_id"] = None

                for parsed_card in parsed_cards_list:
                    matched_relaxed = match_offer_to_buylist_item(
                        parsed_card,
                        relaxed_item,
                        expansion_code_map=expansion_code_map,
                        expansion_name_to_code=expansion_name_to_code,
                    )
                    if matched_relaxed is not None:
                        ext_id = matched_relaxed["external_id"]
                        if ext_id not in seen_external_ids:
                            seen_external_ids.add(ext_id)
                            offer_exp = parsed_card.get("expansion_name") or "nieznany dodatek"
                            matched_relaxed["requires_verification"] = True
                            matched_relaxed["verification_note"] = (
                                f"Inne wydanie na MTGSpot: {offer_exp}"
                                + (f" (w buyliście: {orig_exp})" if orig_exp else "")
                            )
                            matched_offers.append(matched_relaxed)
        except Exception as parse_err:
            return False, [], f"Parser error while processing '{card_name}': {parse_err}"

        matched_offers.sort(key=lambda o: (o["price"], -o["stock"]))
        return True, matched_offers, None

    def send_offers_to_karty(
        self,
        offers: List[Dict[str, Any]],
        synced_buylist_item_ids: List[int],
        items_checked: int,
        errors_count: int,
        last_error: Optional[str] = None,
        sync_completed: Optional[bool] = None,
    ) -> Dict[str, Any]:
        status_hint = "ok" if errors_count == 0 else ("partial" if synced_buylist_item_ids else "error")
        completed_flag = (
            sync_completed
            if sync_completed is not None
            else (len(synced_buylist_item_ids) > 0 and status_hint != "error")
        )
        payload = {
            "offers": offers,
            "synced_buylist_item_ids": synced_buylist_item_ids,
            "sync_completed": completed_flag,
            "sync_meta": {
                "status": status_hint,
                "items_checked": items_checked,
                "offers_found": len(offers),
                "errors_count": errors_count,
                "last_error": redact_secrets(last_error or "", self.config.sync_token) if last_error else None,
            },
        }

        resp = self._request_karty_api("POST", "/api/integrations/mtgspot/offers", json_payload=payload)
        if resp.status_code in (401, 403):
            raise PermissionError(
                f"Authorization failed when posting offers to Karty (HTTP {resp.status_code})"
            )
        if resp.status_code not in (200, 201):
            safe_body = redact_secrets(resp.text[:200], self.config.sync_token)
            raise RuntimeError(
                f"Failed to send offers to Karty: HTTP {resp.status_code} ({safe_body})"
            )
        return resp.json()

    def run_sync_once(self) -> Dict[str, Any]:
        """
        Execute one full synchronization cycle.
        Prevents concurrent executions via a non-blocking lock acquisition.
        """
        if not self.config.enabled:
            return {
                "status": "disabled",
                "message": "Karty integration is disabled (missing configuration or disabled by flag).",
            }

        if not self._sync_lock.acquire(blocking=False):
            logger.info("Karty synchronization is already running; skipping overlapping run.")
            return {
                "status": "already_running",
                "message": "Synchronization is already in progress.",
                "wantlist_fetched": self.last_items_fetched,
                "items_checked": self.last_items_checked,
                "offers_found": self.last_offers_found,
            }

        now_iso = datetime.now(timezone.utc).isoformat()
        self.last_attempt_at = now_iso
        self.status = "running"

        try:
            wantlist = self.fetch_wantlist()
            self.last_items_fetched = len(wantlist)
            self.last_items_checked = 0
            self.last_offers_found = 0

            expansion_code_map, expansion_name_to_code = self._load_expansion_maps()

            all_matched_offers: List[Dict[str, Any]] = []
            synced_item_ids: List[int] = []
            errors: List[str] = []
            items_checked = 0

            for item in wantlist:
                item_id = int(item["id"])
                items_checked += 1
                succeeded, item_offers, item_err = self.search_offers_for_buylist_item(
                    item,
                    expansion_code_map=expansion_code_map,
                    expansion_name_to_code=expansion_name_to_code,
                )
                if succeeded:
                    synced_item_ids.append(item_id)
                    all_matched_offers.extend(item_offers)
                else:
                    safe_err = redact_secrets(item_err or "Unknown MTGSpot error", self.config.sync_token)
                    logger.warning(
                        "Skipping unavailable marking for buylist item #%d (%s) due to error: %s",
                        item_id,
                        item.get("card_name"),
                        safe_err,
                    )
                    errors.append(safe_err)

                self.last_items_checked = items_checked
                self.last_offers_found = len(all_matched_offers)

                # Send incremental progress every 10 items on larger buylists so Karty UI updates live
                if len(wantlist) > 10 and items_checked < len(wantlist) and items_checked % 10 == 0:
                    try:
                        self.send_offers_to_karty(
                            offers=all_matched_offers,
                            synced_buylist_item_ids=synced_item_ids,
                            items_checked=items_checked,
                            errors_count=len(errors),
                            last_error=errors[0] if errors else None,
                            sync_completed=False,
                        )
                    except Exception as inc_exc:
                        logger.warning(
                            "Incremental progress update to Karty failed: %s",
                            redact_secrets(str(inc_exc), self.config.sync_token),
                        )

            last_err_msg = errors[0] if errors else None
            post_result = self.send_offers_to_karty(
                offers=all_matched_offers,
                synced_buylist_item_ids=synced_item_ids,
                items_checked=items_checked,
                errors_count=len(errors),
                last_error=last_err_msg,
            )

            self.last_items_checked = items_checked
            self.last_offers_found = len(all_matched_offers)
            self.last_errors_count = len(errors)
            self.last_error = last_err_msg

            if len(errors) == 0:
                self.status = "ok"
                self.last_success_at = datetime.now(timezone.utc).isoformat()
            elif synced_item_ids:
                self.status = "partial"
                self.last_success_at = datetime.now(timezone.utc).isoformat()
            else:
                self.status = "error"

            logger.info(
                "Karty sync finished: wantlist_fetched=%d, checked=%d, offers_found=%d, errors=%d",
                len(wantlist),
                items_checked,
                len(all_matched_offers),
                len(errors),
            )

            return {
                "status": self.status,
                "wantlist_fetched": len(wantlist),
                "items_checked": items_checked,
                "synced_buylist_item_ids": synced_item_ids,
                "offers_found": len(all_matched_offers),
                "errors_count": len(errors),
                "last_error": self.last_error,
                "karty_response": post_result,
            }

        except Exception as exc:
            safe_err = redact_secrets(str(exc), self.config.sync_token)
            self.status = "error"
            self.last_error = safe_err
            self.last_errors_count += 1
            logger.error("Karty synchronization failed: %s", safe_err)
            return {
                "status": "error",
                "wantlist_fetched": self.last_items_fetched,
                "items_checked": self.last_items_checked,
                "offers_found": 0,
                "errors_count": self.last_errors_count,
                "last_error": safe_err,
            }
        finally:
            self._sync_lock.release()

    def get_status(self) -> Dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "status": self.status,
            "interval_seconds": self.config.interval_seconds,
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "last_items_fetched": self.last_items_fetched,
            "last_items_checked": self.last_items_checked,
            "last_offers_found": self.last_offers_found,
            "last_errors_count": self.last_errors_count,
            "last_error": self.last_error,
            "is_thread_running": bool(self._thread and self._thread.is_alive()),
        }

    def _loop(self) -> None:
        logger.info(
            "KartySyncService background loop started (interval=%ds).",
            self.config.interval_seconds,
        )
        while not self._stop_event.is_set():
            try:
                self.run_sync_once()
            except Exception as exc:
                logger.error(
                    "Unexpected error in KartySyncService loop: %s",
                    redact_secrets(str(exc), self.config.sync_token),
                )

            for _ in range(max(1, self.config.interval_seconds)):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

        logger.info("KartySyncService background loop stopped.")

    def start(self) -> None:
        if not self.config.enabled:
            logger.info("KartySyncService is disabled (no KARTY_API_URL / KARTY_SYNC_TOKEN configured).")
            return
        if self._thread and self._thread.is_alive():
            logger.info("KartySyncService thread is already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="MTGSpotKartySyncThread",
        )
        self._thread.start()
        logger.info("KartySyncService started.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
            logger.info("KartySyncService stopped.")
