import unittest
import tempfile
import os
import shutil
from mtgspot.db import Database
from mtgspot.discord import send_discord_card_alert, send_test_webhook


class TestWatchlistAndDiscord(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_wl.db")
        self.db = Database(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_watchlist_crud(self):
        # 1. Add item
        item_id = self.db.add_watchlist_item(
            card_name="Vile Entomber",
            max_price=4.50,
            condition="NM,EX",
            is_foil=False
        )
        self.assertIsInstance(item_id, int)

        # 2. Get item
        item = self.db.get_watchlist_item(item_id)
        self.assertIsNotNone(item)
        self.assertEqual(item["card_name"], "Vile Entomber")
        self.assertEqual(item["max_price"], 4.50)
        self.assertEqual(item["is_foil"], False)

        # 3. Update stock
        self.db.update_watchlist_stock(item_id, last_stock=3, notified=True)
        updated = self.db.get_watchlist_item(item_id)
        self.assertEqual(updated["last_stock"], 3)
        self.assertIsNotNone(updated["last_notified_at"])

        # 4. Notification deduplication
        self.assertFalse(self.db.has_been_notified(item_id, "card_hash_1", 3.50))
        self.db.record_notification(item_id, "card_hash_1", "Vile Entomber", 3.50, 3)
        self.assertTrue(self.db.has_been_notified(item_id, "card_hash_1", 3.50))

        # Different card hash or price should not be marked as notified
        self.assertFalse(self.db.has_been_notified(item_id, "card_hash_2", 3.50))
        self.assertFalse(self.db.has_been_notified(item_id, "card_hash_1", 2.50))

        # 5. Delete item
        deleted = self.db.delete_watchlist_item(item_id)
        self.assertTrue(deleted)
        self.assertIsNone(self.db.get_watchlist_item(item_id))

    def test_watchlist_deduplication(self):
        # 1. Add "Counterspell"
        id1, is_new1 = self.db.add_or_update_watchlist_item(
            card_name="Counterspell",
            max_price=5.00
        )
        self.assertTrue(is_new1)

        # 2. Add "counterspell" again with different price
        id2, is_new2 = self.db.add_or_update_watchlist_item(
            card_name="  counterspell  ",
            max_price=7.50
        )
        self.assertFalse(is_new2)
        self.assertEqual(id1, id2)

        # Check total watchlist count is 1, not 2
        items = self.db.get_watchlist()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["max_price"], 7.50)

        # 3. Bulk add with internal duplicate and existing card
        cards = [
            {"card_name": "Counterspell", "max_price": 6.00},
            {"card_name": "Sol Ring", "max_price": 10.00},
            {"card_name": "sol ring", "max_price": 12.00},
        ]
        inserted, existing = self.db.add_watchlist_items_bulk(cards)
        self.assertEqual(len(inserted), 1)  # Only Sol Ring is newly inserted
        self.assertEqual(len(existing), 1)  # Counterspell was existing

        # Total in watchlist should now be 2 ("Counterspell" and "Sol Ring")
        all_items = self.db.get_watchlist()
        self.assertEqual(len(all_items), 2)

    def test_wishlists_management(self):
        # 1. Default wishlist exists
        wishlists = self.db.get_wishlists()
        self.assertEqual(len(wishlists), 1)
        self.assertEqual(wishlists[0]["name"], "Główna lista")

        # 2. Create custom wishlists
        rakdos_id = self.db.create_wishlist("Rakdos deck", description="Commander deck")
        boros_id = self.db.create_wishlist("Boros deck", description="Standard deck")
        self.assertIsInstance(rakdos_id, int)
        self.assertIsInstance(boros_id, int)

        # Total wishlists should now be 3
        wishlists = self.db.get_wishlists()
        self.assertEqual(len(wishlists), 3)

        # 3. Add same card to Rakdos and Boros
        r_item, is_new_r = self.db.add_or_update_watchlist_item("Sol Ring", list_id=rakdos_id, max_price=10.0)
        b_item, is_new_b = self.db.add_or_update_watchlist_item("Sol Ring", list_id=boros_id, max_price=15.0)
        self.assertTrue(is_new_r)
        self.assertTrue(is_new_b)
        self.assertNotEqual(r_item, b_item)

        # Duplicate in Rakdos should NOT create a new row
        r_item2, is_new_r2 = self.db.add_or_update_watchlist_item("Sol Ring", list_id=rakdos_id, max_price=12.0)
        self.assertFalse(is_new_r2)
        self.assertEqual(r_item, r_item2)

        # Filter watchlist by list_id
        rakdos_cards = self.db.get_watchlist(list_id=rakdos_id)
        self.assertEqual(len(rakdos_cards), 1)
        self.assertEqual(rakdos_cards[0]["card_name"], "Sol Ring")
        self.assertEqual(rakdos_cards[0]["max_price"], 12.0)
        self.assertEqual(rakdos_cards[0]["list_name"], "Rakdos deck")

        # Check total cards across all lists
        all_cards = self.db.get_watchlist()
        self.assertEqual(len(all_cards), 2)

        # 4. Edit card in watchlist
        updated = self.db.update_watchlist_item(r_item, max_price=8.0, is_foil=True)
        self.assertTrue(updated)
        item_data = self.db.get_watchlist_item(r_item)
        self.assertEqual(item_data["max_price"], 8.0)
        self.assertTrue(item_data["is_foil"])

        # 5. Delete Boros wishlist (should delete its Sol Ring, keeping Rakdos intact)
        self.db.delete_wishlist(boros_id)
        self.assertEqual(len(self.db.get_wishlists()), 2)
        self.assertEqual(len(self.db.get_watchlist()), 1)
        self.assertIsNotNone(self.db.get_watchlist_item(r_item))
        self.assertIsNone(self.db.get_watchlist_item(b_item))

    def test_settings(self):
        self.assertIsNone(self.db.get_setting("discord_webhook"))
        self.db.set_setting("discord_webhook", "https://discord.com/api/webhooks/123/abc")
        self.assertEqual(self.db.get_setting("discord_webhook"), "https://discord.com/api/webhooks/123/abc")

    def test_invalid_webhook_validation(self):
        ok, msg = send_test_webhook("invalid-url")
        self.assertFalse(ok)
        self.assertIn("Nieprawidłowy adres", msg)


if __name__ == "__main__":
    unittest.main()
