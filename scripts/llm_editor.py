#!/usr/bin/env python3
"""
llm_editor.py - AI Editor for Automated News Scanning
======================================================
Replaces deterministic keyword filtering with Gemini Flash AI-powered
story selection. Reads candidate articles, an editorial profile, and
recent post history, then calls Gemini to pick the top stories.

Usage:
    python3 llm_editor.py --file candidates.txt [--github github.txt]

Input format (pipe-delimited, one per line):
    TITLE|URL|SOURCE
    TITLE|URL|SOURCE|TIER   (tier is optional, ignored by LLM)

Output (stdout, one JSON object per line):
    {"rank": 1, "title": "...", "url": "...", "source": "...",
     "type": "rss", "summary": "...", "category": "..."}

Logs picked stories to scanner_presented.md (append).
All status/debug messages go to stderr.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

try:
    from dedup_db import DedupDB, normalize_url
    HAS_DEDUP_DB = True
except ImportError:
    HAS_DEDUP_DB = False

# ── Paths (customize to your workspace) ──────────────────────────────
WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE",
                                os.path.expanduser("~/.alef-agent/workspace")))
MEMORY = WORKSPACE / "newsroom" / "data"
EDITORIAL_PROFILE = MEMORY / "editorial_profile.md"
SCANNER_PRESENTED = MEMORY / "scanner_presented.md"
NEWS_LOG = MEMORY / "news_log.md"

# ── Configuration ────────────────────────────────────────────────────
GEMINI_MODEL = "gemini-3-flash-preview"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
TEMPERATURE = 0.3
TIMEOUT_SEC = 120
MAX_ARTICLES = 500

# ── Failover LLM chain ──────────────────────────────────────────────
FAILOVER_CHAIN = [
    {
        "name": "Gemini 3 Flash Preview",
        "model": "gemini-3-flash-preview",
        "api": "gemini",
        "env_key": "GEMINI_API_KEY",
        "timeout": 90,
    },
    {
        "name": "DeepSeek V4 Flash (OpenRouter)",
        "model": "deepseek/deepseek-v4-flash",
        "api": "openrouter",
        "env_key": "OPENROUTER_API_KEY",
        "timeout": 90,
    },
    {
        "name": "Grok 4.3 (OpenRouter)",
        "model": "x-ai/grok-4.3",
        "api": "openrouter",
        "env_key": "OPENROUTER_API_KEY",
        "timeout": 90,
    },
]
VALID_CATEGORIES = {
    "ai_product", "m_and_a", "model_release", "security", "geopolitics",
    "github_trending", "gaming", "fintech", "hardware", "open_source", "other"
}
RECENT_PUBLISHED_DAYS = 7
MAX_DUPLICATE_MATCHES = 5
MAX_DUPLICATE_JUDGES = 12
STOPWORDS = {
    "about", "after", "almost", "among", "because", "been", "being", "builds",
    "could", "first", "from", "gets", "have", "into", "just", "more", "most",
    "near", "nearly", "plans", "push", "ramp", "ramps", "says", "than", "that",
    "their", "them", "then", "they", "this", "through", "what", "when", "with",
    "year", "years", "over", "under", "using", "used", "your", "ours", "ourselves",
    "itself", "it", "its", "while", "also", "across", "still", "very", "much",
    "said", "saying", "report", "reported", "reportedly", "according", "amid",
    "open", "source", "ai", "news", "https", "http", "www", "html", "amp",
    "article", "articles", "story", "stories", "post", "posts"
}


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[llm_editor {ts}] {msg}", file=sys.stderr)


def estimate_tokens(text):
    return len(text) // 4


def parse_articles(filepath):
    articles = []
    try:
        with open(filepath, "r") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) < 3:
                    continue
                articles.append({
                    "title": parts[0].strip(),
                    "url": parts[1].strip(),
                    "source": parts[2].strip(),
                })
    except FileNotFoundError:
        log(f"ERROR: File not found: {filepath}")
        sys.exit(1)
    return articles


def tokenize_text(text):
    tokens = re.findall(r"[A-Za-z0-9]+", (text or "").lower())
    return [
        t for t in tokens
        if len(t) >= 4 and t not in STOPWORDS and not t.isdigit()
    ]


def extract_query_terms(article, limit=5):
    raw = " ".join([
        article.get("title", ""),
        article.get("source", ""),
        article.get("url", ""),
    ])
    seen = set()
    terms = []
    for token in tokenize_text(raw):
        if token not in seen:
            seen.add(token)
            terms.append(token)
        if len(terms) >= limit:
            break
    return terms


def _parse_dateish(value):
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value[:len(fmt)], fmt)
        except ValueError:
            continue
    return None


def retrieve_recent_published_matches(article, db, days=RECENT_PUBLISHED_DAYS, limit=MAX_DUPLICATE_MATCHES):
    now = datetime.now()
    article_title_tokens = set(tokenize_text(article.get("title", "")))
    article_url_tokens = set(tokenize_text(article.get("url", "")))
    article_tokens = article_title_tokens.union(article_url_tokens)
    if not article_tokens:
        return []

    candidates = {}
    for term in extract_query_terms(article):
        for match in db.search_published(term, limit=8):
            msg_id = match.get("message_id")
            if not msg_id:
                continue
            published_at = _parse_dateish(match.get("date", ""))
            if published_at and (now - published_at).days > days:
                continue

            title_tokens = set(tokenize_text(match.get("title", "")))
            url_tokens = set(tokenize_text(match.get("source_url", "")))
            title_overlap = article_title_tokens.intersection(title_tokens)
            url_overlap = article_url_tokens.intersection(url_tokens)
            any_overlap = title_overlap.union(url_overlap)
            if not any_overlap:
                continue

            prev = candidates.get(msg_id)
            score = (len(title_overlap) * 3) + len(url_overlap)
            entry = dict(match)
            entry["overlap_tokens"] = sorted(any_overlap)
            entry["score"] = score
            if prev is None or score > prev["score"]:
                candidates[msg_id] = entry

    matches = sorted(
        candidates.values(),
        key=lambda x: (x["score"], x.get("date", "")),
        reverse=True
    )
    return matches[:limit]


def build_duplicate_judge_prompt(article, matches):
    lines = []
    for i, match in enumerate(matches, 1):
        lines.append(
            f"{i}. message_id={match.get('message_id')} | date={match.get('date')} | "
            f"title={match.get('title')} | source_url={match.get('source_url')} | "
            f"overlap={','.join(match.get('overlap_tokens', []))}"
        )

    matches_text = "\n".join(lines)
    return f"""You are checking whether a scan candidate is materially the same news story
as one of the recently published Telegram posts.

Return ONLY a JSON object:
{{
  "decision": "same_story" | "follow_up" | "different",
  "confidence": 0.0,
  "matched_message_id": "123 or null",
  "reason": "one short sentence"
}}

Rules:
- same_story: same underlying event/fact, even if the wording or outlet differs
- follow_up: related company/topic, but clearly a new development
- different: not the same story
- Prefer "different" over false positives when evidence is weak

Candidate:
title={article.get('title', '')}
url={article.get('url', '')}
source={article.get('source', '')}

Recent published candidates:
{matches_text}
"""


def _parse_json_object(text):
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return None
    return None


def _call_gemini_json(prompt, api_key, model_url, timeout):
    url = "%s?key=%s" % (model_url, api_key)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        }
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
        text = result["candidates"][0]["content"]["parts"][0]["text"]
        return _parse_json_object(text)
    except Exception as e:
        log("  Duplicate judge Gemini call failed: %s" % e)
        return None


def _call_openrouter_json(prompt, api_key, model, timeout):
    url = "https://openrouter.ai/api/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
        text = result["choices"][0]["message"]["content"]
        return _parse_json_object(text)
    except Exception as e:
        log("  Duplicate judge OpenRouter call failed: %s" % e)
        return None


def judge_duplicate_candidate(article, matches):
    prompt = build_duplicate_judge_prompt(article, matches)
    for provider in FAILOVER_CHAIN:
        env_key = provider.get("env_key")
        api_key = os.environ.get(env_key) if env_key else None
        if env_key and not api_key:
            continue

        if provider["api"] == "gemini":
            model_url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "%s:generateContent" % provider["model"]
            )
            result = _call_gemini_json(prompt, api_key, model_url, min(provider["timeout"], 45))
        elif provider["api"] == "openrouter":
            result = _call_openrouter_json(prompt, api_key, provider["model"], min(provider["timeout"], 45))
        else:
            result = None

        if result:
            return result
    return None


def semantic_duplicate_filter(articles):
    if not HAS_DEDUP_DB:
        return articles

    db = DedupDB()
    filtered = []
    judged = 0
    suppressed = 0

    for article in articles:
        matches = retrieve_recent_published_matches(article, db)
        if not matches or judged >= MAX_DUPLICATE_JUDGES:
            filtered.append(article)
            continue

        judged += 1
        result = judge_duplicate_candidate(article, matches)
        if not result:
            filtered.append(article)
            continue

        decision = result.get("decision", "different")
        confidence = result.get("confidence", 0)
        matched_message_id = result.get("matched_message_id")
        reason = result.get("reason", "")

        if decision == "same_story":
            suppressed += 1
            log("Semantic duplicate suppressed: %s | matched=%s | confidence=%s | %s" % (
                article.get("title", "")[:80],
                matched_message_id,
                confidence,
                reason,
            ))
            continue

        filtered.append(article)

    log("Semantic duplicate filter judged %d candidates, suppressed %d" % (judged, suppressed))
    return filtered


def load_file_safe(path, tail_lines=None):
    try:
        with open(path, "r") as f:
            lines = f.readlines()
        if tail_lines and len(lines) > tail_lines:
            lines = lines[-tail_lines:]
        return "".join(lines)
    except FileNotFoundError:
        return ""
    except Exception as e:
        log(f"  Error reading {path}: {e}")
        return ""


def filter_already_posted(articles):
    """
    Deterministic pre-filter: remove articles already published to Telegram.
    Checks published_posts table only — scored/presented candidates stay eligible.
    Falls back to text-file URL matching if dedup_db unavailable.
    """
    if HAS_DEDUP_DB:
        db = DedupDB()
        try:
            conn = sqlite3.connect(str(db.db_path))

            # 1. published_posts with real source_url
            rows = conn.execute(
                "SELECT source_url FROM published_posts WHERE source_url IS NOT NULL AND source_url != ''"
            ).fetchall()
            blocked = set(normalize_url(r[0]) for r in rows if r[0])

            # 2. seen_articles published (seeded from news_log.md)
            rows2 = conn.execute(
                "SELECT url_normalized FROM seen_articles WHERE status = 'published'"
            ).fetchall()
            blocked.update(r[0] for r in rows2 if r[0])

            # 3. recently presented (last 48h) — shown to Jacob but not yet posted
            rows3 = conn.execute(
                "SELECT url_normalized FROM seen_articles WHERE status = 'presented' "
                "AND first_seen > datetime('now', '-48 hours')"
            ).fetchall()
            blocked.update(r[0] for r in rows3 if r[0])

            conn.close()

            filtered = []
            removed = 0
            for a in articles:
                if normalize_url(a["url"]) in blocked:
                    removed += 1
                else:
                    filtered.append(a)
            if removed:
                log("Pre-filtered %d candidates (published or recently shown)" % removed)
            return filtered
        except Exception as e:
            log(f"Warning: dedup filter failed: {e}, skipping deterministic dedup")

    # Fallback: original text-file matching
    full_log = load_file_safe(NEWS_LOG)
    if not full_log:
        return articles

    presented_log = load_file_safe(SCANNER_PRESENTED)

    url_pattern = re.compile(r'https?://[^\s|>\]\)"\']+')
    posted_urls = set()
    for text in [full_log, presented_log]:
        for url in url_pattern.findall(text):
            url = url.rstrip(".,;:)")
            posted_urls.add(url)

    if not posted_urls:
        return articles

    filtered = []
    removed = 0
    for a in articles:
        candidate_url = a["url"].rstrip(".,;:)")
        if candidate_url in posted_urls:
            log("  PRE-FILTERED (already posted): %s" % a['title'][:60])
            removed += 1
        else:
            filtered.append(a)

    log("Pre-filtered %d candidates (already posted)" % removed)
    return filtered


def build_prompt(articles, github_articles, editorial_profile, recent_posts, top_n):
    article_list = []
    for i, a in enumerate(articles, 1):
        article_list.append(f"  {i}. [{a['source']}] {a['title']}\n     URL: {a['url']}")
    articles_text = "\n".join(article_list)

    github_text = ""
    if github_articles:
        gh_list = []
        for i, g in enumerate(github_articles, 1):
            gh_list.append(f"  {i}. [{g['source']}] {g['title']}\n     URL: {g['url']}")
        github_text = (
            "\n\n## GitHub Trending Repos\n"
            "These are trending GitHub repositories. Include any that are genuinely\n"
            "newsworthy for your audience.\n\n"
            + "\n".join(gh_list)
        )

    prompt = f"""You are the AI editor for an automated news channel. Your job is to select
the top {top_n} stories from the candidate list below.

## Editorial Profile
{editorial_profile}

## Recently Posted Stories (do NOT pick duplicates of these)
{recent_posts if recent_posts else '(No recent posts available)'}

## Candidate Articles
{articles_text}
{github_text}

## Your Task
Select UP TO {top_n} stories from the candidates above. Rank them by
newsworthiness for the target audience.

## Rules
1. Return UP TO {top_n} stories. Quality matters more than quantity — 3 great picks are better than 7 mediocre ones.
2. Do NOT pick stories that duplicate recently posted stories (same event).
   If a candidate covers the SAME EVENT as a recently posted story — even
   from a different source or with a different headline — do NOT pick it.
3. Maximum 2 stories from the same source.
4. "summary": ultra-short editorial note, max 8 words, fragment style (NOT a full sentence). Examples: "Record AI M&A pace", "New video model from Google", "AI safety lawsuit, legal precedent", "Enterprise adoption milestone", "China robotics push". Do NOT write sentences like "Company X announced Y which means Z". Just the angle.
5. Rank by newsworthiness: breaking news > major deals > product launches > analysis.
6. Prefer concrete news (X acquired Y, X launched Z) over speculation or opinion.
7. Exclude generic commentary, essays, newsletters, weekly roundups, rumor posts, or social chatter.
8. Exclude consumer gadget launches unless the story is directly about AI capability, chips, or developer tooling.
9. Exclude meme-like tweets, vague Reddit chatter, and political/culture-war posts unless they are clearly tied to AI, chips, or automation.
10. Prefer primary sources, tier-1 reporting, and stories with concrete implications for builders, AI business, infrastructure, models, or policy.
11. Include at most 1 GitHub repo unless multiple repos are clearly exceptional and broadly useful.
12. Assign each story a category from this list:
   ai_product, m_and_a, model_release, security, geopolitics,
   github_trending, gaming, fintech, hardware, open_source, other

## Required JSON Output Format
Return a JSON array of your selected stories (up to {top_n}), each with these fields:
[
  {{
    "rank": 1,
    "title": "Story headline",
    "url": "https://...",
    "source": "Source name",
    "type": "rss, twitter, or github (use twitter for X/Twitter sources)",
    "summary": "Record AI M&A pace",
    "category": "category_from_list_above"
  }}
]

Return ONLY the JSON array. No markdown, no commentary, no code fences."""
    return prompt


def call_gemini(prompt, api_key):
    url = f"{GEMINI_URL}?key={api_key}"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": TEMPERATURE,
            "responseMimeType": "application/json",
        }
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    token_est = estimate_tokens(prompt)
    log(f"Sending prompt to Gemini Flash (~{token_est} tokens)")

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else "(no body)"
        log(f"API HTTP error {e.code}: {error_body[:500]}")
        return None
    except urllib.error.URLError as e:
        log(f"API connection error: {e.reason}")
        return None
    except Exception as e:
        log(f"API call failed: {e}")
        return None

    try:
        text = result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        log(f"Unexpected API response structure: {e}")
        return None

    try:
        picks = json.loads(text)
        if isinstance(picks, list):
            return picks
        if isinstance(picks, dict) and "stories" in picks:
            return picks["stories"]
        return None
    except json.JSONDecodeError:
        match = re.search(r'\[\s*\{.*?\}\s*\]', text, re.DOTALL)
        if match:
            try:
                picks = json.loads(match.group())
                if isinstance(picks, list):
                    return picks
            except json.JSONDecodeError:
                pass
        log(f"Could not parse LLM response. First 500 chars: {text[:500]}")
        return None


def call_llm_with_failover(prompt, articles, github_articles, editorial_profile, recent_posts, top_n):
    """
    Try LLM providers in sequence: Gemini Flash -> Gemini Flash Lite -> OpenRouter.
    Each step may reduce candidate count for speed.
    """
    for i, provider in enumerate(FAILOVER_CHAIN):
        env_key = provider.get("env_key")
        api_key = os.environ.get(env_key) if env_key else None
        if env_key and not api_key:
            log("  Skipping %s: %s not set" % (provider["name"], env_key))
            continue

        log("Trying %s (model: %s, timeout: %ds)" % (
            provider["name"], provider["model"], provider["timeout"]))

        # For later failovers, reduce candidate list for speed
        current_articles = articles
        current_github = github_articles
        if i >= 1:
            current_articles = articles[:30]
            current_github = github_articles[:5] if github_articles else []

        # Rebuild prompt with current candidates
        current_prompt = build_prompt(
            current_articles, current_github, editorial_profile, recent_posts, top_n
        )

        if provider["api"] == "gemini":
            model_url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "%s:generateContent" % provider["model"]
            )
            picks = _call_gemini_api(current_prompt, api_key, model_url, provider["timeout"])
        elif provider["api"] == "openrouter":
            picks = _call_openrouter_api(current_prompt, api_key, provider["model"], provider["timeout"])
        else:
            continue

        if picks is not None:
            log("  %s returned %d picks" % (provider["name"], len(picks)))
            return picks

        log("  %s failed, trying next..." % provider["name"])

    log("ERROR: All LLM providers failed")
    return None


def _call_gemini_api(prompt, api_key, model_url, timeout):
    """Call a Gemini API model. Returns parsed picks list or None."""
    url = "%s?key=%s" % (model_url, api_key)

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": TEMPERATURE,
            "responseMimeType": "application/json",
        }
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    token_est = estimate_tokens(prompt)
    log("  Sending ~%d tokens to Gemini API" % token_est)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else "(no body)"
        log("  Gemini API HTTP error %d: %s" % (e.code, error_body[:500]))
        return None
    except urllib.error.URLError as e:
        log("  Gemini API connection error: %s" % e.reason)
        return None
    except Exception as e:
        log("  Gemini API call failed: %s" % e)
        return None

    try:
        text = result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        log("  Unexpected Gemini response structure: %s" % e)
        return None

    return _parse_llm_json(text)


def _call_openrouter_api(prompt, api_key, model, timeout):
    """Call OpenRouter API. Returns parsed picks list or None."""
    url = "https://openrouter.ai/api/v1/chat/completions"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % api_key,
        },
        method="POST",
    )

    token_est = estimate_tokens(prompt)
    log("  Sending ~%d tokens to OpenRouter (%s)" % (token_est, model))

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else "(no body)"
        log("  OpenRouter HTTP error %d: %s" % (e.code, error_body[:500]))
        return None
    except urllib.error.URLError as e:
        log("  OpenRouter connection error: %s" % e.reason)
        return None
    except Exception as e:
        log("  OpenRouter call failed: %s" % e)
        return None

    try:
        text = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        log("  Unexpected OpenRouter response structure: %s" % e)
        return None

    return _parse_llm_json(text)


def _parse_llm_json(text):
    """Parse LLM response text into a list of picks."""
    try:
        picks = json.loads(text)
        if isinstance(picks, list):
            return picks
        if isinstance(picks, dict) and "stories" in picks:
            return picks["stories"]
        if isinstance(picks, dict):
            # Try to find a list value in the dict
            for v in picks.values():
                if isinstance(v, list):
                    return v
        return None
    except json.JSONDecodeError:
        match = re.search(r'\[\s*\{.*?\}\s*\]', text, re.DOTALL)
        if match:
            try:
                picks = json.loads(match.group())
                if isinstance(picks, list):
                    return picks
            except json.JSONDecodeError:
                pass
        log("  Could not parse LLM response. First 500 chars: %s" % text[:500])
        return None


def validate_picks(picks, top_n):
    validated = []
    for i, pick in enumerate(picks):
        if not isinstance(pick, dict):
            continue
        entry = {
            "rank": pick.get("rank", i + 1),
            "title": pick.get("title", "(no title)"),
            "url": pick.get("url", ""),
            "source": pick.get("source", "unknown"),
            "type": pick.get("type", "rss"),
            "summary": pick.get("summary", ""),
            "category": pick.get("category", "other"),
        }
        if entry["category"] not in VALID_CATEGORIES:
            entry["category"] = "other"
        if entry["type"] not in ("rss", "twitter", "github"):
            entry["type"] = "rss"
        validated.append(entry)

    for i, v in enumerate(validated):
        v["rank"] = i + 1

    if len(validated) != top_n:
        log(f"  Warning: expected {top_n} picks, got {len(validated)}")
    return validated


def log_to_scanner_presented(picks):
    today = datetime.now().strftime("%Y-%m-%d")
    today_header = f"## {today}"
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    try:
        existing = ""
        if SCANNER_PRESENTED.exists():
            existing = SCANNER_PRESENTED.read_text()

        with open(SCANNER_PRESENTED, "a") as f:
            if today_header not in existing:
                f.write(f"\n{today_header}\n\n")
            for pick in picks:
                f.write(f"[{ts}] {pick['title']} | {pick['url']}\n")

        log(f"Logged {len(picks)} picks to scanner_presented.md")
    except Exception as e:
        log(f"Warning: could not log to scanner_presented.md: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="AI Editor — selects top stories using Gemini Flash"
    )
    parser.add_argument("--file", "-f", required=True,
                       help="Path to article candidates file")
    parser.add_argument("--github", "-g",
                       help="Path to GitHub trending repos file")
    parser.add_argument("--dry-run", action="store_true",
                       help="Build prompt and print to stderr, but don't call API")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log("WARNING: GEMINI_API_KEY not set (failover providers may still work)")

    top_n = int(os.environ.get("TOP_N", "7"))
    log(f"Configuration: top_n={top_n}, model={GEMINI_MODEL}")

    log(f"Loading articles from {args.file}")
    articles = parse_articles(args.file)
    log(f"  Loaded {len(articles)} candidates")
    if len(articles) > MAX_ARTICLES:
        articles = articles[:MAX_ARTICLES]

    if not articles:
        log("ERROR: No articles found in input file")
        sys.exit(1)

    github_articles = []
    if args.github:
        github_articles = parse_articles(args.github)
        log(f"  Loaded {len(github_articles)} GitHub repos")

    log("Running deterministic URL pre-filter")
    articles = filter_already_posted(articles)
    if github_articles:
        github_articles = filter_already_posted(github_articles)

    log("Running semantic duplicate pre-filter")
    articles = semantic_duplicate_filter(articles)

    total_candidates = len(articles) + len(github_articles)
    if total_candidates == 0:
        log("No valid candidates after pre-filter — all articles already seen. Exiting with 0 picks.")
        return 0
    if top_n > total_candidates:
        top_n = total_candidates

    log("Loading editorial profile")
    editorial_profile = load_file_safe(EDITORIAL_PROFILE)
    if not editorial_profile:
        editorial_profile = (
            "Select stories about AI, LLMs, tech deals, and security.\n"
            "Prefer breaking news and concrete announcements over opinion."
        )

    log("Loading recent post history for dedup")
    recent_presented = load_file_safe(SCANNER_PRESENTED, tail_lines=60)
    recent_news_log = load_file_safe(NEWS_LOG, tail_lines=150)
    recent_posts = ""
    if recent_presented:
        recent_posts += "### scanner_presented.md (recent)\n" + recent_presented + "\n"
    if recent_news_log:
        recent_posts += "### news_log.md (recent)\n" + recent_news_log + "\n"

    prompt = build_prompt(articles, github_articles, editorial_profile, recent_posts, top_n)
    prompt_tokens = estimate_tokens(prompt)
    log(f"Prompt built: ~{prompt_tokens} estimated tokens")

    if args.dry_run:
        log("DRY RUN — printing prompt to stderr")
        print(prompt, file=sys.stderr)
        return

    picks = call_llm_with_failover(
        prompt, articles, github_articles, editorial_profile, recent_posts, top_n
    )

    if picks is None:
        log("ERROR: All LLM providers failed. No stories to output.")
        return 1

    picks = validate_picks(picks, top_n)

    for pick in picks:
        print(json.dumps(pick, ensure_ascii=False))

    log_to_scanner_presented(picks)

    # Record picks to SQLite dedup database
    if HAS_DEDUP_DB:
        db = DedupDB()
        pick_articles = [{"url": p["url"], "title": p["title"], "source": p.get("source", "")} for p in picks]
        db.record_batch(pick_articles, status="presented")
        log("Recorded %d picks to dedup database" % len(picks))

    log(f"Done. {len(picks)} stories selected.")


if __name__ == "__main__":
    main()
