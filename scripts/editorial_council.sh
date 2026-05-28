#!/usr/bin/env bash
# ----------------------------------------
# editorial_council.sh — Spawn 3 Alef agents to vote on which stories to draft
# ----------------------------------------
# Requires: Bash 4+ (associative arrays), jq, curl, python3
# Usage:
#   bash editorial_council.sh /path/to/picks.json
#   DRY_RUN=true bash editorial_council.sh /path/to/picks.json
# Output: JSON lines (one per approved story) to stdout
# ----------------------------------------

set -euo pipefail

ALEF_HOME="${ALEF_HOME:-$HOME/.alef-agent}"
API_TOKEN_FILE="$ALEF_HOME/data/api-token"
API_BASE="http://127.0.0.1:3141"
NEWSROOM_CHAT_ID="${NEWSROOM_CHAT_ID:-task:newsroom}"
POLL_TIMEOUT="${COUNCIL_POLL_TIMEOUT:-600}"  # 10 min — handles queued → running → completed
POLL_INTERVAL=5
DRY_RUN="${DRY_RUN:-false}"
MAX_APPROVED=5

# ── Validate inputs ────────────────────────────────────────────
if [ $# -lt 1 ]; then
  echo "Usage: editorial_council.sh <picks_file>" >&2
  exit 1
fi

PICKS_FILE="$1"

if [ ! -f "$PICKS_FILE" ]; then
  echo "error: picks file not found: $PICKS_FILE" >&2
  exit 1
fi

if [ ! -f "$API_TOKEN_FILE" ]; then
  echo "error: API token not found at $API_TOKEN_FILE" >&2
  exit 1
fi

API_TOKEN=$(cat "$API_TOKEN_FILE")

# ── Build council prompt ───────────────────────────────────────
EDITORIAL_PROFILE=""
PROFILE_PATH="$ALEF_HOME/workspace/newsroom/data/editorial_profile.md"
if [ -f "$PROFILE_PATH" ]; then
  EDITORIAL_PROFILE=$(cat "$PROFILE_PATH")
fi

STORIES=$(cat "$PICKS_FILE")

# ── Load recent drop signals from telemetry DB ─────────────────
DEDUP_DB="$ALEF_HOME/workspace/newsroom/data/news_dedup.db"
RECENT_DROPS=""
if [ -f "$DEDUP_DB" ]; then
  RECENT_DROPS=$(DEDUP_DB_PATH="$DEDUP_DB" python3 - <<'PYEOF'
import sqlite3, sys, os, json

db_path = os.environ["DEDUP_DB_PATH"]
try:
    conn = sqlite3.connect(db_path)
    # Look back 7 days for drops that signal editorial avoidance
    rows = conn.execute("""
        SELECT title, category, detail, ts
        FROM post_telemetry
        WHERE action = 'drop'
          AND detail IN ('fatigue', 'duplicate', 'niche', 'boring')
          AND ts >= datetime('now', '-7 days')
        ORDER BY ts DESC
        LIMIT 20
    """).fetchall()
    conn.close()
    if not rows:
        print("")
        sys.exit(0)
    print("=== RECENT EDITORIAL DROPS (avoid similar) ===")
    for title, category, reason, ts in rows:
        label = {
            "fatigue": "📉 Topic Fatigue",
            "duplicate": "🔁 Duplicate",
            "niche": "🎯 Too Niche",
            "boring": "😴 Not Interesting",
        }.get(reason, reason)
        cat = f" [{category}]" if category else ""
        print(f"- {label}{cat}: {title}")
except Exception as e:
    print("", file=sys.stderr)
PYEOF
  )
fi

COUNCIL_PROMPT="You are an editorial reviewer for Gen AI Spotlight, a curated AI news Telegram channel.

=== EDITORIAL PROFILE ===
$EDITORIAL_PROFILE

${RECENT_DROPS:+$RECENT_DROPS

}=== CANDIDATE STORIES ===
$STORIES

=== TASK ===
Review the candidate stories above. For each, decide DRAFT or SKIP.

Criteria:
- Matches editorial taste (not anti-patterns listed in profile)
- Fresh and newsworthy (not old rehashes or generic AI hype)
- Distinct from others in the batch (prefer variety across categories)
- Strong enough to stand alone as a published Telegram post
- SKIP stories similar to recent drops listed above (same topic, same company, same angle)

Use only rank numbers that appear in the stories above.
Return ONLY a JSON array of story rank numbers to draft, e.g. [1, 3, 5, 7]
No explanation. No markdown fences. No commentary. Just the JSON array."

# ── Dry-run mode ───────────────────────────────────────────────
if [ "$DRY_RUN" = "true" ]; then
  echo "=== DRY RUN: Council prompt (${#COUNCIL_PROMPT} chars) ===" >&2
  echo "$COUNCIL_PROMPT" | head -20 >&2
  echo "..." >&2
  echo "=== DRY RUN: Would spawn 3 agents (claude-haiku-4-5, gemini-3-flash-preview, gpt-5.4-mini) ===" >&2
  # Fixture output: first 3 stories approved
  python3 -c "
import json, sys
ranks = [1, 3, 5]
for line in open('$PICKS_FILE'):
    line = line.strip()
    if not line: continue
    try:
        obj = json.loads(line)
        if obj.get('rank') in ranks:
            print(json.dumps(obj))
    except: pass
" 2>/dev/null
  exit 0
fi

# ── Daemon health check ───────────────────────────────────────
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $API_TOKEN" "$API_BASE/api/health" || echo "000")
if [ "$HTTP_STATUS" != "200" ] && [ "$HTTP_STATUS" != "401" ]; then
  echo "error: Alef daemon not reachable at $API_BASE (HTTP $HTTP_STATUS)" >&2
  exit 1
fi


# ── Spawn 3 agents via API (with permMode: readonly) ──────────
spawn_agent() {
  local RUNNER="$1" MODEL="$2"

  # Build JSON payload with jq (handles prompt escaping safely)
  local PAYLOAD
  PAYLOAD=$(jq -n \
    --arg chatId "$NEWSROOM_CHAT_ID" \
    --arg runner "$RUNNER" \
    --arg model "$MODEL" \
    --arg task "$COUNCIL_PROMPT" \
    '{chatId: $chatId, runner: $runner, model: $model, task: $task,
      role: "reviewer", permMode: "readonly"}')

  local RESP
  RESP=$(curl -sf -X POST \
    -H "Authorization: Bearer $API_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "$API_BASE/api/orchestrator/spawn" 2>/dev/null) || {
    echo "spawn failed ($RUNNER): curl error" >&2
    echo ""
    return 1
  }

  # Validate: spawn returns top-level { agentId, queued } — no { ok, data } wrapper
  local AGENT_ID
  AGENT_ID=$(echo "$RESP" | jq -r '.agentId // empty' 2>/dev/null)
  if [ -z "$AGENT_ID" ]; then
    echo "spawn failed ($RUNNER): no agentId in response: $RESP" >&2
    echo ""
    return 1
  fi

  echo "  spawned $RUNNER ($MODEL) → agent $AGENT_ID" >&2
  echo "$AGENT_ID"
}

echo "Spawning council agents..." >&2
AGENT1=$(spawn_agent claude claude-haiku-4-5) || true
AGENT2=$(spawn_agent gemini gemini-3-flash-preview) || true
AGENT3=$(spawn_agent codex gpt-5.4-mini) || true

AGENTS=()
for A in "$AGENT1" "$AGENT2" "$AGENT3"; do
  [ -n "$A" ] && AGENTS+=("$A")
done

if [ ${#AGENTS[@]} -lt 2 ]; then
  echo "error: fewer than 2 agents spawned (${#AGENTS[@]}), cannot reach majority" >&2
  exit 1
fi

echo "  ${#AGENTS[@]} agents spawned, polling for results (timeout: ${POLL_TIMEOUT}s)..." >&2

# ── Poll check-agent until all complete ────────────────────────
check_agent() {
  curl -sf -X POST \
    -H "Authorization: Bearer $API_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"agentId\":\"$1\"}" \
    "$API_BASE/api/orchestrator/check-agent" 2>/dev/null || echo '{"status":"error"}'
}

ELAPSED=0

while true; do
  # Check if all resolved
  ALL_RESOLVED=true
  for AGENT_ID in "${AGENTS[@]}"; do
    if [ ! -f "/tmp/result_${AGENT_ID}.txt" ]; then
      ALL_RESOLVED=false
      break
    fi
  done

  if [ "$ALL_RESOLVED" = "true" ] || [ $ELAPSED -ge "$POLL_TIMEOUT" ]; then
    break
  fi

  sleep $POLL_INTERVAL
  ELAPSED=$((ELAPSED + POLL_INTERVAL))

  for AGENT_ID in "${AGENTS[@]}"; do
    # Skip already-resolved agents
    [ -f "/tmp/result_${AGENT_ID}.txt" ] && continue

    STATUS_JSON=$(check_agent "$AGENT_ID")
    STATUS=$(echo "$STATUS_JSON" | jq -r '.status // "unknown"')

    case "$STATUS" in
      completed)
        echo "$(echo "$STATUS_JSON" | jq -r '.resultSummary // "[]"')" > "/tmp/result_${AGENT_ID}.txt"
        echo "  agent $AGENT_ID: completed (${ELAPSED}s)" >&2
        ;;
      failed|cancelled)
        echo "[]" > "/tmp/result_${AGENT_ID}.txt"
        echo "  agent $AGENT_ID: $STATUS — counting as no votes" >&2
        ;;
      queued|running|starting|idle)
        # Keep waiting
        ;;
      *)
        echo "  agent $AGENT_ID: unknown status '$STATUS'" >&2
        ;;
    esac
  done
done

# Treat still-running agents as no votes after timeout
for AGENT_ID in "${AGENTS[@]}"; do
  if [ ! -f "/tmp/result_${AGENT_ID}.txt" ]; then
    echo "[]" > "/tmp/result_${AGENT_ID}.txt"
    echo "  agent $AGENT_ID: timed out after ${POLL_TIMEOUT}s — counting as no votes" >&2
  fi
done

# ── Tally votes ────────────────────────────────────────────────
# Write results to a temp file for Python to parse safely (avoids quoting hell)
VOTE_TMPFILE=$(mktemp /tmp/council_votes.XXXXXX)

cleanup_all() {
  rm -f "$VOTE_TMPFILE"
  for AGENT_ID in "${AGENTS[@]}"; do
    rm -f "/tmp/result_${AGENT_ID}.txt"
  done
}
trap cleanup_all EXIT

for AGENT_ID in "${AGENTS[@]}"; do
  # Each line: agent_id<TAB>result_summary
  RAW_CONTENT=$(cat "/tmp/result_${AGENT_ID}.txt" 2>/dev/null || echo "[]")
  printf '%s\t%s\n' "$AGENT_ID" "$RAW_CONTENT" >> "$VOTE_TMPFILE"
done

python3 -c "
import json, re, sys, os

MAX_APPROVED = $MAX_APPROVED
PICKS_FILE = '$PICKS_FILE'
VOTE_FILE = '$VOTE_TMPFILE'

def extract_ranks(raw):
    \"\"\"Extract a JSON array of integers 1-10 from agent output. Returns list (may be empty for SKIP), or None for auth/error."""
    raw = (raw or '').strip()
    # Auth / fatal error: don't count as a valid vote
    low = raw.lower()
    if any(kw in low for kw in ['401', 'authenticate', 'invalid authentication', 'failed to authenticate']):
        return None
    # Try direct JSON parse
    try:
        arr = json.loads(raw)
        if isinstance(arr, list):
            return [int(x) for x in arr if isinstance(x, (int, float)) and 1 <= int(x) <= 10]
    except (json.JSONDecodeError, ValueError):
        pass
    # Regex fallback: find LAST [...] (agent may emit prose then the array)
    # Use * not + so empty [] matches too
    matches = re.findall(r'\[[\d\s,]*\]', raw)
    for m in reversed(matches):
        try:
            arr = json.loads(m)
            if isinstance(arr, list):
                return [int(x) for x in arr if isinstance(x, (int, float)) and 1 <= int(x) <= 10]
        except (json.JSONDecodeError, ValueError):
            pass
    return []

# Read agent results
votes = {}
with open(VOTE_FILE) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        parts = line.split('\t', 1)
        if len(parts) != 2:
            continue
        agent_id, raw = parts
        ranks = extract_ranks(raw)
        if ranks is None:
            print(f'  warning: agent {agent_id} returned auth/error — excluded from vote', file=sys.stderr)
            continue
        if ranks:
            print(f'  agent {agent_id} voted for ranks: {ranks}', file=sys.stderr)
            for r in ranks:
                votes[r] = votes.get(r, 0) + 1
        else:
            print(f'  agent {agent_id} voted SKIP (no ranks)', file=sys.stderr)

# Majority vote (>=2), sort by vote count desc, tie-break lowest rank, cap
approved = sorted(
    [(rank, count) for rank, count in votes.items() if count >= 2],
    key=lambda x: (-x[1], x[0])
)[:MAX_APPROVED]

approved_ranks = [r for r, _ in approved]
print(f'  majority approved: {approved_ranks} (from votes: {dict(votes)})', file=sys.stderr)

_stories_meta = {}
_approved_output = []
for _ln in open(PICKS_FILE):
    _ln = _ln.strip()
    if not _ln: continue
    try:
        _o = json.loads(_ln)
        _r = _o.get('rank')
        if _r:
            _stories_meta[_r] = {
                'title': _o.get('title', ''),
                'url': _o.get('url', ''),
                'source': _o.get('source', ''),
                'category': _o.get('category', 'AI'),
            }
            if _r in approved_ranks:
                _approved_output.append(json.dumps(_o))
    except (json.JSONDecodeError, ValueError): pass
_votes_path = f\"/tmp/council_votes_{os.environ.get('USER', 'user')}.json\"
try:
    with open(_votes_path, 'w') as _vf:
        json.dump({'votes': votes, 'approved_ranks': approved_ranks, 'stories': _stories_meta}, _vf)
except OSError as _e:
    print(f'  warn: could not write votes file: {_e}', file=sys.stderr)

for _out in _approved_output:
    print(_out)
" 2>&2

