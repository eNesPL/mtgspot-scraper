#!/usr/bin/env python3
"""
MTG Spot Cards Scraper, Search & API CLI
"""

import sys
import argparse
import json
import logging
from mtgspot.db import Database, DEFAULT_DB_PATH
from mtgspot.scraper import MTGSpotScraper
from mtgspot.discord import send_test_webhook
from mtgspot.watcher import WatchService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def cmd_scrape(args):
    db = Database(args.db)
    scraper = MTGSpotScraper(db=db)
    print(f"[*] Starting MTG Spot Scraper...")
    if args.clear:
        print("[*] Clearing existing cards from database...")
        db.clear_cards()

    def on_progress(cur, tot, pct, msg):
        sys.stdout.write(f"\r\033[K[+] {msg} ({pct:.1f}%)")
        sys.stdout.flush()

    try:
        res = scraper.scrape_all_available_cards(
            batch_size=args.batch_size,
            max_cards=args.limit,
            progress_callback=on_progress
        )
        print("\n\n" + "=" * 50)
        print("Scrape Complete!")
        print(f"Total in MTG Spot store : {res['total_available_in_store']:,}")
        print(f"Cards scraped and saved : {res['cards_scraped']:,}")
        print(f"Duration                : {res['duration_seconds']}s")
        print("=" * 50)
    except KeyboardInterrupt:
        print("\n[!] Scrape interrupted by user.")
    except Exception as e:
        print(f"\n[!] Scrape failed: {e}")


def cmd_search(args):
    db = Database(args.db)
    cards, total = db.search_cards(
        q=args.query,
        expansion=args.set,
        condition=args.condition,
        is_foil=args.foil,
        rarity=args.rarity,
        min_price=args.min_price,
        max_price=args.max_price,
        min_stock=args.min_stock,
        sort_by=args.sort,
        limit=args.limit
    )

    print(f"Found {total} matching cards (displaying top {len(cards)}):")
    print("-" * 80)
    for c in cards:
        foil_str = "✨ FOIL" if c["is_foil"] else "REG "
        cond_str = f"[{c['condition']}]"
        print(f"{c['title']:<30} | {c['expansion_name']:<24} | {cond_str:<5} | {foil_str} | Stock: {c['stock']:<3} | {c['price']:>6.2f} zł")
    print("-" * 80)


def cmd_stats(args):
    db = Database(args.db)
    data = db.get_filter_options()
    stats = data.get("stats", {})
    meta = db.get_scrape_meta()

    print("=" * 50)
    print("MTG Spot Local Database Stats")
    print("=" * 50)
    print(f"Total Available Cards : {stats.get('total_cards', 0):,}")
    print(f"Unique Card Names     : {stats.get('unique_titles', 0):,}")
    print(f"Total Stock Copies    : {stats.get('total_stock', 0):,}")
    print(f"Price Range (PLN)     : {stats.get('min_price', 0):.2f} zł - {stats.get('max_price', 0):.2f} zł (avg: {stats.get('avg_price', 0):.2f} zł)")
    print(f"Foil Copies           : {stats.get('foil_count', 0):,}")
    print(f"Last Scraped At       : {meta.get('last_scraped_at', 'Never')}")
    print("=" * 50)


def cmd_serve(args):
    import uvicorn
    print(f"[*] Starting MTG Spot API & Web UI on http://{args.host}:{args.port}")
    print(f"[*] API Docs available at http://{args.host}:{args.port}/docs")
    uvicorn.run("mtgspot.api:app", host=args.host, port=args.port, reload=args.reload)


def cmd_watchlist(args):
    db = Database(args.db)
    scraper = MTGSpotScraper(db=db)
    watcher = WatchService(db=db, scraper=scraper)

    if args.action == "list":
        items = db.get_watchlist()
        if not items:
            print("Lista obserwowanych kart jest pusta. Użyj: cli.py watchlist add \"Nazwa Karty\"")
            return
        print("=" * 80)
        print(f"{'ID':<4} | {'Karta':<28} | {'Max Cena':<10} | {'Stan':<6} | {'Foil':<6} | {'Ilość':<6} | {'Status'}")
        print("-" * 80)
        for it in items:
            p_str = f"{it['max_price']:.2f} zł" if it['max_price'] is not None else "Bez limitu"
            c_str = it['condition'] or "Każdy"
            f_str = "✨ Foil" if it['is_foil'] is True else ("Regular" if it['is_foil'] is False else "Każdy")
            st_str = f"{it['last_stock']} szt."
            status = "🟢 Dostępna!" if it['last_stock'] > 0 else "⚪ Oczekiwanie"
            print(f"{it['id']:<4} | {it['card_name']:<28} | {p_str:<10} | {c_str:<6} | {f_str:<6} | {st_str:<6} | {status}")
        print("=" * 80)

    elif args.action == "add":
        item_id = db.add_watchlist_item(
            card_name=args.name,
            max_price=args.max_price,
            condition=args.condition,
            is_foil=args.foil,
            expansion=args.set,
            webhook_url=args.webhook
        )
        print(f"[+] Dodano '{args.name}' do listy obserwowanych (ID: {item_id})!")
        # Instant check
        item = db.get_watchlist_item(item_id)
        default_webhook = db.get_setting("discord_webhook")
        res = watcher.check_watchlist_item(item, default_webhook)
        if res.get("in_stock_count", 0) > 0:
            print(f"🎉 UWAGA: Karta jest obecnie DOSTĘPNA na MTG Spot! Znaleziono {res['in_stock_count']} sztuk.")
            if res.get("alerts_sent", 0) > 0:
                print(f"[+] Wysłano powiadomienie Discord!")
        else:
            print(f"[*] Karta obecnie nie jest na stanie. Monitor powiadomi Cię na Discordzie, gdy tylko się pojawi.")

    elif args.action == "remove":
        if db.delete_watchlist_item(args.id):
            print(f"[+] Usunięto pozycję ID {args.id} z listy obserwowanych.")
        else:
            print(f"[!] Nie znaleziono pozycji o ID {args.id}.")

    elif args.action == "check":
        print("[*] Sprawdzanie dostępności wszystkich kart z listy...")
        res = watcher.check_all_watchlist()
        total_alerts = sum(r.get("alerts_sent", 0) for r in res)
        print(f"[+] Sprawdzono {len(res)} pozycji. Wysłano {total_alerts} nowych powiadomień Discord.")
        for r in res:
            stock = r.get("in_stock_count", 0)
            icon = "🟢" if stock > 0 else "⚪"
            print(f"  {icon} {r['card_name']}: {stock} szt. w sklepie")


def cmd_discord(args):
    db = Database(args.db)
    if args.webhook:
        db.set_setting("discord_webhook", args.webhook.strip())
        print(f"[+] Zapisano domyślny Discord Webhook URL.")

    if args.test:
        wh = args.webhook or db.get_setting("discord_webhook")
        if not wh:
            print("[!] Podaj webhook do przetestowania: cli.py discord --webhook <URL> --test")
            return
        print(f"[*] Wysyłanie wiadomości testowej do Discord...")
        ok, msg = send_test_webhook(wh)
        if ok:
            print(f"[+] Sukces: {msg}")
        else:
            print(f"[!] Błąd: {msg}")

    if not args.webhook and not args.test:
        current = db.get_setting("discord_webhook")
        if current:
            # Mask part of the webhook URL for privacy
            masked = current[:35] + "..." + current[-10:] if len(current) > 50 else current
            print(f"Aktualny Discord Webhook: {masked}")
        else:
            print("Discord Webhook nie jest jeszcze skonfigurowany.")


def cmd_watch(args):
    db = Database(args.db)
    scraper = MTGSpotScraper(db=db)
    watcher = WatchService(db=db, scraper=scraper)
    interval = args.interval or int(db.get_setting("watch_interval_seconds", "300"))
    db.set_setting("watch_interval_seconds", str(interval))

    wh = db.get_setting("discord_webhook")
    print(f"[*] Uruchamianie ciągłego monitora MTG Spot -> Discord...")
    print(f"[*] Interwał sprawdzania: {interval} sekund")
    if wh:
        print(f"[*] Domyślny Discord Webhook: aktywny")
    else:
        print(f"[!] Ostrzeżenie: Brak skonfigurowanego Discord Webhook! (Użyj: cli.py discord --webhook <URL>)")

    print("[*] Naciśnij CTRL+C, aby zatrzymać monitor.\n")
    try:
        while True:
            t_now = time.strftime("%H:%M:%S")
            print(f"[{t_now}] Sprawdzanie kart z listy obserwowanych...")
            res = watcher.check_all_watchlist()
            for r in res:
                stock = r.get("in_stock_count", 0)
                if stock > 0:
                    print(f"  🎯 {r['card_name']}: {stock} szt. DOSTĘPNA! (Wysłano powiadomień: {r.get('alerts_sent', 0)})")
                else:
                    print(f"  ⚪ {r['card_name']}: brak na stanie")
            print(f"[*] Oczekiwanie {interval}s do następnego sprawdzenia...\n")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[!] Monitor zatrzymany przez użytkownika.")


def main():
    parser = argparse.ArgumentParser(description="MTG Spot Scraper and Search Tool")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"Path to SQLite DB (default: {DEFAULT_DB_PATH})")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Scrape command
    scrape_parser = subparsers.add_parser("scrape", help="Scrape available cards from MTG Spot")
    scrape_parser.add_argument("--limit", type=int, default=None, help="Max cards to scrape (default: all)")
    scrape_parser.add_argument("--batch-size", type=int, default=2000, help="Batch size per request (default: 2000)")
    scrape_parser.add_argument("--clear", action="store_true", help="Clear existing DB records before scraping")

    # Search command
    search_parser = subparsers.add_parser("search", help="Search cards in the local database")
    search_parser.add_argument("query", nargs="?", default=None, help="Full-text search query")
    search_parser.add_argument("--set", help="Filter by set/expansion name")
    search_parser.add_argument("--condition", help="Filter by condition (NM, EX, GD, LP, PL, PO)")
    search_parser.add_argument("--foil", type=lambda x: (str(x).lower() == 'true'), default=None, help="Filter by foil (true/false)")
    search_parser.add_argument("--rarity", help="Filter by rarity (Common, Uncommon, Rare, Mythic, Token)")
    search_parser.add_argument("--min-price", type=float, help="Min price in PLN")
    search_parser.add_argument("--max-price", type=float, help="Max price in PLN")
    search_parser.add_argument("--min-stock", type=int, default=1, help="Min stock (default: 1)")
    search_parser.add_argument("--sort", default="price_asc", choices=["price_asc", "price_desc", "title_asc", "title_desc", "stock_desc", "newest"])
    search_parser.add_argument("--limit", type=int, default=20, help="Number of results to display")

    # Stats command
    subparsers.add_parser("stats", help="Display database summary statistics")

    # Serve command
    serve_parser = subparsers.add_parser("serve", help="Run the FastAPI server and Web UI")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
    serve_parser.add_argument("--reload", action="store_true", help="Enable auto-reload")

    # Watchlist command
    wl_parser = subparsers.add_parser("watchlist", help="Manage monitored cards (Discord alerts)")
    wl_sub = wl_parser.add_subparsers(dest="action", required=True)

    # Watchlist add
    wl_add = wl_sub.add_parser("add", help="Add card to watchlist")
    wl_add.add_argument("name", help="Card name to watch (e.g. 'Vile Entomber')")
    wl_add.add_argument("--max-price", type=float, help="Max price alert threshold in PLN")
    wl_add.add_argument("--condition", help="Required condition (e.g. 'NM', 'NM,EX')")
    wl_add.add_argument("--foil", type=lambda x: (str(x).lower() == 'true'), default=None, help="Foil (true/false)")
    wl_add.add_argument("--set", help="Specific expansion name")
    wl_add.add_argument("--webhook", help="Custom Discord webhook URL for this card")

    # Watchlist list
    wl_sub.add_parser("list", help="List all watched cards")

    # Watchlist remove
    wl_rm = wl_sub.add_parser("remove", help="Remove card from watchlist by ID")
    wl_rm.add_argument("id", type=int, help="Watchlist item ID")

    # Watchlist check
    wl_sub.add_parser("check", help="Check MTG Spot now and send Discord alerts for available cards")

    # Discord command
    disc_parser = subparsers.add_parser("discord", help="Configure or test Discord Webhook")
    disc_parser.add_argument("--webhook", help="Set default Discord Webhook URL")
    disc_parser.add_argument("--test", action="store_true", help="Send test message to Discord webhook")

    # Watch daemon/loop command
    watch_parser = subparsers.add_parser("watch", help="Run continuous background monitor for MTG Spot -> Discord")
    watch_parser.add_argument("--interval", type=int, default=300, help="Check interval in seconds (default: 300)")

    args = parser.parse_args()

    if args.command == "scrape":
        cmd_scrape(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "watchlist":
        cmd_watchlist(args)
    elif args.command == "discord":
        cmd_discord(args)
    elif args.command == "watch":
        cmd_watch(args)


if __name__ == "__main__":
    main()
