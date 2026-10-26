import os
import sqlite3
import json
import logging
from contextlib import contextmanager
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger("mtgspot.db")

DEFAULT_DB_PATH = os.environ.get(
    "MTGSPOT_DB_PATH",
    os.path.expanduser("~/.gemini/antigravity-cli/scratch/mtgspot-service/mtgspot.db")
)


class Database:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self.init_db()

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Enable WAL mode for better concurrency and fast reads
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        try:
            yield conn
        finally:
            conn.close()

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Cards table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cards (
                    id TEXT PRIMARY KEY,
                    id_article INTEGER,
                    id_product INTEGER,
                    title TEXT NOT NULL,
                    description TEXT,
                    language TEXT,
                    condition TEXT,
                    is_foil INTEGER DEFAULT 0,
                    is_signed INTEGER DEFAULT 0,
                    is_playset INTEGER DEFAULT 0,
                    is_altered INTEGER DEFAULT 0,
                    id_expansion INTEGER,
                    expansion_name TEXT,
                    rarity TEXT,
                    stock INTEGER NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0.0,
                    image TEXT,
                    card_url TEXT,
                    scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Create standard indices for filtering and sorting
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cards_title ON cards(title);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cards_expansion ON cards(expansion_name);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cards_price ON cards(price);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cards_stock ON cards(stock);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cards_condition ON cards(condition);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cards_foil ON cards(is_foil);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cards_rarity ON cards(rarity);")

            # FTS5 Full-Text Search virtual table
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
                    id UNINDEXED,
                    title,
                    expansion_name,
                    rarity,
                    condition,
                    tokenize = 'unicode61 remove_diacritics 2'
                );
            """)

            # Triggers to keep FTS in sync with cards table
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_cards_after_insert AFTER INSERT ON cards BEGIN
                    INSERT INTO cards_fts(id, title, expansion_name, rarity, condition)
                    VALUES (new.id, new.title, new.expansion_name, new.rarity, new.condition);
                END;
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_cards_after_delete AFTER DELETE ON cards BEGIN
                    DELETE FROM cards_fts WHERE id = old.id;
                END;
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_cards_after_update AFTER UPDATE ON cards BEGIN
                    DELETE FROM cards_fts WHERE id = old.id;
                    INSERT INTO cards_fts(id, title, expansion_name, rarity, condition)
                    VALUES (new.id, new.title, new.expansion_name, new.rarity, new.condition);
                END;
            """)

            # Expansions table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS expansions (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    code TEXT,
                    release_date INTEGER
                );
            """)

            # Scrape metadata / status table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS scrape_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_scraped_at TEXT,
                    total_available INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'idle',
                    duration_seconds REAL DEFAULT 0,
                    error_message TEXT
                );
            """)
            cursor.execute("""
                INSERT OR IGNORE INTO scrape_meta (id, status) VALUES (1, 'idle');
            """)

            # Wishlists / Decklists table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS wishlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT,
                    webhook_url TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("""
                INSERT OR IGNORE INTO wishlists (id, name, description)
                VALUES (1, 'Main Wishlist', 'Default watched cards list');
            """)
            cursor.execute("""
                UPDATE wishlists SET 
                    name = 'Main Wishlist', 
                    description = 'Default watched cards list' 
                WHERE id = 1 AND name = 'Główna lista';
            """)

            # Watchlist / Alerts table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_id INTEGER DEFAULT 1,
                    card_name TEXT NOT NULL,
                    max_price REAL,
                    condition TEXT,
                    is_foil INTEGER,
                    expansion TEXT,
                    webhook_url TEXT,
                    last_stock INTEGER DEFAULT 0,
                    last_notified_at TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (list_id) REFERENCES wishlists(id) ON DELETE CASCADE
                );
            """)

            # Ensure list_id column exists and create compound unique index
            try:
                cursor.execute("PRAGMA table_info(watchlist);")
                cols = [c[1] for c in cursor.fetchall()]
                if "list_id" not in cols:
                    cursor.execute("ALTER TABLE watchlist ADD COLUMN list_id INTEGER DEFAULT 1;")
                    cursor.execute("UPDATE watchlist SET list_id = 1 WHERE list_id IS NULL;")

                cursor.execute("DROP INDEX IF EXISTS idx_watchlist_card_name;")

                # Deduplicate existing entries in watchlist per list_id
                cursor.execute("SELECT id, COALESCE(list_id, 1), card_name FROM watchlist ORDER BY id ASC;")
                seen_wl = {}
                for row_id, lid, cname in cursor.fetchall():
                    norm = (lid, cname.strip().lower())
                    if norm in seen_wl:
                        keep_id = seen_wl[norm]
                        cursor.execute("UPDATE notification_history SET watchlist_id = ? WHERE watchlist_id = ?", (keep_id, row_id))
                        cursor.execute("DELETE FROM watchlist WHERE id = ?", (row_id,))
                    else:
                        seen_wl[norm] = row_id

                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_list_card 
                    ON watchlist(list_id, LOWER(TRIM(card_name)));
                """)
            except Exception as e:
                logger.warning(f"Watchlist migration error: {e}")

            # Notification history table to avoid duplicate spamming
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS notification_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watchlist_id INTEGER,
                    card_id TEXT,
                    card_title TEXT,
                    price REAL,
                    stock INTEGER,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # App settings table (for default discord webhook, check intervals, etc.)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            conn.commit()

    def upsert_expansions(self, expansions: List[Dict[str, Any]]):
        if not expansions:
            return
        with self.get_connection() as conn:
            cursor = conn.cursor()
            records = []
            for exp in expansions:
                eid = exp.get("id")
                attrs = exp.get("attributes", {})
                records.append((
                    int(eid) if eid is not None else None,
                    attrs.get("name", ""),
                    attrs.get("code", ""),
                    attrs.get("release_date")
                ))
            cursor.executemany("""
                INSERT INTO expansions (id, name, code, release_date)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    code = excluded.code,
                    release_date = excluded.release_date;
            """, records)
            conn.commit()

    def upsert_cards_batch(self, cards: List[Dict[str, Any]]):
        """Batch upsert cards parsed from MTG Spot API responses."""
        if not cards:
            return
        with self.get_connection() as conn:
            cursor = conn.cursor()
            records = []
            for c in cards:
                records.append((
                    c["id"],
                    c.get("id_article"),
                    c.get("id_product"),
                    c["title"],
                    c.get("description", ""),
                    c.get("language", "English"),
                    c.get("condition", "NM"),
                    1 if c.get("is_foil") else 0,
                    1 if c.get("is_signed") else 0,
                    1 if c.get("is_playset") else 0,
                    1 if c.get("is_altered") else 0,
                    c.get("id_expansion"),
                    c.get("expansion_name", ""),
                    c.get("rarity", ""),
                    int(c.get("stock", 0)),
                    float(c.get("price", 0.0)),
                    c.get("image", ""),
                    c.get("card_url", "")
                ))
            cursor.executemany("""
                INSERT INTO cards (
                    id, id_article, id_product, title, description, language,
                    condition, is_foil, is_signed, is_playset, is_altered,
                    id_expansion, expansion_name, rarity, stock, price, image, card_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    id_article = excluded.id_article,
                    id_product = excluded.id_product,
                    title = excluded.title,
                    description = excluded.description,
                    language = excluded.language,
                    condition = excluded.condition,
                    is_foil = excluded.is_foil,
                    is_signed = excluded.is_signed,
                    is_playset = excluded.is_playset,
                    is_altered = excluded.is_altered,
                    id_expansion = excluded.id_expansion,
                    expansion_name = excluded.expansion_name,
                    rarity = excluded.rarity,
                    stock = excluded.stock,
                    price = excluded.price,
                    image = excluded.image,
                    card_url = excluded.card_url,
                    scraped_at = CURRENT_TIMESTAMP;
            """, records)
            conn.commit()

    def clear_cards(self):
        with self.get_connection() as conn:
            conn.execute("DELETE FROM cards;")
            conn.execute("DELETE FROM cards_fts;")
            conn.commit()

    def update_scrape_meta(self, status: str, total_available: int = None,
                           duration: float = None, error: str = None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if status == "success":
                cursor.execute("""
                    UPDATE scrape_meta SET
                        status = ?,
                        last_scraped_at = CURRENT_TIMESTAMP,
                        total_available = COALESCE(?, total_available),
                        duration_seconds = COALESCE(?, duration_seconds),
                        error_message = NULL
                    WHERE id = 1;
                """, (status, total_available, duration))
            elif status == "error":
                cursor.execute("""
                    UPDATE scrape_meta SET
                        status = ?,
                        error_message = ?
                    WHERE id = 1;
                """, (status, error))
            else:
                cursor.execute("""
                    UPDATE scrape_meta SET status = ? WHERE id = 1;
                """, (status,))
            conn.commit()

    def get_scrape_meta(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM scrape_meta WHERE id = 1").fetchone()
            if row:
                return dict(row)
            return {}

    def search_cards(
        self,
        q: Optional[str] = None,
        title: Optional[str] = None,
        expansion: Optional[str] = None,
        id_expansion: Optional[int] = None,
        condition: Optional[str] = None,
        is_foil: Optional[bool] = None,
        rarity: Optional[str] = None,
        language: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        min_stock: Optional[int] = 1,
        sort_by: Optional[str] = "price_asc",
        limit: int = 50,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Execute filtered and indexed search on cards.
        Returns (list_of_cards, total_count).
        """
        conditions = []
        params = []
        use_fts = False

        if q and q.strip():
            use_fts = True
            # Sanitize and prepare FTS query (prefix matching for tokens)
            cleaned_terms = [t for t in q.replace('"', '').replace("'", '').split() if t]
            if cleaned_terms:
                fts_query = " ".join([f'"{term}"*' for term in cleaned_terms])
                conditions.append("c.id IN (SELECT id FROM cards_fts WHERE cards_fts MATCH ?)")
                params.append(fts_query)

        if title and title.strip():
            conditions.append("c.title LIKE ?")
            params.append(f"%{title.strip()}%")

        if expansion and expansion.strip():
            conditions.append("c.expansion_name LIKE ?")
            params.append(f"%{expansion.strip()}%")

        if id_expansion is not None:
            conditions.append("c.id_expansion = ?")
            params.append(id_expansion)

        if condition and condition.strip():
            # Allow comma-separated multiple conditions e.g. "NM,EX"
            cond_list = [c.strip().upper() for c in condition.split(",") if c.strip()]
            if len(cond_list) == 1:
                conditions.append("c.condition = ?")
                params.append(cond_list[0])
            elif len(cond_list) > 1:
                placeholders = ",".join(["?"] * len(cond_list))
                conditions.append(f"c.condition IN ({placeholders})")
                params.extend(cond_list)

        if is_foil is not None:
            conditions.append("c.is_foil = ?")
            params.append(1 if is_foil else 0)

        if rarity and rarity.strip():
            rarity_list = [r.strip() for r in rarity.split(",") if r.strip()]
            if len(rarity_list) == 1:
                conditions.append("LOWER(c.rarity) = LOWER(?)")
                params.append(rarity_list[0])
            elif len(rarity_list) > 1:
                placeholders = ",".join(["LOWER(?)"] * len(rarity_list))
                conditions.append(f"LOWER(c.rarity) IN ({placeholders})")
                params.extend(rarity_list)

        if language and language.strip():
            conditions.append("LOWER(c.language) = LOWER(?)")
            params.append(language.strip())

        if min_price is not None:
            conditions.append("c.price >= ?")
            params.append(min_price)

        if max_price is not None:
            conditions.append("c.price <= ?")
            params.append(max_price)

        if min_stock is not None:
            conditions.append("c.stock >= ?")
            params.append(min_stock)

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        # Sorting logic
        sort_map = {
            "price_asc": "c.price ASC",
            "price_desc": "c.price DESC",
            "title_asc": "c.title ASC",
            "title_desc": "c.title DESC",
            "stock_desc": "c.stock DESC",
            "stock_asc": "c.stock ASC",
            "newest": "c.scraped_at DESC, c.price ASC"
        }
        order_clause = sort_map.get(sort_by, "c.price ASC")

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Count total
            count_sql = f"SELECT COUNT(*) FROM cards c {where_clause}"
            cursor.execute(count_sql, params)
            total = cursor.fetchone()[0]

            # Query items
            query_sql = f"""
                SELECT c.* FROM cards c
                {where_clause}
                ORDER BY {order_clause}
                LIMIT ? OFFSET ?
            """
            cursor.execute(query_sql, params + [limit, offset])
            rows = [dict(row) for row in cursor.fetchall()]

            # Convert boolean integers for clean output
            for r in rows:
                r["is_foil"] = bool(r["is_foil"])
                r["is_signed"] = bool(r["is_signed"])
                r["is_playset"] = bool(r["is_playset"])
                r["is_altered"] = bool(r["is_altered"])

            return rows, total

    def get_card_by_id(self, card_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
            if row:
                d = dict(row)
                d["is_foil"] = bool(d["is_foil"])
                d["is_signed"] = bool(d["is_signed"])
                d["is_playset"] = bool(d["is_playset"])
                d["is_altered"] = bool(d["is_altered"])
                return d
            return None

    def get_filter_options(self) -> Dict[str, Any]:
        """Returns distinct filter choices with counts for the UI."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Conditions
            cursor.execute("""
                SELECT condition, COUNT(*) as count 
                FROM cards 
                WHERE condition IS NOT NULL AND condition != '' 
                GROUP BY condition 
                ORDER BY count DESC
            """)
            conditions = [dict(r) for r in cursor.fetchall()]

            # Rarities
            cursor.execute("""
                SELECT rarity, COUNT(*) as count 
                FROM cards 
                WHERE rarity IS NOT NULL AND rarity != '' 
                GROUP BY rarity 
                ORDER BY count DESC
            """)
            rarities = [dict(r) for r in cursor.fetchall()]

            # Expansions (Top 100 sets by in-stock cards)
            cursor.execute("""
                SELECT expansion_name, COUNT(*) as count, MAX(id_expansion) as id_expansion
                FROM cards 
                WHERE expansion_name IS NOT NULL AND expansion_name != ''
                GROUP BY expansion_name 
                ORDER BY expansion_name ASC
            """)
            expansions = [dict(r) for r in cursor.fetchall()]

            # Price min/max and counts
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_cards,
                    COUNT(DISTINCT title) as unique_titles,
                    COALESCE(SUM(stock), 0) as total_stock,
                    COALESCE(MIN(price), 0.0) as min_price,
                    COALESCE(MAX(price), 0.0) as max_price,
                    COALESCE(AVG(price), 0.0) as avg_price,
                    COALESCE(SUM(CASE WHEN is_foil = 1 THEN 1 ELSE 0 END), 0) as foil_count
                FROM cards
            """)
            stats = dict(cursor.fetchone())

            return {
                "conditions": conditions,
                "rarities": rarities,
                "expansions": expansions,
                "stats": stats
            }

    # ==================== Settings Management ====================

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
            """, (key, value))
            conn.commit()

    # ==================== Wishlists Management ====================

    def get_wishlists(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    w.id,
                    w.name,
                    w.description,
                    w.webhook_url,
                    w.is_active,
                    w.created_at,
                    COUNT(wl.id) as card_count,
                    SUM(CASE WHEN wl.last_stock > 0 THEN 1 ELSE 0 END) as in_stock_count
                FROM wishlists w
                LEFT JOIN watchlist wl ON wl.list_id = w.id
                GROUP BY w.id
                ORDER BY w.id ASC;
            """)
            return [dict(r) for r in cursor.fetchall()]

    def get_wishlist(self, list_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM wishlists WHERE id = ?;", (list_id,)).fetchone()
            return dict(row) if row else None

    def create_wishlist(
        self,
        name: str,
        description: Optional[str] = None,
        webhook_url: Optional[str] = None
    ) -> int:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Wishlist name cannot be empty")
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO wishlists (name, description, webhook_url)
                VALUES (?, ?, ?);
            """, (clean_name, description, webhook_url))
            conn.commit()
            return cursor.lastrowid

    def update_wishlist(
        self,
        list_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        webhook_url: Optional[str] = None,
        is_active: Optional[bool] = None
    ) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            updates = []
            params = []
            if name is not None and name.strip():
                updates.append("name = ?")
                params.append(name.strip())
            if description is not None:
                updates.append("description = ?")
                params.append(description)
            if webhook_url is not None:
                updates.append("webhook_url = ?")
                params.append(webhook_url or None)
            if is_active is not None:
                updates.append("is_active = ?")
                params.append(1 if is_active else 0)

            if not updates:
                return False

            params.append(list_id)
            cursor.execute(f"UPDATE wishlists SET {', '.join(updates)} WHERE id = ?;", params)
            conn.commit()
            return cursor.rowcount > 0

    def delete_wishlist(self, list_id: int) -> bool:
        if list_id == 1:
            raise ValueError("Cannot delete the default Main Wishlist")
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM notification_history WHERE watchlist_id IN (SELECT id FROM watchlist WHERE list_id = ?);", (list_id,))
            cursor.execute("DELETE FROM watchlist WHERE list_id = ?;", (list_id,))
            cursor.execute("DELETE FROM wishlists WHERE id = ?;", (list_id,))
            conn.commit()
            return cursor.rowcount > 0

    # ==================== Watchlist Management ====================

    def add_or_update_watchlist_item(
        self,
        card_name: str,
        list_id: int = 1,
        max_price: Optional[float] = None,
        condition: Optional[str] = None,
        is_foil: Optional[bool] = None,
        expansion: Optional[str] = None,
        webhook_url: Optional[str] = None
    ) -> Tuple[int, bool]:
        """
        Add a card to a specific wishlist, or update the existing item in that list.
        Returns (item_id, is_new).
        """
        clean_name = card_name.strip()
        if not clean_name:
            raise ValueError("Card name cannot be empty")
        list_id = int(list_id or 1)

        foil_val = None
        if is_foil is not None:
            foil_val = 1 if is_foil else 0

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM watchlist
                WHERE list_id = ? AND LOWER(TRIM(card_name)) = LOWER(?)
                LIMIT 1;
            """, (list_id, clean_name))
            existing = cursor.fetchone()

            if existing:
                item_id = existing["id"]
                cursor.execute("""
                    UPDATE watchlist
                    SET max_price = COALESCE(?, max_price),
                        condition = COALESCE(?, condition),
                        is_foil = COALESCE(?, is_foil),
                        expansion = COALESCE(?, expansion),
                        webhook_url = COALESCE(?, webhook_url),
                        is_active = 1
                    WHERE id = ?;
                """, (max_price, condition, foil_val, expansion, webhook_url, item_id))
                conn.commit()
                return item_id, False
            else:
                cursor.execute("""
                    INSERT INTO watchlist (
                        list_id, card_name, max_price, condition, is_foil, expansion, webhook_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """, (list_id, clean_name, max_price, condition, foil_val, expansion, webhook_url))
                conn.commit()
                return cursor.lastrowid, True

    def add_watchlist_item(
        self,
        card_name: str,
        list_id: int = 1,
        max_price: Optional[float] = None,
        condition: Optional[str] = None,
        is_foil: Optional[bool] = None,
        expansion: Optional[str] = None,
        webhook_url: Optional[str] = None
    ) -> int:
        item_id, _ = self.add_or_update_watchlist_item(
            card_name=card_name,
            list_id=list_id,
            max_price=max_price,
            condition=condition,
            is_foil=is_foil,
            expansion=expansion,
            webhook_url=webhook_url
        )
        return item_id

    def add_watchlist_items_bulk(
        self,
        cards: List[Dict[str, Any]],
        list_id: int = 1
    ) -> Tuple[List[int], List[int]]:
        """
        Add multiple cards to a specific wishlist in a single transaction.
        Deduplicates within the batch and checks against existing items in this list.
        Returns (inserted_ids, existing_ids).
        """
        if not cards:
            return [], []
        list_id = int(list_id or 1)

        inserted_ids = []
        existing_ids = []
        seen_in_batch = set()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            for c in cards:
                card_name = c["card_name"].strip()
                if not card_name:
                    continue
                norm = card_name.lower()
                if norm in seen_in_batch:
                    continue
                seen_in_batch.add(norm)

                foil_val = None
                if c.get("is_foil") is not None:
                    foil_val = 1 if c.get("is_foil") else 0

                cursor.execute("""
                    SELECT id FROM watchlist
                    WHERE list_id = ? AND LOWER(TRIM(card_name)) = ?
                    LIMIT 1;
                """, (list_id, norm))
                existing = cursor.fetchone()

                if existing:
                    item_id = existing["id"]
                    cursor.execute("""
                        UPDATE watchlist
                        SET max_price = COALESCE(?, max_price),
                            condition = COALESCE(?, condition),
                            is_foil = COALESCE(?, is_foil),
                            expansion = COALESCE(?, expansion),
                            webhook_url = COALESCE(?, webhook_url),
                            is_active = 1
                        WHERE id = ?;
                    """, (
                        c.get("max_price"),
                        c.get("condition"),
                        foil_val,
                        c.get("expansion"),
                        c.get("webhook_url"),
                        item_id
                    ))
                    existing_ids.append(item_id)
                else:
                    cursor.execute("""
                        INSERT INTO watchlist (
                            list_id, card_name, max_price, condition, is_foil, expansion, webhook_url
                        ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """, (
                        list_id,
                        card_name,
                        c.get("max_price"),
                        c.get("condition"),
                        foil_val,
                        c.get("expansion"),
                        c.get("webhook_url")
                    ))
                    inserted_ids.append(cursor.lastrowid)
            conn.commit()
        return inserted_ids, existing_ids

    def get_watchlist(
        self,
        list_id: Optional[int] = None,
        active_only: bool = False
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT 
                wl.*,
                COALESCE(w.name, 'Main Wishlist') as list_name,
                w.webhook_url as list_webhook_url
            FROM watchlist wl
            LEFT JOIN wishlists w ON wl.list_id = w.id
            WHERE 1=1
        """
        params = []
        if list_id is not None:
            query += " AND wl.list_id = ?"
            params.append(list_id)
        if active_only:
            query += " AND wl.is_active = 1"
        query += " ORDER BY wl.id DESC"

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = [dict(r) for r in cursor.fetchall()]
            for r in rows:
                if r["is_foil"] is not None:
                    r["is_foil"] = bool(r["is_foil"])
            return rows

    def get_watchlist_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("""
                SELECT 
                    wl.*,
                    COALESCE(w.name, 'Main Wishlist') as list_name,
                    w.webhook_url as list_webhook_url
                FROM watchlist wl
                LEFT JOIN wishlists w ON wl.list_id = w.id
                WHERE wl.id = ?
            """, (item_id,)).fetchone()
            if row:
                d = dict(row)
                if d["is_foil"] is not None:
                    d["is_foil"] = bool(d["is_foil"])
                return d
            return None

    def update_watchlist_item(
        self,
        item_id: int,
        card_name: Optional[str] = None,
        max_price: Optional[float] = None,
        condition: Optional[str] = None,
        is_foil: Optional[bool] = None,
        expansion: Optional[str] = None,
        list_id: Optional[int] = None,
        is_active: Optional[bool] = None
    ) -> bool:
        foil_val = None
        if is_foil is not None:
            foil_val = 1 if is_foil else 0

        with self.get_connection() as conn:
            cursor = conn.cursor()
            updates = []
            params = []
            if card_name is not None and card_name.strip():
                updates.append("card_name = ?")
                params.append(card_name.strip())
            if max_price is not None:
                updates.append("max_price = ?")
                params.append(max_price)
            if condition is not None:
                updates.append("condition = ?")
                params.append(condition)
            if is_foil is not None:
                updates.append("is_foil = ?")
                params.append(foil_val)
            if expansion is not None:
                updates.append("expansion = ?")
                params.append(expansion)
            if list_id is not None:
                updates.append("list_id = ?")
                params.append(list_id)
            if is_active is not None:
                updates.append("is_active = ?")
                params.append(1 if is_active else 0)

            if not updates:
                return False

            params.append(item_id)
            cursor.execute(f"UPDATE watchlist SET {', '.join(updates)} WHERE id = ?;", params)
            conn.commit()
            return cursor.rowcount > 0

    def delete_watchlist_item(self, item_id: int) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM watchlist WHERE id = ?", (item_id,))
            cursor.execute("DELETE FROM notification_history WHERE watchlist_id = ?", (item_id,))
            conn.commit()
            return cursor.rowcount > 0

    def update_watchlist_stock(self, item_id: int, last_stock: int, notified: bool = False):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if notified:
                cursor.execute("""
                    UPDATE watchlist SET
                        last_stock = ?,
                        last_notified_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, (last_stock, item_id))
            else:
                cursor.execute("UPDATE watchlist SET last_stock = ? WHERE id = ?;", (last_stock, item_id))
            conn.commit()

    def has_card_been_notified(self, card_name: str, hours: int = 6) -> bool:
        """
        Check if an alert for this card name was already sent recently
        (within the last `hours` hours) to prevent repeated spam.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM notification_history
                WHERE (
                    LOWER(TRIM(card_title)) = LOWER(TRIM(?))
                    OR INSTR(LOWER(card_title), LOWER(TRIM(?))) > 0
                    OR INSTR(LOWER(TRIM(?)), LOWER(card_title)) > 0
                )
                  AND sent_at >= datetime('now', ? || ' hours')
                LIMIT 1;
            """, (card_name, card_name, card_name, f"-{hours}"))
            return cursor.fetchone() is not None

    def has_been_notified(self, watchlist_id: int, card_id: str, price: float) -> bool:
        """
        Check if we already notified for this exact card offer at this price
        within the last 24 hours to prevent repeated spam.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM notification_history
                WHERE watchlist_id = ? 
                  AND card_id = ?
                  AND price = ?
                  AND sent_at >= datetime('now', '-24 hours')
                LIMIT 1;
            """, (watchlist_id, card_id, price))
            return cursor.fetchone() is not None

    def record_notification(self, watchlist_id: int, card_id: str, card_title: str, price: float, stock: int):
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO notification_history (watchlist_id, card_id, card_title, price, stock)
                VALUES (?, ?, ?, ?, ?);
            """, (watchlist_id, card_id, card_title, price, stock))
            conn.commit()

