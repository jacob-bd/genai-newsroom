# Gen AI Spotlight - Newsroom

Automated AI news publishing pipeline for [@genaispot](https://t.me/genaispot).

Powered by [Alef Agent](https://github.com/jacob-bd/alef-agent).

## What This Does

Scans 7+ AI news sources every few hours, runs editorial council (3 AI agents vote), drafts posts with DeepSeek, generates news card images, and stages them in a Telegram test channel for human review before publishing.

## Directory Structure

```
newsroom/
├── scripts/          All pipeline scripts
├── skills/
│   ├── newsroom/     Newsroom skill (voice, format, workflow rules)
│   └── news-cards/   HTML-to-PNG card renderer (Puppeteer + templates)
├── data/             Runtime state (gitignored: DB, pending, whiteboard)
├── media/            Generated images (gitignored)
└── tmp/              Draft text files (gitignored)
```

## Pipeline

```
news_scan_deduped.sh
    → editorial_council.sh   (3-agent majority vote)
    → auto_draft.sh          (DeepSeek via OpenCode)
    → newsroom_post.py       (stages to test Telegram channel)
    → [human review via inline keyboard]
    → telegram_post.py       (publishes to @genaispot)
    → buffer_push.py         (queues to Buffer for X/LinkedIn)
```

## Cron Jobs

| ID | Schedule | Job |
|----|----------|-----|
| 1  | 7,10,13,16,19h | AI News Scanner (LLM editor picks) |
| 11 | Tuesdays 1am | Editorial Profile Rebuild |
| 12 | 6,9,12,15,18,21h | Autonomous Newsroom (full pipeline) |

## Key Scripts

| Script | Role |
|--------|------|
| `news_scan_deduped.sh` | Fetch, score, dedup, present candidates |
| `editorial_council.sh` | 3-agent voting council |
| `auto_draft.sh` | Draft + image + post to test channel |
| `newsroom_post.py` | Telegram staging with review keyboard |
| `dedup_db.py` | SQLite wrapper for `news_dedup.db` |
| `nr_.py` | Inline keyboard callback handler (`callbacks/nr_.py`, symlinked from `../callbacks/nr_.py`) |

## Callback Handler

`nr_.py` is tracked at `newsroom/callbacks/nr_.py`. Alef Agent still invokes `~/.alef-agent/workspace/callbacks/nr_.py`, which should be a symlink to the tracked file. All its data paths point into this `newsroom/` directory.

## Setup

```bash
cd skills/news-cards
npm install
```

Requires: `node`, `python3`, `jq`, `alef` CLI, `pwm` (Perplexity), `gsearch`.
