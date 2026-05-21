#!/usr/bin/env python3
"""
Embed published_posts titles into sqlite-vec for semantic search.

Usage:
    python3 embed_posts.py           # embed all unembedded posts
    python3 embed_posts.py --all     # re-embed everything
    python3 embed_posts.py --id 123  # embed one specific post_id
"""

import sys
import json
import struct
import argparse
import urllib.request
from pathlib import Path

import apsw
import sqlite_vec

DB_PATH = str(Path.home() / ".alef-agent/workspace/newsroom/data/news_dedup.db")
OLLAMA_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "nomic-embed-text"
DIM = 768


def get_embedding(text: str) -> list:
    payload = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["embeddings"][0]


def floats_to_blob(vec: list) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def open_conn():
    conn = apsw.Connection(DB_PATH)
    conn.enableloadextension(True)
    conn.load_extension(sqlite_vec.loadable_path())
    conn.enableloadextension(False)
    return conn


def ensure_table(conn):
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS post_embeddings
        USING vec0(
            post_id INTEGER PRIMARY KEY,
            embedding float[768]
        )
    """)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="Re-embed all posts")
    parser.add_argument("--id", type=int, help="Embed single post by id")
    args = parser.parse_args()

    conn = open_conn()
    ensure_table(conn)

    if args.id:
        rows = list(conn.execute(
            "SELECT id, title, full_text FROM published_posts WHERE id = ?", (args.id,)
        ))
    elif args.all:
        rows = list(conn.execute(
            "SELECT id, title, full_text FROM published_posts ORDER BY id"
        ))
    else:
        rows = list(conn.execute("""
            SELECT p.id, p.title, p.full_text FROM published_posts p
            WHERE NOT EXISTS (
                SELECT 1 FROM post_embeddings e WHERE e.post_id = p.id
            )
            ORDER BY p.id
        """))

    total = len(rows)
    if total == 0:
        print("All posts already embedded.", file=sys.stderr)
        return

    print(f"Embedding {total} posts...", file=sys.stderr)
    ok = 0
    for i, (post_id, title, full_text) in enumerate(rows):
        text = title or ""
        if full_text:
            body = (full_text or "")[:300].strip()
            if body and body != title:
                text = f"{title}. {body}"
        try:
            vec = get_embedding(text)
            blob = floats_to_blob(vec)
            conn.execute("DELETE FROM post_embeddings WHERE post_id = ?", (post_id,))
            conn.execute(
                "INSERT INTO post_embeddings(post_id, embedding) VALUES (?, ?)",
                (post_id, blob)
            )
            ok += 1
            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{total}", file=sys.stderr)
        except Exception as e:
            print(f"  SKIP post {post_id}: {e}", file=sys.stderr)

    print(f"Done: {ok}/{total} embedded.", file=sys.stderr)


if __name__ == "__main__":
    main()
