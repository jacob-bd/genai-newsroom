#!/usr/bin/env python3
"""
Quality scoring pre-filter for the news scan pipeline.

Reads pipe-delimited articles (TITLE|URL|SOURCE or TITLE|URL|SOURCE|TIER),
scores them based on source tier, title quality, freshness signals, and
deduplicates by title similarity.

Outputs the top N articles in the same pipe-delimited format, sorted by score.

Usage:
    python3 quality_score.py --input articles.txt [--max 50] [--dedup-threshold 0.80]
"""

import sys
import re
import sqlite3
import argparse
from difflib import SequenceMatcher

try:
    from dedup_db import DedupDB, normalize_url
    HAS_DEDUP_DB = True
except ImportError:
    HAS_DEDUP_DB = False

# ── Source priority scoring ──────────────────────────────────────────
# Higher = better. Customize to match your blogwatcher feed names.
PRIORITY_SOURCES = {
    # T1: Wire services + official AI lab blogs (+5 bonus)
    'Reuters Tech': 5, 'Bloomberg Tech': 5, 'Axios AI': 5, 'CNBC Tech': 5,
    'OpenAI Blog': 5,
    # T2: Tech press + priority bloggers (+3 bonus)
    'TechCrunch AI': 3, 'The Verge': 3, 'THE DECODER': 3, 'VentureBeat AI': 3,
    'Ars Technica': 3, '404 Media': 3, 'Wired AI': 3, 'MIT Tech Review': 3,
    'Google AI Blog': 3, 'Hugging Face Blog': 3, 'Simon Willison': 3,
    'Latent Space': 3, 'Crunchbase News': 3,
    # T3: Aggregators (+1 bonus)
    'Hacker News AI': 1, 'SiliconANGLE AI': 1, 'AI News': 1,
    'Gary Marcus': 1, 'Bens Bites': 1,
    # X/Twitter (+2 — original source, not aggregated)
    'X/Twitter': 2,
}

# Strong AI relevance signals. Used to avoid generic tech or politics bleed-through.
AI_CONTEXT_KEYWORDS = re.compile(
    r'\b(ai|artificial intelligence|llm|llms|model|models|chatgpt|openai|'
    r'anthropic|claude|gemini|deepmind|copilot|codex|agent|agents|agentic|'
    r'inference|training|fine-tun|gpu|gpus|hbm|chip|chips|semiconductor|'
    r'nvidia|amd|robot|robotics|autonomous|open.source|hugging face|qwen|'
    r'llama|mistral|deepseek|xai|grok|benchmark)\b',
    re.IGNORECASE
)

# High-value keywords that boost score
HIGH_VALUE_KEYWORDS = re.compile(
    r'\b(acqui|merger|billion|partnership|launch|release|'
    r'announce|breakthrough|regulation|ban|security|vulnerability|'
    r'open.source|Pentagon|military|government|antitrust)\b',
    re.IGNORECASE
)

# Signal words for breaking/exclusive news
BREAKING_KEYWORDS = re.compile(
    r'\b(breaking|exclusive|just in|confirmed|leaked|first look|'
    r'officially|unveil|reveal)\b',
    re.IGNORECASE
)

LOW_SIGNAL_PATTERNS = re.compile(
    r'(^the download:)|'
    r'\b(newsletter|roundup|digest|podcast|opinion|essay|explainer)\b|'
    r'\b(i copy and pasted|not truely local|quietly launches)\b',
    re.IGNORECASE
)

CONSUMER_TECH_PATTERNS = re.compile(
    r'\b(airpods|iphone|ipad|apple watch|macbook|pixel watch|galaxy buds)\b',
    re.IGNORECASE
)

CHATTER_PATTERNS = re.compile(
    r'\b(convo|speaking their own language|meme|shitpost)\b',
    re.IGNORECASE
)


def is_hard_filtered(title, source):
    """Drop obvious junk before scoring."""
    if LOW_SIGNAL_PATTERNS.search(title):
        return True
    if CONSUMER_TECH_PATTERNS.search(title) and not AI_CONTEXT_KEYWORDS.search(title):
        return True
    if CHATTER_PATTERNS.search(title):
        return True
    if source.startswith('X/') and title.count('@') >= 2 and not AI_CONTEXT_KEYWORDS.search(title):
        return True
    return False


def title_similarity(t1, t2):
    """Fast title similarity using SequenceMatcher."""
    return SequenceMatcher(None, t1.lower(), t2.lower()).ratio()


def compute_score(title, source, tier_str):
    """Compute a quality score for an article."""
    score = 0

    score += PRIORITY_SOURCES.get(source, 0)

    if source.startswith('r/'):
        score += 1

    if source.startswith('GitHub'):
        score += 2

    try:
        tier = int(tier_str) if tier_str else 3
    except ValueError:
        tier = 3
    if tier == 1:
        score += 4
    elif tier == 2:
        score += 2
    elif tier == 3:
        score += 1

    hv_matches = HIGH_VALUE_KEYWORDS.findall(title)
    score += min(len(hv_matches) * 2, 6)

    if BREAKING_KEYWORDS.search(title):
        score += 3

    if AI_CONTEXT_KEYWORDS.search(title):
        score += 2
    elif source.startswith('r/'):
        score -= 2
    elif source.startswith('X/'):
        score -= 3

    title_len = len(title)
    if title_len < 30:
        score -= 1
    elif 50 <= title_len <= 150:
        score += 1

    if CONSUMER_TECH_PATTERNS.search(title) and not AI_CONTEXT_KEYWORDS.search(title):
        score -= 8
    if LOW_SIGNAL_PATTERNS.search(title):
        score -= 6
    if source.startswith('X/') and title.count('@') >= 2:
        score -= 4

    return score


def deduplicate(articles, threshold=0.80):
    """Remove near-duplicate articles by title similarity. Keep highest-scored."""
    unique = []
    for article in articles:
        is_dup = False
        for existing in unique:
            sim = title_similarity(article['title'], existing['title'])
            if sim >= threshold:
                is_dup = True
                if article['score'] > existing['score']:
                    unique.remove(existing)
                    unique.append(article)
                break
        if not is_dup:
            unique.append(article)
    return unique


def cross_scan_dedup(articles):
    """Block published articles and recently-presented articles (last 48h). Never block old scored entries."""
    if not HAS_DEDUP_DB:
        print("  Warning: dedup_db not available, skipping cross-scan dedup", file=sys.stderr)
        return articles

    db = DedupDB()
    try:
        conn = sqlite3.connect(str(db.db_path))

        # 1. published_posts with a real source_url (telegram_post.py records these)
        rows = conn.execute(
            "SELECT source_url FROM published_posts WHERE source_url IS NOT NULL AND source_url != ''"
        ).fetchall()
        blocked = set(normalize_url(r[0]) for r in rows if r[0])

        # 2. seen_articles with status='published' (seeded from news_log.md via seed_from_logs)
        rows2 = conn.execute(
            "SELECT url_normalized FROM seen_articles WHERE status = 'published'"
        ).fetchall()
        blocked.update(r[0] for r in rows2 if r[0])

        # 3. seen_articles with status='presented' in the last 48 hours (recently shown to Jacob)
        rows3 = conn.execute(
            "SELECT url_normalized FROM seen_articles WHERE status = 'presented' "
            "AND first_seen > datetime('now', '-48 hours')"
        ).fetchall()
        blocked.update(r[0] for r in rows3 if r[0])

        # 4. title similarity against published_posts last 30 days (catches manual posts with no source_url)
        pub_titles = [r[0] for r in conn.execute(
            "SELECT title FROM published_posts WHERE date > date('now', '-30 days')"
        ).fetchall() if r[0]]

        conn.close()

        filtered = []
        removed = 0
        for a in articles:
            norm = normalize_url(a["url"])
            if norm in blocked:
                removed += 1
                continue
            title = a.get("title", "")
            if title and any(title_similarity(title, pt) >= 0.75 for pt in pub_titles):
                removed += 1
                continue
            filtered.append(a)
        if removed > 0:
            print(f"  Cross-scan dedup: removed {removed} (published or recently shown)", file=sys.stderr)
        return filtered
    except Exception as e:
        print(f"  Warning: cross-scan dedup failed: {e}", file=sys.stderr)
        return articles


def main():
    parser = argparse.ArgumentParser(description="Quality scoring pre-filter")
    parser.add_argument('--input', '-i', required=True, help='Input pipe-delimited file')
    parser.add_argument('--max', type=int, default=50, help='Max articles to output (default: 50)')
    parser.add_argument('--dedup-threshold', type=float, default=0.80,
                       help='Title similarity threshold for dedup (default: 0.80)')
    args = parser.parse_args()

    articles = []
    try:
        with open(args.input, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split('|')
                if len(parts) < 3:
                    continue
                title = parts[0]
                url = parts[1]
                source = parts[2]
                tier = parts[3] if len(parts) > 3 else ''

                if is_hard_filtered(title, source):
                    continue

                score = compute_score(title, source, tier)
                articles.append({
                    'title': title,
                    'url': url,
                    'source': source,
                    'tier': tier,
                    'score': score,
                    'line': line,
                })
    except FileNotFoundError:
        print(f"Error: file not found: {args.input}", file=sys.stderr)
        return 1

    if not articles:
        print("No articles to score", file=sys.stderr)
        return 0

    articles.sort(key=lambda x: -x['score'])
    unique = deduplicate(articles, args.dedup_threshold)
    unique = cross_scan_dedup(unique)
    unique = [a for a in unique if a['score'] >= 0]
    unique.sort(key=lambda x: -x['score'])
    output = unique[:args.max]

    for article in output:
        if article['tier']:
            print(f"{article['title']}|{article['url']}|{article['source']}|{article['tier']}")
        else:
            print(f"{article['title']}|{article['url']}|{article['source']}")

    total = len(articles)
    deduped = total - len(unique)
    final = len(output)
    print(f"  Done: {total} in -> {deduped} dupes removed -> {final} out", file=sys.stderr)


if __name__ == "__main__":
    main()
