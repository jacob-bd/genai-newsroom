#!/usr/bin/env python3
"""
Fetch Digg AI news (di.gg/ai) directly using HTTP requests and regular expressions.

Bypasses headless Chrome scraping (no bot blocks, no CAPTCHAs, 100% reliable).
Outputs pipe-delimited TITLE|URL|SOURCE format.

Usage:
    python3 fetch_digg_news.py [--max-results 15]
"""

import argparse
import html
import re
import ssl
import sys
import urllib.request

_SSL_CTX = ssl.create_default_context()

TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def fetch_digg_ai(max_results=15):
    """Fetch and parse stories from di.gg/ai."""
    url = "https://digg.com/ai"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
            html_content = resp.read().decode("utf-8", errors="replace")
            return parse_digg_html(html_content, max_results)
    except Exception as e:
        print(f"  Warning: Digg AI fetch failed: {e}", file=sys.stderr)
        return []


def parse_digg_html(html_content, max_results):
    """Parse article links and titles from digg.com/ai HTML."""
    # Pattern to match <a href="/ai/xxxx"><h3 ...>Title</h3></a>
    pattern = re.compile(
        r'<a[^>]+href="(/ai/[^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>.*?</a>',
        re.DOTALL,
    )
    stories = []
    seen_urls = set()

    for m in pattern.finditer(html_content):
        path = m.group(1).split("?")[0]  # strip rank query parameters
        url = f"https://di.gg{path}"

        if url in seen_urls:
            continue
        seen_urls.add(url)

        h3_content = m.group(2)
        # Strip any nested HTML tags (like spans, comments, etc.)
        title = re.sub(r"<[^>]+>", "", h3_content)
        # Decode HTML entities (e.g. &amp; -> &, &mdash; -> —)
        title = html.unescape(title)
        # Clean whitespace and replace pipe characters
        title = re.sub(r"\s+", " ", title).strip()
        title_clean = title.replace("|", " -").strip()

        if title_clean:
            stories.append({"title": title_clean, "url": url, "source": "Digg AI"})

        if len(stories) >= max_results:
            break

    return stories


def main():
    parser = argparse.ArgumentParser(description="Fetch Digg AI stories")
    parser.add_argument(
        "--max-results",
        type=int,
        default=15,
        help="Maximum results to return (default: 15).",
    )
    args = parser.parse_args()

    articles = fetch_digg_ai(args.max_results)

    for art in articles:
        print(f"{art['title']}|{art['url']}|{art['source']}")

    print(
        f"  Done: {len(articles)} stories from Digg AI",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
