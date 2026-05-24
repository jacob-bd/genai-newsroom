#!/bin/bash
# ----------------------------------------
# news_scan_deduped.sh — Automated News Scan Pipeline v2
# ----------------------------------------
#
# Orchestrates seven data sources and pipes them through quality scoring,
# enrichment, and Gemini Flash (llm_editor.py) for AI-powered curation.
#
# Flow:
#   1. RSS via blogwatcher (25 feeds)
#   2. Reddit via JSON API (13 subreddits, score-filtered)
#   3. Twitter via twitter CLI + twitterapi.io
#   4. GitHub trending + releases
#   5. Tavily web search (breaking news supplement)
#   6. Google Search via gsearch CLI (news, last 24h)
#   7. All → quality_score.py → enrich_top_articles.py → llm_editor.py
#   7. blogwatcher read-all
#
# Usage:
#   ./news_scan_deduped.sh              # default: top 7 picks
#   ./news_scan_deduped.sh --top 5      # top 5 picks
# ----------------------------------------

set -e

# Load environment variables from .env if present (safely parsed via Python)
if [ -f "/Users/jbd/.alef-agent/.env" ]; then
  eval $(python3 -c '
with open("/Users/jbd/.alef-agent/.env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("\"").strip("\u0027")
            # Escape single quotes in value for bash safety
            v_escaped = v.replace("\u0027", "\u0027\"\\\"\u0027\"\u0027")
            print(f"export {k}=\u0027{v_escaped}\u0027")
')
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# ── Parse arguments ----------------------------------------
TOP_N=10
NO_LLM=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --top) TOP_N="$2"; shift 2 ;;
    --no-llm) NO_LLM=true; shift ;;
    -h|--help)
      echo "Usage: $0 [--top N] [--no-llm]"
      echo "  --top N    Number of stories to curate (default: 7)"
      echo "  --no-llm   Skip taste_prefilter and llm_editor; output scored candidates only"
      exit 0
      ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

export TOP_N

# ── Temp files (cleaned up on exit) ----------------------------------------
ARTICLES_FILE=$(mktemp /tmp/newscan_articles.XXXXXX)
REDDIT_FILE=$(mktemp /tmp/newscan_reddit.XXXXXX)
TAVILY_FILE=$(mktemp /tmp/newscan_tavily.XXXXXX)
TWITTER_API_FILE=$(mktemp /tmp/newscan_twitterapi.XXXXXX)
SCORED_FILE=$(mktemp /tmp/newscan_scored.XXXXXX)
TASTE_FILE=$(mktemp /tmp/newscan_taste.XXXXXX)
ENRICHED_FILE=$(mktemp /tmp/newscan_enriched.XXXXXX)
PERSISTENT_CANDIDATES="$SCRIPT_DIR/../data/last_scan_candidates.txt"
PERSISTENT_GITHUB="$SCRIPT_DIR/../data/last_scan_github.txt"
PERSISTENT_PICKS="$SCRIPT_DIR/../data/last_scan_picks.json"
PERSISTENT_PICKS_PREV="$SCRIPT_DIR/../data/last_scan_picks_prev.json"
GITHUB_FILE=$(mktemp /tmp/newscan_github.XXXXXX)
TWITTER_RAW=$(mktemp /tmp/newscan_twitter.XXXXXX)
PICKS_FILE=$(mktemp /tmp/newscan_picks.XXXXXX)

cleanup() {
  rm -f "$ARTICLES_FILE" "$REDDIT_FILE" "$TAVILY_FILE" "$TWITTER_API_FILE" \
        "$SCORED_FILE" "$TASTE_FILE" "$ENRICHED_FILE" "$GITHUB_FILE" "$TWITTER_RAW" "$PICKS_FILE"
}
trap cleanup EXIT

# ── Counters for stats ----------------------------------------
RSS_COUNT=0
REDDIT_COUNT=0
TWITTER_COUNT=0
TWITTER_API_COUNT=0
GITHUB_COUNT=0
TAVILY_COUNT=0
PICKS_COUNT=0

GSEARCH_COUNT=0
DIGG_COUNT=0

SS_rss="warn:0"
SS_reddit="warn:0"
SS_twitter_cli="warn:0"
SS_twitter_api="warn:0"
SS_github="warn:0"
SS_tavily="warn:0"
SS_gsearch="warn:0"
SS_digg="warn:0"

echo "----------------------------------------"
echo "  News Scanner v2 (top $TOP_N)"
echo "----------------------------------------"
echo ""

# ----------------------------------------
# SOURCE 1: RSS via blogwatcher (25 feeds)
# ----------------------------------------
echo "[1/6] Scanning RSS feeds..."

RSS_ERR=$(mktemp)
/usr/local/bin/gtimeout 90s /usr/local/bin/blogwatcher scan > /dev/null 2>"$RSS_ERR" || true
RSS_SCAN_ERR=$(cat "$RSS_ERR"); rm -f "$RSS_ERR"

python3 -c '
import sys, subprocess, re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

outpath = sys.argv[1]

# Aggregator domains that republish old or stale content
STALE_DOMAINS = {"msn.com"}

def is_stale_domain(url):
    try:
        domain = urlparse(url).netloc.lower().lstrip("www.")
        return domain in STALE_DOMAINS or any(domain.endswith("." + d) for d in STALE_DOMAINS)
    except Exception:
        return False

def url_date_too_old(url, max_age_days=7):
    """Returns True if URL contains a /YYYY/MM/DD/ date older than max_age_days."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    m = re.search(r"/(\d{4})/(\d{1,2})/(\d{1,2})(?:/|$)", url)
    if m:
        try:
            article_date = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            return article_date < cutoff
        except ValueError:
            pass
    return False

try:
    result = subprocess.run(
        ["/usr/local/bin/blogwatcher", "articles"],
        capture_output=True, text=True, timeout=30
    )
    raw = result.stdout
except Exception as e:
    print(f"  Warning: Could not run blogwatcher articles: {e}", file=sys.stderr)
    raw = ""

# ── AI keyword filter (same logic as filter_ai_news.sh) ----------------------------------------
SHORT_KW = re.compile(r"\b(AI|AGI|LLM|GPU|TPU|RAG|API)\b")
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
    re.IGNORECASE
)

lines = raw.split("\n")
articles = []
filtered_out = 0
i = 0

while i < len(lines):
    line = lines[i].strip()
    m = re.match(r"^\[\d+\]\s+\[new\]\s+(.+)$", line)
    if m:
        title = m.group(1).strip()
        title = title.replace("|", " -")
        source = ""
        url = ""
        for j in range(i + 1, min(i + 5, len(lines))):
            next_line = lines[j].strip()
            if next_line.startswith("Blog:"):
                source = next_line[5:].strip().replace("|", " -")
            elif next_line.startswith("URL:"):
                url = next_line[4:].strip()
        if title and url:
            # Skip "ICYMI" / "In Case You Missed" recycled/stale content
            if re.match(r"^ICYMI[:\s]|^In Case You Missed", title, re.IGNORECASE):
                filtered_out += 1
            elif is_stale_domain(url):
                filtered_out += 1
            elif url_date_too_old(url):
                filtered_out += 1
            elif SHORT_KW.search(title) or LONG_KW.search(title):
                articles.append(f"{title}|{url}|{source}")
            else:
                filtered_out += 1
    i += 1

with open(outpath, "w") as f:
    for a in articles:
        f.write(a + "\n")

print(f"  Extracted {len(articles)} AI-relevant RSS articles ({filtered_out} non-AI filtered out)", file=sys.stderr)
' "$ARTICLES_FILE"

RSS_COUNT=$(wc -l < "$ARTICLES_FILE" | tr -d ' ')
echo "     Found $RSS_COUNT articles from RSS feeds"
if [ "$RSS_COUNT" -gt 0 ]; then
  SS_rss="ok:$RSS_COUNT"
elif [ -n "$RSS_SCAN_ERR" ]; then
  SS_rss="error:$(echo "$RSS_SCAN_ERR" | head -1 | cut -c1-60)"
fi

# ----------------------------------------
# SOURCE 2: Reddit via JSON API (score-filtered)
# ----------------------------------------
echo ""
echo "[2/6] Scanning Reddit (JSON API)..."

REDDIT_ERR=$(mktemp)
if /usr/local/bin/gtimeout 60s python3 "$SCRIPT_DIR/fetch_reddit_news.py" --hours 24 > "$REDDIT_FILE" 2>"$REDDIT_ERR"; then
  REDDIT_COUNT=$(wc -l < "$REDDIT_FILE" | tr -d ' ')
  echo "  Found $REDDIT_COUNT Reddit posts (score-filtered)"
  cat "$REDDIT_FILE" >> "$ARTICLES_FILE"
  rm -f "$REDDIT_ERR"
  if [ "$REDDIT_COUNT" -gt 0 ]; then
    SS_reddit="ok:$REDDIT_COUNT"
  fi
else
  REDDIT_ERR_MSG=$(cat "$REDDIT_ERR"); rm -f "$REDDIT_ERR"
  echo "  Warning: Reddit scan failed (continuing without)"
  REDDIT_COUNT=0
  SS_reddit="error:$(echo "$REDDIT_ERR_MSG" | tail -1 | cut -c1-60)"
fi

# ----------------------------------------
# SOURCE 3: Twitter/X (twitter CLI + twitterapi.io)
# ----------------------------------------
echo ""
echo "[3/6] Scanning X/Twitter..."

# 3a: Twitter CLI (primary — account-based)
TWITTER_ERR=$(mktemp)
/usr/local/bin/gtimeout 90s "$SCRIPT_DIR/scan_twitter_ai.sh" > "$TWITTER_RAW" 2>"$TWITTER_ERR" || true
TWITTER_ERR_MSG=$(cat "$TWITTER_ERR"); rm -f "$TWITTER_ERR"

if [ -s "$TWITTER_RAW" ]; then
  TWITTER_COUNT=$(python3 -c '
import sys, re

twitter_file = sys.argv[1]
articles_file = sys.argv[2]
count = 0

with open(twitter_file, "r") as f:
    lines = f.readlines()

with open(articles_file, "a") as out:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith(("===", "---", "Scanning", "Tier", "Breaking", "Product", "CEO", "TEST")):
            continue
        text = line.replace("|", " -")
        urls = re.findall(r"(https?://\S+)", line)
        external_url = ""
        tweet_url = ""
        for u in urls:
            if "x.com/" in u or "twitter.com/" in u or "t.co/" in u:
                if not tweet_url:
                    tweet_url = u
            else:
                if not external_url:
                    external_url = u
        if external_url:
            out.write(f"{text}|{external_url}|X/Twitter\n")
        else:
            url = tweet_url
            out.write(f"{text}|{url}|X/Twitter (tweet)\n")
        count += 1

print(count)
' "$TWITTER_RAW" "$ARTICLES_FILE")
  echo "     Twitter CLI: $TWITTER_COUNT tweets"
  if [ "$TWITTER_COUNT" -gt 0 ]; then
    SS_twitter_cli="ok:$TWITTER_COUNT"
  fi
else
  TWITTER_COUNT=0
fi
if [ "$SS_twitter_cli" = "warn:0" ]; then
  if echo "$TWITTER_ERR_MSG" | grep -qi "skipped\|no auth"; then
    SS_twitter_cli="skip:no auth"
  fi
fi

# 3b: twitterapi.io (supplement — keyword search)
TWAPI_ERR=$(mktemp)
if /usr/local/bin/gtimeout 30s python3 "$SCRIPT_DIR/fetch_twitter_api.py" --max-queries 2 > "$TWITTER_API_FILE" 2>"$TWAPI_ERR"; then
  TWITTER_API_COUNT=$(wc -l < "$TWITTER_API_FILE" | tr -d ' ')
  echo "     twitterapi.io: $TWITTER_API_COUNT tweets"
  cat "$TWITTER_API_FILE" >> "$ARTICLES_FILE"
  rm -f "$TWAPI_ERR"
  if [ "$TWITTER_API_COUNT" -gt 0 ]; then
    SS_twitter_api="ok:$TWITTER_API_COUNT"
  fi
else
  TWAPI_ERR_MSG=$(cat "$TWAPI_ERR"); rm -f "$TWAPI_ERR"
  echo "  Warning: twitterapi.io scan failed (continuing)"
  TWITTER_API_COUNT=0
  SS_twitter_api="error:$(echo "$TWAPI_ERR_MSG" | tail -1 | cut -c1-60)"
fi

# ----------------------------------------
# SOURCE 4: GitHub Trending + Releases
# ----------------------------------------
echo ""
echo "[4/6] Scanning GitHub trending + releases..."

GH_ERR=$(mktemp)
if /usr/local/bin/gtimeout 45s python3 "$SCRIPT_DIR/github_trending.py" > "$GITHUB_FILE" 2>"$GH_ERR"; then
  GITHUB_COUNT=$(wc -l < "$GITHUB_FILE" | tr -d ' ')
  echo "  Found $GITHUB_COUNT trending/release repos"
  rm -f "$GH_ERR"
  if [ "$GITHUB_COUNT" -gt 0 ]; then
    SS_github="ok:$GITHUB_COUNT"
  fi
else
  GH_ERR_MSG=$(cat "$GH_ERR"); rm -f "$GH_ERR"
  echo "  Warning: GitHub scan timed out or failed (continuing)"
  GITHUB_COUNT=0
  SS_github="error:$(echo "$GH_ERR_MSG" | tail -1 | cut -c1-60)"
fi

# ----------------------------------------
# SOURCE 5: Tavily Web Search (breaking news supplement)
# ----------------------------------------
echo ""
echo "[5/6] Tavily web search..."

TAV_ERR=$(mktemp)
if /usr/local/bin/gtimeout 30s python3 "$SCRIPT_DIR/fetch_web_news.py" --max-queries 3 --max-results 5 > "$TAVILY_FILE" 2>"$TAV_ERR"; then
  TAVILY_COUNT=$(wc -l < "$TAVILY_FILE" | tr -d ' ')
  echo "  Found $TAVILY_COUNT web articles"
  cat "$TAVILY_FILE" >> "$ARTICLES_FILE"
  rm -f "$TAV_ERR"
  if [ "$TAVILY_COUNT" -gt 0 ]; then
    SS_tavily="ok:$TAVILY_COUNT"
  fi
else
  TAV_ERR_MSG=$(cat "$TAV_ERR"); rm -f "$TAV_ERR"
  echo "  Warning: Tavily scan failed (continuing)"
  TAVILY_COUNT=0
  SS_tavily="error:$(echo "$TAV_ERR_MSG" | tail -1 | cut -c1-60)"
fi

# ----------------------------------------
# SOURCE 6: Google Search via RSS Feed
# ----------------------------------------
echo ""
echo "[6/7] Google Search (RSS Feed)..."

GSEARCH_FILE=$(mktemp /tmp/newscan_gsearch.XXXXXX)
GSEARCH_ERR=$(mktemp)

if /usr/local/bin/gtimeout 45s python3 "$SCRIPT_DIR/fetch_google_news_rss.py" --max-results 5 > "$GSEARCH_FILE" 2>"$GSEARCH_ERR"; then
  GSEARCH_COUNT=$(wc -l < "$GSEARCH_FILE" | tr -d ' ')
  echo "  Found $GSEARCH_COUNT articles from Google Search"
  rm -f "$GSEARCH_ERR"
  if [ "$GSEARCH_COUNT" -gt 0 ]; then
    cat "$GSEARCH_FILE" >> "$ARTICLES_FILE"
    SS_gsearch="ok:$GSEARCH_COUNT"
  fi
else
  GSEARCH_ERR_MSG=$(cat "$GSEARCH_ERR"); rm -f "$GSEARCH_ERR"
  echo "  Warning: Google News RSS fetch failed (continuing)"
  GSEARCH_COUNT=0
  SS_gsearch="error:$(echo "$GSEARCH_ERR_MSG" | tail -1 | cut -c1-60)"
fi

rm -f "$GSEARCH_FILE"

# ----------------------------------------
# SOURCE 7: Digg AI Top Stories (di.gg/ai)
# ----------------------------------------
echo ""
echo "[7/7] Digg AI (di.gg/ai)..."

DIGG_ERR=$(mktemp)
DIGG_FILE=$(mktemp /tmp/newscan_digg.XXXXXX)

if /usr/local/bin/gtimeout 30s python3 "$SCRIPT_DIR/fetch_digg_news.py" --max-results 15 > "$DIGG_FILE" 2>"$DIGG_ERR"; then
  DIGG_COUNT=$(wc -l < "$DIGG_FILE" | tr -d ' ')
  echo "  Found $DIGG_COUNT stories from Digg AI"
  if [ "$DIGG_COUNT" -gt 0 ]; then
    cat "$DIGG_FILE" >> "$ARTICLES_FILE"
    SS_digg="ok:$DIGG_COUNT"
  fi
else
  DIGG_ERR_MSG=$(cat "$DIGG_ERR")
  echo "  Warning: Digg AI fetch failed"
  DIGG_COUNT=0
  SS_digg="error:$(echo "$DIGG_ERR_MSG" | tail -1 | cut -c1-60)"
fi

rm -f "$DIGG_FILE" "$DIGG_ERR"

# ----------------------------------------
# QUALITY SCORING PRE-FILTER
# ----------------------------------------
echo ""
TOTAL_RAW=$((RSS_COUNT + REDDIT_COUNT + TWITTER_COUNT + TWITTER_API_COUNT + TAVILY_COUNT + GSEARCH_COUNT + DIGG_COUNT))
echo "Quality scoring ($TOTAL_RAW candidates)..."

if [ "$TOTAL_RAW" -gt 0 ]; then
  python3 "$SCRIPT_DIR/quality_score.py" --input "$ARTICLES_FILE" --max 50 > "$SCORED_FILE" 2>/dev/null
  SCORED_COUNT=$(wc -l < "$SCORED_FILE" | tr -d ' ')
  echo "  Top $SCORED_COUNT articles after scoring + dedup"
else
  cp "$ARTICLES_FILE" "$SCORED_FILE"
  SCORED_COUNT=0
fi

# ----------------------------------------
# TASTE PRE-FILTER (editorial profile alignment) — skipped with --no-llm
# ----------------------------------------
TASTE_COUNT=0
if [ "$NO_LLM" = "true" ]; then
  cp "$SCORED_FILE" "$TASTE_FILE"
  TASTE_COUNT=$SCORED_COUNT
else
  echo ""
  echo "Running taste pre-filter (editorial profile)..."
  if [ "$SCORED_COUNT" -gt 0 ]; then
    if /usr/local/bin/gtimeout 30s python3 "$SCRIPT_DIR/taste_prefilter.py" --input "$SCORED_FILE" --max 25 > "$TASTE_FILE" 2>/tmp/taste_prefilter_${USER}.log; then
      TASTE_COUNT=$(wc -l < "$TASTE_FILE" | tr -d ' ')
      cat /tmp/taste_prefilter_${USER}.log 2>/dev/null
      echo "  $TASTE_COUNT articles after taste filter"
    else
      echo "  Warning: taste filter failed, using scored file"
      cat /tmp/taste_prefilter_${USER}.log 2>/dev/null
      cp "$SCORED_FILE" "$TASTE_FILE"
      TASTE_COUNT=$SCORED_COUNT
    fi
  else
    cp "$SCORED_FILE" "$TASTE_FILE"
    TASTE_COUNT=0
  fi
fi

# ----------------------------------------
# ARTICLE ENRICHMENT (full text for top articles) — skipped with --no-llm
# ----------------------------------------
export FIRECRAWL_LOCAL_URL="${FIRECRAWL_LOCAL_URL:-http://192.168.68.9:3002}"

if [ "$NO_LLM" = "true" ]; then
  cp "$TASTE_FILE" "$ENRICHED_FILE"
else
  echo ""
  echo "Enriching top articles with full text..."
  if [ "$TASTE_COUNT" -gt 0 ]; then
    if /usr/local/bin/gtimeout 60s python3 "$SCRIPT_DIR/enrich_top_articles.py" --input "$TASTE_FILE" --max 15 --max-chars 1200 > "$ENRICHED_FILE" 2>/dev/null; then
      echo "  Enrichment complete"
    else
      echo "  Warning: Enrichment failed (using taste-filtered articles without full text)"
      cp "$TASTE_FILE" "$ENRICHED_FILE"
    fi
  else
    cp "$SCORED_FILE" "$ENRICHED_FILE"
  fi
fi

# ----------------------------------------
# LLM EDITORIAL FILTER (Gemini Flash via llm_editor.py) — skipped with --no-llm
# ----------------------------------------
TOTAL_CANDIDATES=$((TOTAL_RAW + GITHUB_COUNT))

if [ "$TOTAL_CANDIDATES" -eq 0 ]; then
  echo ""
  echo "No new stories found from any source. Nothing to curate."
  exit 0
fi

PICKS_COUNT=0
LLM_SUCCESS=false

if [ "$NO_LLM" = "true" ]; then
  # Output scored candidates for backend LLM to process
  cp "$TASTE_FILE" "$PICKS_FILE"
  PICKS_COUNT=$TASTE_COUNT
  LLM_SUCCESS=true
  echo ""
  echo "LLM skipped (--no-llm). ${PICKS_COUNT} scored candidates ready for backend selection."
  echo "   Pipeline: ${TOTAL_RAW} raw -> ${SCORED_COUNT:-$TOTAL_RAW} scored -> ${PICKS_COUNT} candidates"
else
  echo ""
  echo "Running LLM editorial filter (Gemini Flash)..."
  echo "   Pipeline: ${TOTAL_RAW} raw -> ${SCORED_COUNT:-$TOTAL_RAW} scored -> ${TASTE_COUNT:-$SCORED_COUNT} taste-filtered -> LLM"

  LLM_CMD="python3 $SCRIPT_DIR/llm_editor.py --file $ENRICHED_FILE"
  if [ -s "$GITHUB_FILE" ]; then
    LLM_CMD="$LLM_CMD --github $GITHUB_FILE"
  fi

  if eval "$LLM_CMD" > "$PICKS_FILE" 2>/tmp/llm_editor_${USER}.log; then
    PICKS_COUNT=$(wc -l < "$PICKS_FILE" | tr -d ' ')
    LLM_SUCCESS=true
    echo "  LLM selected $PICKS_COUNT stories"
  else
    echo "  Warning: LLM editor failed (see /tmp/llm_editor_${USER}.log)"
  fi
fi

# ----------------------------------------
# AUTO-DRAFT: Council review → Draft → Post to test channel
# ----------------------------------------
if [ "$LLM_SUCCESS" = true ] && [ -s "$PICKS_FILE" ] && [ "$NO_LLM" = false ]; then
  echo ""
  echo "=========================================="
  echo "AUTO-DRAFT PIPELINE"
  echo "=========================================="

  # Pre-flight: daemon must be running (curl health, not alef status which works offline)
  AUTODRAFT_OK=true
  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3141/api/health || echo "000")
  if [ "$HTTP_STATUS" != "200" ] && [ "$HTTP_STATUS" != "401" ]; then
    echo "  SKIP: Alef daemon not reachable (HTTP $HTTP_STATUS) — auto-draft disabled"
    AUTODRAFT_OK=false
  fi

  if [ "$AUTODRAFT_OK" = true ]; then
    echo "Running editorial council on $PICKS_COUNT stories..."
    COUNCIL_OUT=$(bash "$SCRIPT_DIR/editorial_council.sh" "$PICKS_FILE" \
      2>/tmp/council_${USER}.log) || true
    COUNCIL_COUNT=$(echo "$COUNCIL_OUT" | grep -c . 2>/dev/null || echo 0)
    echo "  Council approved $COUNCIL_COUNT stories for drafting"

    # Log council output for debugging
    if [ -f /tmp/council_${USER}.log ]; then
      tail -20 /tmp/council_${USER}.log 2>/dev/null | sed 's/^/  [council] /' >&2 || true
    fi

    if [ "$COUNCIL_COUNT" -gt 0 ]; then
      # Post council selection summary to newsroom group (-1003682312998)
      export COUNCIL_OUT
      NOTIFICATION_TEXT=$(python3 <<'EOF'
import sys, os, json

votes_path = f"/tmp/council_votes_{os.environ.get('USER', 'user')}.json"
council_out = os.environ.get("COUNCIL_OUT", "")

approved_stories = []
for line in council_out.splitlines():
    line = line.strip()
    if not line: continue
    try:
        approved_stories.append(json.loads(line))
    except (json.JSONDecodeError, ValueError): pass

votes_data = {}
try:
    with open(votes_path) as vf:
        votes_data = json.load(vf)
except (OSError, json.JSONDecodeError): pass

# Normalize votes keys to int once (JSON always deserializes dict keys as str)
votes = {int(k): v for k, v in votes_data.get('votes', {}).items()}
all_stories = votes_data.get('stories', {})
approved_ranks = votes_data.get('approved_ranks', [])

fmt_link = lambda u, s: f'<a href="{u}">{s}</a>' if u else s
get_vc = lambda r, d: votes.get(r, d)

output = [f"🏛️ <b>Editorial Council</b> — {len(approved_stories)} approved\n"]

if approved_stories:
    output.append("<b>✅ Approved:</b>")
    for idx, obj in enumerate(approved_stories, 1):
        rank = obj.get('rank', 0)
        title = obj.get('title', '(no title)')
        url = obj.get('url', '')
        source = obj.get('source', '')
        cat = obj.get('category', 'AI')
        vc = get_vc(rank, '?')
        vote_str = f" <i>({vc}/3)</i>" if vc != '?' else ""
        output.append(f"{idx}. <b>{title}</b>{vote_str}\n   🔗 {fmt_link(url, source)} | <code>{cat}</code>")
    output.append("")

skipped = []
for rank_key, meta in all_stories.items():
    rank_int = int(rank_key)
    if rank_int not in approved_ranks:
        vc = get_vc(rank_int, 0)
        skipped.append((vc, rank_int, meta))
skipped.sort(key=lambda x: (-x[0], x[1]))

if skipped:
    output.append("<b>⏭ Skipped:</b>")
    for vc, rank_int, meta in skipped:
        title = meta.get('title', '(no title)')
        url = meta.get('url', '')
        source = meta.get('source', '')
        vote_str = f"({vc}/3)" if vc else "(0/3)"
        output.append(f"• {title} {vote_str}\n  🔗 {fmt_link(url, source)}")

output.append("\n🚀 Auto-drafting initiated...")
print("\n".join(output))
EOF
)
      python3 "$SCRIPT_DIR/telegram_post.py" \
        --channel "-1003682312998" \
        --text "$NOTIFICATION_TEXT" >/dev/null 2>&1 || true

      echo "Auto-drafting approved stories..."
      echo "$COUNCIL_OUT" | bash "$SCRIPT_DIR/auto_draft.sh" \
        --enriched "$ENRICHED_FILE" \
        2>/tmp/auto_draft_${USER}.log || true

      # Report results
      if [ -f /tmp/auto_draft_${USER}.log ]; then
        echo ""
        tail -5 /tmp/auto_draft_${USER}.log 2>/dev/null | sed 's/^/  [draft] /' || true
      fi
    fi
  fi
fi

# ----------------------------------------
# FORMAT & DISPLAY OUTPUT
# ----------------------------------------
echo ""
# Save scored candidates (preserves scores) instead of enriched format
{
  echo "SCAN: $(date +'%Y-%m-%d %H:%M') ET | ${SCORED_COUNT:-0} scored stories"
  echo "----------------------------------------"
  cat "$SCORED_FILE"
} > "$PERSISTENT_CANDIDATES" 2>/dev/null
cp "$GITHUB_FILE" "$PERSISTENT_GITHUB" 2>/dev/null

# Rotate picks: shift current -> prev, save new as current (keeps last 2 scans)
if [ -f "$PERSISTENT_PICKS" ]; then
  cp "$PERSISTENT_PICKS" "$PERSISTENT_PICKS_PREV" 2>/dev/null
fi
echo "{\"_scan_meta\": {\"scan_time\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"top_n\": $TOP_N}}" > "$PERSISTENT_PICKS"
cat "$PICKS_FILE" >> "$PERSISTENT_PICKS" 2>/dev/null

# ── Build scan health block ----------------------------------------
_status_icon() {
  case "${1%%:*}" in
    ok)    echo "✅" ;;
    warn)  echo "⚠️ " ;;
    skip)  echo "⚠️ " ;;
    error) echo "❌" ;;
    *)     echo "❓" ;;
  esac
}
_status_label() {
  local val="${1#*:}"
  case "${1%%:*}" in
    ok)    echo "$val articles" ;;
    warn)  echo "0 (no results)" ;;
    skip)  echo "skipped ($val)" ;;
    error) echo "error: $val" ;;
    *)     echo "unknown" ;;
  esac
}

SCAN_TIME="$(date +'%b %-d %-I:%M %p') ET"
FILTERED=$((TOTAL_CANDIDATES - ${SCORED_COUNT:-0}))

HEALTH_BLOCK="📊 SCAN HEALTH — ${SCAN_TIME}
$(_status_icon "$SS_rss") RSS (blogwatcher): $(_status_label "$SS_rss")
$(_status_icon "$SS_reddit") Reddit: $(_status_label "$SS_reddit")
$(_status_icon "$SS_twitter_api") twitterapi.io: $(_status_label "$SS_twitter_api")
$(_status_icon "$SS_twitter_cli") Twitter CLI: $(_status_label "$SS_twitter_cli")
$(_status_icon "$SS_github") GitHub trending: $(_status_label "$SS_github")
$(_status_icon "$SS_tavily") Tavily web: $(_status_label "$SS_tavily")
$(_status_icon "$SS_gsearch") Google Search: $(_status_label "$SS_gsearch")
$(_status_icon "$SS_digg") Digg AI: $(_status_label "$SS_digg")
----------------------------------------
🔢 ${TOTAL_CANDIDATES} raw → ${SCORED_COUNT:-0} scored → ${PICKS_COUNT} candidates
🗑️  Filtered: ${FILTERED} (dedup + low score)"

echo "$HEALTH_BLOCK"
echo ""

if [ "$NO_LLM" = "true" ]; then
  echo "----------------------------------------"
  echo "  SCORED CANDIDATES (select top $TOP_N)"
  echo "----------------------------------------"
  echo ""
  if [ -s "$PICKS_FILE" ]; then
    counter=0
    while IFS='|' read -r title url source tier; do
      [ -z "$title" ] && continue
      counter=$((counter + 1))
      echo "$counter. <b>$title</b>"
      echo ""
      echo "   $url"
      echo "   Source: $source"
      echo ""
    done < "$PICKS_FILE"
  else
    echo "No candidates available."
  fi
else
  echo "----------------------------------------"
  echo "  TOP PICKS"
  echo "----------------------------------------"
  echo ""

  if [ "$LLM_SUCCESS" = false ] || [ ! -s "$PICKS_FILE" ]; then
    echo "All LLM providers failed. No curated stories to display."
    echo "Check /tmp/llm_editor_${USER}.log for details."
    echo ""
    echo "Candidates were saved to: $PERSISTENT_CANDIDATES"
    echo "Re-run manually: python3 $SCRIPT_DIR/llm_editor.py --file $PERSISTENT_CANDIDATES"
  else
    python3 -c '
import sys, json

picks_file = sys.argv[1]

EMOJI_MAP = {
    "rss": "📰",
    "twitter": "🐦",
    "github": "🔧",
}

with open(picks_file, "r") as f:
    lines = f.readlines()

counter = 0

for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        pick = json.loads(line)
    except json.JSONDecodeError:
        continue

    counter += 1
    title = pick.get("title", "(no title)")
    summary = pick.get("summary", "")
    url = pick.get("url", "")
    source = pick.get("source", "unknown")
    category = pick.get("category", "other")
    story_type = pick.get("type", "rss")

    is_tweet = "(tweet)" in source
    emoji = "🐦" if is_tweet else EMOJI_MAP.get(story_type, "📰")
    source_display = source.replace(" (tweet)", "")

    if counter > 1:
        print("")
    print(f"{counter}. {emoji} <b>{title}</b>")
    if summary:
        print(f"   {summary}")
    if url:
        print(f"   {url}")
    print(f"   Source: {source_display}")
' "$PICKS_FILE"
  fi
fi

# ----------------------------------------
# RECORD ALL SCORED CANDIDATES TO DEDUP DB
# ----------------------------------------
if [ -s "$SCORED_FILE" ]; then
  python3 -c '
import sys
sys.path.insert(0, sys.argv[2])
try:
    from dedup_db import DedupDB
    db = DedupDB()
    articles = []
    with open(sys.argv[1], "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) >= 3:
                articles.append({"url": parts[1], "title": parts[0], "source": parts[2]})
    db.record_batch(articles, status="scored")
    print(f"  Recorded {len(articles)} scored candidates to dedup DB", file=sys.stderr)
except ImportError:
    print("  Warning: dedup_db not available, skipping DB recording", file=sys.stderr)
except Exception as e:
    print(f"  Warning: DB recording failed: {e}", file=sys.stderr)
' "$SCORED_FILE" "$SCRIPT_DIR" 2>&1
fi

# ----------------------------------------
# CLEANUP: Mark articles as read in blogwatcher
# ----------------------------------------
echo "Marking RSS articles as read..."
echo "y" | /usr/local/bin/blogwatcher read-all > /dev/null 2>&1 || echo "  Warning: Could not mark articles as read"

# ----------------------------------------
# DEDUP DB MAINTENANCE: prune entries older than 30 days
# ----------------------------------------
python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
try:
    from dedup_db import DedupDB
    import sqlite3
    db = DedupDB()
    conn = sqlite3.connect(str(db.db_path))
    before = conn.execute("SELECT COUNT(*) FROM seen_articles").fetchone()[0]
    conn.execute("DELETE FROM seen_articles WHERE first_seen < datetime(\"now\", \"-30 days\")")
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM seen_articles").fetchone()[0]
    pruned = before - after
    if pruned > 0:
        conn.execute("VACUUM")
        print(f"  Pruned {pruned} dedup entries older than 30 days ({after} remaining)", file=sys.stderr)
    conn.close()
except Exception as e:
    print(f"  Warning: dedup cleanup failed: {e}", file=sys.stderr)
' "$SCRIPT_DIR" 2>&1

# ----------------------------------------
# STATS
# ----------------------------------------
echo "----------------------------------------"
echo "Sources: $RSS_COUNT RSS + $REDDIT_COUNT Reddit + $((TWITTER_COUNT + TWITTER_API_COUNT)) Twitter + $GITHUB_COUNT GitHub + $TAVILY_COUNT Tavily + $GSEARCH_COUNT gsearch + $DIGG_COUNT Digg AI"
echo "Pipeline: $TOTAL_CANDIDATES raw -> ${SCORED_COUNT:-N/A} scored -> $PICKS_COUNT curated picks"
echo "----------------------------------------"
