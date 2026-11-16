import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import requests
from fastapi.testclient import TestClient

from mtgspot.db import Database
from mtgspot.scraper import MTGSpotScraper
from mtgspot.karty_sync import (
    KartySyncConfig,
    KartySyncService,
    build_offer_external_id,
    match_offer_to_buylist_item,
    redact_secrets,
)
import mtgspot.api as api_module


class FakeResponse:
    def __init__(self, status_code: int, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (str(json_data) if json_data is not None else "")

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data


class TestKartySyncIntegration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_mtgspot.db")
        self.db = Database(self.db_path)
        self.scraper = MTGSpotScraper(db=self.db)
        self.config = KartySyncConfig(
            api_url="http://localhost:3000",
            sync_token="super-secret-sync-token-12345",
            interval_seconds=300,
            enabled=True,
            timeout_seconds=5,
            max_retries=2,
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    # ==================== 1. Configuration & Secret Redaction Tests ====================

    def test_config_validation_and_defaults(self):
        # Empty env -> cleanly disabled with default interval 300s
        cfg_disabled = KartySyncConfig.from_env({})
        self.assertFalse(cfg_disabled.enabled)
        self.assertEqual(cfg_disabled.interval_seconds, 300)

        # Missing token when URL is set -> raises ValueError
        with self.assertRaises(ValueError):
            KartySyncConfig.from_env({"KARTY_API_URL": "http://localhost:3000"})

        # Short token (< 8 chars) -> raises ValueError
        with self.assertRaises(ValueError):
            KartySyncConfig.from_env(
                {"KARTY_API_URL": "http://localhost:3000", "KARTY_SYNC_TOKEN": "short"}
            )

        # Invalid interval -> raises ValueError
        with self.assertRaises(ValueError):
            KartySyncConfig.from_env(
                {
                    "KARTY_API_URL": "http://localhost:3000",
                    "KARTY_SYNC_TOKEN": "valid-token-12345",
                    "MTGSPOT_KARTY_SYNC_INTERVAL_SECONDS": "0",
                }
            )

        # Production HTTPS requirement for non-local URLs
        with self.assertRaises(ValueError):
            KartySyncConfig.from_env(
                {
                    "KARTY_API_URL": "http://karty.example.com",
                    "KARTY_SYNC_TOKEN": "valid-token-12345",
                    "MTGSPOT_ENV": "production",
                }
            )

        cfg_prod = KartySyncConfig.from_env(
            {
                "KARTY_API_URL": "https://karty.example.com/",
                "KARTY_SYNC_TOKEN": "valid-token-12345",
                "MTGSPOT_ENV": "production",
            }
        )
        self.assertTrue(cfg_prod.enabled)
        self.assertEqual(cfg_prod.api_url, "https://karty.example.com")
        self.assertEqual(cfg_prod.interval_seconds, 300)
        self.assertNotIn("valid-token-12345", repr(cfg_prod))

    def test_secret_redaction(self):
        token = "secret-integration-token-999"
        raw = f"Request failed with Authorization: Bearer {token} and token={token}"
        sanitized = redact_secrets(raw, token)
        self.assertNotIn(token, sanitized)
        self.assertIn("[REDACTED]", sanitized)

    # ==================== 2. Matching & Printing Accuracy Tests ====================

    def test_match_offer_exact_name_foil_condition_language_and_expansion(self):
        want_item = {
            "id": 10,
            "card_name": "Sheoldred, the Apocalypse",
            "set_code": "dmu",
            "set_name": "Dominaria United",
            "collector_number": "107",
            "scryfall_id": "d67be074-cdd4-41d9-ac89-0a0456c4e4b2",
            "condition": "LP",
            "is_foil": False,
            "language": "en",
            "target_price": 250.0,
        }

        # Valid matching offer (EX is acceptable for LP, DMU matches set_code from image URL)
        valid_offer = {
            "id": "art_1001",
            "id_article": 1001,
            "id_product": 5001,
            "title": "Sheoldred, the Apocalypse",
            "description": "",
            "language": "English",
            "condition": "EX",
            "is_foil": False,
            "id_expansion": 400,
            "expansion_name": "Dominaria United",
            "stock": 2,
            "price": 235.50,
            "image": "https://gateway.mtgspot.pl/images/singles/DMU/5001.jpg",
            "card_url": "https://mtgspot.pl/single/dominaria-united/400/sheoldred-the-apocalypse/5001/1001",
            "scraped_at": "2026-11-16T10:00:00Z",
        }
        matched = match_offer_to_buylist_item(valid_offer, want_item)
        self.assertIsNotNone(matched)
        self.assertEqual(matched["external_id"], "art_1001")
        self.assertEqual(matched["buylist_item_id"], 10)
        self.assertEqual(matched["price"], 235.50)
        self.assertEqual(matched["stock"], 2)
        self.assertEqual(matched["condition"], "EX")
        self.assertFalse(matched["is_foil"])
        # Because MTGSpot offer does not specify collector_number #107, requires_verification must be True
        self.assertTrue(matched["requires_verification"])

        # Offer with explicit matching collector number in title (#107) -> requires_verification is False
        exact_num_offer = dict(valid_offer, title="Sheoldred, the Apocalypse #107")
        matched_exact = match_offer_to_buylist_item(exact_num_offer, want_item)
        self.assertIsNotNone(matched_exact)
        self.assertFalse(matched_exact["requires_verification"])

        # Offer with conflicting collector number (#290) -> MUST NOT match
        wrong_num_offer = dict(valid_offer, title="Sheoldred, the Apocalypse #290")
        self.assertIsNone(match_offer_to_buylist_item(wrong_num_offer, want_item))

        # Offer from ': Extras' or Showcase variant when standard #107 was requested -> MUST NOT match
        extras_offer = dict(valid_offer, expansion_name="Dominaria United: Extras")
        self.assertIsNone(match_offer_to_buylist_item(extras_offer, want_item))

        showcase_offer = dict(valid_offer, title="Sheoldred, the Apocalypse (Showcase)")
        self.assertIsNone(match_offer_to_buylist_item(showcase_offer, want_item))

        # Offer from a different expansion (e.g. Promo Pack / different set code) -> MUST NOT match
        other_set_offer = dict(
            valid_offer,
            expansion_name=" Commander Masters ",
            image="https://gateway.mtgspot.pl/images/singles/CMM/5001.jpg",
        )
        self.assertIsNone(match_offer_to_buylist_item(other_set_offer, want_item))

        # Foil mismatch -> MUST NOT match
        foil_offer = dict(valid_offer, is_foil=True)
        self.assertIsNone(match_offer_to_buylist_item(foil_offer, want_item))

        # Condition worse than LP (e.g. PL/PO) -> MUST NOT match
        poor_offer = dict(valid_offer, condition="PO")
        self.assertIsNone(match_offer_to_buylist_item(poor_offer, want_item))

        # Language mismatch (Japanese when English requested) -> MUST NOT match
        jp_offer = dict(valid_offer, language="Japanese")
        self.assertIsNone(match_offer_to_buylist_item(jp_offer, want_item))

        # Different card with substring overlap -> MUST NOT match
        other_card = dict(valid_offer, title="Sheoldred, Whispering One")
        self.assertIsNone(match_offer_to_buylist_item(other_card, want_item))

    def test_deterministic_external_id_generation(self):
        offer_with_id = {"id": "abc999", "id_article": 123, "title": "Sol Ring"}
        self.assertEqual(build_offer_external_id(offer_with_id), "abc999")

        offer_with_article = {"id": "", "id_article": 456, "title": "Sol Ring"}
        self.assertEqual(build_offer_external_id(offer_with_article), "article_456")

        offer_fallback_1 = {
            "title": "Sol Ring",
            "expansion_name": "Commander Masters",
            "condition": "NM",
            "is_foil": False,
            "language": "English",
            "id_product": 789,
            "card_url": "https://mtgspot.pl/single/sol-ring",
        }
        offer_fallback_2 = dict(offer_fallback_1, price=99.99, stock=5)
        # Deterministic ID must stay identical even when price and stock change!
        id1 = build_offer_external_id(offer_fallback_1)
        id2 = build_offer_external_id(offer_fallback_2)
        self.assertTrue(id1.startswith("det_"))
        self.assertEqual(id1, id2)

    # ==================== 3. Wantlist Parsing & Auth Error Handling ====================

    def test_fetch_wantlist_and_no_retry_on_401_403(self):
        service = KartySyncService(db=self.db, scraper=self.scraper, config=self.config)

        mock_session = MagicMock()
        mock_session.request.return_value = FakeResponse(
            200,
            {
                "items": [
                    {"id": 1, "card_name": "Lightning Bolt", "active": True, "is_foil": False},
                    {"id": 2, "card_name": "Inactive Card", "active": False},
                ]
            },
        )
        service.session = mock_session

        items = service.fetch_wantlist()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], 1)
        self.assertEqual(items[0]["card_name"], "Lightning Bolt")
        self.assertEqual(mock_session.request.call_count, 1)

        # 401/403 must fail immediately without retrying
        mock_session.reset_mock()
        mock_session.request.return_value = FakeResponse(403, {"error": "Forbidden"})
        with self.assertRaises(PermissionError):
            service.fetch_wantlist()
        self.assertEqual(mock_session.request.call_count, 1)

    # ==================== 4. Safeguard: 0 Results vs Error / Incomplete Scan ====================

    def test_zero_results_marks_item_synced_while_errors_do_not(self):
        service = KartySyncService(db=self.db, scraper=self.scraper, config=self.config)
        item = {"id": 42, "card_name": "Black Lotus", "is_foil": None}

        # Case A: Confirmed 0 results from MTGSpot (valid JSON with empty data list)
        self.scraper.session = MagicMock()
        self.scraper.session.get.return_value = FakeResponse(
            200, {"response": {"data": [], "total": 0}}
        )
        succeeded, offers, err = service.search_offers_for_buylist_item(item)
        self.assertTrue(succeeded)
        self.assertEqual(offers, [])
        self.assertIsNone(err)

        # Case B: HTTP 500 error from MTGSpot -> scan_succeeded MUST be False
        self.scraper.session.get.return_value = FakeResponse(500, text="Internal Server Error")
        succeeded_500, offers_500, err_500 = service.search_offers_for_buylist_item(item)
        self.assertFalse(succeeded_500)
        self.assertEqual(offers_500, [])
        self.assertIn("HTTP 500", err_500)

        # Case C: Network timeout -> scan_succeeded MUST be False
        self.scraper.session.get.side_effect = requests.Timeout("Connection timed out")
        succeeded_timeout, _, err_timeout = service.search_offers_for_buylist_item(item)
        self.assertFalse(succeeded_timeout)
        self.assertIn("timed out", err_timeout)

        # Case D: Incomplete / malformed JSON (missing 'response.data') -> scan_succeeded MUST be False
        self.scraper.session.get.side_effect = None
        self.scraper.session.get.return_value = FakeResponse(200, {"unexpected": "payload"})
        succeeded_bad_json, _, err_bad_json = service.search_offers_for_buylist_item(item)
        self.assertFalse(succeeded_bad_json)
        self.assertIn("Incomplete MTGSpot JSON", err_bad_json)

        # Case E: Malformed article item in data list -> scan_succeeded MUST be False
        self.scraper.session.get.return_value = FakeResponse(
            200, {"response": {"data": [{"invalid_item_without_attributes": True}]}}
        )
        succeeded_bad_item, _, err_bad_item = service.search_offers_for_buylist_item(item)
        self.assertFalse(succeeded_bad_item)
        self.assertIn("Malformed article item", err_bad_item)

    # ==================== 5. Isolation from Watchlist & Discord Alerts ====================

    def test_complete_isolation_from_manual_watchlist_and_discord(self):
        # Populate manual wishlist & watchlist before Karty sync
        manual_item_id = self.db.add_watchlist_item(
            card_name="Manual Watchlist Card", max_price=50.0
        )
        watchlist_before = self.db.get_watchlist()
        self.assertEqual(len(watchlist_before), 1)

        service = KartySyncService(db=self.db, scraper=self.scraper, config=self.config)

        # Mock Karty API wantlist & offers endpoints
        def mock_karty_request(method, url, headers=None, json=None, timeout=None):
            if method == "GET" and url.endswith("/api/integrations/mtgspot/wantlist"):
                return FakeResponse(
                    200,
                    {
                        "items": [
                            {
                                "id": 77,
                                "card_name": "Orcish Bowmasters",
                                "set_code": "ltr",
                                "set_name": "The Lord of the Rings: Tales of Middle-earth",
                                "condition": "NM",
                                "is_foil": False,
                                "target_price": 180.0,
                                "active": True,
                            }
                        ]
                    },
                )
            if method == "POST" and url.endswith("/api/integrations/mtgspot/offers"):
                return FakeResponse(200, {"status": "ok", "upserted": len(json.get("offers", []))})
            return FakeResponse(404)

        service.session = MagicMock()
        service.session.request.side_effect = mock_karty_request

        # Mock MTGSpot gateway returning 2 matching offers for Orcish Bowmasters
        self.scraper.session = MagicMock()
        self.scraper.session.get.return_value = FakeResponse(
            200,
            {
                "response": {
                    "data": [
                        {
                            "id": "bow_1",
                            "attributes": {
                                "title": "Orcish Bowmasters",
                                "language": "English",
                                "condition": "NM",
                                "is_foil": False,
                                "id_article": 9001,
                                "id_product": 3001,
                                "id_expansion": 55,
                                "expansion_name": "The Lord of the Rings: Tales of Middle-earth",
                                "stock": 3,
                                "price": "175.00",
                                "image": "https://gateway.mtgspot.pl/images/singles/LTR/3001.jpg",
                            },
                        }
                    ]
                }
            },
        )

        with patch("mtgspot.discord.send_discord_card_alert") as mock_discord:
            res = service.run_sync_once()
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["offers_found"], 1)
            self.assertEqual(res["synced_buylist_item_ids"], [77])
            # Discord alert must NEVER be called by Karty sync
            mock_discord.assert_not_called()

        # Manual watchlist must remain completely untouched
        watchlist_after = self.db.get_watchlist()
        self.assertEqual(len(watchlist_after), 1)
        self.assertEqual(watchlist_after[0]["id"], manual_item_id)
        self.assertEqual(watchlist_after[0]["card_name"], "Manual Watchlist Card")

    # ==================== 6. FastAPI Endpoints & Auth Tests ====================

    def test_fastapi_karty_integration_endpoints_auth_and_sync(self):
        original_service = api_module.karty_sync
        try:
            test_service = KartySyncService(db=self.db, scraper=self.scraper, config=self.config)
            api_module.karty_sync = test_service
            client = TestClient(api_module.app)

            # 1. Missing token -> 401
            r_no_auth = client.get("/api/integrations/karty/status")
            self.assertEqual(r_no_auth.status_code, 401)

            # 2. Invalid token -> 403
            r_bad_auth = client.get(
                "/api/integrations/karty/status",
                headers={"Authorization": "Bearer wrong-token-value"},
            )
            self.assertEqual(r_bad_auth.status_code, 403)

            # 3. Valid token -> 200
            r_ok = client.get(
                "/api/integrations/karty/status",
                headers={"Authorization": f"Bearer {self.config.sync_token}"},
            )
            self.assertEqual(r_ok.status_code, 200)
            self.assertEqual(r_ok.json()["status"], "idle")

            # 4. Trigger sync via POST /api/integrations/karty/sync
            with patch.object(
                test_service,
                "run_sync_once",
                return_value={"status": "ok", "wantlist_fetched": 2, "offers_found": 4, "errors_count": 0},
            ):
                r_sync = client.post(
                    "/api/integrations/karty/sync",
                    headers={"Authorization": f"Bearer {self.config.sync_token}"},
                )
                self.assertEqual(r_sync.status_code, 200)
                self.assertEqual(r_sync.json()["offers_found"], 4)
        finally:
            api_module.karty_sync = original_service

    # ==================== 7. Full Multi-Cycle E2E Simulation ====================

    def test_e2e_sync_lifecycle_upsert_price_change_and_error_protection(self):
        """
        Simulates a stateful Karty backend receiving sync payloads across 3 cycles:
        Cycle 1: 2 offers found for item #1 -> both stored as available.
        Cycle 2: 1 offer changes price & stock, 1 offer disappears -> no duplicates, disappeared offer marked unavailable.
        Cycle 3: MTGSpot returns HTTP 503 for item #1 -> item #1 NOT in synced_buylist_item_ids -> existing offers stay available!
        """
        karty_db_offers = {}

        def fake_karty_backend(method, url, headers=None, json=None, timeout=None):
            if method == "GET" and url.endswith("/api/integrations/mtgspot/wantlist"):
                return FakeResponse(
                    200,
                    {
                        "items": [
                            {
                                "id": 1,
                                "card_name": "Counterspell",
                                "set_code": "mh2",
                                "set_name": "Modern Horizons 2",
                                "condition": "LP",
                                "is_foil": False,
                                "target_price": 8.0,
                                "active": True,
                            }
                        ]
                    },
                )
            if method == "POST" and url.endswith("/api/integrations/mtgspot/offers"):
                synced_ids = set(json.get("synced_buylist_item_ids", []))
                incoming_offers = json.get("offers", [])
                seen_keys = set()

                for off in incoming_offers:
                    key = (off["buylist_item_id"], off["external_id"])
                    seen_keys.add(key)
                    karty_db_offers[key] = {
                        **off,
                        "is_available": off["stock"] > 0,
                    }

                if json.get("sync_completed"):
                    for key, existing in karty_db_offers.items():
                        b_id, _ = key
                        if b_id in synced_ids and key not in seen_keys:
                            existing["is_available"] = False
                            existing["stock"] = 0

                return FakeResponse(200, {"status": "ok"})
            return FakeResponse(404)

        service = KartySyncService(db=self.db, scraper=self.scraper, config=self.config)
        service.session = MagicMock()
        service.session.request.side_effect = fake_karty_backend
        self.scraper.session = MagicMock()

        # --- Cycle 1: Two offers (NM and EX) ---
        self.scraper.session.get.return_value = FakeResponse(
            200,
            {
                "response": {
                    "data": [
                        {
                            "id": "cs_nm",
                            "attributes": {
                                "title": "Counterspell",
                                "language": "English",
                                "condition": "NM",
                                "is_foil": False,
                                "id_article": 101,
                                "id_product": 201,
                                "id_expansion": 15,
                                "expansion_name": "Modern Horizons 2",
                                "stock": 4,
                                "price": "7.50",
                                "image": "https://gateway.mtgspot.pl/images/singles/MH2/201.jpg",
                            },
                        },
                        {
                            "id": "cs_ex",
                            "attributes": {
                                "title": "Counterspell",
                                "language": "English",
                                "condition": "EX",
                                "is_foil": False,
                                "id_article": 102,
                                "id_product": 201,
                                "id_expansion": 15,
                                "expansion_name": "Modern Horizons 2",
                                "stock": 2,
                                "price": "6.00",
                                "image": "https://gateway.mtgspot.pl/images/singles/MH2/201.jpg",
                            },
                        },
                    ]
                }
            },
        )
        res1 = service.run_sync_once()
        self.assertEqual(res1["status"], "ok")
        self.assertEqual(len(karty_db_offers), 2)
        self.assertTrue(karty_db_offers[(1, "cs_nm")]["is_available"])
        self.assertTrue(karty_db_offers[(1, "cs_ex")]["is_available"])

        # --- Cycle 2: cs_nm changes price to 7.00 & stock to 1; cs_ex sells out (disappears) ---
        self.scraper.session.get.return_value = FakeResponse(
            200,
            {
                "response": {
                    "data": [
                        {
                            "id": "cs_nm",
                            "attributes": {
                                "title": "Counterspell",
                                "language": "English",
                                "condition": "NM",
                                "is_foil": False,
                                "id_article": 101,
                                "id_product": 201,
                                "id_expansion": 15,
                                "expansion_name": "Modern Horizons 2",
                                "stock": 1,
                                "price": "7.00",
                                "image": "https://gateway.mtgspot.pl/images/singles/MH2/201.jpg",
                            },
                        }
                    ]
                }
            },
        )
        res2 = service.run_sync_once()
        self.assertEqual(res2["status"], "ok")
        # Still 2 records (no duplicates created)
        self.assertEqual(len(karty_db_offers), 2)
        self.assertEqual(karty_db_offers[(1, "cs_nm")]["price"], 7.00)
        self.assertEqual(karty_db_offers[(1, "cs_nm")]["stock"], 1)
        self.assertTrue(karty_db_offers[(1, "cs_nm")]["is_available"])
        # Disappeared offer cs_ex is now marked unavailable
        self.assertFalse(karty_db_offers[(1, "cs_ex")]["is_available"])
        self.assertEqual(karty_db_offers[(1, "cs_ex")]["stock"], 0)

        # --- Cycle 3: MTGSpot gateway fails with HTTP 503 ---
        self.scraper.session.get.return_value = FakeResponse(503, text="Service Unavailable")
        res3 = service.run_sync_once()
        self.assertEqual(res3["status"], "error")
        self.assertEqual(res3["synced_buylist_item_ids"], [])
        # CRITICAL: cs_nm MUST remain available despite MTGSpot error!
        self.assertTrue(karty_db_offers[(1, "cs_nm")]["is_available"])
        self.assertEqual(karty_db_offers[(1, "cs_nm")]["stock"], 1)


if __name__ == "__main__":
    unittest.main()
