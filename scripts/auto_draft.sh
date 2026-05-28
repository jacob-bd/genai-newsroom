#!/usr/bin/env bash
# ----------------------------------------
# auto_draft.sh — Draft council-approved stories via Alef + DeepSeek
# ----------------------------------------
# Requires: Bash 4+, jq, python3, node, alef CLI
# Input: JSON lines on stdin (from editorial_council.sh)
# Usage:
#   bash editorial_council.sh picks.json | bash auto_draft.sh --enriched enriched.json
#   DRY_RUN=true bash auto_draft.sh --enriched enriched.json < approved.json
# ----------------------------------------

set -euo pipefail

ALEF_HOME="${ALEF_HOME:-$HOME/.alef-agent}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export SCRIPT_DIR
NEWSROOM_CHAT_ID="${NEWSROOM_CHAT_ID:-task:newsroom}"
DRY_RUN="${DRY_RUN:-false}"
SUCCESS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

# ── Parse arguments ────────────────────────────────────────────
ENRICHED_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --enriched) ENRICHED_FILE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "Unknown arg: $1" >&2; shift ;;
  esac
done

if [ -z "$ENRICHED_FILE" ]; then
  echo "FATAL: --enriched <path> required" >&2
  exit 1
fi

if [ ! -f "$ENRICHED_FILE" ]; then
  echo "FATAL: enriched file not found: $ENRICHED_FILE" >&2
  exit 1
fi

# ── Pre-flight: OpenCode + DeepSeek ───────────────────────────
if [ "$DRY_RUN" != "true" ]; then
  if ! alef opencode status >/dev/null 2>&1; then
    echo "FATAL: OpenCode unavailable — drafts skipped" >&2
    exit 1
  fi
  echo "Pre-flight: OpenCode available" >&2
fi

# ── Load skill content for inlining in prompt ─────────────────
VOICE_SKILL_PATH="$ALEF_HOME/workspace/skills/jbd-my-voice/SKILL.md"
NEWSROOM_SKILL_PATH="$ALEF_HOME/workspace/newsroom/skills/newsroom/SKILL.md"
EDITORIAL_PROFILE_PATH="$ALEF_HOME/workspace/newsroom/data/editorial_profile.md"

VOICE_SKILL=""
if [ -f "$VOICE_SKILL_PATH" ]; then
  # Extract Telegram mode section only (most relevant for drafting)
  VOICE_SKILL=$(sed -n '/## 📢 TELEGRAM MODE/,/## 📧 EMAIL MODES/p' "$VOICE_SKILL_PATH" | head -80)
fi

NEWSROOM_EXCERPT=""
if [ -f "$NEWSROOM_SKILL_PATH" ]; then
  # Extract Phase 1 drafting rules + file organization
  NEWSROOM_EXCERPT=$(sed -n '/## File Organization/,/## Phase 2/p' "$NEWSROOM_SKILL_PATH" | head -60)
fi

EDITORIAL_PROFILE=""
if [ -f "$EDITORIAL_PROFILE_PATH" ]; then
  EDITORIAL_PROFILE=$(cat "$EDITORIAL_PROFILE_PATH")
fi

# Also load banned words / universal DNA from voice skill
VOICE_DNA=""
if [ -f "$VOICE_SKILL_PATH" ]; then
  VOICE_DNA=$(sed -n '/## STEP 2: UNIVERSAL DNA/,/## 💼 PUBLIC MODE/p' "$VOICE_SKILL_PATH" | head -40)
fi

echo "Skills loaded: voice=$([ -n "$VOICE_SKILL" ] && echo "yes" || echo "NO"), newsroom=$([ -n "$NEWSROOM_EXCERPT" ] && echo "yes" || echo "NO")" >&2

# ── Process each approved story ───────────────────────────────
while IFS= read -r STORY_JSON; do
  [ -z "$STORY_JSON" ] && continue

  # Parse story fields
  TITLE=$(echo "$STORY_JSON" | jq -r '.title // "untitled"')
  URL=$(echo "$STORY_JSON" | jq -r '.url // ""')
  SOURCE=$(echo "$STORY_JSON" | jq -r '.source // "unknown"')
  RANK=$(echo "$STORY_JSON" | jq -r '.rank // 0')
  SUMMARY=$(echo "$STORY_JSON" | jq -r '.summary // ""')
  CATEGORY=$(echo "$STORY_JSON" | jq -r '.category // "AI"')
  SLUG=$(echo "$TITLE" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/-\+/-/g; s/^-//; s/-$//' | cut -c1-50)

  echo "" >&2
  echo "━━━ Story #$RANK: $TITLE ━━━" >&2

  # 1. Dedup check
  DEDUP_OUT=$(DEDUP_TITLE="$TITLE" DEDUP_URL="$URL" python3 -c "
import os, sys, json
sys.path.insert(0, os.environ.get('SCRIPT_DIR', '.'))
title = os.environ.get('DEDUP_TITLE', '')
url = os.environ.get('DEDUP_URL', '')
try:
    from dedup_db import DedupDB
    db = DedupDB()
    results = db.search(title, limit=3)
    for r in results:
        if r.get('status') == 'published':
            print('DUPLICATE: ' + r.get('title', ''))
            sys.exit(0)
    if db.check_url(url):
        print('DUPLICATE_URL')
        sys.exit(0)
    print('CLEAN')
except ImportError:
    print('CLEAN')
except Exception:
    print('CLEAN')
" 2>/dev/null || echo "CLEAN")

  if echo "$DEDUP_OUT" | grep -q "DUPLICATE"; then
    echo "  SKIP: duplicate detected — $DEDUP_OUT" >&2
    SKIP_COUNT=$((SKIP_COUNT + 1))
    continue
  fi

  # 1b. Check newsroom_pending.json for same slug (prevents double-post within same run)
  PENDING_SLUG_CHECK=$(python3 -c "
import json, sys, os
pending_path = os.path.expanduser('~/.alef-agent/workspace/newsroom/data/newsroom_pending.json')
slug = '$SLUG'
try:
    with open(pending_path) as f:
        pending = json.load(f)
    for entry in pending.values():
        if entry.get('slug') == slug:
            print('DUPLICATE_PENDING:' + str(entry.get('message_id', '?')))
            sys.exit(0)
    print('CLEAN')
except FileNotFoundError:
    print('CLEAN')
except Exception:
    print('CLEAN')
" 2>/dev/null || echo "CLEAN")

  if echo "$PENDING_SLUG_CHECK" | grep -q "DUPLICATE_PENDING"; then
    echo "  SKIP: slug '$SLUG' already in pending — $PENDING_SLUG_CHECK" >&2
    SKIP_COUNT=$((SKIP_COUNT + 1))
    continue
  fi

  # 2. Get enriched text for this story
  ENRICHED_TEXT=$(python3 -c "
import json, sys
rank = $RANK
for line in open('$ENRICHED_FILE'):
    line = line.strip()
    if not line: continue
    try:
        obj = json.loads(line)
        if obj.get('rank') == rank:
            print(obj.get('full_text', obj.get('enriched_text', obj.get('summary', ''))))
            sys.exit(0)
    except: pass
# Fallback: use summary from the story itself
print('$SUMMARY')
" 2>/dev/null || echo "$SUMMARY")

  # 3. Check prior coverage for callback weaving
  RECENT_COVERAGE=$(DEDUP_TITLE="$TITLE" python3 -c "
import os, sys, json
sys.path.insert(0, os.environ.get('SCRIPT_DIR', '.'))
title = os.environ.get('DEDUP_TITLE', '')
try:
    from dedup_db import DedupDB
    db = DedupDB()
    results = db.search(title, limit=5)
    published = [r for r in results if r.get('status') == 'published' and r.get('telegram_url')]
    if published:
        for p in published[:3]:
            print(f\"- {p.get('title', 'N/A')} ({p.get('telegram_url', 'N/A')})\")
    else:
        print('None')
except Exception:
    print('None')
" 2>/dev/null || echo "None")

  # 4. Build draft prompt
  DRAFT_PROMPT="You are drafting a Telegram news post for Gen AI Spotlight (@genaispot).

=== VOICE RULES (MANDATORY — follow exactly) ===
$VOICE_DNA

$VOICE_SKILL

=== NEWSROOM FORMAT RULES ===
$NEWSROOM_EXCERPT

=== EDITORIAL PROFILE ===
$EDITORIAL_PROFILE

=== STORY TO DRAFT ===
Title: $TITLE
URL: $URL
Source: $SOURCE
Category: $CATEGORY
Full text / summary:
$ENRICHED_TEXT

Prior coverage (weave a callback link ONLY if genuinely relevant):
$RECENT_COVERAGE

=== OUTPUT FORMAT ===
CRITICAL: Output ONLY the raw JSON object. Zero text before or after. No markdown fences. No explanation. Start your response with { and end with }.

Draft the post following Telegram mode from the voice rules exactly.
Then return ONLY valid JSON (no markdown fences, no explanation):
{
  \"draft_html\": \"the full HTML draft text with <b> and <a href> tags, each sentence on its own line with blank lines between\",
  \"slug\": \"short-kebab-slug\",
  \"headline_line1\": \"3-4 words for image line 1\",
  \"headline_line2\": \"3-4 words for image line 2\",
  \"emoji\": \"single story-appropriate emoji\",
  \"category\": \"AI / SUBCATEGORY\",
  \"template_highlight\": \"2-3 key words or phrases that capture the essence of the story, comma-separated (e.g. \\\"OpenAI,IPO\\\", \\\"Karpathy,Anthropic\\\", \\\"\$355M,Modal\\\") — each MUST be an exact substring of headline_line1 or headline_line2. Pick the most newsworthy nouns: company names, dollar amounts, model names, action verbs. NOT generic words like 'new', 'AI', 'says'.\",
  \"subline\": \"short subline for the news card (include dollar amounts like \$1B, \$492M; do NOT strip the \$ sign — 'raises \$1B at \$25B' not 'raises 1B at 25B'). Example: 'Raises \$1B at \$25B valuation' or 'Hits \$492M ARR with 50% growth'\"
}"

  # Dry-run: show prompt, skip actual call
  if [ "$DRY_RUN" = "true" ]; then
    echo "  DRY RUN: would draft '$TITLE' (prompt: ${#DRAFT_PROMPT} chars)" >&2
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    continue
  fi

  # 5. Draft via alef chat send — retry once, then fall back to Claude Haiku
  echo "  Drafting via OpenCode/DeepSeek..." >&2
  RESP=$(alef chat send "$DRAFT_PROMPT" \
    --backend opencode \
    --model deepseek/deepseek-v4-flash \
    --chat "$NEWSROOM_CHAT_ID" \
    --cwd "$ALEF_HOME/workspace" \
    --json 2>/dev/null || echo '{"ok":false}')

  if ! echo "$RESP" | jq -e '.ok == true' >/dev/null 2>&1; then
    echo "  WARN: OpenCode attempt 1 failed — retrying in 3s..." >&2
    sleep 3
    RESP=$(alef chat send "$DRAFT_PROMPT" \
      --backend opencode \
      --model deepseek/deepseek-v4-flash \
      --chat "$NEWSROOM_CHAT_ID" \
      --cwd "$ALEF_HOME/workspace" \
      --json 2>/dev/null || echo '{"ok":false}')
  fi

  if ! echo "$RESP" | jq -e '.ok == true' >/dev/null 2>&1; then
    echo "  WARN: OpenCode unavailable — falling back to Claude Haiku..." >&2
    RESP=$(alef chat send "$DRAFT_PROMPT" \
      --backend claude \
      --model claude-haiku-4-5-20251001 \
      --json 2>/dev/null || echo '{"ok":false}')
  fi

  if ! echo "$RESP" | jq -e '.ok == true' >/dev/null 2>&1; then
    echo "  FAIL: all backends failed for '$TITLE'" >&2
    echo "  Last response: $(echo "$RESP" | head -c 200)" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
    continue
  fi

  RAW_TEXT=$(echo "$RESP" | jq -r '.data.text // ""')

  # 6. Extract draft JSON (handles fences, prose preamble, unescaped control chars)
  DRAFT_JSON=$(python3 -c "
import json, re, sys

text = sys.stdin.read()

def try_parse(s):
    for strict in (True, False):
        try:
            obj = json.loads(s, strict=strict)
            if 'draft_html' in obj:
                return obj
        except Exception:
            pass
    return None

# Strategy 1: JSON in markdown fences
fenced = re.search(r'\`\`\`(?:json)?\s*(\{.*?\})\s*\`\`\`', text, re.DOTALL)
if fenced:
    obj = try_parse(fenced.group(1))
    if obj:
        print(json.dumps(obj))
        sys.exit(0)

# Strategy 2: Brace-balanced scanner (handles prose before JSON)
depth = 0
start = -1
for i, ch in enumerate(text):
    if ch == '{':
        if depth == 0:
            start = i
        depth += 1
    elif ch == '}':
        depth -= 1
        if depth == 0 and start >= 0:
            obj = try_parse(text[start:i+1])
            if obj:
                print(json.dumps(obj))
                sys.exit(0)
            start = -1

# Strategy 3: Locate draft_html key, scan outward for enclosing object
m = re.search(r'\"draft_html\"\s*:', text)
if m:
    for j in range(m.start(), -1, -1):
        if text[j] == '{':
            for k in range(len(text) - 1, m.end(), -1):
                if text[k] == '}':
                    obj = try_parse(text[j:k+1])
                    if obj:
                        print(json.dumps(obj))
                        sys.exit(0)
            break

print('')
" <<< "$RAW_TEXT" 2>/dev/null)

  if [ -z "$DRAFT_JSON" ]; then
    echo "  FAIL: could not parse draft JSON from response" >&2
    echo "  Raw text (first 300 chars): $(echo "$RAW_TEXT" | head -c 300)" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
    continue
  fi

  # 7. Extract fields from draft JSON
  SLUG=$(echo "$DRAFT_JSON" | jq -r '.slug // "untitled"')
  DRAFT_HTML=$(echo "$DRAFT_JSON" | jq -r '.draft_html // ""')
  H1=$(echo "$DRAFT_JSON" | jq -r '.headline_line1 // "Breaking"')
  H2=$(echo "$DRAFT_JSON" | jq -r '.headline_line2 // "News"')
  EMOJI=$(echo "$DRAFT_JSON" | jq -r '.emoji // "📰"')
  CATEGORY=$(echo "$DRAFT_JSON" | jq -r '.category // "AI"')
  HIGHLIGHT=$(echo "$DRAFT_JSON" | jq -r '.template_highlight // ""')
  SUBLINE=$(echo "$DRAFT_JSON" | jq -r '.subline // ""')
  # Auto-restore missing dollar signs: "492M" → "$492M", "\$492M" → "$492M"
  SUBLINE=$(echo "$SUBLINE" | sed -E 's/\\?\$?([0-9]+(\.[0-9]+)?[BMKbmk])\b/$\1/g')
  TODAY=$(date +%Y-%m-%d)
  TEMPLATE="dark-editorial"

  if [ -z "$DRAFT_HTML" ]; then
    echo "  FAIL: draft_html is empty" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
    continue
  fi

  # Validate headline lines: H2 must not start with lowercase (split-word bug)
  H2_FIRST=$(echo "$H2" | cut -c1)
  if echo "$H2_FIRST" | grep -q '[a-z]'; then
    echo "  WARN: headline_line2 starts lowercase ('$H2') — likely split word, auto-fixing" >&2
    # Merge and re-split at last space before midpoint
    FULL_HEAD="$H1 $H2"
    MIDPOINT=$(( ${#FULL_HEAD} / 2 ))
    # Find last space at or before midpoint
    H1=$(echo "$FULL_HEAD" | python3 -c "
import sys
s = sys.stdin.read().strip()
mid = len(s) // 2
idx = s.rfind(' ', 0, mid + 1)
if idx < 0: idx = s.find(' ')
print(s[:idx] if idx >= 0 else s)
")
    H2=$(echo "$FULL_HEAD" | python3 -c "
import sys
s = sys.stdin.read().strip()
mid = len(s) // 2
idx = s.rfind(' ', 0, mid + 1)
if idx < 0: idx = s.find(' ')
print(s[idx+1:] if idx >= 0 else '')
")
    echo "  Auto-fixed headlines: '$H1' / '$H2'" >&2
  fi

  echo "  Draft: $SLUG (${#DRAFT_HTML} chars), template: $TEMPLATE" >&2

  # 8. Save draft to file
  mkdir -p "$ALEF_HOME/workspace/newsroom/tmp"
  echo "$DRAFT_HTML" > "$ALEF_HOME/workspace/newsroom/tmp/${SLUG}_draft.txt"

  # 9. Render news-card image
  mkdir -p "$ALEF_HOME/workspace/newsroom/media"
  IMAGE_PATH="$ALEF_HOME/workspace/newsroom/media/${TODAY}_${SLUG}.png"

  echo "  Rendering news card ($TEMPLATE)..." >&2
  if ! node "$ALEF_HOME/workspace/newsroom/skills/news-cards/render.mjs" \
    --template "$TEMPLATE" \
    --category "$CATEGORY" \
    --headline "$H1 $H2" \
    --highlight "$HIGHLIGHT" \
    --subline "$SUBLINE" \
    --output "$IMAGE_PATH" 2>&1; then
    echo "  FAIL: render.mjs failed for $SLUG" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
    continue
  fi

  if [ ! -f "$IMAGE_PATH" ]; then
    echo "  FAIL: image not created at $IMAGE_PATH" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
    continue
  fi

  # 9.5 Render clean background image for callbacks/classic mode
  CLEAN_BG_PATH="$ALEF_HOME/workspace/newsroom/media/${TODAY}_${SLUG}_clean.png"
  echo "  Rendering clean background ($TEMPLATE)..." >&2
  if ! node "$ALEF_HOME/workspace/newsroom/skills/news-cards/render.mjs" \
    --template "$TEMPLATE" \
    --category " " \
    --headline " " \
    --subline " " \
    --output "$CLEAN_BG_PATH" 2>&1; then
    echo "  FAIL: render.mjs clean bg failed for $SLUG" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
    continue
  fi

  if [ ! -f "$CLEAN_BG_PATH" ]; then
    echo "  FAIL: clean background not created at $CLEAN_BG_PATH" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
    continue
  fi

  # 10. Post to test channel via newsroom_post.py
  echo "  Posting to test channel..." >&2
  if ! python3 "$ALEF_HOME/workspace/newsroom/scripts/newsroom_post.py" \
    --slug "$SLUG" \
    --draft "$ALEF_HOME/workspace/newsroom/tmp/${SLUG}_draft.txt" \
    --image "$IMAGE_PATH" \
    --clean-bg "$CLEAN_BG_PATH" \
    --headline1 "$H1" --headline2 "$H2" \
    --emoji "$EMOJI" --source-url "$URL" --title "$TITLE" \
    --template-category "$CATEGORY" \
    --template-headline "$H1 $H2" \
    --template-subline "$SUBLINE" \
    --template-highlight "$HIGHLIGHT" 2>&1; then
    echo "  FAIL: newsroom_post.py failed for $SLUG" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
    continue
  fi

  echo "  SUCCESS: posted $SLUG to test channel" >&2
  SUCCESS_COUNT=$((SUCCESS_COUNT + 1))

done

echo "" >&2
echo "Auto-draft complete: $SUCCESS_COUNT success, $FAIL_COUNT failed, $SKIP_COUNT skipped" >&2
exit 0
