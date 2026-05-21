#!/usr/bin/env python3
"""
Fetch Reddit posts via JSON API with score filtering and noise reduction.

Replaces blogwatcher RSS for Reddit. Uses Reddit's public JSON API
(no authentication required). Outputs pipe-delimited TITLE|URL|SOURCE format.

Usage:
    python3 fetch_reddit_news.py [--hours 24] [--min-score 20]
"""

import json
import re
import ssl
import sys
import time
import argparse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

_SSL_CTX = ssl.create_default_context()

# ── Configuration ────────────────────────────────────────────────────
TIMEOUT = 20
MAX_WORKERS = 3
RETRY_COUNT = 1
RETRY_DELAY = 3
USER_AGENT = "NewsScanner/1.0 (bot; reddit-scanner)"

# Subreddit configs: dict with optional "flairs" list for flair filtering.
# When "flairs" is set, only posts matching those flairs are included
# (case-insensitive substring match on link_flair_text).
SUBREDDITS = [
    # AI-focused subs — no flair filter needed (already on-topic)
    {"sub": "LocalLLaMA",           "sort": "hot", "limit": 25, "min_score": 30},
    {"sub": "singularity",          "sort": "hot", "limit": 25, "min_score": 50},
    {"sub": "ChatGPT",              "sort": "hot", "limit": 25, "min_score": 50},
    {"sub": "Anthropic",            "sort": "hot", "limit": 25, "min_score": 30},

    # Flair-filtered subs — use flairs to cut noise, lower score threshold
    {"sub": "MachineLearning",      "sort": "hot", "limit": 25, "min_score": 30,
     "flairs": ["[N]", "[R]", "[P]"]},
    {"sub": "technology",           "sort": "hot", "limit": 25, "min_score": 30,
     "flairs": ["AI", "Artificial Intelligence"]},
    {"sub": "OpenAI",               "sort": "hot", "limit": 25, "min_score": 30,
     "flairs": ["News"]},
    {"sub": "artificial",           "sort": "hot", "limit": 25, "min_score": 20,
     "flairs": ["News"]},
    {"sub": "ClaudeAI",             "sort": "hot", "limit": 25, "min_score": 20,
     "flairs": ["News"]},

    # New subs — added via flair filtering (too noisy without)
    {"sub": "Futurology",           "sort": "hot", "limit": 25, "min_score": 30,
     "flairs": ["AI", "Artificial Intelligence", "Robotics/Automation"]},
    {"sub": "ArtificialIntelligence", "sort": "hot", "limit": 25, "min_score": 20,
     "flairs": ["News"]},
    {"sub": "Bard",                 "sort": "hot", "limit": 25, "min_score": 20,
     "flairs": ["News"]},
    {"sub": "GeminiAI",             "sort": "hot", "limit": 25, "min_score": 20,
     "flairs": ["News"]},
]

# Reddit noise filter — skip questions, rants, memes
NOISE_START = re.compile(
    r'^(Why|How|What|Can|Does|Is|Has|Are|Do|Should|Would|Could|Anyone|'
    r'Help|Rant|Vent|Am I|ELI5|CMV|PSA|Unpopular|Hot take|DAE|TIL|'
    r'Gah|Kindly explain|Seriously|From Frustration|Gemini Memory|'
    r'I just|I don.t|My experience|Thank you|Appreciation|Shoutout|'
    r'Just deleted|Thanks to everyone|I.m happy to report|'
    r'Overtaken!|F that|RIP|Goodbye)',
    re.IGNORECASE
)

# AI relevance keywords (must match at least one)
SHORT_KW = re.compile(r'\b(AI|AGI|LLM|GPU|TPU|RAG)\b', re.IGNORECASE)
LONG_KW = re.compile(
    r'artificial intelligence|machine learning|deep learning|language model|'
    r'GPT|Claude|Gemini|ChatGPT|OpenAI|Anthropic|Google AI|DeepMind|'
    r'agentic|neural network|transformer|diffusion|generative AI|gen AI|'
    r'Llama|Mistral|Hugging Face|inference|training|fine-tuning|'
    r'open.source|NVIDIA|DeepSeek|Grok|xAI|Qwen|Codex|Copilot|'
    r'Meta AI|Cohere|Perplexity|multimodal|reasoning model|'
    r'acquisition|funding|valuation|launch|release|benchmark',
    re.IGNORECASE
)


def is_noise(title):
    """Return True if title looks like Reddit noise (questions, rants, etc)."""
    t = title.strip()
    if NOISE_START.match(t):
        return True
    if t.endswith('?'):
        return True
    if len(t) < 20:
        return True
    return False


def is_ai_relevant(title):
    """Return True if title contains AI-related keywords."""
    return bool(SHORT_KW.search(title) or LONG_KW.search(title))


def flair_matches(post_flair, allowed_flairs):
    """Check if a post's flair matches any in the allowed list (case-insensitive)."""
    if not post_flair:
        return False
    pf = post_flair.lower().strip()
    for af in allowed_flairs:
        if af.lower() in pf:
            return True
    return False


def fetch_subreddit(subreddit, sort, limit, min_score, cutoff, flairs=None):
    """Fetch posts from a single subreddit. If flairs is set, only matching posts."""
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}&raw_json=1"

    for attempt in range(RETRY_COUNT + 1):
        try:
            req = Request(url, headers={
                'User-Agent': USER_AGENT,
                'Accept': 'text/html,application/json',
            })
            with urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode('utf-8'))

            posts = []
            for child in data.get('data', {}).get('children', []):
                post = child.get('data', {})
                if not post:
                    continue

                created_utc = post.get('created_utc', 0)
                post_time = datetime.fromtimestamp(created_utc, tz=timezone.utc)
                if post_time < cutoff:
                    continue

                score = post.get('score', 0)
                if score < min_score:
                    continue

                if post.get('stickied', False):
                    continue

                title = post.get('title', '').strip()
                if not title:
                    continue

                if flairs:
                    post_flair = post.get('link_flair_text', '')
                    if not flair_matches(post_flair, flairs):
                        continue

                if is_noise(title):
                    continue

                ai_focused = subreddit.lower() in {
                    'localllama', 'machinelearning', 'chatgpt', 'openai',
                    'artificial', 'anthropic', 'claudeai', 'singularity',
                    'artificialintelligence', 'bard', 'geminiai',
                }
                if not ai_focused and not is_ai_relevant(title):
                    continue

                permalink = f"https://www.reddit.com{post.get('permalink', '')}"
                external_url = post.get('url', '')
                is_self = post.get('is_self', True)

                if is_self or 'reddit.com' in external_url or 'redd.it' in external_url:
                    link = permalink
                else:
                    link = external_url

                title_clean = title.replace('|', ' -')
                num_comments = post.get('num_comments', 0)

                posts.append({
                    'title': title_clean,
                    'url': link,
                    'source': f"r/{subreddit}",
                    'score': score,
                    'comments': num_comments,
                })

            return posts

        except HTTPError as e:
            if e.code == 429 and attempt < RETRY_COUNT:
                time.sleep(10)
                continue
            elif e.code == 403:
                print(f"  Warning: r/{subreddit} is private/quarantined", file=sys.stderr)
                return []
            print(f"  Warning: r/{subreddit}: HTTP {e.code}", file=sys.stderr)
        except (URLError, OSError) as e:
            print(f"  Warning: r/{subreddit}: network error", file=sys.stderr)
        except Exception as e:
            print(f"  Warning: r/{subreddit}: {e}", file=sys.stderr)

        if attempt < RETRY_COUNT:
            time.sleep(RETRY_DELAY)

    return []


def main():
    parser = argparse.ArgumentParser(description="Fetch Reddit posts via JSON API")
    parser.add_argument('--hours', type=int, default=24, help='Hours lookback (default: 24)')
    parser.add_argument('--min-score', type=int, default=0, help='Override min score for all subs')
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    all_posts = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for cfg in SUBREDDITS:
            sub = cfg["sub"]
            sort = cfg.get("sort", "hot")
            limit = cfg.get("limit", 25)
            min_score = cfg.get("min_score", 30)
            flairs = cfg.get("flairs", None)
            effective_min = args.min_score if args.min_score > 0 else min_score
            future = pool.submit(fetch_subreddit, sub, sort, limit, effective_min, cutoff, flairs)
            futures[future] = sub

        for future in as_completed(futures):
            posts = future.result()
            all_posts.extend(posts)

    all_posts.sort(key=lambda x: -x['score'])

    seen_urls = set()
    unique_posts = []
    for post in all_posts:
        if post['url'] not in seen_urls:
            seen_urls.add(post['url'])
            unique_posts.append(post)

    for post in unique_posts:
        print(f"{post['title']}|{post['url']}|{post['source']}")

    print(f"  Done: {len(unique_posts)} posts from {len(SUBREDDITS)} subreddits", file=sys.stderr)


if __name__ == "__main__":
    main()
