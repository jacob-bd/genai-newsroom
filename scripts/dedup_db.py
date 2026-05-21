#!/usr/bin/env python3
"""
dedup_db.py — SQLite-backed dedup database for the news scan pipeline.

Shared by quality_score.py and llm_editor.py. Stores normalized URLs
and titles from every scan to prevent cross-scan duplicates.

Database: ~/.alef-agent/workspace/newsroom/data/news_dedup.db

Usage as module:
    from dedup_db import DedupDB
    db = DedupDB()
    if db.is_seen(url):
        print("duplicate!")
    db.record(url, title, source, status="presented")

Usage as CLI (seed from logs):
    python3 dedup_db.py --seed
    python3 dedup_db.py --stats
    python3 dedup_db.py --check-url "https://example.com/article"
"""

import os
import re
import sqlite3
import subprocess
import sys
import argparse
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Set
from urllib.parse import urlparse, urlunparse

# ── Paths ────────────────────────────────────────────────────────────
WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE",
                                os.path.expanduser("~/.alef-agent/workspace")))
DB_PATH = WORKSPACE / "memory" / "news_dedup.db"
NEWS_LOG = WORKSPACE / "memory" / "news_log.md"
SCANNER_PRESENTED = WORKSPACE / "memory" / "scanner_presented.md"


# ── Entity Aliases (for channel history search) ──────────────────────
# Maps canonical company/org name -> list of aliases that should match.
# When searching, a query matching any alias expands to the canonical name.
# When indexing, title text is scanned for all aliases to tag entities.

ENTITY_ALIASES = {
    "Nvidia": ["NVIDIA", "Jensen Huang", "DLSS", "Nemotron", "CUDA", "GeForce",
               "Rubin", "Vera", "GTC", "Feynman", "NemoClaw", "Blackwell"],
    "OpenAI": ["ChatGPT", "GPT-4", "GPT-4.5", "GPT-5", "Sam Altman", "Codex",
               "DALL-E", "Sora"],
    "Google": ["Gemini", "DeepMind", "Google AI", "NotebookLM", "Android",
               "AI Studio", "Google Cloud", "Mariner"],
    "Anthropic": ["Claude", "Claude Sonnet", "Claude Opus", "Dario Amodei"],
    "Meta": ["Meta AI", "Llama", "Zuckerberg", "Facebook", "Instagram"],
    "Microsoft": ["Copilot", "Azure", "Bing", "GitHub Copilot", "MAI-Image"],
    "Apple": ["Apple Intelligence", "Siri", "iPhone", "Apple AI"],
    "Mistral": ["Mistral AI", "Mistral Small", "Mistral Forge", "Mixtral"],
    "xAI": ["Grok", "Elon Musk"],
    "Amazon": ["AWS", "Amazon Web Services", "Bedrock", "Alexa"],
    "Samsung": ["Samsung AI", "HBM4", "Samsung Foundry"],
    "Qualcomm": ["Snapdragon", "Qualcomm AI"],
    "Intel": ["Intel AI", "Arc", "Gaudi"],
    "AMD": ["Radeon", "EPYC", "ROCm"],
    "Tesla": ["Tesla AI", "FSD", "Optimus"],
    "Shopify": ["Shopify AI"],
    "LinkedIn": ["LinkedIn AI"],
    "Cursor": ["Cursor AI", "Anysphere"],
    "Hugging Face": ["HuggingFace", "Transformers"],
    "Perplexity": ["Perplexity AI", "Perplexity Health"],
    "Alibaba": ["Qwen", "Alibaba Cloud"],
    "ByteDance": ["TikTok", "Doubao"],
    "Baidu": ["Ernie", "Baidu AI"],
    "DeepSeek": ["DeepSeek AI", "DeepSeek-R1"],
    "Stability AI": ["Stable Diffusion", "StabilityAI"],
    "Adobe": ["Firefly", "Adobe AI"],
    "Visa": ["Visa AI", "Visa Intelligent Commerce"],
    "Stripe": ["Stripe AI"],
    "DoorDash": ["DoorDash AI"],
    "Spotify": ["Spotify AI"],
    "OpenClaw": ["MCP", "Model Context Protocol"],
    "US Government": ["White House", "Pentagon", "Congress", "DARPA", "Treasury"],
    "UK Government": ["UK AI", "Rachel Reeves"],
    "EU": ["EU AI Act", "European Union AI"],
    "China": ["China AI", "Beijing", "Hua Hong"],
    "Unsloth": ["Unsloth AI", "Unsloth Studio"],
    "MiniMax": ["MiniMax AI"],
    "Harmonic": ["Aristotle", "Harmonic AI"],
    "Multiverse Computing": ["CompactifAI"],
    "Kalshi": ["Kalshi AI", "Prediction Markets"],
    "Super Micro": ["Supermicro", "SMCI"],
    "Encyclopedia Britannica": ["Britannica"],
    "Xiaomi": ["MiMo", "Xiaomi AI"],
}

# Build reverse lookup: alias -> canonical name
_ALIAS_TO_CANONICAL = {}
for _canon, _aliases in ENTITY_ALIASES.items():
    _ALIAS_TO_CANONICAL[_canon.lower()] = _canon
    for _alias in _aliases:
        _ALIAS_TO_CANONICAL[_alias.lower()] = _canon


# ── URL normalization ────────────────────────────────────────────────

def normalize_url(url):
    """
    Normalize a URL for dedup comparison:
    - Strip query parameters and fragments
    - Remove www. prefix
    - Normalize to https://
    - Remove trailing slashes
    - Lowercase domain
    """
    if not url:
        return ""

    url = url.strip().rstrip(".,;:)")

    try:
        parsed = urlparse(url)
    except Exception:
        return url.lower()

    # Normalize scheme to https
    scheme = "https"

    # Lowercase and strip www from domain
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    # Keep path, strip trailing slash (but keep "/" for root)
    path = parsed.path.rstrip("/") if parsed.path != "/" else "/"

    # Drop query params and fragment entirely
    normalized = urlunparse((scheme, netloc, path, "", "", ""))

    return normalized


# ── Database class ───────────────────────────────────────────────────

class DedupDB:
    """SQLite-backed dedup database."""

    def __init__(self, db_path=None):
        # type: (Optional[str]) -> None
        self.db_path = db_path or str(DB_PATH)
        self._ensure_db()

    def _ensure_db(self):
        """Create database and tables if they don't exist."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url_normalized TEXT NOT NULL,
                url_original TEXT NOT NULL,
                title TEXT NOT NULL,
                source TEXT DEFAULT '',
                status TEXT DEFAULT 'presented',
                first_seen TEXT NOT NULL,
                scan_id TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_url_norm
            ON seen_articles(url_normalized)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_first_seen
            ON seen_articles(first_seen)
        """)
        # ── Published posts (permanent, never pruned) ────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS published_posts (
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
        # FTS5 full-text search on titles and full_text
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS published_posts_fts
                USING fts5(title, full_text, content='published_posts', content_rowid='id')
            """)
        except sqlite3.OperationalError:
            pass  # FTS5 already exists or not available
        # Entity tagging for published posts
        conn.execute("""
            CREATE TABLE IF NOT EXISTS post_entities (
                post_id INTEGER REFERENCES published_posts(id),
                entity TEXT NOT NULL,
                PRIMARY KEY (post_id, entity)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_post_entity
            ON post_entities(entity)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_published_date
            ON published_posts(date)
        """)
        conn.commit()
        conn.close()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def is_seen(self, url):
        """Check if a normalized URL already exists in the database."""
        norm = normalize_url(url)
        if not norm:
            return False
        conn = self._connect()
        cursor = conn.execute(
            "SELECT 1 FROM seen_articles WHERE url_normalized = ? LIMIT 1",
            (norm,)
        )
        found = cursor.fetchone() is not None
        conn.close()
        return found

    def find_similar_titles(self, title, threshold=0.75, days=7):
        """
        Find titles in the DB similar to the given title.
        Only checks articles from the last N days for performance.
        Returns list of (db_title, similarity_score, url_normalized).
        """
        if not title:
            return []

        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = self._connect()
        cursor = conn.execute(
            "SELECT title, url_normalized FROM seen_articles WHERE first_seen > ?",
            (cutoff,)
        )
        rows = cursor.fetchall()
        conn.close()

        matches = []
        title_lower = title.lower()
        for db_title, db_url in rows:
            sim = SequenceMatcher(None, title_lower, db_title.lower()).ratio()
            if sim >= threshold:
                matches.append((db_title, sim, db_url))

        matches.sort(key=lambda x: -x[1])
        return matches

    def record(self, url, title, source="", status="presented", scan_id=""):
        """Record an article in the database."""
        norm = normalize_url(url)
        if not norm:
            return
        now = datetime.now().isoformat()
        conn = self._connect()
        conn.execute(
            """INSERT INTO seen_articles
               (url_normalized, url_original, title, source, status, first_seen, scan_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (norm, url, title, source, status, now, scan_id)
        )
        conn.commit()
        conn.close()

    def bulk_check(self, articles):
        """
        Check a list of article dicts against the database.
        Returns (new_articles, duplicate_articles, url_dupe_count, title_dupe_count).

        Each article dict must have 'url' and 'title' keys.
        Checks both URL match and title similarity (>75% over last 2 days).
        """
        new = []
        dupes = []
        url_dupes = 0
        title_dupes = 0

        for article in articles:
            url = article.get("url", "")
            title = article.get("title", "")

            # Check URL first (fast)
            if self.is_seen(url):
                url_dupes += 1
                dupes.append(article)
                continue

            # Check title similarity (slower, only last 2 days for speed)
            similar = self.find_similar_titles(title, threshold=0.75, days=2)
            if similar:
                title_dupes += 1
                dupes.append(article)
                continue

            new.append(article)

        return new, dupes, url_dupes, title_dupes

    def record_batch(self, articles, status="presented", scan_id=""):
        """Record multiple articles in a single transaction."""
        if not articles:
            return
        now = datetime.now().isoformat()
        conn = self._connect()
        for a in articles:
            url = a.get("url", "")
            title = a.get("title", "")
            source = a.get("source", "")
            norm = normalize_url(url)
            if norm:
                conn.execute(
                    """INSERT INTO seen_articles
                       (url_normalized, url_original, title, source, status, first_seen, scan_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (norm, url, title, source, status, now, scan_id)
                )
        conn.commit()
        conn.close()

    def stats(self):
        """Return database statistics."""
        conn = self._connect()
        total = conn.execute("SELECT COUNT(*) FROM seen_articles").fetchone()[0]
        presented = conn.execute(
            "SELECT COUNT(*) FROM seen_articles WHERE status='presented'"
        ).fetchone()[0]
        published = conn.execute(
            "SELECT COUNT(*) FROM seen_articles WHERE status='published'"
        ).fetchone()[0]
        today = datetime.now().strftime("%Y-%m-%d")
        today_count = conn.execute(
            "SELECT COUNT(*) FROM seen_articles WHERE first_seen LIKE ?",
            (today + "%",)
        ).fetchone()[0]
        pub_count = conn.execute(
            "SELECT COUNT(*) FROM published_posts"
        ).fetchone()[0]
        conn.close()
        return {
            "total": total,
            "presented": presented,
            "published_posts": pub_count,
            "published": published,
            "today": today_count,
        }

    def seed_from_logs(self, news_log_path=None, scanner_presented_path=None):
        """
        One-time import: parse existing news_log.md and scanner_presented.md
        to populate the database with historical URLs and titles.
        """
        news_log = news_log_path or str(NEWS_LOG)
        scanner = scanner_presented_path or str(SCANNER_PRESENTED)
        imported = 0

        # Parse news_log.md
        # Format: DATE | POSTED | TITLE | msg_id:NNN | t.me_url | article_url
        try:
            with open(news_log, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("|")
                    if len(parts) >= 6:
                        title = parts[2].strip()
                        article_url = parts[5].strip()
                        if article_url and not article_url.startswith("http"):
                            continue
                        if title and article_url:
                            if not self.is_seen(article_url):
                                self.record(article_url, title, status="published")
                                imported += 1
        except FileNotFoundError:
            pass

        # Parse scanner_presented.md
        # Format: [TIMESTAMP] TITLE | URL
        url_pattern = re.compile(r'https?://[^\s|>\]\)"\']+')
        try:
            with open(scanner, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Extract timestamp, title, URL
                    m = re.match(r'\[([^\]]+)\]\s*(.+)', line)
                    if not m:
                        continue
                    rest = m.group(2)
                    parts = rest.split("|")
                    title = parts[0].strip() if parts else ""
                    url = parts[1].strip() if len(parts) > 1 else ""
                    if not url:
                        urls = url_pattern.findall(rest)
                        url = urls[0] if urls else ""
                    url = url.rstrip(".,;:)")
                    if title and url and url.startswith("http"):
                        if "t.me/" in url:
                            continue
                        if not self.is_seen(url):
                            self.record(url, title, status="presented")
                            imported += 1
        except FileNotFoundError:
            pass

        return imported


    # ── Published Posts (Channel History) ─────────────────────────────

    @staticmethod
    def _extract_entities(title):
        """Extract canonical entity names from a title using alias matching."""
        entities = set()
        title_lower = title.lower()
        for alias_lower, canonical in _ALIAS_TO_CANONICAL.items():
            # Word boundary check: ensure alias isn't part of a larger word
            idx = title_lower.find(alias_lower)
            if idx == -1:
                continue
            # Check boundaries
            before_ok = (idx == 0 or not title_lower[idx - 1].isalnum())
            after_idx = idx + len(alias_lower)
            after_ok = (after_idx >= len(title_lower) or
                        not title_lower[after_idx].isalnum())
            if before_ok and after_ok:
                entities.add(canonical)
        return entities

    @staticmethod
    def _extract_domain(url):
        """Extract clean domain from a URL."""
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]
            return domain
        except Exception:
            return ""

    def record_published(self, title, date, message_id=None,
                         telegram_link=None, source_url=None, full_text=""):
        """Record a published post to the permanent channel history."""
        if not title or not date:
            return
        domain = self._extract_domain(source_url)
        conn = self._connect()
        post_id = None
        try:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO published_posts
                   (date, title, full_text, message_id, telegram_link, source_url, source_domain)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (date, title.strip(), full_text, message_id, telegram_link, source_url, domain)
            )
            if cursor.rowcount == 0:
                conn.close()
                return  # Already exists (duplicate message_id)
            post_id = cursor.lastrowid

            # Update FTS index
            try:
                conn.execute(
                    "INSERT INTO published_posts_fts(rowid, title, full_text) VALUES (?, ?, ?)",
                    (post_id, title.strip(), full_text)
                )
            except sqlite3.OperationalError:
                pass  # FTS might not be available

            # Extract and store entities
            entities = self._extract_entities(title)
            for entity in entities:
                conn.execute(
                    "INSERT OR IGNORE INTO post_entities (post_id, entity) VALUES (?, ?)",
                    (post_id, entity)
                )

            conn.commit()
        except Exception as e:
            print(f"Warning: failed to record published post: {e}", file=sys.stderr)
        finally:
            conn.close()

        if post_id:
            embed_script = Path(__file__).parent / "embed_posts.py"
            subprocess.Popen(
                [sys.executable, str(embed_script), "--id", str(post_id)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def search_published(self, query, limit=10):
        """
        Search published posts by keyword with entity alias expansion.

        Strategy:
        1. Check if query matches any entity alias -> expand to canonical name
        2. Search post_entities table for the canonical name
        3. Also run FTS5 keyword search on titles
        4. Merge and deduplicate results, ordered by date descending

        Returns list of dicts: {title, date, message_id, telegram_link, source_url}
        """
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        results = {}

        # Step 1: Entity alias expansion
        query_lower = query.lower().strip()
        canonical = _ALIAS_TO_CANONICAL.get(query_lower)

        # Step 2: Search by entity tag
        if canonical:
            rows = conn.execute("""
                SELECT p.id, p.date, p.title, p.message_id,
                       p.telegram_link, p.source_url
                FROM published_posts p
                JOIN post_entities pe ON p.id = pe.post_id
                WHERE pe.entity = ?
                ORDER BY p.date DESC
            """, (canonical,)).fetchall()
            for row in rows:
                results[row["id"]] = dict(row)

        # Step 3: FTS5 keyword search on titles
        try:
            fts_query = " AND ".join(f'"{w}"*' for w in query.split() if w)
            fts_rows = conn.execute("""
                SELECT p.id, p.date, p.title, p.message_id,
                       p.telegram_link, p.source_url
                FROM published_posts_fts fts
                JOIN published_posts p ON fts.rowid = p.id
                WHERE published_posts_fts MATCH ?
                ORDER BY p.date DESC
                LIMIT ?
            """, (fts_query, limit * 2)).fetchall()
            for row in fts_rows:
                if row["id"] not in results:
                    results[row["id"]] = dict(row)
        except sqlite3.OperationalError:
            # FTS5 not available, fall back to LIKE
            like_rows = conn.execute("""
                SELECT id, date, title, message_id, telegram_link, source_url
                FROM published_posts
                WHERE title LIKE ? OR full_text LIKE ?
                ORDER BY date DESC
                LIMIT ?
            """, (f"%{query}%", f"%{query}%", limit * 2)).fetchall()
            for row in like_rows:
                if row["id"] not in results:
                    results[row["id"]] = dict(row)

        conn.close()

        # Sort by date descending, limit results
        sorted_results = sorted(results.values(), key=lambda x: x.get("date", ""),
                                reverse=True)[:limit]

        # Clean up for output (remove internal id)
        for r in sorted_results:
            r.pop("id", None)

        return sorted_results

    def published_stats(self):
        """Return statistics about the published posts archive."""
        conn = self._connect()
        total = conn.execute("SELECT COUNT(*) FROM published_posts").fetchone()[0]
        entities = conn.execute(
            "SELECT COUNT(DISTINCT entity) FROM post_entities"
        ).fetchone()[0]
        date_range = conn.execute(
            "SELECT MIN(date), MAX(date) FROM published_posts"
        ).fetchone()
        top_entities = conn.execute("""
            SELECT entity, COUNT(*) as cnt
            FROM post_entities
            GROUP BY entity
            ORDER BY cnt DESC
            LIMIT 10
        """).fetchall()
        conn.close()
        return {
            "total_posts": total,
            "unique_entities": entities,
            "date_range": (date_range[0], date_range[1]) if date_range else (None, None),
            "top_entities": [(e[0], e[1]) for e in top_entities],
        }

    def seed_published_from_log(self, news_log_path=None):
        """
        Seed published_posts from news_log.md.
        Handles both flat-line format and structured markdown format.
        """
        log_path = news_log_path or str(NEWS_LOG)
        imported = 0

        try:
            with open(log_path, "r") as f:
                lines = f.readlines()
        except FileNotFoundError:
            print(f"news_log.md not found at {log_path}", file=sys.stderr)
            return 0

        # State for structured entries (## heading blocks)
        current_title = None
        current_date = None
        current_msg_id = None
        current_link = None
        current_source = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Flat format: DATE | POSTED | TITLE | msg_id:NNN | tg_link | source_url
            if "|" in line and "POSTED" in line and not line.startswith("#"):
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 6:
                    date = parts[0].split()[0] if parts[0] else ""
                    title = parts[2]
                    msg_part = parts[3]
                    tg_link = parts[4]
                    source_url = parts[5] if len(parts) > 5 else ""

                    # Extract message_id
                    msg_id = None
                    if "msg_id:" in msg_part:
                        try:
                            msg_id = int(msg_part.replace("msg_id:", "").strip())
                        except ValueError:
                            pass
                    elif "msg_id" in msg_part:
                        try:
                            msg_id = int(re.search(r'\d+', msg_part).group())
                        except (ValueError, AttributeError):
                            pass

                    if title and date and msg_id:
                        self.record_published(
                            title=title, date=date, message_id=msg_id,
                            telegram_link=tg_link, source_url=source_url
                        )
                        imported += 1
                continue

            # Structured format: ## DATE Title (Message NNN)
            if line.startswith("## "):
                # Flush previous structured entry
                if current_title and current_date and current_msg_id:
                    self.record_published(
                        title=current_title, date=current_date,
                        message_id=current_msg_id,
                        telegram_link=current_link, source_url=current_source
                    )
                    imported += 1

                # Parse new heading
                heading = line[3:]
                # Extract date (YYYY-MM-DD at start)
                date_match = re.match(r'(\d{4}-\d{2}-\d{2})\s+(.*)', heading)
                if date_match:
                    current_date = date_match.group(1)
                    rest = date_match.group(2)
                    # Extract message ID from "(Message NNN)"
                    msg_match = re.search(r'\(Message\s+(\d+)\)', rest)
                    if msg_match:
                        current_msg_id = int(msg_match.group(1))
                        current_title = rest[:msg_match.start()].strip()
                    else:
                        current_msg_id = None
                        current_title = rest.strip()
                else:
                    current_date = None
                    current_title = None
                    current_msg_id = None
                current_link = None
                current_source = None
                continue

            # Inside structured block: extract fields
            if current_title:
                if line.startswith("**Telegram Live**:"):
                    current_link = line.split(":", 1)[1].strip() if ":" in line else None
                    # Handle markdown link format
                    url_match = re.search(r'https?://[^\s)]+', line)
                    if url_match:
                        current_link = url_match.group()
                elif line.startswith("**Source**:"):
                    url_match = re.search(r'https?://[^\s)]+', line)
                    if url_match:
                        current_source = url_match.group()
                    elif ":" in line:
                        current_source = line.split(":", 1)[1].strip()

        # Flush final structured entry
        if current_title and current_date and current_msg_id:
            self.record_published(
                title=current_title, date=current_date,
                message_id=current_msg_id,
                telegram_link=current_link, source_url=current_source
            )
            imported += 1

        return imported


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="News dedup database utility")
    parser.add_argument("--seed", action="store_true",
                       help="Seed dedup DB from news_log.md and scanner_presented.md")
    parser.add_argument("--stats", action="store_true",
                       help="Show database statistics")
    parser.add_argument("--check-url",
                       help="Check if a URL has been seen")
    parser.add_argument("--check-title",
                       help="Find similar titles in the DB")
    # Channel history (published posts) commands
    parser.add_argument("--search",
                       help="Search published channel history by keyword or company")
    parser.add_argument("--search-limit", type=int, default=10,
                       help="Max results for --search (default: 10)")
    parser.add_argument("--seed-published", action="store_true",
                       help="Seed published_posts from news_log.md")
    parser.add_argument("--published-stats", action="store_true",
                       help="Show published posts archive statistics")
    parser.add_argument("--json", action="store_true",
                       help="Output search results as JSON")
    args = parser.parse_args()

    db = DedupDB()

    if args.seed:
        count = db.seed_from_logs()
        print("Seeded %d articles from existing logs" % count)
        s = db.stats()
        print("DB stats: %d total, %d published, %d presented" % (
            s["total"], s["published"], s["presented"]))

    elif args.stats:
        s = db.stats()
        print("Total: %d" % s["total"])
        print("  Published (dedup): %d" % s["published"])
        print("  Presented: %d" % s["presented"])
        print("  Published posts archive: %d" % s["published_posts"])
        print("  Today: %d" % s["today"])

    elif args.check_url:
        norm = normalize_url(args.check_url)
        seen = db.is_seen(args.check_url)
        print("URL: %s" % args.check_url)
        print("Normalized: %s" % norm)
        print("Seen: %s" % seen)

    elif args.check_title:
        matches = db.find_similar_titles(args.check_title)
        if matches:
            print("Found %d similar titles:" % len(matches))
            for title, sim, url in matches[:5]:
                print("  %.0f%% | %s | %s" % (sim * 100, title[:80], url))
        else:
            print("No similar titles found")

    elif args.search:
        results = db.search_published(args.search, limit=args.search_limit)
        if args.json:
            import json as json_mod
            print(json_mod.dumps(results, indent=2, ensure_ascii=False))
        elif results:
            print("Found %d matching posts for '%s':" % (len(results), args.search))
            for r in results:
                link = r.get("telegram_link", "") or ""
                print("  %s | %s | %s" % (
                    r.get("date", "?"),
                    r.get("title", "")[:80],
                    link
                ))
        else:
            print("No published posts found matching '%s'" % args.search)

    elif args.seed_published:
        count = db.seed_published_from_log()
        print("Seeded %d posts from news_log.md" % count)
        ps = db.published_stats()
        print("Published archive: %d posts, %d entities" % (
            ps["total_posts"], ps["unique_entities"]))
        if ps["top_entities"]:
            print("Top entities:")
            for name, cnt in ps["top_entities"]:
                print("  %s: %d posts" % (name, cnt))

    elif args.published_stats:
        ps = db.published_stats()
        print("Published Posts Archive")
        print("  Total: %d" % ps["total_posts"])
        print("  Entities: %d unique" % ps["unique_entities"])
        if ps["date_range"][0]:
            print("  Date range: %s to %s" % ps["date_range"])
        if ps["top_entities"]:
            print("  Top entities:")
            for name, cnt in ps["top_entities"]:
                print("    %s: %d posts" % (name, cnt))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
