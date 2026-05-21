#!/usr/bin/env python3
"""
newsroom_post.py — Atomic test-channel post with full state management.

Posts a story to the test channel with Approve/Edit/Drop keyboard, then
atomically writes newsroom_pending.json, updates the whiteboard, and
notifies the News Update Group. Backends call this ONE script instead of
managing 5 separate steps.

Usage:
  python3 newsroom_post.py \
    --slug "musk-appeal" \
    --draft /path/to/draft.txt \
    --image /path/to/image.png \
    --clean-bg /path/to/clean.png \
    --headline1 "MUSK APPEALS 134B LOSS" \
    --headline2 "CALENDAR TECHNICALITY" \
    --emoji "🔥" \
    [--source-url "https://..."] \
    [--title "Full story title"] \
    [--dry-run]

Output on success:
  SUCCESS: msg_id=3951 url=https://t.me/c/3889167143/3951
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests

WORKSPACE = Path(os.path.expanduser("~/.alef-agent/workspace"))
PENDING_FILE = WORKSPACE / "memory/newsroom_pending.json"
WHITEBOARD = WORKSPACE / "memory/newsroom_whiteboard.md"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
UPDATE_GROUP = "-1003682312998"
TEST_CHAT = "-1003889167143"

# Full Telegram-allowed reaction set (keep in sync with telegram_post.py)
ALLOWED_REACTIONS = {
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢",
    "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊️", "🤡", "🥱", "🥴", "😍", "🐳",
    "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾",
    "💋", "🖕", "😈", "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇",
    "😨", "🤝", "🤗", "🫡", "🎅", "🎄", "☃️", "💅", "🤪", "🗿", "🆒", "💘",
    "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷", "😡",
}

# Map thematic emojis to nearest allowed equivalent
EMOJI_FALLBACK = {
    "⚖️": "👏",  "🔧": "🤔",  "🛠️": "🤔",  "⚙️": "🤔",
    "🔒": "😱",  "🛡️": "😱",  "🔓": "😱",  "⚠️": "🤯",
    "💰": "🤩",  "💵": "🤩",  "💸": "🤩",  "🏦": "🤔",
    "📊": "👏",  "📈": "🤩",  "📉": "😢",
    "🤖": "🤔",  "🧠": "🤔",  "💡": "🤔",  "🔬": "🤔",
    "🌍": "👍",  "🌎": "👍",  "🌐": "👍",  "📡": "👍",
    "🚀": "🔥",  "⚡": "⚡",  "💥": "🔥",
    "🎯": "💯",  "🏗️": "🤔",  "🖥️": "🤔",  "💻": "🤔",
    "📱": "👍",  "🎮": "🤩",  "🌱": "👏",  "🎓": "👏",
    "💊": "💊",  "🏥": "😢",  "📰": "👍",  "🗂️": "👍",
}


def sanitize_emoji(raw: str) -> str:
    if raw in ALLOWED_REACTIONS:
        return raw
    mapped = EMOJI_FALLBACK.get(raw)
    if mapped and mapped in ALLOWED_REACTIONS:
        print(f"[emoji] '{raw}' not allowed → mapped to '{mapped}'", flush=True)
        return mapped
    print(f"[emoji] '{raw}' not allowed and no mapping → falling back to 🔥", flush=True)
    return "🔥"


def load_pending() -> dict:
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except Exception:
            pass
    return {}


def save_pending(data: dict):
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(data, indent=2))


def update_whiteboard(slug: str, msg_id: str, image_path: str):
    if not WHITEBOARD.exists():
        WHITEBOARD.write_text("")
    lines = WHITEBOARD.read_text().splitlines(keepends=True)
    lines = [l for l in lines if f"| {slug} |" not in l]
    while lines and lines[-1].strip() == "":
        lines.pop()
    lines.append(f"| {slug} | {msg_id} | {image_path} | Test Posted |\n")
    WHITEBOARD.write_text("".join(lines))


def notify_update_group(msg_id: str, slug: str):
    if not TOKEN:
        return
    url = f"https://t.me/c/3889167143/{msg_id}"
    text = f"Draft posted: <a href='{url}'>{slug}</a>\nTap ✅ Approve, ✏️ Edit, or 🗑 Drop."
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": UPDATE_GROUP, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        print(f"[warn] Update group notify failed: {e}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Atomic newsroom test-channel post")
    parser.add_argument("--slug", required=True, help="Story slug (e.g. musk-appeal)")
    parser.add_argument("--draft", required=True, help="Path to draft .txt file")
    parser.add_argument("--image", required=True, help="Path to overlaid image (.png)")
    parser.add_argument("--clean-bg", required=True, dest="clean_bg",
                        help="Path to clean background image (.png)")
    parser.add_argument("--headline1", required=True, help="First headline line for image regen")
    parser.add_argument("--headline2", required=True, help="Second headline line for image regen")
    parser.add_argument("--emoji", default="🔥",
                        help="Reaction emoji (auto-sanitized to Telegram allowed set)")
    parser.add_argument("--source-url", default="", dest="source_url")
    parser.add_argument("--title", default="",
                        help="Story title (defaults to first non-empty line of draft)")
    parser.add_argument("--template-category", default="", dest="template_category",
                        help="Category tag for news-card templates (e.g. 'AI / Research')")
    parser.add_argument("--template-headline", default="", dest="template_headline",
                        help="Full headline for news-card templates")
    parser.add_argument("--template-subline", default="", dest="template_subline",
                        help="Subline/tagline for news-card templates")
    parser.add_argument("--template-highlight", default="", dest="template_highlight",
                        help="Word/phrase to highlight in hot-pink on news-card headline")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate all inputs without posting")
    args = parser.parse_args()

    # Validate paths
    for label, path in [("draft", args.draft), ("image", args.image), ("clean-bg", args.clean_bg)]:
        if not Path(path).exists():
            print(f"ERROR: {label} file not found: {path}")
            sys.exit(1)

    emoji = sanitize_emoji(args.emoji)

    # Resolve title from draft if not provided
    title = args.title
    if not title:
        raw = Path(args.draft).read_text()
        for line in raw.splitlines():
            stripped = re.sub(r"<[^>]+>", "", line).strip()
            if stripped:
                title = stripped
                break

    if args.dry_run:
        print("DRY-RUN: All inputs valid.")
        print(f"  slug        = {args.slug}")
        print(f"  draft       = {args.draft}")
        print(f"  image       = {args.image}")
        print(f"  clean_bg    = {args.clean_bg}")
        print(f"  headline1   = {args.headline1}")
        print(f"  headline2   = {args.headline2}")
        print(f"  emoji       = {emoji}  (requested: {args.emoji})")
        print(f"  title       = {title}")
        print(f"  source_url  = {args.source_url}")
        print("DRY-RUN: Would post to test channel with --draft-mode keyboard.")
        print("SUCCESS: dry-run passed")
        sys.exit(0)

    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)

    # Post to test channel
    cmd = [
        "python3", str(WORKSPACE / "scripts/telegram_post.py"),
        "--channel", "test",
        "--image", args.image,
        "--file", args.draft,
        "--react", emoji,
        "--draft-mode",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        env={**os.environ, "HOME": os.path.expanduser("~")},
    )

    if "SUCCESS" not in result.stdout:
        print(f"ERROR: Telegram post failed:\n{result.stdout[:500]}")
        if result.stderr:
            print(f"stderr: {result.stderr[:200]}")
        sys.exit(1)

    # Parse message_id
    msg_id = None
    for line in result.stdout.splitlines():
        if line.startswith("MESSAGE_ID:"):
            msg_id = line.split(":", 1)[1].strip()
            break

    if not msg_id:
        print(f"ERROR: Could not parse MESSAGE_ID from output:\n{result.stdout[:400]}")
        sys.exit(1)

    # Write pending.json (merge — never overwrite other stories)
    pending = load_pending()
    pending[str(msg_id)] = {
        "message_id": int(msg_id),
        "slug": args.slug,
        "emoji": emoji,
        "title": title,
        "headline_line1": args.headline1,
        "headline_line2": args.headline2,
        "draft_path": str(Path(args.draft).resolve()),
        "image_path": str(Path(args.image).resolve()),
        "clean_bg_path": str(Path(args.clean_bg).resolve()),
        "source_url": args.source_url,
        "template_category": args.template_category,
        "template_headline": args.template_headline,
        "template_subline": args.template_subline,
        "template_highlight": args.template_highlight,
        "chat_id": TEST_CHAT,
        "created_at": datetime.now().isoformat(),
    }
    save_pending(pending)

    # Update whiteboard
    update_whiteboard(args.slug, msg_id, args.image)

    # Notify update group
    notify_update_group(msg_id, args.slug)

    print(f"SUCCESS: msg_id={msg_id} url=https://t.me/c/3889167143/{msg_id}")
    sys.exit(0)


if __name__ == "__main__":
    main()
