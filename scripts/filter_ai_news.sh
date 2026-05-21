#!/bin/bash
# AI News Filter for the News Scan Pipeline
# Entry-based filtering with source tiers and Reddit noise filter
# Output: TITLE|URL|SOURCE|TIER (pipe-delimited, sorted by tier)
# Requires: blogwatcher CLI

python3 << 'PYEOF'
import subprocess, sys, re

result = subprocess.run(
    ['/usr/local/bin/blogwatcher', 'articles'],
    capture_output=True, text=True, timeout=90
)
raw = result.stdout

# ── Keywords ─────────────────────────────────────────────────────────
# Short keywords (<=3 chars) need word boundaries to prevent substring matches
# e.g., "AI" must not match "affairs", "explain", "maintain"
SHORT_KEYWORDS = ['AI', 'AGI', 'LLM', 'GPU', 'TPU', 'RAG', 'API']

LONG_KEYWORDS = [
    'artificial intelligence', 'machine learning', 'deep learning',
    'language model', 'GPT', 'Claude', 'Gemini', 'ChatGPT',
    'OpenAI', 'Anthropic', 'Google AI', 'Microsoft AI', 'DeepMind',
    'agentic', 'agent', 'neural network', 'transformer', 'diffusion',
    'generative AI', 'gen AI', 'reasoning model', 'multimodal',
    'vision model', 'text-to-image', 'text-to-video', 'Sora', 'DALL-E',
    'Stable Diffusion', 'Midjourney', 'Llama', 'Mistral', 'Hugging Face',
    'inference', 'training', 'fine-tuning', 'embedding', 'vector',
    'context window', 'benchmark', 'open source', 'open-source',
    'robotics', 'autonomous', 'chip', 'NVIDIA',
    'acquisition', 'funding', 'Series', 'valuation',
    'launch', 'release', 'rollout', 'deploy',
    'OpenClaw', 'Qwen', 'DeepSeek', 'Grok', 'xAI',
    'Nano Banana', 'Meta AI', 'Cohere', 'Perplexity', 'Codex',
    'Copilot', 'GitHub Copilot', 'Amazon Q', 'Bedrock',
]

EXCLUDE_KEYWORDS = [
    'layoffs', 'hiring', 'conference', 'event', 'podcast', 'interview',
    'salary', 'office', 'real estate', 'delivery', 'e-commerce',
    'crypto', 'blockchain', 'NFT', 'web3',
]

# Build combined pattern: word-bounded short keywords + substring long keywords
short_pat = r'\b(' + '|'.join(re.escape(k) for k in SHORT_KEYWORDS) + r')\b'
long_pat = '|'.join(re.escape(k) for k in sorted(LONG_KEYWORDS, key=len, reverse=True))
ai_pattern = re.compile(short_pat + '|' + long_pat, re.IGNORECASE)

exclude_pattern = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in EXCLUDE_KEYWORDS) + r')\b',
    re.IGNORECASE
)

# ── Source tiers (names must match blogwatcher exactly) ──────────────
# Customize: add your RSS feed names here with their trust tier
SOURCE_TIERS = {
    # T1: Wire services + official AI lab blogs
    'Reuters Tech': 1, 'Bloomberg Tech': 1, 'Axios AI': 1, 'CNBC Tech': 1,
    'OpenAI Blog': 1,
    # T2: Tech press + priority bloggers
    'TechCrunch AI': 2, 'The Verge': 2, 'THE DECODER': 2, 'VentureBeat AI': 2,
    'Ars Technica': 2, '404 Media': 2, '9to5Google': 2, 'TestingCatalog': 2,
    'Crunchbase News': 2, 'Wired AI': 2, 'MIT Tech Review': 2, 'Google AI Blog': 2,
    'Hugging Face Blog': 2, 'Simon Willison': 2, 'Latent Space': 2,
    # T3: Aggregator / community press / analysis
    'Hacker News AI': 3, 'SiliconANGLE AI': 3, 'AI News': 3,
    'Gary Marcus': 3, 'Bens Bites': 3,
}

# Reddit discussion noise (questions, complaints, memes)
REDDIT_NOISE_START = re.compile(
    r'^(Why|How|What|Can|Does|Is|Has|Are|Do|Should|Would|Could|Anyone|'
    r'Help|Rant|Vent|Am I|ELI5|CMV|PSA|Unpopular|Hot take|DAE|TIL|'
    r'Gah|Kindly explain|Seriously|From Frustration|Gemini Memory)',
    re.IGNORECASE
)

def get_tier(source, title):
    if source in SOURCE_TIERS:
        return SOURCE_TIERS[source]
    if source.startswith('r/') or 'reddit.com' in source:
        title_s = title.strip()
        if REDDIT_NOISE_START.match(title_s):
            return 99
        if title_s.endswith('?'):
            return 99
        if len(title_s) < 20:
            return 99
        return 4
    if source.startswith('http'):
        if 'bloomberg.com' in source: return 1
        if 'cnbc.com' in source: return 1
        if 'reuters.com' in source: return 1
        if 'techcrunch.com' in source: return 2
        if 'theverge.com' in source: return 2
        if 'wired.com' in source: return 2
        return 3
    return 3

# ── Parse blogwatcher entries ────────────────────────────────────────
title = url = source = None
results = []

for line in raw.split('\n'):
    stripped = line.strip()

    m = re.match(r'\[(\d+)\]\s*\[new\]\s*(.*)', stripped)
    if m:
        if title and url and source is not None:
            tier = get_tier(source, title)
            if tier != 99:
                if ai_pattern.search(title) and not exclude_pattern.search(title):
                    results.append((title, url, source, tier))
        title = m.group(2).strip()
        url = source = None
        continue

    if stripped.startswith('Blog:'):
        source = stripped[5:].strip()
    elif stripped.startswith('URL:'):
        url = stripped[4:].strip()

if title and url and source is not None:
    tier = get_tier(source, title)
    if tier != 99:
        if ai_pattern.search(title) and not exclude_pattern.search(title):
            results.append((title, url, source, tier))

results.sort(key=lambda x: x[3])

for t, u, s, tier in results:
    t_clean = t.replace('|', ' —')
    print(f"{t_clean}|{u}|{s}|{tier}")
PYEOF
