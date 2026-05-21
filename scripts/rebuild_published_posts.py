#!/usr/bin/env python3
"""
rebuild_published_posts.py — Full clean rebuild of published_posts from Telegram.

Backs up DB, drops published_posts + derived tables (post_entities, post_embeddings,
FTS indexes), scrapes all posts from https://t.me/s/genaispot/N, inserts clean records.

seen_articles is NEVER touched.

Usage:
  HOME=/Users/jbd python3 rebuild_published_posts.py [--dry-run] [--end N]

Options:
  --dry-run   Parse and report without writing anything
  --end N     Override max message ID (default: auto-detect from Telegram)
"""

import os
import sys
import time
import shutil
import sqlite3
import subprocess
import re
import argparse
from datetime import datetime

try:
    import apsw
    import sqlite_vec
    HAS_VEC = True
except ImportError:
    HAS_VEC = False

CHANNEL_SLUG = "genaispot"
DB_PATH = os.path.expanduser("~/.alef-agent/workspace/newsroom/data/news_dedup.db")
BACKUP_PATH = DB_PATH + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
DELAY = 0.4

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
}

def fetch_page(msg_id, retries=2, delay=1.5):
    url = f"https://t.me/s/{CHANNEL_SLUG}/{msg_id}"
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["gsearch", "fetch", url],
                capture_output=True, text=True, timeout=25,
                env={**os.environ, "HOME": os.path.expanduser("~")}
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(delay)
    return None

def detect_max_id():
    """Fetch the channel page and find the highest message ID."""
    content = fetch_page(9999999)
    if not content:
        return None
    ids = re.findall(rf"https://t\.me/{CHANNEL_SLUG}/(\d+)", content)
    if ids:
        return max(int(x) for x in ids)
    return None

def extract_post_block(content, msg_id):
    pattern = rf"\[\]\(https://t\.me/{CHANNEL_SLUG}/(\d+)\)"
    parts = re.split(pattern, content)
    for i in range(1, len(parts) - 1, 2):
        if int(parts[i]) == msg_id:
            return parts[i + 1].strip()
    return None

def parse_date(block):
    """
    Extract post date from block text.
    Telegram /s/ pages render dates like 'May 5', '5 May', 'May 5, 2026', or 'HH:MM' (today).
    Returns YYYY-MM-DD string or None.
    """
    # Full date: May 13, 2026 or 13 May 2026
    m = re.search(
        r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*,?\s+(\d{4})",
        block, re.IGNORECASE
    )
    if m:
        day, mon, year = int(m.group(1)), MONTH_MAP[m.group(2).lower()[:3]], int(m.group(3))
        return f"{year:04d}-{mon:02d}-{day:02d}"

    m = re.search(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})",
        block, re.IGNORECASE
    )
    if m:
        mon, day, year = MONTH_MAP[m.group(1).lower()[:3]], int(m.group(2)), int(m.group(3))
        return f"{year:04d}-{mon:02d}-{day:02d}"

    # Short date without year: "May 13" — assume current year
    m = re.search(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})\b",
        block, re.IGNORECASE
    )
    if m:
        mon, day = MONTH_MAP[m.group(1).lower()[:3]], int(m.group(2))
        year = datetime.now().year
        return f"{year:04d}-{mon:02d}-{day:02d}"

    return None

def parse_block(block, msg_id):
    if not block or len(block) < 10:
        return None

    # Strip reaction counts at end of block
    block = re.sub(r"\n[🔥👍❤️🎉😂🤔👏💯🔴🟢⚡👎😢😮😡🎊🤩💔🐳]+\d*\s*$", "", block).strip()

    date_str = parse_date(block) or datetime.now().strftime("%Y-%m-%d")

    # Extract source URL from markdown links
    md_links = re.findall(r"\[([^\]]+)\]\((https?://[^\)]+)\)", block)
    source_url = ""
    for label, url in md_links:
        if "t.me" not in url and "telegram" not in url and "preview.redd.it" not in url:
            source_url = url.rstrip(".,;)")
            break

    # Fall back to plain URL scan
    if not source_url:
        for url in re.findall(r"https?://[^\s\)\]>\"']+", block):
            if "t.me" not in url and "telegram" not in url:
                source_url = url.rstrip(".,;)")
                break

    # Convert markdown to plain text
    plain = re.sub(r"\[([^\]]*)\]\([^\)]+\)", r"\1", block)
    plain = re.sub(r"\*+", "", plain)
    plain = plain.strip()

    if not plain or len(plain) < 10:
        return None

    lines = [l.strip() for l in plain.splitlines() if l.strip()]
    if not lines:
        return None

    title_raw = lines[0]
    title = re.sub(r"^[\U0001F300-\U0001FFFF\U00002600-\U000027BF\s#*]+", "", title_raw).strip()
    if not title:
        title = title_raw[:200]
    title = title[:200]

    # Skip channel header / admin messages
    if title.lower().startswith("gen ai spotlight") or len(plain) < 20:
        return None

    domain = ""
    m = re.match(r"https?://(?:www\.)?([^/]+)", source_url or "")
    if m:
        domain = m.group(1)

    return {
        "title": title,
        "full_text": plain,
        "source_url": source_url,
        "source_domain": domain,
        "date": date_str,
    }

def drop_and_recreate(db_path):
    """Drop all derived tables and recreate published_posts clean.
    Uses apsw+sqlite_vec to handle vec0 virtual tables if available."""
    if HAS_VEC:
        conn = apsw.Connection(db_path)
        conn.enableloadextension(True)
        conn.load_extension(sqlite_vec.loadable_path())
        conn.enableloadextension(False)
        execute = conn.execute
    else:
        conn = sqlite3.connect(db_path)
        execute = conn.execute

    # FTS tables
    for t in ["published_posts_fts", "published_posts_fts_data",
              "published_posts_fts_idx", "published_posts_fts_docsize",
              "published_posts_fts_config"]:
        try:
            execute(f"DROP TABLE IF EXISTS [{t}]")
        except Exception as e:
            print(f"  WARN drop {t}: {e}")

    # Embeddings (sqlite-vec virtual tables)
    for t in ["post_embeddings", "post_embeddings_chunks",
              "post_embeddings_info", "post_embeddings_rowids",
              "post_embeddings_vector_chunks00"]:
        try:
            execute(f"DROP TABLE IF EXISTS [{t}]")
        except Exception as e:
            print(f"  WARN drop {t}: {e}")

    # Derived tables referencing published_posts
    execute("DROP TABLE IF EXISTS post_entities")
    execute("DROP INDEX IF EXISTS idx_published_date")
    execute("DROP TABLE IF EXISTS published_posts")

    conn.close()

    # Recreate with standard sqlite3 (no extension needed for plain tables)
    plain = sqlite3.connect(db_path)
    plain.execute("""
        CREATE TABLE published_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            title TEXT NOT NULL,
            full_text TEXT DEFAULT '',
            message_id INTEGER UNIQUE,
            telegram_link TEXT,
            source_url TEXT,
            source_domain TEXT,
            published_at TEXT DEFAULT (datetime('now'))
        )
    """)
    plain.execute("CREATE INDEX idx_published_date ON published_posts(date)")
    plain.execute("""
        CREATE TABLE post_entities (
            post_id INTEGER REFERENCES published_posts(id),
            entity TEXT NOT NULL,
            PRIMARY KEY (post_id, entity)
        )
    """)
    plain.execute("CREATE INDEX idx_post_entity ON post_entities(entity)")
    plain.commit()
    plain.close()

def insert_post(conn, data, msg_id):
    conn.execute(
        """INSERT OR IGNORE INTO published_posts
           (date, title, full_text, message_id, telegram_link, source_url, source_domain)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (data["date"], data["title"], data["full_text"], msg_id,
         f"https://t.me/{CHANNEL_SLUG}/{msg_id}",
         data["source_url"], data["source_domain"])
    )

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no DB writes")
    parser.add_argument("--end", type=int, help="Max message ID to scrape")
    args = parser.parse_args()

    print("=== rebuild_published_posts.py ===")
    print(f"DB: {DB_PATH}")

    # Determine max ID
    if args.end:
        max_id = args.end
    else:
        print("Detecting max message ID from Telegram...", flush=True)
        max_id = detect_max_id()
        if not max_id:
            print("ERROR: Could not detect max ID. Pass --end N explicitly.")
            sys.exit(1)
    print(f"Max ID: {max_id}")
    print(f"IDs to scan: 1 to {max_id} ({max_id} total)")

    if args.dry_run:
        print("DRY RUN — no DB changes\n")
    else:
        # Backup
        print(f"Backing up DB to {BACKUP_PATH}...")
        shutil.copy2(DB_PATH, BACKUP_PATH)
        print("Backup done.")

        # Wipe + recreate
        print("Dropping and recreating tables (seen_articles preserved)...")
        drop_and_recreate(DB_PATH)
        print("Schema ready.\n")

    inserted = 0
    deleted = 0
    parse_fail = 0
    fetch_fail = 0

    conn = sqlite3.connect(DB_PATH) if not args.dry_run else None

    for msg_id in range(1, max_id + 1):
        time.sleep(DELAY)

        content = fetch_page(msg_id)
        if not content:
            fetch_fail += 1
            print(f"  [{msg_id}] FETCH FAIL", flush=True)
            continue

        block = extract_post_block(content, msg_id)
        if not block:
            deleted += 1
            continue

        data = parse_block(block, msg_id)
        if not data:
            parse_fail += 1
            continue

        print(f"  [{msg_id}] {data['date']} | {data['title'][:65]}", flush=True)

        if not args.dry_run:
            insert_post(conn, data, msg_id)
            if inserted % 50 == 0 and inserted > 0:
                conn.commit()
            inserted += 1
        else:
            inserted += 1

    if conn:
        conn.commit()
        conn.close()

    if not args.dry_run:
        print("\nRebuilding FTS index...", flush=True)
        fts_conn = sqlite3.connect(DB_PATH)
        fts_conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS published_posts_fts
            USING fts5(title, full_text, content='published_posts', content_rowid='id')
        """)
        fts_conn.execute("INSERT INTO published_posts_fts(published_posts_fts) VALUES('rebuild')")
        fts_count = fts_conn.execute("SELECT COUNT(*) FROM published_posts_fts").fetchone()[0]
        fts_conn.commit()
        fts_conn.close()
        print(f"FTS ready: {fts_count} rows indexed.", flush=True)

    print(f"\n=== DONE ===")
    print(f"Inserted:    {inserted}")
    print(f"Deleted:     {deleted}")
    print(f"Parse fail:  {parse_fail}")
    print(f"Fetch fail:  {fetch_fail}")
    if not args.dry_run:
        print(f"\nNOTE: post_embeddings dropped. Re-run embed script to rebuild vectors.")
        print(f"Backup at: {BACKUP_PATH}")

if __name__ == "__main__":
    main()
