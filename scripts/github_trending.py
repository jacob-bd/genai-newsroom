#!/usr/bin/env python3
"""
Fetch GitHub trending repos (daily) and filter for AI relevance.

Outputs pipe-delimited TITLE|URL|SOURCE format compatible with news_scan_deduped.sh.

Usage:
    python3 github_trending.py [--max-results 15] [--language python]
"""

import argparse
import html
import re
import ssl
import sys
import urllib.request

_SSL_CTX = ssl.create_default_context()

TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# AI relevance filter — same logic as news_scan_deduped.sh
SHORT_KW = re.compile(r"\b(AI|LLM|GPU|TPU|RAG|AGI|MCP|API|NLP|GPT|RL|LoRA)\b")
LONG_KW = re.compile(
    r"artificial.intelligence|machine.learning|deep.learning|"
    r"language.model|transformer|diffusion|generative|inference|"
    r"training|fine.tun|embedding|vector|neural|multimodal|"
    r"openai|anthropic|gemini|claude|llama|mistral|deepseek|"
    r"qwen|grok|cohere|hugging|ollama|vllm|onnx|triton|"
    r"cuda|pytorch|tensorflow|jax|xla|"
    r"agent|agentic|autonomous|reasoning|retrieval|rag|"
    r"chatbot|assistant|copilot|"
    r"stable.diffusion|imagen|flux|midjourney|"
    r"robotics|reinforcement|reward|policy|"
    r"benchmark|evaluation|evals|"
    r"lora|qlora|peft|sft|rlhf|dpo|grpo",
    re.IGNORECASE,
)


def is_ai_relevant(text):
    return bool(SHORT_KW.search(text) or LONG_KW.search(text))


def fetch_trending(language="", since="daily", max_results=15):
    url = f"https://github.com/trending/{language}?since={since}&spoken_language_code=en"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Warning: GitHub trending fetch failed: {e}", file=sys.stderr)
        return ""


def parse_trending(html_content, max_results):
    # Each repo is in an <article class="Box-row"> block
    article_pattern = re.compile(
        r'<article[^>]+class="[^"]*Box-row[^"]*"[^>]*>(.*?)</article>',
        re.DOTALL,
    )
    repo_link_pattern = re.compile(r'<h2[^>]*>\s*<a[^>]+href="/([^"]+)"', re.DOTALL)
    desc_pattern = re.compile(r'<p[^>]+class="[^"]*col-9[^"]*"[^>]*>(.*?)</p>', re.DOTALL)
    stars_pattern = re.compile(r'([\d,]+)\s+stars today', re.IGNORECASE)

    results = []

    for m in article_pattern.finditer(html_content):
        block = m.group(1)

        repo_m = repo_link_pattern.search(block)
        if not repo_m:
            continue
        repo_path = repo_m.group(1).strip().strip("/")
        if repo_path.count("/") != 1:
            continue
        repo_url = f"https://github.com/{repo_path}"

        # Description
        desc_m = desc_pattern.search(block)
        if desc_m:
            desc_raw = desc_m.group(1)
            desc = html.unescape(re.sub(r"<[^>]+>", "", desc_raw)).strip()
        else:
            desc = ""

        # Stars today
        stars_m = stars_pattern.search(block)
        stars_str = stars_m.group(1).replace(",", "") if stars_m else "0"
        try:
            stars_today = int(stars_str)
        except ValueError:
            stars_today = 0

        # Build combined text for AI relevance check
        full_text = f"{repo_path} {desc}"
        if not is_ai_relevant(full_text):
            continue

        # Title: "owner/repo — description" (capped at 120 chars)
        owner_repo = repo_path.replace("|", " -")
        if desc:
            desc_clean = desc.replace("|", " -")[:80]
            title = f"{owner_repo}: {desc_clean}"
        else:
            title = owner_repo

        source_label = f"GitHub Trending ({stars_today:,} stars today)" if stars_today else "GitHub Trending"
        results.append({
            "title": title,
            "url": repo_url,
            "source": source_label,
            "stars_today": stars_today,
        })

        if len(results) >= max_results:
            break

    # Sort by stars descending
    results.sort(key=lambda x: x["stars_today"], reverse=True)
    return results[:max_results]


def main():
    parser = argparse.ArgumentParser(description="Fetch AI-relevant GitHub trending repos")
    parser.add_argument("--max-results", type=int, default=15, help="Max repos to output (default: 15)")
    parser.add_argument("--language", default="", help="Filter by language slug (e.g. python). Default: all.")
    parser.add_argument("--since", default="daily", choices=["daily", "weekly", "monthly"],
                        help="Trending period (default: daily)")
    args = parser.parse_args()

    html_content = fetch_trending(args.language, args.since, args.max_results)
    if not html_content:
        sys.exit(1)

    repos = parse_trending(html_content, args.max_results)

    for r in repos:
        print(f"{r['title']}|{r['url']}|{r['source']}")

    print(f"  Done: {len(repos)} AI-relevant repos from GitHub Trending", file=sys.stderr)


if __name__ == "__main__":
    main()
