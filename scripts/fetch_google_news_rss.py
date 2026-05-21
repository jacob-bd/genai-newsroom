#!/usr/bin/env python3
"""
Fetch Google News articles via official RSS search feed.

Bypasses headless Chrome scraping (no bot blocks, no CAPTCHAs, 100% reliable).
Outputs pipe-delimited TITLE|URL|SOURCE format.

Usage:
    python3 fetch_google_news_rss.py [--hours 24] [--max-results 5]
"""

import argparse
import re
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

_SSL_CTX = ssl.create_default_context()

TIMEOUT = 15
USER_AGENT = "NewsScanner/1.0 (bot; google-news-rss)"

SEARCH_QUERIES = [
    "AI artificial intelligence",
    "generative AI LLM",
    "OpenAI Anthropic Google AI",
]

# Relevance filter (same as news_scan_deduped.sh)
SHORT_KW = re.compile(r"\b(AI|AGI|LLM|GPU|TPU|RAG|API)\b", re.IGNORECASE)
LONG_KW = re.compile(
    r"artificial intelligence|machine learning|deep learning|"
    r"language model|GPT|Claude|Gemini|ChatGPT|OpenAI|Anthropic|"
    r"Google AI|DeepMind|agentic|neural network|transformer|"
    r"diffusion|generative AI|gen AI|Llama|Mistral|Hugging Face|"
    r"inference|training|fine-tuning|open.source|NVIDIA|DeepSeek|"
    r"Grok|xAI|Qwen|Codex|Copilot|Meta AI|Cohere|Perplexity|"
    r"multimodal|reasoning model|robotics|autonomous|chip|"
    r"acquisition|funding|valuation|launch|release|"
    r"OpenClaw|Amazon Q|Bedrock|benchmark",
    re.IGNORECASE,
)


def fetch_news_rss(query, time_suffix="when:1d", max_results=5):
    """Fetch news from Google News RSS feed for a specific query."""
    full_query = f"{query} {time_suffix}".strip()
    encoded_query = urllib.parse.quote_plus(full_query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/xml, text/xml, */*",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
            xml_data = resp.read()
            return parse_google_news_xml(xml_data, max_results)
    except Exception as e:
        print(f"  Warning: RSS fetch failed for query '{query}': {e}", file=sys.stderr)
        return []


def parse_google_news_xml(xml_data, max_results):
    """Parse Google News RSS XML response."""
    results = []
    try:
        root = ET.fromstring(xml_data)
        items = root.findall(".//item")

        for item in items[: max_results * 2]:  # fetch a bit more for filtering
            title_elem = item.find("title")
            link_elem = item.find("link")
            source_elem = item.find("source")

            title = title_elem.text if title_elem is not None else ""
            url = link_elem.text if link_elem is not None else ""
            source = source_elem.text if source_elem is not None else "Google Search"

            if not title or not url:
                continue

            # Clean up title by removing the source suffix (e.g. "Article Title - TechCrunch")
            if " - " in title:
                title_part, _, source_part = title.rpartition(" - ")
                if len(title_part.strip()) > 10:
                    title = title_part.strip()

            title_clean = title.replace("|", " -").strip()
            source_clean = source.replace("|", " -").strip()

            # Apply AI relevance filtering
            if SHORT_KW.search(title_clean) or LONG_KW.search(title_clean):
                results.append(
                    {
                        "title": title_clean,
                        "url": url.strip(),
                        "source": f"{source_clean} (gsearch)",
                    }
                )

            if len(results) >= max_results:
                break

    except ET.ParseError as e:
        print(f"  Warning: Failed to parse XML response: {e}", file=sys.stderr)
    except Exception as e:
        print(f"  Warning: Unexpected parsing error: {e}", file=sys.stderr)

    return results


def main():
    parser = argparse.ArgumentParser(description="Fetch Google News via RSS feed")
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Lookback hours. Standard RSS supports 24h lookup via when:1d suffix.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=5,
        help="Maximum results per query (default: 5).",
    )
    args = parser.parse_args()

    # RSS search handles time query filters like 'when:1d' or 'when:24h'
    time_suffix = "when:1d" if args.hours <= 24 else f"when:{args.hours // 24}d"

    seen_urls = set()
    all_articles = []

    for query in SEARCH_QUERIES:
        articles = fetch_news_rss(query, time_suffix, args.max_results)
        for art in articles:
            if art["url"] not in seen_urls:
                seen_urls.add(art["url"])
                all_articles.append(art)

    for art in all_articles:
        print(f"{art['title']}|{art['url']}|{art['source']}")

    print(
        f"  Done: {len(all_articles)} articles from {len(SEARCH_QUERIES)} Google News RSS queries",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
