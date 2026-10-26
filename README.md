# MTG Spot Cards Scraper, Search API & Explorer UI

A high-performance scraper, SQLite FTS5 search index, REST API, and web interface for extracting and searching all available (in-stock) Magic: The Gathering cards from [mtgspot.pl](https://mtgspot.pl).

---

## ⚡ Highlights

- **Direct Gateway Integration**: Fetches real-time card listings directly from the MTG Spot backend gateway (`https://gateway.mtgspot.pl`).
- **High Speed Batch Scraping**: Scrapes available in-stock cards in batches of up to 2,000–5,000 items per request, completing a full store inventory scrape (~66,600+ cards) in ~30–45 seconds.
- **SQLite + FTS5 Full-Text Search**: Sub-millisecond text search with prefix matching, word stems, and automatic index synchronisation triggers.
- **Rich Filtering & Sorting**:
  - Full-text search (`q`) across card names, expansions, rarities, and conditions
  - Expansion / Set filter (`expansion`, `id_expansion`)
  - Foil status (`is_foil=true|false`)
  - Condition (`condition=NM,EX,GD,LP,PL,PO`)
  - Rarity (`rarity=Mythic,Rare,Uncommon,Common,Token`)
  - Price bounds (`min_price`, `max_price` in PLN)
  - Minimum stock availability (`min_stock`)
  - Sorting (`price_asc`, `price_desc`, `title_asc`, `title_desc`, `stock_desc`, `newest`)
- **Modern REST API**: Built with FastAPI, auto-generating interactive Swagger docs at `/docs` and ReDoc at `/redoc`.
- **Interactive Dark-Mode Web Dashboard**: Served at `http://localhost:8000/` featuring live search, visual card grid with card art, spreadsheet table view, live sync progress bar, and CSV/JSON export.
- **CLI Utilities**: Command-line interface for headless scraping, querying, and checking database stats.
- **Discord Alerts & Watchlist Monitor**: Continuous background monitor sending rich Discord embeds as soon as out-of-stock or wanted cards appear in stock on MTG Spot (with price thresholds, condition/foil filters, and anti-spam deduplication).

---

## 📂 Project Structure

```
mtgspot-service/
├── cli.py                   # Command-line interface (scrape, search, stats, serve)
├── requirements.txt         # Python dependencies
├── README.md                # Documentation & quick start guide
├── mtgspot.db               # SQLite database with FTS5 search index
├── mtgspot/
│   ├── __init__.py
│   ├── db.py                # Database models, FTS5 triggers, queries & aggregations
│   ├── scraper.py           # MTG Spot API scraper & batch pipeline
│   ├── api.py               # FastAPI REST endpoints & background task runner
│   └── static/
│       └── index.html       # Single-page explorer UI with filters & real-time search
└── tests/
    └── test_all.py          # Unit tests for DB, FTS, scraper normalization & slugify
```

---

## 🐳 Docker Deployment

You can run the entire service (API, Web UI, Background Watcher) inside Docker with a single command:

### Using Docker Compose (Recommended)
```bash
docker compose up -d
```
Access the dashboard at **`http://localhost:8000`**. Data is persisted in the `mtgspot-data` volume.

### Using Docker CLI
```bash
# Build image
docker build -t mtgspot-service:latest .

# Run container with persistent data volume
docker run -d \
  --name mtgspot-service \
  -p 8000:8000 \
  -v mtgspot-data:/data \
  --restart unless-stopped \
  mtgspot-service:latest
```

---

## 🚀 Quick Start (Local)

### 1. Requirements

Ensure Python 3.10+ is installed with the required packages:

```bash
pip install -r requirements.txt
```

*(Core dependencies: `fastapi`, `uvicorn`, `requests`)*

---

### 2. Command-Line Usage (`cli.py`)

#### Scrape Available Cards
```bash
# Scrape all ~66,600+ in-stock cards into SQLite database
python3 cli.py scrape

# Test run with a limited number of cards (e.g. 2,000 cards)
python3 cli.py scrape --limit 2000

# Custom batch size and clear DB before scraping
python3 cli.py scrape --batch-size 2000 --clear
```

#### Query Cards Directly via CLI
```bash
# Full-text search
python3 cli.py search "Lightning Bolt"

# Filter by set and foil
python3 cli.py search --set "Mirrodin" --foil true

# Filter by price and condition
python3 cli.py search --min-price 10.0 --max-price 50.0 --condition "NM" --sort price_desc
```

#### View Database Statistics
```bash
python3 cli.py stats
```

#### Discord Watchlist & Notifications (CLI)
```bash
# Set your Discord webhook URL
python3 cli.py discord --webhook "https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN" --test

# Add cards to your alert watchlist
python3 cli.py watchlist add "Vile Entomber" --max-price 5.0
python3 cli.py watchlist add "Gravebreaker Lamia"
python3 cli.py watchlist add "Diabolic Tutor"

# List all watched cards
python3 cli.py watchlist list

# Trigger an immediate stock check & Discord notification
python3 cli.py watchlist check

# Run continuous background monitoring in terminal (checks every 5 min)
python3 cli.py watch --interval 300
```

#### Start the Server and Web UI
```bash
python3 cli.py serve --port 8000
```
Open **[http://localhost:8000](http://localhost:8000)** in your browser!

---

## 🌐 Web Explorer UI

Navigate to `http://localhost:8000/` to use the interactive dashboard:

- **Instant Search**: Type any card name or card text with debounced live updates.
- **Facet Filters**:
  - Filter by expansion / set (searchable datalist)
  - Toggle Foil / Regular cards
  - Multi-select conditions: `NM` (Near Mint), `EX` (Excellent), `GD` (Good), `LP` (Light Played), `PL` (Played), `PO` (Poor)
  - Multi-select rarities: `Mythic`, `Rare`, `Uncommon`, `Common`, `Token`
  - Price min/max sliders and inputs (in PLN zł)
  - Minimum stock threshold
- **Views**: Toggle between Card Grid (with high-res card art) and Compact Table view.
- **Direct Links**: Click "Buy" or the card link to open the exact card listing directly on `mtgspot.pl`.
- **Sync Trigger**: Click "Sync Scraper" to initiate a background scrape and watch real-time progress.
- **Export**: One-click export of filtered results to CSV or JSON.

---

## 🔌 REST API Reference

Swagger UI documentation is available at **`http://localhost:8000/docs`**.

### 1. Search Cards
`GET /api/cards`

Query Parameters:
| Parameter | Type | Default | Description |
|---|---|---|---|
| `q` | string | `null` | Full-text search across card title, set, rarity, and condition |
| `title` | string | `null` | Substring match on card name |
| `expansion` | string | `null` | Expansion / Set name substring match |
| `id_expansion` | integer | `null` | Numeric set ID |
| `condition` | string | `null` | Comma-separated conditions (e.g., `NM`, `NM,EX`) |
| `is_foil` | boolean | `null` | `true` for foil only, `false` for regular only |
| `rarity` | string | `null` | Comma-separated rarities (e.g., `Rare,Mythic`) |
| `language` | string | `null` | Card language (e.g., `English`) |
| `min_price` | float | `null` | Minimum price in PLN |
| `max_price` | float | `null` | Maximum price in PLN |
| `min_stock` | integer | `1` | Minimum stock copies (default 1 for available) |
| `sort` | string | `price_asc` | `price_asc`, `price_desc`, `title_asc`, `title_desc`, `stock_desc`, `newest` |
| `page` | integer | `1` | Page number |
| `page_size` | integer | `24` | Results per page (max 200) |

**Example Request:**
```bash
curl "http://localhost:8000/api/cards?q=Lotus&is_foil=false&min_price=100&sort=price_desc"
```

**Example Response:**
```json
{
  "items": [
    {
      "id": "abc123456",
      "id_article": 12345,
      "id_product": 67890,
      "title": "Jeweled Lotus",
      "description": "",
      "language": "English",
      "condition": "NM",
      "is_foil": false,
      "is_signed": false,
      "is_playset": false,
      "is_altered": false,
      "id_expansion": 1234,
      "expansion_name": "Commander Legends",
      "rarity": "Mythic",
      "stock": 2,
      "price": 349.99,
      "image": "https://gateway.mtgspot.pl/images/singles/CMR/67890.jpg",
      "card_url": "https://mtgspot.pl/single/commander-legends/1234/jeweled-lotus/67890/12345",
      "scraped_at": "2026-10-26 11:20:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 24,
  "total_pages": 1,
  "has_next": false,
  "has_prev": false
}
```

---

### 2. Get Card by ID
`GET /api/cards/{card_id}`

Fetches single card details by unique hash ID.

---

### 3. Get Filter Options & Aggregate Stats
`GET /api/filters`

Returns distinct available expansions, conditions, rarities, and price bounds currently stored in the database.

---

### 4. Trigger Scraper
`POST /api/scrape`

Starts a background task to scrape MTG Spot.

**Request Body (optional):**
```json
{
  "batch_size": 2000,
  "max_cards": null,
  "clear_existing": false
}
```

---

### 5. Check Scraper Progress
`GET /api/scrape/status`

**Response:**
```json
{
  "status": "running",
  "current": 24000,
  "total": 66645,
  "percent": 36.0,
  "message": "Fetched 24000/66645 cards...",
  "duration": 18.2,
  "error": null
}
```

---

### 6. Live Upstream Search (No local sync required)
`GET /api/live-search?name=Sol+Ring&is_foil=1`

Direct proxy to MTG Spot's gateway API for immediate live results.

---

## 🧪 Testing

Run test suite:
```bash
python3 -m unittest discover tests
```
