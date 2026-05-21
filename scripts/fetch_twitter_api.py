#!/usr/bin/env python3
"""
Fetch AI tweets via twitterapi.io as a supplement to Twitter CLI.

Uses twitterapi.io's search endpoint for keyword-based Twitter searches.
This catches breaking news that might be missed by the Twitter CLI's
account-based approach.

Output: pipe-delimited TITLE|URL|SOURCE format.
Sources tagged with "(tweet)" when no external article URL exists.

Usage:
    python3 fetch_twitter_api.py [--max-queries 3]

Environment:
    TWITTERAPI_IO_KEY — required
"""

import json
import os
import re
import sys
import ssl
import argparse
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

_SSL_CTX = ssl.create_default_context()

TIMEOUT = 15
API_BASE = "https://api.twitterapi.io/twitter"

# Search queries — focused on breaking AI news
SEARCH_QUERIES = [
    '"breaking" (AI OR "artificial intelligence" OR LLM) -is:retweet lang:en',
    '(Anthropic OR Claude OR OpenAI) (announce OR launch OR release) -is:retweet lang:en',
    '(AI OR "artificial intelligence") (acquisition OR merger OR billion OR deal) -is:retweet lang:en',
]

# Minimum engagement to filter noise
MIN_LIKES = 50
MIN_FOLLOWERS = 5000


def search_twitter(query, api_key, max_results=10):
    """Search tweets via twitterapi.io."""
    from urllib.parse import urlencode

    params = urlencode({
        "query": query,
        "queryType": "Latest",
    })
    url = f"{API_BASE}/tweet/advanced_search?{params}"

    req = Request(url, headers={
        "X-API-Key": api_key,
        "User-Agent": "NewsScanner/1.0",
    })

    try:
        with urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data.get("tweets", [])
    except HTTPError as e:
        if e.code == 401:
            print("  Error: Invalid TWITTERAPI_IO_KEY", file=sys.stderr)
        elif e.code == 429:
            print("  Warning: twitterapi.io rate limit", file=sys.stderr)
        else:
            print(f"  Warning: twitterapi.io HTTP {e.code}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  Warning: twitterapi.io error: {e}", file=sys.stderr)
        return []


def extract_url_from_tweet(tweet):
    """
    Extract the first external URL from a tweet.
    Returns (url, is_tweet_only):
      - (external_url, False) if an article URL was found in entities
      - (tweet_url, True) if only the tweet's own URL is available
      - ("", True) if no URL could be constructed
    """
    entities = tweet.get("entities", {})
    urls = entities.get("urls", [])
    for u in urls:
        expanded = u.get("expanded_url", u.get("url", ""))
        if expanded and "twitter.com" not in expanded and "t.co" not in expanded and "x.com" not in expanded:
            return expanded, False

    author = tweet.get("author", {})
    screen_name = author.get("userName", "")
    tweet_id = tweet.get("id", "")
    if screen_name and tweet_id:
        return f"https://x.com/{screen_name}/status/{tweet_id}", True
    return "", True


def _load_env_file():
    env_path = os.path.expanduser("~/.alef-agent/.env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ[key] = value


def main():
    _load_env_file()
    parser = argparse.ArgumentParser(description="Fetch AI tweets via twitterapi.io")
    parser.add_argument('--max-queries', type=int, default=3,
                       help='Max queries to run (default: 3)')
    args = parser.parse_args()

    api_key = os.environ.get("TWITTERAPI_IO_KEY", "")
    if not api_key:
        print("  Warning: TWITTERAPI_IO_KEY not set, skipping", file=sys.stderr)
        return 0

    seen_urls = set()
    all_results = []

    queries = SEARCH_QUERIES[:args.max_queries]

    for query in queries:
        tweets = search_twitter(query, api_key)
        for tweet in tweets:
            likes = tweet.get("likeCount", 0)
            author = tweet.get("author", {})
            followers = author.get("followers", 0)

            if likes < MIN_LIKES and followers < MIN_FOLLOWERS:
                continue

            text = tweet.get("text", "").strip()
            if not text:
                continue

            title = re.sub(r'https?://\S+', '', text).strip()
            title = title.replace('\n', ' ').replace('|', ' -')
            title = re.sub(r'\s+', ' ', title)

            if len(title) > 200:
                title = title[:197] + "..."
            if len(title) < 15:
                continue

            url, is_tweet_only = extract_url_from_tweet(tweet)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            screen_name = author.get("userName", "unknown")
            source_tag = f"X/@{screen_name} (tweet)" if is_tweet_only else f"X/@{screen_name}"
            all_results.append(f"{title}|{url}|{source_tag}")

    for line in all_results:
        print(line)

    print(f"  Done: {len(all_results)} tweets from {len(queries)} queries", file=sys.stderr)


if __name__ == "__main__":
    main()
