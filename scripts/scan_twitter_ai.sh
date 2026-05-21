#!/bin/bash
# Twitter/X AI News Scanner (Upgraded for twitter-cli)
# Scans official accounts, reporters/leakers, and trending AI topics

set -e

TWITTER_CLI=$(which twitter || echo "/Users/jbd/.local/bin/twitter")

# Auth check: twitter-cli uses environment variables
if [ -z "$TWITTER_AUTH_TOKEN" ] || [ -z "$TWITTER_CT0" ]; then
  echo "Warning: No Twitter auth environment variables found."
  echo "Twitter scan skipped."
  exit 0
fi

# Calculate yesterday's date to filter out stale news
YESTERDAY=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d yesterday +%Y-%m-%d)
echo "Scanning X/Twitter timeline via twitter feed (since: $YESTERDAY)..."

# Pull from the For You timeline and filter to last 24h.
# Upgrade note: switched from clix to twitter-cli
/usr/local/bin/gtimeout 45s $TWITTER_CLI feed --max 100 --json --full-text 2>/dev/null | \
  jq -rc --arg since "${YESTERDAY}T00:00:00Z" \
    '.data[]? | select(.createdAtISO >= $since) | "\(.text | gsub("\n"; " ")) https://x.com/\(.author.screenName)/status/\(.id)"' \
  2>/dev/null | head -100 || true
