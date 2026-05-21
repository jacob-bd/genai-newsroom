#!/usr/bin/env python3
"""
Semantic search over published Gen AI Spotlight posts.

Uses sqlite-vec + nomic-embed-text (Ollama) for cosine similarity search.

Usage:
    python3 post_search.py "new model releases"
    python3 post_search.py "AI security vulnerabilities" --top 5
    python3 post_search.py "voice AI" --days 30
    python3 post_search.py "workforce cuts" --json
"""

import sys
import json
import struct
import argparse
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta

import apsw
import sqlite_vec

DB_PATH = str(Path.home() / ".alef-agent/workspace/newsroom/data/news_dedup.db")
OLLAMA_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "nomic-embed-text"


def get_embedding(text: str) -> list:
    payload = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["embeddings"][0]


def floats_to_blob(vec: list) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def open_conn():
    conn = apsw.Connection(DB_PATH)
    conn.enableloadextension(True)
    conn.load_extension(sqlite_vec.loadable_path())
    conn.enableloadextension(False)
    return conn


def main():
    parser = argparse.ArgumentParser(description="Semantic post search")
    parser.add_argument("query", help="Natural language search query")
    parser.add_argument("--top", type=int, default=10, help="Number of results (default: 10)")
    parser.add_argument("--days", type=int, default=0, help="Limit to last N days (0 = all time)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")
    args = parser.parse_args()

    conn = open_conn()

    try:
        q_vec = get_embedding(args.query)
    except Exception as e:
        print(f"ERROR: Could not embed query: {e}", file=sys.stderr)
        sys.exit(1)

    q_blob = floats_to_blob(q_vec)

    if args.days > 0:
        # Date-scoped search: fetch ALL posts in window, rank by similarity in Python
        cutoff = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
        candidate_ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM published_posts WHERE date >= ? ORDER BY date DESC",
                (cutoff,)
            )
        ]
        if not candidate_ids:
            rows = []
        else:
            total_posts = conn.execute("SELECT COUNT(*) FROM post_embeddings").fetchone()[0]
            fetch_k = total_posts
            all_rows = list(conn.execute("""
                SELECT
                    p.id,
                    p.date,
                    p.title,
                    p.telegram_link,
                    p.source_url,
                    e.distance
                FROM post_embeddings e
                JOIN published_posts p ON p.id = e.post_id
                WHERE e.embedding MATCH ?
                  AND k = ?
                ORDER BY e.distance
            """, (q_blob, fetch_k)))
            scoped_ids = set(candidate_ids)
            rows = [r for r in all_rows if r[0] in scoped_ids][:args.top]
    else:
        fetch_k = args.top * 4
        rows = list(conn.execute("""
            SELECT
                p.id,
                p.date,
                p.title,
                p.telegram_link,
                p.source_url,
                e.distance
            FROM post_embeddings e
            JOIN published_posts p ON p.id = e.post_id
            WHERE e.embedding MATCH ?
              AND k = ?
            ORDER BY e.distance
        """, (q_blob, fetch_k)))[:args.top]
    conn.close()

    if not rows:
        print("No results found.", file=sys.stderr)
        sys.exit(0)

    def cos_sim(dist):
        # nomic-embed-text outputs unit vectors; L2 dist → cosine sim: cos = 1 - d²/2
        return max(0.0, 1.0 - (dist ** 2) / 2)

    if args.as_json:
        out = [{"id": r[0], "date": r[1], "title": r[2],
                "telegram_link": r[3], "source_url": r[4],
                "similarity": round(cos_sim(r[5]), 4)} for r in rows]
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f'\nSemantic search: "{args.query}"\n' + "-" * 50)
        for i, (post_id, date, title, tg_link, src_url, dist) in enumerate(rows, 1):
            score_pct = round(cos_sim(dist) * 100, 1)
            print(f"{i:2}. [{date}] {title}")
            if tg_link:
                print(f"    {tg_link}  (similarity: {score_pct}%)")
            print()


if __name__ == "__main__":
    main()
