# Newsroom Automation Reference

## Cron Schedule

| Job ID | Cron | What runs |
|--------|------|-----------|
| 1 | `15 7,10,13,16,19 * * *` | News Scanner (LLM picks top 7, logs to `last_scan_picks.json`) |
| 6 | `30 15 * * 5` | Weekly Digest prep (Fridays 3:30pm ET) |
| 11 | `0 1 * * 2` | Editorial Profile Rebuild (Tuesdays 1am) |
| 12 | `0 6,9,12,15,18,21 * * *` | Autonomous Newsroom (full pipeline, every 3h) |

## Full Pipeline (Job 12)

```bash
# Step 1: Scan
bash scripts/news_scan_deduped.sh --top 10 --no-llm > /tmp/picks.json

# Step 2: Council vote
bash scripts/editorial_council.sh /tmp/picks.json > /tmp/approved.json

# Step 3: Enrich
python3 scripts/enrich_top_articles.py /tmp/approved.json > /tmp/enriched.json

# Step 4: Draft + post to test channel
bash scripts/editorial_council.sh /tmp/picks.json | \
  bash scripts/auto_draft.sh --enriched /tmp/enriched.json
```

## Manual Operations

```bash
# Run scanner manually
HOME=/Users/jbd bash scripts/news_scan_deduped.sh --top 7

# Force-draft a specific story
HOME=/Users/jbd python3 scripts/newsroom_post.py \
  --slug my-slug \
  --draft /path/to/draft.txt \
  --image /path/to/image.png \
  --headline1 "Breaking" --headline2 "News"

# Rebuild editorial profile
HOME=/Users/jbd python3 scripts/editorial_profile_builder.py --synthesize

# Search published posts
HOME=/Users/jbd python3 scripts/post_search.py "query terms"
```

## Data Files (in `data/`)

| File | Purpose |
|------|---------|
| `news_dedup.db` | SQLite: published posts, dedup index, telemetry |
| `editorial_profile.md` | AI-synthesized taste profile (updated weekly) |
| `last_scan_picks.json` | Latest scan candidates with metadata |
| `scanner_presented.md` | Running log of all presented URLs (dedup fence) |
| `newsroom_pending.json` | Live review state: staged posts awaiting approval |
| `newsroom_whiteboard.md` | Human-readable story queue |

## Telemetry (`post_telemetry` table in `news_dedup.db`)

Actions logged: `approve`, `drop`, `buffer:queue`, `buffer:publish`, `buffer:draft`, `buffer:skip`, `image:classic`, `image:template:<name>`, `edit:<mode>`, `opinion:<style>`, `factcheck`, `newsource`

Drop reasons feed back into editorial council as avoidance signals (7-day window).

## Image Pipeline

1. `gemcli_image.sh` generates clean background (no text)
2. `news_image_overlay.py` stamps headline bars (Pillow)
   - Line 1 bar: `#F000E7` (hot pink)
   - Line 2 bar: `#0CD9EA` (cyan)

OR for template cards:

1. `skills/news-cards/render.mjs` renders HTML template to PNG
   - Templates: `dark-editorial`, `hot-pink-split`, `cyan-drenched`
   - Default: `dark-editorial`

## Adding a News Source

Edit `news_scan_deduped.sh` — add to the source list section. Each source runs a fetcher script from `scripts/fetch_*.py`.

## Editorial Council Config

`scripts/editorial_council.sh`:
- 3 agents: Claude Haiku, Gemini Flash, GPT mini
- Majority vote (≥2 of 3)
- Max 5 approved per run
- Reads recent drop signals from `post_telemetry` to avoid repeat topics
