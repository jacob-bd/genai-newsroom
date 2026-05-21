#!/usr/bin/env python3
"""
Taste pre-filter for the news scan pipeline.

Reads pipe-delimited articles (TITLE|URL|SOURCE[|TIER]), scores them against
editorial_profile.md using a single fast LLM call, and outputs the top N
articles sorted by taste score in the same pipe-delimited format.

Falls back gracefully (pass-through) if LLM is unavailable.

Usage:
    python3 taste_prefilter.py --input scored.txt [--max 25]
"""

import sys
import os
import re
import json
import argparse
import urllib.request
import urllib.error
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
MEMORY_DIR = SCRIPT_DIR.parent / "data"
EDITORIAL_PROFILE = MEMORY_DIR / "editorial_profile.md"

GEMINI_MODEL = "gemini-3.1-flash-lite-preview"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "x-ai/grok-4.1-fast"


def load_profile():
    try:
        return EDITORIAL_PROFILE.read_text()
    except Exception:
        return None


def call_gemini(prompt, api_key, timeout=45):
    url = f"{GEMINI_URL}?key={api_key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048},
    }).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"  Gemini call failed: {e}", file=sys.stderr)
        return None


def call_openrouter(prompt, api_key, timeout=45):
    body = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 2048,
    }).encode()
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://alef-agent.local",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  OpenRouter call failed: {e}", file=sys.stderr)
        return None


def build_prompt(articles, profile):
    lines = [f"{i}. [{a['source']}] {a['title']}" for i, a in enumerate(articles, 1)]
    article_list = "\n".join(lines)
    return f"""You are an editorial filter for "Gen AI Spotlight", an AI news Telegram channel.

## Editorial Profile
{profile}

## Task
Score each article on how well it matches the editor's taste (1-5):
- 5: Perfect match — editor almost certainly wants this
- 4: Strong match — likely wants this
- 3: Borderline — could go either way
- 2: Weak — editor usually skips this type
- 1: No match — editor definitely skips this

## Articles to Score
{article_list}

## Output
Return ONLY a compact JSON array. No explanation, no markdown fences.
Format: [{{"i":1,"s":5}},{{"i":2,"s":2}},...]
Score all {len(articles)} articles."""


def parse_scores(text, count):
    match = re.search(r'\[.*?\]', text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
        scores = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            idx = item.get('i', item.get('index', item.get('idx')))
            score = item.get('s', item.get('score', item.get('rating')))
            if idx is not None and score is not None:
                try:
                    scores[int(idx)] = max(1, min(5, int(score)))
                except (ValueError, TypeError):
                    pass
        return scores if scores else None
    except (json.JSONDecodeError, KeyError):
        return None


def passthrough(articles, max_out):
    for a in articles[:max_out]:
        print(a['line'])


def main():
    parser = argparse.ArgumentParser(description="Taste pre-filter for news pipeline")
    parser.add_argument('--input', '-i', required=True, help='Input pipe-delimited file')
    parser.add_argument('--max', type=int, default=25, help='Max articles to output (default: 25)')
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
                articles.append({
                    'title': parts[0],
                    'url': parts[1],
                    'source': parts[2],
                    'tier': parts[3] if len(parts) > 3 else '',
                    'line': line,
                })
    except FileNotFoundError:
        print(f"Error: file not found: {args.input}", file=sys.stderr)
        return 1

    if not articles:
        print("No articles to filter", file=sys.stderr)
        return 0

    profile = load_profile()
    if not profile:
        print("  Warning: editorial profile missing, passing through unchanged", file=sys.stderr)
        passthrough(articles, args.max)
        return 0

    prompt = build_prompt(articles, profile)

    gemini_key = os.environ.get('GEMINI_API_KEY')
    openrouter_key = os.environ.get('OPENROUTER_API_KEY')

    text = None
    if gemini_key:
        text = call_gemini(prompt, gemini_key)
    if text is None and openrouter_key:
        text = call_openrouter(prompt, openrouter_key)

    if text is None:
        print("  Warning: all LLM providers failed, passing through unchanged", file=sys.stderr)
        passthrough(articles, args.max)
        return 0

    scores = parse_scores(text, len(articles))
    if scores is None:
        print("  Warning: could not parse LLM scores, passing through unchanged", file=sys.stderr)
        passthrough(articles, args.max)
        return 0

    for i, a in enumerate(articles, 1):
        a['taste_score'] = scores.get(i, 2)

    articles.sort(key=lambda x: -x['taste_score'])
    output = articles[:args.max]

    kept = len(output)
    dropped = len(articles) - kept
    avg = sum(a['taste_score'] for a in output) / kept if kept else 0
    print(
        f"  Taste filter: {len(articles)} in -> {kept} out "
        f"({dropped} dropped, avg taste score {avg:.1f}/5)",
        file=sys.stderr,
    )

    for a in output:
        print(a['line'])


if __name__ == "__main__":
    sys.exit(main() or 0)
