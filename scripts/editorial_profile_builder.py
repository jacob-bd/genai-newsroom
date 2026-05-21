#!/usr/bin/env python3
"""
editorial_profile_builder.py
Mines published_posts to build Jacob's editorial preference profile.
Output: ~/.alef-agent/workspace/newsroom/data/editorial_profile.json
"""

import sqlite3
import json
import re
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

DB_PATH = os.path.expanduser("~/.alef-agent/workspace/newsroom/data/news_dedup.db")
OUTPUT_PATH = os.path.expanduser("~/.alef-agent/workspace/newsroom/data/editorial_profile.json")

CATEGORY_PATTERNS = {
    "funding_raises":     [r'\$\d', r'raise[sd]', r'\bfunding\b', r'valuation', r'series [abcde]', r'invest'],
    "acquisitions_ma":    [r'acqui[res]+', r'\bbuys\b', r'merger', r'\bdeal\b', r'takeover'],
    "product_launches":   [r'launch', r'release[sd]?', r'introduce[sd]?', r'announce[sd]?', r'\bdrops?\b', r'unveil'],
    "open_source":        [r'open.source', r'github', r'open.weight', r'model weight', r'open model'],
    "regulation_policy":  [r'\bEU\b', r'\bFTC\b', r'\bDOJ\b', r'regulat', r'\bban\b', r'policy', r'\blaw\b', r'congress'],
    "security":           [r'breach', r'hack', r'vulnerabilit', r'malware', r'exploit'],
    "hardware_chips":     [r'\bGPU\b', r'\bchips?\b', r'hardware', r'Nvidia', r'\bAMD\b', r'H100', r'silicon'],
    "ai_models_research": [r'GPT', r'Claude', r'Gemini', r'Llama', r'\bmodel\b', r'\bLLM\b', r'reasoning', r'benchmark'],
    "business_strategy":  [r'\bCEO\b', r'layoff', r'partner', r'enterprise', r'revenue', r'profit', r'IPO'],
    "agents_automation":  [r'\bagents?\b', r'autonom', r'agentic', r'workflow', r'copilot'],
    "breaking_news":      [r'BREAKING', r'JUST IN', r'EXCLUSIVE'],
}

def clean(text):
    return re.sub(r'<[^>]+>', '', text or '').strip()

def categorize(title):
    t = title.lower()
    cats = [cat for cat, pats in CATEGORY_PATTERNS.items()
            if any(re.search(p, t, re.IGNORECASE) for p in pats)]
    return cats or ["other"]

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthesize", action="store_true",
                        help="Run LLM synthesis to regenerate prose_profile")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("""
        SELECT date, title, source_domain, source_url, full_text
        FROM published_posts ORDER BY date ASC
    """)
    posts = cur.fetchall()
    conn.close()

    total = len(posts)
    print(f"Analyzing {total} published posts ({posts[0][0]} to {posts[-1][0]})...")

    cat_counts = Counter()
    domain_counts = Counter()
    monthly = defaultdict(int)
    cat_examples = defaultdict(list)

    for date, title, domain, url, full_text in posts:
        t = clean(title)
        cats = categorize(t)
        for c in cats:
            cat_counts[c] += 1
            if len(cat_examples[c]) < 6:
                cat_examples[c].append(t)
        if domain:
            domain_counts[domain] += 1
        if date:
            monthly[date[:7]] += 1

    profile = {
        "generated_at": datetime.now().isoformat(),
        "total_posts": total,
        "date_range": {"start": posts[0][0], "end": posts[-1][0]},
        "top_categories": [
            {"category": c, "count": n, "pct": round(n / total * 100, 1), "examples": cat_examples[c]}
            for c, n in cat_counts.most_common(15)
        ],
        "top_domains": [
            {"domain": d, "count": n, "pct": round(n / total * 100, 1)}
            for d, n in domain_counts.most_common(20)
        ],
        "monthly_volume": dict(sorted(monthly.items())),
    }

    # Build LLM synthesis prompt
    cat_lines = "\n".join(
        f"  {r['category']}: {r['count']} stories ({r['pct']}%) — e.g. {r['examples'][:2]}"
        for r in profile["top_categories"][:12]
    )
    domain_lines = "\n".join(
        f"  {r['domain']}: {r['count']} stories"
        for r in profile["top_domains"][:12]
    )
    recent = [clean(p[1]) for p in posts[-120:] if p[1]]
    titles_block = "\n".join(f"- {t}" for t in recent if t)

    prompt = f"""You are analyzing the editorial taste of Jacob Ben-David, who curates the "Gen AI Spotlight" Telegram channel.
He has published {total} AI news stories between {posts[0][0]} and {posts[-1][0]}.

CATEGORY BREAKDOWN (keyword-matched):
{cat_lines}

TOP TRUSTED SOURCES:
{domain_lines}

SAMPLE OF RECENT PUBLISHED TITLES:
{titles_block}

Write a concise editorial preference profile (200-250 words) for use as a pre-filter instruction injected into an AI news scanner LLM prompt.

Structure it as:
1. COVERS: core story types Jacob always publishes
2. THRESHOLD: what makes a story big enough to publish (funding size, company tier, impact)
3. AVOIDS: what he consistently skips
4. SOURCES: trusted outlets
5. ANGLE: his editorial framing style

Start with: "Jacob's editorial taste:"
Be specific and concrete. Use examples from the data. This will directly instruct the scanner LLM what to keep and what to drop.
"""

    prose = None
    if args.synthesize:
        print("Running LLM synthesis via alef chat send...")
        try:
            import subprocess
            # Trim prompt: top 20 recent titles only to stay within CLI limits
            recent_short = recent[-20:]
            short_titles = "\n".join(f"- {t}" for t in recent_short if t)
            short_prompt = prompt.replace(titles_block, short_titles)
            result = subprocess.run(
                ["alef", "chat", "send", "--backend", "claude", short_prompt],
                capture_output=True, text=True, timeout=180,
                env={**os.environ, "HOME": "/Users/jbd"}
            )
            if result.returncode == 0 and result.stdout.strip():
                prose = result.stdout.strip()
            else:
                print(f"alef chat send failed: {result.stderr[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"LLM synthesis error: {e}", file=sys.stderr)
    else:
        print("Skipping LLM synthesis (pass --synthesize to regenerate prose_profile)")

    profile["prose_profile"] = prose

    with open(OUTPUT_PATH, "w") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)

    print(f"\nSaved: {OUTPUT_PATH}")

    if prose:
        md_path = os.path.expanduser("~/.alef-agent/workspace/newsroom/data/editorial_profile.md")
        updated = datetime.utcnow().strftime("%Y-%m-%d")
        md_content = f"""# Editorial Profile — Gen AI Spotlight (@genaispot)

> This profile is read by the AI editor on every news scan.
> It captures what Jacob picks, what he skips, and what makes a story worth posting.
> Auto-regenerated from {total} published posts. Last updated: {updated}

{prose}
"""
        with open(md_path, "w") as f:
            f.write(md_content)
        print(f"Updated: {md_path}")
        print(f"\n--- PROSE PROFILE ---\n{prose}\n---")
    else:
        print("Prose profile unavailable — structured data only saved. editorial_profile.md NOT updated.")

if __name__ == "__main__":
    main()
