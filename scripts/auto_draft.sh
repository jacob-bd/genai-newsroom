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
NEWSROOM_CHAT_ID="${NEWSROOM_CHAT_ID:-task:newsroom}"
DRY_RUN="${DRY_RUN:-false}"
TEMPLATES=("dark-editorial" "hot-pink-split" "cyan-drenched")
TEMPLATE_IDX=0
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

  echo "" >&2
  echo "━━━ Story #$RANK: $TITLE ━━━" >&2

  # 1. Dedup check
  DEDUP_OUT=$(python3 -c "
import sys, json
sys.path.insert(0, '$SCRIPT_DIR')
try:
    from dedup_db import DedupDB
    db = DedupDB()
    results = db.search('$(echo "$TITLE" | sed "s/'/\\\\'/g")', limit=3)
    # Check for exact or near-duplicate in published posts
    for r in results:
        if r.get('status') == 'published':
            print('DUPLICATE: ' + r.get('title', ''))
            sys.exit(0)
    # Also check by URL
    if db.check_url('$URL'):
        print('DUPLICATE_URL')
        sys.exit(0)
    print('CLEAN')
except ImportError:
    print('CLEAN')  # If dedup_db not available, proceed
except Exception as e:
    print('CLEAN')  # On error, proceed cautiously
" 2>/dev/null || echo "CLEAN")

  if echo "$DEDUP_OUT" | grep -q "DUPLICATE"; then
    echo "  SKIP: duplicate detected — $DEDUP_OUT" >&2
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
  RECENT_COVERAGE=$(python3 -c "
import sys, json
sys.path.insert(0, '$SCRIPT_DIR')
try:
    from dedup_db import DedupDB
    db = DedupDB()
    results = db.search('$(echo "$TITLE" | sed "s/'/\\\\'/g")', limit=5)
    published = [r for r in results if r.get('status') == 'published' and r.get('telegram_url')]
    if published:
        for p in published[:3]:
            print(f\"- {p.get('title', 'N/A')} ({p.get('telegram_url', 'N/A')})\")
    else:
        print('None')
except:
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
Draft the post following Telegram mode from the voice rules exactly.
Then return ONLY valid JSON (no markdown fences, no explanation):
{
  \"draft_html\": \"the full HTML draft text with <b> and <a href> tags, each sentence on its own line with blank lines between\",
  \"slug\": \"short-kebab-slug\",
  \"headline_line1\": \"3-4 words for image line 1\",
  \"headline_line2\": \"3-4 words for image line 2\",
  \"emoji\": \"single story-appropriate emoji\",
  \"category\": \"AI / SUBCATEGORY\",
  \"template_highlight\": \"one word or short phrase to highlight in hot pink — MUST be an exact substring of headline_line1 or headline_line2, not from the body text\",
  \"subline\": \"short subline for the news card\"
}"

  # Dry-run: show prompt, skip actual call
  if [ "$DRY_RUN" = "true" ]; then
    echo "  DRY RUN: would draft '$TITLE' (prompt: ${#DRAFT_PROMPT} chars)" >&2
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    continue
  fi

  # 5. Draft via alef chat send
  echo "  Drafting via OpenCode/DeepSeek..." >&2
  RESP=$(alef chat send "$DRAFT_PROMPT" \
    --backend opencode \
    --model deepseek/deepseek-v4-flash \
    --chat "$NEWSROOM_CHAT_ID" \
    --cwd "$ALEF_HOME/workspace" \
    --json 2>/dev/null || echo '{"ok":false}')

  # Parse response: alef chat send --json → { ok, data: { text, ... } }
  if ! echo "$RESP" | jq -e '.ok == true' >/dev/null 2>&1; then
    echo "  FAIL: alef chat send returned error" >&2
    echo "  Response: $(echo "$RESP" | head -c 200)" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
    continue
  fi

  RAW_TEXT=$(echo "$RESP" | jq -r '.data.text // ""')

  # 6. Extract draft JSON from response (Python — handles fences, preamble, nested braces)
  DRAFT_JSON=$(python3 -c "
import json, re, sys

text = sys.stdin.read()

# Strategy 1: Find JSON block in markdown fences
fenced = re.search(r'\`\`\`(?:json)?\s*(\{.*?\})\s*\`\`\`', text, re.DOTALL)
if fenced:
    try:
        obj = json.loads(fenced.group(1))
        if 'draft_html' in obj:
            print(json.dumps(obj))
            sys.exit(0)
    except json.JSONDecodeError:
        pass

# Strategy 2: Find JSON using brace-balanced scanning
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
            candidate = text[start:i+1]
            try:
                obj = json.loads(candidate)
                if 'draft_html' in obj:
                    print(json.dumps(obj))
                    sys.exit(0)
            except json.JSONDecodeError:
                pass
            start = -1

print('')  # No valid JSON found
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
