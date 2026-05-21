#!/usr/bin/env python3
"""
Enrich top articles with full text content for better LLM curation.

Reads pipe-delimited articles, fetches full text for the top N articles
using Cloudflare Markdown for Agents (preferred), HTML extraction (fallback),
agent-browser headless Chromium (JS-heavy sites), then self-hosted Firecrawl
when both are empty and FIRECRAWL_LOCAL_URL is set (e.g. Reddit threads, some paywalls).

Appends full text as a 4th/5th pipe field: TITLE|URL|SOURCE|TIER|FULLTEXT

Usage:
    python3 enrich_top_articles.py --input articles.txt [--max 10] [--max-chars 1500]
"""

import re
import sys
import os
import json
import argparse
import ssl
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

_SSL_CTX = ssl.create_default_context()

TIMEOUT = 8
MAX_WORKERS = 4
USER_AGENT = "TechDigest/3.0 (article enrichment)"
AGENT_BROWSER = "/usr/local/bin/agent-browser"
BROWSER_TIMEOUT = 25  # seconds per URL

# Domains to skip enrichment entirely (paywalled, JS-heavy, or not articles)
SKIP_DOMAINS = {
    "twitter.com", "x.com",
    "reddit.com", "old.reddit.com",
    "github.com",
    "youtube.com", "youtu.be",
    "arxiv.org",
    "bloomberg.com", "nytimes.com", "wsj.com", "ft.com",
}

# Domains to skip even for browser fetch (same as SKIP_DOMAINS — paywalled even with real browser)
BROWSER_SKIP_DOMAINS = SKIP_DOMAINS


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._text = []
        self._skip = False
        self._skip_tags = {"script", "style", "nav", "footer", "header", "aside", "noscript"}

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._skip = True

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._skip = False
        if tag in ("p", "br", "div", "h1", "h2", "h3", "li"):
            self._text.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._text.append(data)

    def get_text(self):
        raw = "".join(self._text)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def fetch_full_text(url, max_chars=1500):
    """Fetch article full text via CF Markdown or HTML extraction."""
    domain = urlparse(url).netloc.lower().lstrip("www.")
    if domain in SKIP_DOMAINS:
        return ""

    try:
        req = Request(url, headers={
            "Accept": "text/markdown, text/html;q=0.9",
            "User-Agent": USER_AGENT,
        })
        with urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()

            if raw[:2] == b"\x1f\x8b":
                import gzip
                raw = gzip.decompress(raw)

            text = raw.decode("utf-8", errors="replace")

            # Cloudflare Markdown endpoint
            if "text/markdown" in content_type:
                return text[:max_chars]

            # HTML extraction fallback
            article_match = re.search(r"<article[^>]*>(.*?)</article>", text, re.DOTALL | re.IGNORECASE)
            fragment = article_match.group(1) if article_match else text
            extractor = TextExtractor()
            try:
                extractor.feed(fragment)
            except Exception:
                return ""
            extracted = extractor.get_text()
            if len(extracted) < 80:
                return ""
            return extracted[:max_chars]

    except (HTTPError, URLError, OSError):
        return ""
    except Exception:
        return ""


def fetch_browser_text(url, max_chars=1500):
    """Last-resort fetch via agent-browser headless Chromium.
    Used when simple fetch returns empty (JS-rendered or CF-protected sites).
    Runs sequentially — agent-browser uses a shared browser daemon.
    """
    domain = urlparse(url).netloc.lower().lstrip("www.")
    if domain in BROWSER_SKIP_DOMAINS:
        return ""

    try:
        # Navigate to URL (allow timeout — page may still be usable)
        subprocess.run(
            [AGENT_BROWSER, "open", url],
            capture_output=True, text=True, timeout=BROWSER_TIMEOUT
        )

        # Extract paragraphs with meaningful content via JS
        js = (
            "Array.from(document.querySelectorAll('p'))"
            ".filter(p=>p.innerText.length>80)"
            ".map(p=>p.innerText)"
            ".slice(0,8)"
            ".join(' ')"
        )
        result = subprocess.run(
            [AGENT_BROWSER, "eval", js],
            capture_output=True, text=True, timeout=10
        )
        text = result.stdout.strip().strip('"')
        # Clean up JS string escapes
        text = text.replace("\\n", " ").replace("\\t", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 80:
            return ""
        return text[:max_chars]

    except Exception:
        return ""


def fetch_firecrawl_markdown(url, max_chars, base_url, timeout=35):
    """POST /v1/scrape on a self-hosted Firecrawl instance. No-op if misconfigured."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    payload = json.dumps({"url": url, "formats": ["markdown"]}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        req = Request(
            f"{base}/v1/scrape",
            data=payload,
            headers=headers,
            method="POST",
        )
        with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
        return ""
    if not data.get("success"):
        return ""
    md = (data.get("data") or {}).get("markdown") or ""
    md = md.strip()
    if len(md) < 40:
        return ""
    return md[:max_chars]


def main():
    parser = argparse.ArgumentParser(description="Enrich top articles with full text")
    parser.add_argument('--input', '-i', required=True, help='Input pipe-delimited file')
    parser.add_argument('--max', type=int, default=10, help='Max articles to enrich (default: 10)')
    parser.add_argument('--max-chars', type=int, default=1500, help='Max chars per article (default: 1500)')
    args = parser.parse_args()

    articles = []
    try:
        with open(args.input, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                articles.append(line)
    except FileNotFoundError:
        print(f"Error: file not found: {args.input}", file=sys.stderr)
        return 1

    if not articles:
        print("No articles to enrich", file=sys.stderr)
        return 0

    # Only enrich top N (file should already be sorted by quality_score.py)
    to_enrich = articles[:args.max]
    pass_through = articles[args.max:]

    # Pass 1: fast parallel fetch (CF Markdown + HTML extraction)
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for i, line in enumerate(to_enrich):
            parts = line.split('|')
            if len(parts) >= 2:
                url = parts[1]
                futures[pool.submit(fetch_full_text, url, args.max_chars)] = i

        for future in as_completed(futures):
            idx = futures[future]
            text = future.result()
            if text:
                results[idx] = text

    # Pass 2: browser fallback for articles that got no text
    empty_indices = [
        i for i, line in enumerate(to_enrich)
        if i not in results and len(line.split('|')) >= 2
    ]
    browser_count = 0
    if empty_indices:
        print(f"  🌐 Browser fallback for {len(empty_indices)} articles...", file=sys.stderr)
        for i in empty_indices:
            url = to_enrich[i].split('|')[1]
            text = fetch_browser_text(url, args.max_chars)
            if text:
                results[i] = text
                browser_count += 1

    # Pass 3: Firecrawl when still empty (Reddit and other skips, brittle HTML)
    firecrawl_count = 0
    fc_base = os.environ.get("FIRECRAWL_LOCAL_URL", "").strip()
    still_empty = [
        i for i, line in enumerate(to_enrich)
        if i not in results and len(line.split("|")) >= 2
    ]
    if fc_base and still_empty:
        print(
            f"  🔥 Firecrawl fallback for {len(still_empty)} articles...",
            file=sys.stderr,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            fmap = {
                pool.submit(
                    fetch_firecrawl_markdown,
                    to_enrich[i].split("|")[1],
                    args.max_chars,
                    fc_base,
                ): i
                for i in still_empty
            }
            for fut in as_completed(fmap):
                idx = fmap[fut]
                text = fut.result()
                if text:
                    results[idx] = text
                    firecrawl_count += 1

    # Output: enriched articles first, then remaining
    enriched_count = 0
    for i, line in enumerate(to_enrich):
        parts = line.split('|')
        if i in results:
            clean_text = results[i].replace('|', ' ').replace('\n', ' ').strip()
            clean_text = re.sub(r'\s+', ' ', clean_text)
            print(f"{line}|FULLTEXT:{clean_text[:args.max_chars]}")
            enriched_count += 1
        else:
            print(line)

    for line in pass_through:
        print(line)

    http_count = enriched_count - browser_count - firecrawl_count
    print(
        f"  ✅ Enrichment: {enriched_count}/{len(to_enrich)} articles enriched "
        f"({http_count} http, {browser_count} browser, {firecrawl_count} firecrawl)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
