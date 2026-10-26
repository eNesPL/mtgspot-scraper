import unittest
import tempfile
import os
import shutil
from mtgspot.db import Database
from mtgspot.scraper import MTGSpotScraper, slugify


class TestMTGSpot(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.db = Database(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_slugify(self):
        self.assertEqual(slugify("Black Lotus"), "black-lotus")
        self.assertEqual(slugify("Jace, the Mind Sculptor"), "jace-the-mind-sculptor")
        self.assertEqual(slugify("Commander 2021: Extras"), "commander-2021-extras")

    def test_database_crud_and_fts(self):
        test_cards = [
            {
                "id": "card1",
                "id_article": 101,
                "id_product": 201,
                "title": "Lightning Bolt",
                "description": "Deal 3 damage",
                "language": "English",
                "condition": "NM",
                "is_foil": False,
                "is_signed": False,
                "is_playset": False,
                "is_altered": False,
                "id_expansion": 10,
                "expansion_name": "Magic 2010",
                "rarity": "Common",
                "stock": 5,
                "price": 4.50,
                "image": "https://example.com/bolt.jpg",
                "card_url": "https://mtgspot.pl/single/bolt"
            },
            {
                "id": "card2",
                "id_article": 102,
                "id_product": 202,
                "title": "Lightning Greaves",
                "description": "Equip 0",
                "language": "English",
                "condition": "EX",
                "is_foil": True,
                "is_signed": False,
                "is_playset": False,
                "is_altered": False,
                "id_expansion": 45,
                "expansion_name": "Mirrodin",
                "rarity": "Uncommon",
                "stock": 2,
                "price": 28.00,
                "image": "https://example.com/greaves.jpg",
                "card_url": "https://mtgspot.pl/single/greaves"
            },
            {
                "id": "card3",
                "id_article": 103,
                "id_product": 203,
                "title": "Birds of Paradise",
                "description": "Add one mana",
                "language": "English",
                "condition": "NM",
                "is_foil": True,
                "is_signed": False,
                "is_playset": False,
                "is_altered": False,
                "id_expansion": 10,
                "expansion_name": "Magic 2010",
                "rarity": "Rare",
                "stock": 1,
                "price": 45.00,
                "image": "https://example.com/bop.jpg",
                "card_url": "https://mtgspot.pl/single/bop"
            }
        ]

        self.db.upsert_cards_batch(test_cards)

        # 1. Total search
        cards, total = self.db.search_cards()
        self.assertEqual(total, 3)

        # 2. FTS prefix search on "Lightn"
        cards, total = self.db.search_cards(q="Lightn")
        self.assertEqual(total, 2)

        # 3. Filter by foil
        cards, total = self.db.search_cards(is_foil=True)
        self.assertEqual(total, 2)
        for c in cards:
            self.assertTrue(c["is_foil"])

        # 4. Filter by price range
        cards, total = self.db.search_cards(min_price=10.0, max_price=30.0)
        self.assertEqual(total, 1)
        self.assertEqual(cards[0]["title"], "Lightning Greaves")

        # 5. Filter by condition
        cards, total = self.db.search_cards(condition="EX")
        self.assertEqual(total, 1)
        self.assertEqual(cards[0]["title"], "Lightning Greaves")

        # 6. Filter by rarity
        cards, total = self.db.search_cards(rarity="Rare")
        self.assertEqual(total, 1)
        self.assertEqual(cards[0]["title"], "Birds of Paradise")

        # 7. Sorting
        cards, _ = self.db.search_cards(sort_by="price_desc")
        self.assertEqual(cards[0]["price"], 45.00)
        self.assertEqual(cards[-1]["price"], 4.50)

        # 8. Filter options
        options = self.db.get_filter_options()
        self.assertIn("conditions", options)
        self.assertIn("rarities", options)
        self.assertIn("expansions", options)
        self.assertEqual(options["stats"]["total_cards"], 3)
        self.assertEqual(options["stats"]["unique_titles"], 3)
        self.assertEqual(options["stats"]["total_stock"], 8)

    def test_scraper_parsing(self):
        scraper = MTGSpotScraper(db=self.db)
        raw_api_item = {
            "type": "shop/articles",
            "id": "abc123hash",
            "attributes": {
                "title": "Mox Diamond",
                "description": "",
                "language": "English",
                "condition": "NM",
                "is_foil": False,
                "is_signed": False,
                "is_playset": False,
                "is_altered": False,
                "id_article": 9999,
                "id_product": 8888,
                "id_expansion": 20,
                "expansion_name": "Stronghold",
                "rarity": "Rare",
                "stock": 2,
                "price": "2499.99",
                "image": "https://gateway.mtgspot.pl/images/singles/STH/8888.jpg"
            }
        }

        parsed = scraper.parse_card_item(raw_api_item)
        self.assertEqual(parsed["id"], "abc123hash")
        self.assertEqual(parsed["title"], "Mox Diamond")
        self.assertEqual(parsed["price"], 2499.99)
        self.assertEqual(parsed["stock"], 2)
        self.assertEqual(parsed["is_foil"], False)
        self.assertIn("mtgspot.pl/single/stronghold/20/mox-diamond/8888/9999", parsed["card_url"])


if __name__ == "__main__":
    unittest.main()
