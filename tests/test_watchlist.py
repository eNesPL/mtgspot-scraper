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
