#!/usr/bin/env python3
"""
Backfill missing published_posts entries by fetching public Telegram preview pages.

Uses https://t.me/s/genaispot/N — no bot API, no channel forwarding.

Usage:
  HOME=/Users/jbd python3 backfill_published_posts.py [--dry-run] [--start N] [--end N]
"""

import os
import sys
import time
import sqlite3
import subprocess
import re
import argparse
from datetime import datetime

CHANNEL_SLUG = "genaispot"
DB_PATH = os.path.expanduser("~/.alef-agent/workspace/newsroom/data/news_dedup.db")
DELAY = 0.5

def get_gaps(db_path, start=None, end=None):
    conn = sqlite3.connect(db_path)
    ids = set(r[0] for r in conn.execute(
        "SELECT message_id FROM published_posts WHERE message_id IS NOT NULL"))
    max_id = conn.execute("SELECT MAX(message_id) FROM published_posts").fetchone()[0]
    conn.close()
    start = start or 1
    end = end or max_id
    return [i for i in range(start, end + 1) if i not in ids]

def fetch_page(msg_id):
    url = f"https://t.me/s/{CHANNEL_SLUG}/{msg_id}"
    try:
        result = subprocess.run(
            ["gsearch", "fetch", url],
            capture_output=True, text=True, timeout=20,
            env={**os.environ, "HOME": os.path.expanduser("~")}
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None

def extract_post_block(content, msg_id):
    """
    /s/ pages contain multiple posts. Each post starts with:
      [](https://t.me/genaispot/N)
    Extract the block for the specific msg_id.
    """
    # Split on post boundary markers
    pattern = rf"\[\]\(https://t\.me/{CHANNEL_SLUG}/(\d+)\)"
    parts = re.split(pattern, content)

    # parts = [pre, id1, block1, id2, block2, ...]
    for i in range(1, len(parts) - 1, 2):
        if int(parts[i]) == msg_id:
            return parts[i + 1].strip()
    return None

def extract_source_url(text):
    urls = re.findall(r"https?://[^\s\)\]>\"']+", text or "")
    for url in urls:
        if "t.me" not in url and "telegram" not in url:
            return url.rstrip(".,;)")
    return ""

def parse_block(block):
    if not block or len(block) < 10:
        return None

    # Strip reaction counts (e.g. "🔥1", "👁️ 5") at end
    block = re.sub(r"\n[🔥👍❤️🎉😂🤔👏💯🔴🟢⚡]+\d*\s*$", "", block).strip()

    # Extract source URL from markdown links like [Bloomberg](https://...)
    md_links = re.findall(r"\[([^\]]+)\]\((https?://[^\)]+)\)", block)
    source_url = ""
    for label, url in md_links:
        if "t.me" not in url and "telegram" not in url:
            source_url = url
            break

    # Also check plain URLs
    if not source_url:
        source_url = extract_source_url(block)

    # Remove markdown link syntax for plain text
    plain = re.sub(r"\[([^\]]*)\]\([^\)]+\)", r"\1", block)
    # Remove leftover markdown
    plain = re.sub(r"\*+", "", plain)
    plain = plain.strip()

    if not plain or len(plain) < 10:
        return None

    # Title = first non-empty line, strip leading emoji
    lines = [l.strip() for l in plain.splitlines() if l.strip()]
    if not lines:
        return None

    title_raw = lines[0]
    title = re.sub(r"^[\U0001F300-\U0001FFFF☀-➿\s#*]+", "", title_raw).strip()
    if not title:
        title = title_raw[:200]
    title = title[:200]

    domain = ""
    m = re.match(r"https?://(?:www\.)?([^/]+)", source_url or "")
    if m:
        domain = m.group(1)

    return {"title": title, "full_text": plain, "source_url": source_url, "domain": domain}

def insert_post(db_path, data, msg_id, date_str):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO published_posts
               (date, title, full_text, message_id, telegram_link, source_url, source_domain)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (date_str, data["title"], data["full_text"], msg_id,
             f"https://t.me/{CHANNEL_SLUG}/{msg_id}",
             data["source_url"], data["domain"])
        )
        inserted = cur.rowcount > 0
        conn.commit()
    finally:
        conn.close()
    return inserted

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    args = parser.parse_args()

    gaps = get_gaps(DB_PATH, args.start, args.end)
    print(f"Found {len(gaps)} missing message_ids to check")
    if args.dry_run:
        print("DRY RUN — no writes\n")

    recovered = 0
    not_found = 0
    skipped = 0

    for msg_id in gaps:
        time.sleep(DELAY)

        content = fetch_page(msg_id)
        if not content:
            not_found += 1
            continue

        block = extract_post_block(content, msg_id)
        if not block:
            # Post not in this page or deleted
            not_found += 1
            continue

        data = parse_block(block)
        if not data:
            skipped += 1
            continue

        date_str = datetime.now().strftime("%Y-%m-%d")
        print(f"  [{msg_id}] {data['title'][:70]}")

        if not args.dry_run:
            ok = insert_post(DB_PATH, data, msg_id, date_str)
            if ok:
                recovered += 1

    print()
    print(f"Done. Recovered: {recovered} | Not found/deleted: {not_found} | Skipped: {skipped}")
    if args.dry_run:
        print("(dry run — nothing written)")

if __name__ == "__main__":
    main()
