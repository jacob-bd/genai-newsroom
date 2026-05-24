# Gen AI Spotlight Newsroom Agent Guide

This folder is the Git-backed home for Jacob's Gen AI Spotlight newsroom pipeline.

## Source Of Truth

- Repo path: `/Users/jbd/.alef-agent/workspace/newsroom`
- GitHub remote: `https://github.com/jacob-bd/genai-newsroom.git`
- Tracked newsroom skill: `skills/newsroom/SKILL.md`
- Active newsroom skill path: `/Users/jbd/.alef-agent/workspace/skills/newsroom`
- The active skill path should be a symlink to `../newsroom/skills/newsroom`
- Active callback entrypoint: `/Users/jbd/.alef-agent/workspace/callbacks/nr_.py`
- Tracked callback source: `callbacks/nr_.py`
- The active callback path should be a symlink to `../newsroom/callbacks/nr_.py`
- Runtime database: `data/news_dedup.db`
- Pending review state: `data/newsroom_pending.json`
- Human-readable queue: `data/newsroom_whiteboard.md`
- Generated images: `media/`
- Draft temp files: `tmp/`

Never access Alef Agent's main database directly. For Alef system status, cron, logs, or memory, use the `alef` CLI or MCP tools.

## Current End-To-End Flow

1. `scripts/news_scan_deduped.sh` scans AI news sources, filters duplicates, scores relevance, and writes latest candidates.
2. `scripts/editorial_council.sh` runs agent voting, applies Jacob's editorial taste profile, and selects approved stories.
3. `scripts/enrich_top_articles.py` enriches approved stories with article text and context.
4. `scripts/auto_draft.sh` drafts Telegram copy, creates card metadata, generates or renders image, and posts to the test channel.
5. `scripts/newsroom_post.py` posts test-channel draft, writes `newsroom_pending.json`, updates whiteboard state, and attaches inline review keyboard.
6. Jacob reviews in test channel with Approve, Drop, Fact Check, New Source, Edit, image, headline, and opinion buttons.
7. Alef Agent daemon receives `nr_*` callback data and invokes `callbacks/nr_.py`.
8. `callbacks/nr_.py` syncs current Telegram post state before any action, then edits, approves, drops, fact-checks, or prepares Buffer actions.
9. Approve copies the reviewed test post to live `@genaispot`.
10. Buffer buttons push live post text and selected image to Buffer as queue, publish, or draft.

## Callback Rules

- `callbacks/nr_.py` is single-invocation, not a long-running polling bot.
- Do not resurrect `scripts/newsroom_callback_handler.py`; it is deprecated.
- Every handler must call `get_story()` before using story text or image.
- `get_story()` must refresh from current Telegram post first.
- Refresh behavior:
  - forward current Telegram message
  - delete the temporary forward
  - reconstruct HTML from Telegram entities
  - overwrite draft file with current caption or text
  - download current photo into `image_path`
  - update `newsroom_pending.json`
- Reason: Jacob often edits Telegram caption or replaces image manually. Pending JSON is cache, not truth.

## Image Flow

- For AI background generation, use `scripts/gemcli_image.sh`.
- Do not call `gemcli image` directly for newsroom images.
- Template cards use `skills/news-cards/render.mjs`.
- Current templates:
  - `dark-editorial`
  - `hot-pink-split`
  - `cyan-drenched`
- Default template is `dark-editorial`.
- Classic Pillow overlay uses `scripts/news_image_overlay.py`.
- Gemini must not render text. Text overlay belongs to renderer or Pillow script.

## Telegram Channels

- Test channel: `-1003889167143`
- Live channel: `-1003300061793`
- News update group: `-1003682312998`
- Live handle: `@genaispot`
- Channel URL: `https://t.me/genaispot`

## Safety Rules

- Never post live without Jacob approval by button tap or explicit instruction.
- Never push to Buffer without Jacob button tap or explicit instruction.
- Never treat pending JSON as final source if Telegram post can be fetched.
- Never overwrite Jacob's manual Telegram edits with stale draft text.
- Never frame layoffs as AI-driven unless source explicitly says that.
- Every source link needs checked publish date.
- If exact story claim is not proven by current links, kill the story.

## Common Commands

Run commands with real HOME:

```bash
HOME=/Users/jbd <command>
```

Manual scan:

```bash
HOME=/Users/jbd bash scripts/news_scan_deduped.sh --top 7
```

Autonomous draft from approved JSON:

```bash
HOME=/Users/jbd bash scripts/auto_draft.sh < approved.json
```

Render card:

```bash
HOME=/Users/jbd node skills/news-cards/render.mjs \
  --template dark-editorial \
  --headline "Headline Text" \
  --subline "Short subline" \
  --output /tmp/card.png
```

Post draft to test channel:

```bash
HOME=/Users/jbd python3 scripts/newsroom_post.py \
  --slug story-slug \
  --draft /path/to/draft.txt \
  --image /path/to/image.png
```

Edit existing test post in place:

```bash
HOME=/Users/jbd python3 scripts/telegram_edit.py \
  --channel test \
  --message-id MESSAGE_ID \
  --file /path/to/draft.txt \
  --caption
```

Push live post to Buffer:

```bash
HOME=/Users/jbd python3 scripts/buffer_push.py \
  --telegram-msg LIVE_MSG_ID \
  --image /path/to/image.png \
  --queue
```

## Debug Checklist

1. Confirm repo state: `git status --short --branch`
2. Confirm active callback symlink: `ls -l ../callbacks/nr_.py`
3. Confirm active skill symlink: `ls -l ../skills/newsroom`
4. Syntax-check callback: `python3 -m py_compile callbacks/nr_.py`
5. Check latest cron runs: `HOME=/Users/jbd alef cron runs --json`
6. Check scheduled jobs: `HOME=/Users/jbd alef cron list --json`
7. Check pending staged posts: inspect `data/newsroom_pending.json`
8. Check callback source of truth: current Telegram post should win over pending cache
9. Check Buffer push source: use `--telegram-msg`, not copied text

## Git Hygiene

- Commit source code, docs, templates, and durable editorial config.
- Do not commit database WAL files, pending JSON, generated media, tmp drafts, logs, or debug renders.
- Before push, run syntax checks for changed Python and Node scripts where practical.
- Pushing to GitHub is visible. Get Jacob confirmation before `git push` unless he explicitly says to push now.
