#!/usr/bin/env python3
"""
backfill_channel_history.py — One-time scraper for Gen AI Spotlight Telegram channel.

Fetches ALL historical posts from the public t.me/s/genaispot page and indexes
them in the published_posts table of news_dedup.db.

This is a one-time operation. After backfill, new posts are indexed automatically
via the telegram_post.py hook.

Usage:
    python3 backfill_channel_history.py              # Full backfill
    python3 backfill_channel_history.py --dry-run     # Show what would be indexed
    python3 backfill_channel_history.py --stats       # Show current archive stats
"""

import argparse
import re
import sys
import time
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError

# Import DedupDB from the same directory
sys.path.insert(0, __import__("os").path.dirname(__file__))
from dedup_db import DedupDB

CHANNEL = "genaispot"
BASE_URL = f"https://t.me/s/{CHANNEL}"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
REQUEST_DELAY = 1.0  # seconds between page fetches (be polite)


def fetch_page(before_id=None):
    """Fetch a page of messages from the public channel preview."""
    url = BASE_URL
    if before_id:
        url = f"{BASE_URL}?before={before_id}"

    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except URLError as e:
        print(f"  Error fetching {url}: {e}", file=sys.stderr)
        return None


def parse_messages(html):
    """
    Parse message blocks from the Telegram channel HTML.

    Returns list of dicts: {message_id, date, text, links}
    """
    messages = []

    # Find all message IDs on the page
    msg_ids = re.findall(r'data-post="' + CHANNEL + r'/(\d+)"', html)
    if not msg_ids:
        return messages

    # Find all datetimes
    datetimes = re.findall(r'datetime="([^"]+)"', html)

    # Find all message text blocks
    texts = re.findall(
        r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
        html, re.DOTALL
    )

    # Find links within message blocks
    all_links = re.findall(
        r'<a[^>]+href="(https?://[^"]+)"[^>]*>',
        html
    )

    # Match messages with dates and texts
    for i, msg_id in enumerate(msg_ids):
        msg = {
            "message_id": int(msg_id),
            "date": None,
            "text": "",
            "links": [],
        }

        # Match datetime (if available)
        if i < len(datetimes):
            try:
                dt_str = datetimes[i]
                # Parse ISO format: 2026-01-01T20:49:46+00:00
                dt = datetime.fromisoformat(dt_str)
                msg["date"] = dt.strftime("%Y-%m-%d")
            except (ValueError, IndexError):
                pass

        # Match text (if available)
        if i < len(texts):
            raw = texts[i]
            # Strip HTML tags but preserve text
            clean = re.sub(r'<br\s*/?>', '\n', raw)
            clean = re.sub(r'<[^>]+>', '', clean)
            clean = clean.strip()
            msg["text"] = clean

            # Extract links from this specific message block
            msg_links = re.findall(r'href="(https?://[^"]+)"', raw)
            # Filter out Telegram internal links
            msg["links"] = [
                link for link in msg_links
                if "t.me/" not in link and "telegram.org" not in link
            ]

        messages.append(msg)

    return messages


def extract_title(text, max_length=200):
    """Extract a clean title from message text."""
    if not text:
        return ""

    # Get first meaningful line (skip empty lines)
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    if not lines:
        return ""

    title = lines[0]

    # Clean up common patterns
    title = re.sub(r'^[🔥💡⚡🚀🤖📢💰🎯🏆🧠🎉]+\s*', '', title)  # Leading emoji clusters
    title = title.strip()

    # Truncate if too long
    if len(title) > max_length:
        title = title[:max_length].rsplit(' ', 1)[0] + "..."

    return title


def backfill(dry_run=False):
    """Fetch all posts from the channel and index them."""
    db = DedupDB()
    total_fetched = 0
    total_indexed = 0
    total_skipped = 0
    page_count = 0
    current_before = None

    print(f"{'[DRY RUN] ' if dry_run else ''}Backfilling channel history from t.me/s/{CHANNEL}")
    print("=" * 60)

    while True:
        page_count += 1
        label = f"before={current_before}" if current_before else "latest"
        print(f"  Page {page_count} ({label})...", end=" ", flush=True)

        html = fetch_page(before_id=current_before)
        if not html:
            print("FAILED - stopping")
            break

        messages = parse_messages(html)
        if not messages:
            print("no messages - done!")
            break

        print(f"{len(messages)} messages", end="")

        indexed_this_page = 0
        for msg in messages:
            total_fetched += 1
            title = extract_title(msg["text"])
            if not title or not msg["date"]:
                total_skipped += 1
                continue

            # Skip system messages (channel created, photo updated, etc.)
            skip_patterns = ["Channel created", "Channel photo", "pinned"]
            if any(p.lower() in title.lower() for p in skip_patterns):
                total_skipped += 1
                continue

            source_url = msg["links"][0] if msg["links"] else ""
            telegram_link = f"https://t.me/{CHANNEL}/{msg['message_id']}"

            if not dry_run:
                db.record_published(
                    title=title,
                    date=msg["date"],
                    message_id=msg["message_id"],
                    telegram_link=telegram_link,
                    source_url=source_url,
                    full_text=msg["text"],
                )

            indexed_this_page += 1
            total_indexed += 1

        print(f" -> {indexed_this_page} indexed")

        # Get the lowest message ID for pagination
        lowest_id = min(msg["message_id"] for msg in messages)
        if lowest_id <= 1:
            print("  Reached the beginning of the channel!")
            break

        current_before = lowest_id
        time.sleep(REQUEST_DELAY)

    print("=" * 60)
    print(f"Backfill complete!")
    print(f"  Pages fetched: {page_count}")
    print(f"  Messages found: {total_fetched}")
    print(f"  Posts indexed: {total_indexed}")
    print(f"  Skipped (system/empty): {total_skipped}")

    if not dry_run:
        ps = db.published_stats()
        print(f"\nPublished archive now: {ps['total_posts']} posts, "
              f"{ps['unique_entities']} entities")
        if ps["date_range"][0]:
            print(f"  Date range: {ps['date_range'][0]} to {ps['date_range'][1]}")
        if ps["top_entities"]:
            print("  Top entities:")
            for name, cnt in ps["top_entities"][:10]:
                print(f"    {name}: {cnt} posts")


def main():
    parser = argparse.ArgumentParser(
        description="Backfill Gen AI Spotlight channel history into the dedup database"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be indexed without writing to DB")
    parser.add_argument("--stats", action="store_true",
                        help="Show current published archive statistics")
    args = parser.parse_args()

    if args.stats:
        db = DedupDB()
        ps = db.published_stats()
        print(f"Published Posts Archive")
        print(f"  Total: {ps['total_posts']}")
        print(f"  Entities: {ps['unique_entities']} unique")
        if ps["date_range"][0]:
            print(f"  Date range: {ps['date_range'][0]} to {ps['date_range'][1]}")
        if ps["top_entities"]:
            print("  Top entities:")
            for name, cnt in ps["top_entities"]:
                print(f"    {name}: {cnt} posts")
        return

    backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
