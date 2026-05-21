#!/usr/bin/env python3
"""
Fetch AI news via Tavily web search API.

Uses Tavily's search API (free tier: 1000 queries/month) to find
breaking AI news that RSS feeds might miss.

Output: pipe-delimited TITLE|URL|SOURCE format.

Usage:
    python3 fetch_web_news.py [--max-queries 5] [--max-results 5]

Environment:
    TAVILY_API_KEY — required
"""

import json
import os
import sys
import argparse
import ssl
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

_SSL_CTX = ssl.create_default_context()

TIMEOUT = 15
TAVILY_API = "https://api.tavily.com/search"

# Focused search queries — customize for your editorial focus
SEARCH_QUERIES = [
    "AI artificial intelligence breaking news today",
    "Anthropic Claude OpenAI latest announcement",
    "AI acquisition merger funding billion",
    "AI model release launch new",
    "AI regulation government policy",
]

# Domains to skip (already covered by RSS feeds)
SKIP_DOMAINS = {
    "reddit.com", "twitter.com", "x.com", "youtube.com",
    "github.com", "arxiv.org",
}


def search_tavily(query, api_key, max_results=5):
    """Execute a Tavily search and return results."""
    payload = json.dumps({
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_answer": False,
        "include_raw_content": False,
        "days": 2,
    }).encode('utf-8')

    req = Request(TAVILY_API, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "NewsScanner/1.0",
    })

    try:
        with urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data.get("results", [])
    except HTTPError as e:
        if e.code == 401:
            print("  Error: Invalid TAVILY_API_KEY", file=sys.stderr)
        elif e.code == 429:
            print("  Warning: Tavily rate limit reached", file=sys.stderr)
        else:
            print(f"  Warning: Tavily HTTP {e.code}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  Warning: Tavily error: {e}", file=sys.stderr)
        return []


def get_domain(url):
    """Extract domain from URL."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def main():
    parser = argparse.ArgumentParser(description="Fetch AI news via Tavily search")
    parser.add_argument('--max-queries', type=int, default=3,
                       help='Max search queries to run (default: 3, saves API quota)')
    parser.add_argument('--max-results', type=int, default=5,
                       help='Results per query (default: 5)')
    args = parser.parse_args()

    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        print("  Warning: TAVILY_API_KEY not set, skipping web search", file=sys.stderr)
        return 0

    seen_urls = set()
    all_results = []

    queries = SEARCH_QUERIES[:args.max_queries]

    for query in queries:
        results = search_tavily(query, api_key, args.max_results)
        for r in results:
            url = r.get("url", "")
            title = r.get("title", "").strip()
            if not url or not title:
                continue

            domain = get_domain(url)
            if domain in SKIP_DOMAINS:
                continue
            if url in seen_urls:
                continue
            from urllib.parse import urlparse
            path = urlparse(url).path.rstrip('/')
            if not path or path in ('/technology', '/tech', '/ai', '/tech/ai'):
                continue
            seen_urls.add(url)

            title_clean = title.replace('|', ' -')
            source = f"Tavily/{domain}" if domain else "Tavily/Web"

            all_results.append(f"{title_clean}|{url}|{source}")

    for line in all_results:
        print(line)

    print(f"  Done: {len(all_results)} articles from {len(queries)} queries", file=sys.stderr)


if __name__ == "__main__":
    main()
