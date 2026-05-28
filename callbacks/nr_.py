#!/usr/bin/env python3
"""
nr_.py — Newsroom draft review callback handler (single-invocation).

Called by the Alef Agent daemon when a callback_data starting with "nr_" is received.
Handles: nr_approve, nr_drop, nr_edit, nr_back,
         nr_edit_image, nr_rehighlight, nr_img_classic, nr_img_t1, nr_img_t2, nr_img_t3,
         nr_edit_shorter, nr_edit_punchier, nr_edit_rewrite, nr_edit_snarky,
         nr_edit_opinion, nr_op_matters, nr_op_play, nr_op_real,
         nr_op_signal, nr_op_room, nr_op_tell,
         nr_buf_queue, nr_buf_publish, nr_buf_draft, nr_buf_skip,
         nr_factcheck, nr_newsource,
         nr_drop_stale, nr_drop_beliefs, nr_drop_boring, nr_drop_unverifiable,
         nr_drop_duplicate, nr_drop_fatigue, nr_drop_niche, nr_drop_clickbait.

Approval flow:
  nr_approve  → publishes live to Telegram, then shows Buffer keyboard
  nr_buf_*    → pushes to Buffer (queue/publish/draft) using the live post as source
  Video posts → only nr_buf_draft is offered (user uploads video manually)

Environment variables (set by daemon):
  CALLBACK_DATA       — full callback_data string
  CALLBACK_CHAT_ID    — chat where button was tapped
  CALLBACK_MESSAGE_ID — message the button is attached to
  TELEGRAM_BOT_TOKEN  — bot token for API calls
  OPENROUTER_API_KEY  — for LLM rewrites

State: ~/.alef-agent/workspace/newsroom/data/newsroom_pending.json
"""

import json
import html
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DATA = os.environ.get("CALLBACK_DATA", "")
CHAT_ID = os.environ.get("CALLBACK_CHAT_ID", "")
MESSAGE_ID = os.environ.get("CALLBACK_MESSAGE_ID", "")

BASE = f"https://api.telegram.org/bot{TOKEN}/"
WORKSPACE = Path(os.path.expanduser("~/.alef-agent/workspace"))
NEWSROOM = WORKSPACE / "newsroom"
PENDING_FILE = NEWSROOM / "data/newsroom_pending.json"
WHITEBOARD = NEWSROOM / "data/newsroom_whiteboard.md"

TEST_CHAT = "-1003889167143"
UPDATE_GROUP = "-1003682312998"   # newsroom channel — council summary only
NOTIF_GROUP = "-1003853245974"    # notifications channel — all other workflow events   # newsroom channel — council summary only
NOTIF_GROUP = "-1003853245974"    # notifications channel — everything else
DEDUP_DB = NEWSROOM / "data/news_dedup.db"


# ── Telemetry ────────────────────────────────────────────────────────────────

_TELEMETRY_DDL = """
    CREATE TABLE IF NOT EXISTS post_telemetry (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         TEXT NOT NULL,
        msg_id     TEXT,
        slug       TEXT,
        title      TEXT,
        source_url TEXT,
        category   TEXT,
        action     TEXT NOT NULL,
        detail     TEXT
    )
"""


def log_telemetry(action, detail=None, story=None):
    try:
        s = story or {}
        with sqlite3.connect(str(DEDUP_DB)) as conn:
            conn.execute(_TELEMETRY_DDL)
            conn.execute(
                "INSERT INTO post_telemetry (ts, msg_id, slug, title, source_url, category, action, detail) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    MESSAGE_ID,
                    s.get("slug", ""),
                    s.get("title", ""),
                    s.get("source_url", ""),
                    s.get("template_category", ""),
                    action,
                    detail or "",
                )
            )
    except Exception as e:
        print(f"[TELEMETRY:err] {e}", flush=True)

MAIN_KEYBOARD = [
    [{"text": "✅ Approve", "callback_data": "nr_approve"},
     {"text": "\U0001f5d1 Drop", "callback_data": "nr_drop"}],
    [{"text": "\U0001f50d Fact Check", "callback_data": "nr_factcheck"},
     {"text": "\U0001f50e New Source", "callback_data": "nr_newsource"}],
    [{"text": "✏️ Edit", "callback_data": "nr_edit"}],
]
DROP_REASON_LABELS = {
    "stale":        ("📅", "Stale"),
    "beliefs":      ("🚫", "Against My Beliefs"),
    "boring":       ("😴", "Not Interesting"),
    "unverifiable": ("🤔", "Unverifiable"),
    "duplicate":    ("🔁", "Duplicate"),
    "fatigue":      ("📉", "Topic Fatigue"),
    "niche":        ("🎯", "Too Niche"),
    "clickbait":    ("🗑️", "Clickbait"),
}
_reason_items = [
    {"text": f"{e} {l}", "callback_data": f"nr_drop_{k}"}
    for k, (e, l) in DROP_REASON_LABELS.items()
]
DROP_REASON_KEYBOARD = (
    [[_reason_items[i], _reason_items[i + 1]] for i in range(0, len(_reason_items), 2)]
    + [[{"text": "← Cancel", "callback_data": "nr_back"}]]
)
EDIT_KEYBOARD = [
    [{"text": "\U0001f5bc New image", "callback_data": "nr_edit_image"},
     {"text": "\U0001f3a8 Re-highlight", "callback_data": "nr_rehighlight"}],
    [{"text": "✂️ Shorter", "callback_data": "nr_edit_shorter"},
     {"text": "\U0001f525 Punchier", "callback_data": "nr_edit_punchier"}],
    [{"text": "\U0001f4f0 Rewrite", "callback_data": "nr_edit_rewrite"},
     {"text": "\U0001f608 Snarky", "callback_data": "nr_edit_snarky"}],
    [{"text": "\U0001f4ac Add Opinion", "callback_data": "nr_edit_opinion"},
     {"text": "\U0001f504 Rethink Headline", "callback_data": "nr_rethink_headline"}],
    [{"text": "✏️ Custom Headline", "callback_data": "nr_custom_headline"}],
    [{"text": "« Back", "callback_data": "nr_back"}],
]
IMAGE_KEYBOARD = [
    [{"text": "\U0001f5a4 Dark Editorial", "callback_data": "nr_img_t1"}],
    [{"text": "\U0001f3a8 Classic (Pillow)", "callback_data": "nr_img_classic"}],
    [{"text": "« Back", "callback_data": "nr_edit"}],
]
OPINION_KEYBOARD = [
    [{"text": "\U0001f4ac Why this matters", "callback_data": "nr_op_matters"},
     {"text": "\U0001f3af The play", "callback_data": "nr_op_play"}],
    [{"text": "\U0001f50d The real move", "callback_data": "nr_op_real"},
     {"text": "\U0001f4fb The signal", "callback_data": "nr_op_signal"}],
    [{"text": "\U0001f9e0 Read the room", "callback_data": "nr_op_room"},
     {"text": "⚡ The tell", "callback_data": "nr_op_tell"}],
    [{"text": "« Back", "callback_data": "nr_edit"}],
]
BUFFER_KEYBOARD = [
    [{"text": "\U0001f4c5 Queue", "callback_data": "nr_buf_queue"}],
    [{"text": "⚡ Publish Now", "callback_data": "nr_buf_publish"},
     {"text": "\U0001f4dd Draft", "callback_data": "nr_buf_draft"}],
    [{"text": "⏭ Skip Buffer", "callback_data": "nr_buf_skip"}],
]
BUFFER_VIDEO_KEYBOARD = [
    [{"text": "\U0001f4dd Buffer Draft (no video)", "callback_data": "nr_buf_draft"}],
    [{"text": "⏭ Skip Buffer", "callback_data": "nr_buf_skip"}],
]
BUFFER_PROCESSING_KEYBOARD = [
    [{"text": "⏳ Pushing to Buffer...", "callback_data": "nr_noop"}],
]

OPINION_LABEL_MAP = {
    "matters": ("\U0001f4ac", "Why this matters"),
    "play":    ("\U0001f3af", "The play"),
    "real":    ("\U0001f50d", "The real move"),
    "signal":  ("\U0001f4fb", "The signal"),
    "room":    ("\U0001f9e0", "Read the room"),
    "tell":    ("⚡",    "The tell"),
}

TEMPLATE_MAP = {
    "t1": "dark-editorial",
}

JACOB_EDITORIAL_LENS = """
Jacob's editorial positions (use to inform angle, never quote verbatim):
- Skeptical of corporate announcements without production proof
- EU regulatory actions are performative, not impactful; Europe writes numbers while others build
- Chinese open source (DeepSeek etc.) is a real competitive threat — Western labs charge premium, China gives it away
- Big tech acquisitions eliminate threats, not integrate products; real victims are the customers
- Fear-mongering about AI is lazy; real risks deserve clear-eyed analysis, not hysteria
- The societal impact of AI is massively underappreciated — nobody is reckoning with it
- Pro-builder, pro-democratization; dislikes gatekeeping of vibe coders and no-code builders
- Military and defense AI use is legitimate
- AI + quantum is the real coming inflection point
"""

TEXT_PROMPTS = {
    "shorter":  "Trim this Telegram news post to about 70% of its current length. Keep all key facts. Preserve all HTML tags exactly. Return only the rewritten text.",
    "punchier": "Rewrite this post to be more punchy and impactful. Keep same facts and structure. Preserve all HTML tags. CRITICAL: output must be under 950 characters total. Return only the rewritten text.",
    "rewrite":  "Fully rewrite this post with a fresh angle. Keep same facts. Preserve all HTML tags. CRITICAL: output must be under 950 characters total. Return only the rewritten text.",
    "snarky":   f"""Rewrite this Telegram news post in Jacob's voice: sharp, blunt, zero corporate polish.

Rules:
- Call out the obvious that others won't say. Expose PR spin for what it is.
- No hedging, no "it remains to be seen", no "experts say". Take a position.
- Dry wit is good. Sarcasm at the right moment is good. Fake optimism is banned.
- Keep ALL facts, ALL HTML tags, and ALL structure (heading, body, Read more, hashtags).
- No em-dashes. No banned AI filler words (delve, tapestry, leverage, etc.).
- The headline stays unchanged. Only the body sentences get snarky.
- CRITICAL: output must be under 950 characters total. Cut words if needed, do NOT exceed this.
- Return ONLY the rewritten post — no commentary, no explanation.

{JACOB_EDITORIAL_LENS}""",
}


def tg(method, payload, timeout=30):
    try:
        r = requests.post(BASE + method, json=payload, timeout=timeout)
        return r.json()
    except Exception as e:
        print(f"[TG ERROR] {method}: {e}", flush=True)
        return {}


def set_keyboard(keyboard=None):
    markup = {"inline_keyboard": keyboard} if keyboard else {"inline_keyboard": []}
    tg("editMessageReplyMarkup", {
        "chat_id": CHAT_ID,
        "message_id": int(MESSAGE_ID),
        "reply_markup": json.dumps(markup),
    })


def send_msg(chat_id, text):
    tg("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})


def load_pending():
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except Exception:
            pass
    return {}


def save_pending(data):
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(data, indent=2))


def _extract_title(text):
    title_match = re.search(r"<b>(.+?)</b>", text)
    return title_match.group(1) if title_match else text[:80]


def _slugify(text):
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:50]
    return slug or f"story-{MESSAGE_ID}"


def _utf16_to_py_index(text, target):
    units = 0
    for idx, char in enumerate(text):
        if units == target:
            return idx
        units += len(char.encode("utf-16-le")) // 2
        if units == target:
            return idx + 1
    return len(text)


def _entity_tags(entity):
    kind = entity.get("type")
    if kind == "bold":
        return "<b>", "</b>"
    if kind == "italic":
        return "<i>", "</i>"
    if kind == "underline":
        return "<u>", "</u>"
    if kind == "strikethrough":
        return "<s>", "</s>"
    if kind == "spoiler":
        return "<tg-spoiler>", "</tg-spoiler>"
    if kind == "code":
        return "<code>", "</code>"
    if kind == "pre":
        return "<pre>", "</pre>"
    if kind == "blockquote":
        return "<blockquote>", "</blockquote>"
    if kind == "text_link" and entity.get("url"):
        url = html.escape(entity["url"], quote=True)
        return f'<a href="{url}">', "</a>"
    return None, None


def _message_html(msg):
    text = msg.get("caption", msg.get("text", ""))
    entities = msg.get("caption_entities") or msg.get("entities") or []
    if not text:
        return ""
    if not entities:
        return html.escape(text)

    starts = {}
    ends = {}
    for entity in entities:
        open_tag, close_tag = _entity_tags(entity)
        if not open_tag:
            continue
        start = _utf16_to_py_index(text, entity["offset"])
        end = _utf16_to_py_index(text, entity["offset"] + entity["length"])
        starts.setdefault(start, []).append((end, open_tag))
        ends.setdefault(end, []).append((start, close_tag))

    parts = []
    for idx in range(len(text) + 1):
        for start, close_tag in sorted(ends.get(idx, []), reverse=True):
            parts.append(close_tag)
        for end, open_tag in sorted(starts.get(idx, []), reverse=True):
            parts.append(open_tag)
        if idx < len(text):
            parts.append(html.escape(text[idx]))
    return "".join(parts)


def _fetch_current_message():
    """Fetch current Telegram message state using a forward-then-delete round trip."""
    fwd = tg("forwardMessage", {
        "chat_id": CHAT_ID,
        "from_chat_id": CHAT_ID,
        "message_id": int(MESSAGE_ID),
    })
    if not fwd.get("ok"):
        return None
    msg = fwd["result"]
    tg("deleteMessage", {"chat_id": CHAT_ID, "message_id": msg["message_id"]})
    return msg


def _download_current_photo(msg, story, slug):
    photo = msg.get("photo")
    if not photo:
        return ""

    image_path = story.get("image_path", "")
    if not image_path:
        today = datetime.now().strftime("%Y-%m-%d")
        image_path = str(NEWSROOM / f"media/{today}_{slug}.png")

    file_info = tg("getFile", {"file_id": photo[-1]["file_id"]})
    if not file_info.get("ok"):
        return ""

    fp = file_info["result"]["file_path"]
    Path(image_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        urllib.request.urlretrieve(f"https://api.telegram.org/file/bot{TOKEN}/{fp}", image_path)
    except Exception as e:
        print(f"[SYNC] Image download failed: {type(e).__name__}", flush=True)
        return ""
    return image_path


def _sync_story_from_telegram(story=None):
    """Refresh pending entry from current Telegram post before any edit action."""
    msg = _fetch_current_message()
    if not msg:
        return story

    story = dict(story or {})
    caption = _message_html(msg)
    title = _extract_title(caption)
    slug = story.get("slug") or _slugify(title)

    draft_path = story.get("draft_path") or str(NEWSROOM / f"tmp/{slug}_draft.txt")
    Path(draft_path).parent.mkdir(parents=True, exist_ok=True)
    Path(draft_path).write_text(caption)

    image_path = _download_current_photo(msg, story, slug)

    story.update({
        "slug": slug,
        "title": title,
        "draft_path": draft_path,
        "image_path": image_path,
        "chat_id": CHAT_ID,
        "created_at": story.get("created_at") or datetime.now().isoformat(),
        "source": story.get("source") or "manual",
        "synced_at": datetime.now().isoformat(),
    })
    pending = load_pending()
    pending[MESSAGE_ID] = story
    save_pending(pending)
    print(f"[SYNC] Refreshed pending for msg {MESSAGE_ID}: {slug}", flush=True)
    return story


def get_story():
    """Get story from pending, refreshing from current Telegram post first."""
    pending = load_pending()
    return _sync_story_from_telegram(pending.get(MESSAGE_ID))


def save_story(story):
    """Save story back to pending store under current MESSAGE_ID."""
    pending = load_pending()
    pending[MESSAGE_ID] = story
    save_pending(pending)


def _pop_pending(msg_id):
    pending = load_pending()
    pending.pop(msg_id, None)
    save_pending(pending)


def call_llm(prompt, text):
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None, "OPENROUTER_API_KEY not set"
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "anthropic/claude-sonnet-4-5",
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": f"{prompt}\n\n{text}"}],
            },
            timeout=60,
        )
        result = resp.json()["choices"][0]["message"]["content"].strip()
        return result, None
    except Exception as e:
        return None, str(e)[:150]


def handle_approve():
    story = get_story()
    if not story:
        send_msg(CHAT_ID, "⚠️ Story data not found. Post live manually.")
        return

    set_keyboard()

    slug = story.get("slug", "story")
    log_path = str(NEWSROOM / f"tmp/{slug}_live_msg_id.txt")
    emoji = story.get("emoji", "\U0001f525")
    title = story.get("title", "")

    source_url = story.get("url") or story.get("source_url") or ""

    cmd = [
        "python3", str(NEWSROOM / "scripts/telegram_post.py"),
        "--channel", "live",
        "--copy-from-chat", TEST_CHAT,
        "--copy-msg-id", MESSAGE_ID,
        "--log", log_path,
        "--react", emoji,
    ]
    if title:
        cmd += ["--title", title]
    if source_url:
        cmd += ["--source-url", source_url]

    result = subprocess.run(cmd, capture_output=True, text=True,
                            env={**os.environ, "HOME": os.path.expanduser("~")})

    if "SUCCESS" not in result.stdout:
        send_msg(CHAT_ID, f"❌ Live post failed:\n<code>{result.stdout[:300]}</code>")
        set_keyboard(MAIN_KEYBOARD)
        return

    live_msg_id = None
    for line in result.stdout.splitlines():
        if line.startswith("MESSAGE_ID:"):
            live_msg_id = line.split(":", 1)[1].strip()

    story["live_msg_id"] = live_msg_id
    save_story(story)

    log_telemetry("approve", detail=live_msg_id, story=story)
    link = f"https://t.me/genaispot/{live_msg_id}" if live_msg_id else "(check channel)"
    send_msg(NOTIF_GROUP, f"✅ Posted live: {link}")

    has_video = bool(story.get("video_path"))
    keyboard = BUFFER_VIDEO_KEYBOARD if has_video else BUFFER_KEYBOARD
    set_keyboard(keyboard)

    print(f"[APPROVE] msg {MESSAGE_ID} -> live {live_msg_id}", flush=True)


def _remove_whiteboard_row(slug):
    if not WHITEBOARD.exists():
        return
    lines = WHITEBOARD.read_text().splitlines(keepends=True)
    filtered = [l for l in lines if f"| {slug} |" not in l]
    while filtered and filtered[-1].strip() == "":
        filtered.pop()
    WHITEBOARD.write_text("".join(filtered) + ("\n" if filtered else ""))


def handle_buffer(mode):
    story = get_story()
    if not story:
        set_keyboard()
        return

    set_keyboard(BUFFER_PROCESSING_KEYBOARD)

    live_msg_id = story.get("live_msg_id")
    image_path = story.get("image_path", "")
    has_video = bool(story.get("video_path"))
    slug = story.get("slug", "")

    include_image = image_path and Path(image_path).exists() and not (has_video and mode == "draft")

    flag_map = {"queue": "--queue", "publish": "--publish", "draft": "--draft"}
    cmd = [
        "python3", str(NEWSROOM / "scripts/buffer_push.py"),
        "--telegram-msg", str(live_msg_id or MESSAGE_ID),
    ]
    if include_image:
        cmd += ["--image", image_path]
    else:
        cmd += ["--allow-no-image"]
    cmd.append(flag_map[mode])

    result = subprocess.run(cmd, capture_output=True, text=True,
                            env={**os.environ, "HOME": os.path.expanduser("~")})

    if result.returncode != 0:
        err_snippet = (result.stdout + result.stderr)[:300].strip()
        send_msg(CHAT_ID, f"❌ Buffer push failed:\n<code>{err_snippet}</code>")
        set_keyboard(BUFFER_VIDEO_KEYBOARD if has_video else BUFFER_KEYBOARD)
        return

    log_telemetry(f"buffer:{mode}", story=story)
    set_keyboard()
    _pop_pending(MESSAGE_ID)

    if slug:
        _remove_whiteboard_row(slug)

    label = {"queue": "queued", "publish": "published", "draft": "saved as draft"}[mode]
    suffix = " (no video — upload manually)" if has_video else ""
    send_msg(NOTIF_GROUP, f"✅ Buffer {label}{suffix}")
    print(f"[BUFFER:{mode}] msg {MESSAGE_ID} live={live_msg_id} slug={slug}", flush=True)


def handle_buffer_skip():
    story = get_story()
    if not story:
        send_msg(CHAT_ID, "⚠️ Story data not found.")
        set_keyboard()
        return
    slug = story.get("slug", "")
    log_telemetry("buffer:skip", story=story)
    set_keyboard()
    _pop_pending(MESSAGE_ID)
    if slug:
        _remove_whiteboard_row(slug)
    send_msg(NOTIF_GROUP, "⏭ Buffer skipped — story done.")
    print(f"[BUFFER:skip] msg {MESSAGE_ID} slug={slug}", flush=True)


def handle_drop():
    """Show drop reason sub-menu."""
    set_keyboard(DROP_REASON_KEYBOARD)


def handle_drop_reason(reason):
    """Execute drop with reason, log telemetry, clean up."""
    story = get_story() or {}
    slug = story.get("slug", "")
    log_telemetry("drop", reason, story)
    set_keyboard()
    _pop_pending(MESSAGE_ID)
    if slug:
        _remove_whiteboard_row(slug)
    emoji, text = DROP_REASON_LABELS.get(reason, ("", reason))
    label = f"{emoji} {text}".strip()
    send_msg(NOTIF_GROUP, f"🗑 Dropped ({label}): {story.get('title', 'story')}")
    print(f"[DROP:{reason}] msg {MESSAGE_ID} slug={slug}", flush=True)


def handle_edit():
    set_keyboard(EDIT_KEYBOARD)


def handle_back():
    set_keyboard(MAIN_KEYBOARD)


def handle_edit_image():
    """Show image sub-menu (Classic vs templates)."""
    set_keyboard(IMAGE_KEYBOARD)


def _ai_pick_highlight(headline):
    """Use LLM to pick 1-3 SHORT key terms to highlight in hot-pink."""
    prompt = (
        "Pick 1-3 SHORT terms to highlight on this news headline. "
        "HARD RULES: each term max 2 words, max 20 characters, must be an exact substring of the headline. "
        "PREFER: company names, dollar amounts, model names, percentages, numbers. "
        "AVOID: phrases longer than 2 words, generic words (new, AI, says, launches, now, the, is). "
        "Good examples: 'OpenAI' or '$40B,Gemini' or 'GPT-5,Microsoft' or '1,000,Tokens'. "
        "Bad examples: 'OpenAI announces' or 'Major Partnership Deal' (too long). "
        "Return ONLY the comma-separated terms — no quotes, no explanation."
    )
    result, err = call_llm(prompt, headline)
    if err or not result:
        return ""
    raw = result.strip().strip('"\'')
    if not raw:
        return ""
    hl_upper = headline.upper()
    valid = []
    for term in raw.split(","):
        term = term.strip().strip('"\'')
        # Reject: not a substring, more than 2 words, or longer than 20 chars
        if (term and term.upper() in hl_upper
                and len(term.split()) <= 2
                and len(term) <= 20):
            idx = hl_upper.find(term.upper())
            valid.append(headline[idx:idx + len(term)])
    return ",".join(valid) if valid else ""


CATEGORY_TAGS = [
    "AI / Research", "AI / Business", "AI / Products", "AI / Policy", "AI / Safety",
    "AI / Hardware", "AI / Agents", "AI / Developer", "AI / Design", "AI / Military",
    "AI / Healthcare", "AI / Education", "AI / Finance", "AI / Infrastructure",
    "Open Source", "Big Tech", "Startups", "Breaking",
]

def _derive_category(story):
    """Ask LLM to pick the best category tag from the standard list."""
    draft_path = story.get("draft_path", "")
    title = story.get("title", "")
    try:
        raw = Path(draft_path).read_text() if draft_path and Path(draft_path).exists() else ""
        plain = re.sub(r'<[^>]+>', '', raw).strip()[:600]
    except Exception:
        plain = ""
    tags = ", ".join(CATEGORY_TAGS)
    prompt = (
        f"Pick the single best category tag for this news story from this list:\n{tags}\n\n"
        "Rules: return ONLY the exact tag text, nothing else. No explanation."
    )
    context = f"Title: {title}\n\n{plain}"
    result, err = call_llm(prompt, context)
    if err or not result:
        return "AI / Business"
    candidate = result.strip().strip('"\'')
    return candidate if candidate in CATEGORY_TAGS else "AI / Business"


def _derive_subline(draft_path):
    """Use LLM to generate a punchy subline for template cards from draft text."""
    try:
        raw = Path(draft_path).read_text()
        plain = re.sub(r'<[^>]+>', '', raw).strip()
    except Exception:
        return ""
    if not plain:
        return ""
    prompt = (
        "Write a punchy subline for a news card image. "
        "Max 55 characters. Must add NEW information not already in the headline. "
        "No em-dashes. No quotes around it. No full sentences — fragments preferred. "
        "Use a specific fact, number, or consequence from the body. "
        "Return ONLY the subline text, nothing else."
    )
    result, err = call_llm(prompt, plain)
    if err or not result:
        return ""
    return result.strip().strip('"\'')[:60]


def _derive_edit_path(draft_path):
    edit_path = re.sub(r"_draft\.txt$", "_edit.txt", draft_path)
    if edit_path == draft_path:
        edit_path = draft_path.replace(".txt", "_edit.txt")
    return edit_path


def _edit_image(image_path, draft_path=None):
    """Replace post image, preserving caption from draft file."""
    cmd = [
        "python3", str(NEWSROOM / "scripts/telegram_edit.py"),
        "--channel", "test",
        "--message-id", MESSAGE_ID,
        "--image", image_path,
    ]
    if draft_path and Path(draft_path).exists():
        cmd += ["--file", draft_path, "--caption"]
    subprocess.run(cmd, capture_output=True, text=True,
                   env={**os.environ, "HOME": os.path.expanduser("~")})


def handle_image_classic():
    """Regenerate image using Pillow overlay from clean background (original behavior)."""
    story = get_story()
    if not story:
        send_msg(CHAT_ID, "⚠️ Story data not found.")
        set_keyboard(MAIN_KEYBOARD)
        return

    clean_bg = story.get("clean_bg_path", "")
    image_path = story.get("image_path", "")
    line1 = story.get("headline_line1", "")
    line2 = story.get("headline_line2", "")
    draft_path = story.get("draft_path", "")

    if not clean_bg or not Path(clean_bg).exists():
        send_msg(CHAT_ID, "⚠️ Clean background not found.")
        set_keyboard(MAIN_KEYBOARD)
        return

    result = subprocess.run([
        "python3", str(NEWSROOM / "scripts/news_image_overlay.py"),
        clean_bg, image_path, line1, line2,
    ], capture_output=True, text=True, env={**os.environ, "HOME": os.path.expanduser("~")})

    if result.returncode != 0:
        send_msg(CHAT_ID, "❌ Image regen failed.")
        set_keyboard(MAIN_KEYBOARD)
        return

    log_telemetry("image:classic", story=story)
    _edit_image(image_path, draft_path)
    set_keyboard(MAIN_KEYBOARD)
    print(f"[IMG:classic] Regenerated for msg {MESSAGE_ID}", flush=True)


def handle_image_template(template_key):
    """Render a news-card HTML template and replace the post image."""
    story = get_story()
    if not story:
        send_msg(CHAT_ID, "⚠️ Story data not found.")
        set_keyboard(MAIN_KEYBOARD)
        return

    template_name = TEMPLATE_MAP.get(template_key, "dark-editorial")
    image_path = story.get("image_path", "")
    draft_path = story.get("draft_path", "")
    category = story.get("template_category") or _derive_category(story)
    headline = story.get("template_headline") or story.get("title", "")

    subline = story.get("template_subline", "")
    if not subline:
        subline = _derive_subline(draft_path)
        if subline:
            story["template_subline"] = subline
            save_story(story)

    _raw_hl = story.get("template_highlight", "")
    if _raw_hl and _raw_hl.upper() in headline.upper():
        highlight = _raw_hl
    else:
        highlight = _ai_pick_highlight(headline)
        if highlight:
            story["template_highlight"] = highlight
            save_story(story)

    if not image_path:
        send_msg(CHAT_ID, "⚠️ Image path not found.")
        set_keyboard(MAIN_KEYBOARD)
        return

    render_script = NEWSROOM / "skills/news-cards/render.mjs"
    if not render_script.exists():
        send_msg(CHAT_ID, "❌ render.mjs not found.")
        set_keyboard(MAIN_KEYBOARD)
        return

    cmd = [
        "node", str(render_script),
        "--template", template_name,
        "--category", category,
        "--headline", headline,
        "--subline", subline,
        "--output", image_path,
    ]
    if highlight:
        cmd += ["--highlight", highlight]

    result = subprocess.run(
        cmd, capture_output=True, text=True,
        env={**os.environ, "HOME": os.path.expanduser("~")},
    )

    if result.returncode != 0:
        send_msg(CHAT_ID, f"❌ Template render failed:\n<code>{(result.stderr or result.stdout)[:200]}</code>")
        set_keyboard(MAIN_KEYBOARD)
        return

    story["template_category"] = category
    story["current_template"] = template_name
    save_story(story)
    log_telemetry(f"image:template:{template_name}", story=story)
    _edit_image(image_path, draft_path)
    set_keyboard(MAIN_KEYBOARD)
    print(f"[IMG:template:{template_name}] msg {MESSAGE_ID}", flush=True)


def _generate_headline_from_draft(draft_path, title):
    """Ask LLM to generate fresh headline_line1 + headline_line2 from draft text."""
    try:
        raw = Path(draft_path).read_text()
        plain = re.sub(r'<[^>]+>', '', raw).strip()
    except Exception:
        plain = title
    prompt = (
        "Generate a two-line image headline for this news card. "
        "Rules: 4-6 words per line, 8-10 words total. "
        "State the actual news fact clearly and specifically — no teasers, no questions. "
        "Include the company name and the key number/stat if there is one. "
        "No em dashes. No word may be split across lines. "
        "Return ONLY valid JSON with keys headline_line1 and headline_line2. No markdown fences."
    )
    result, err = call_llm(prompt, f"Title: {title}\n\n{plain[:800]}")
    if err or not result:
        return None, None
    try:
        # Strip markdown fences if present (Claude often wraps JSON in ```json ... ```)
        clean = result.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```[a-z]*\n?", "", clean)
            clean = re.sub(r"\n?```$", "", clean.strip())
        data = json.loads(clean.strip())
        h1 = data.get("headline_line1", "").strip()
        h2 = data.get("headline_line2", "").strip()
        if h1 and h2:
            return h1, h2
    except Exception:
        pass
    return None, None


def _render_current_template(story, h1, h2, image_path):
    """Re-render image with given headlines using story's current template."""
    template_name = story.get("current_template", "dark-editorial")
    category = story.get("template_category") or _derive_category(story)
    subline = story.get("template_subline", "")
    if not subline:
        subline = _derive_subline(story.get("draft_path", ""))
        if subline:
            story["template_subline"] = subline
    _raw_hl = story.get("template_highlight", "")
    headline = f"{h1} {h2}"
    if _raw_hl and _raw_hl.upper() in headline.upper():
        highlight = _raw_hl
    else:
        highlight = _ai_pick_highlight(headline)
    render_script = NEWSROOM / "skills/news-cards/render.mjs"
    cmd = [
        "node", str(render_script),
        "--template", template_name,
        "--category", category,
        "--headline", f"{h1} {h2}",
        "--subline", subline,
        "--output", image_path,
    ]
    if highlight:
        cmd += ["--highlight", highlight]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        env={**os.environ, "HOME": os.path.expanduser("~")},
    )
    if result.returncode == 0:
        story["template_category"] = category
        story["current_template"] = template_name
        save_story(story)
    return result.returncode == 0, highlight


def handle_rethink_headline():
    story = get_story()
    if not story:
        send_msg(CHAT_ID, "⚠️ Story data not found.")
        set_keyboard(MAIN_KEYBOARD)
        return

    draft_path = story.get("draft_path", "")
    title = story.get("title", "")
    image_path = story.get("image_path", "")

    progress = tg("sendMessage", {"chat_id": CHAT_ID, "text": "\U0001f504 Rethinking headline..."})
    progress_msg_id = progress.get("result", {}).get("message_id")
    h1, h2 = _generate_headline_from_draft(draft_path, title)
    if not h1 or not h2:
        if progress_msg_id:
            tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id, "text": "❌ Headline generation failed"})
        else:
            send_msg(CHAT_ID, "❌ Headline generation failed.")
        set_keyboard(MAIN_KEYBOARD)
        return

    # Validate: h2 must not start lowercase (split-word guard)
    if h2 and h2[0].islower():
        full = f"{h1} {h2}"
        mid = len(full) // 2
        idx = full.rfind(' ', 0, mid + 1)
        if idx >= 0:
            h1, h2 = full[:idx], full[idx + 1:]

    ok, highlight = _render_current_template(story, h1, h2, image_path)
    if not ok:
        send_msg(CHAT_ID, "❌ Render failed.")
        set_keyboard(MAIN_KEYBOARD)
        return

    story["headline_line1"] = h1
    story["headline_line2"] = h2
    story["template_headline"] = f"{h1} {h2}"
    story["template_highlight"] = highlight or ""
    story.pop("awaiting_custom_headline", None)
    save_story(story)

    _edit_image(image_path, draft_path)
    if progress_msg_id:
        tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id, "text": "✅ Done rethinking headline"})
    set_keyboard(MAIN_KEYBOARD)
    log_telemetry("headline:rethink", story=story)
    print(f"[HEADLINE:rethink] msg {MESSAGE_ID} → '{h1}' / '{h2}'", flush=True)


def handle_rehighlight():
    story = get_story()
    if not story:
        send_msg(CHAT_ID, "⚠️ Story data not found.")
        set_keyboard(MAIN_KEYBOARD)
        return

    h1 = story.get("headline_line1", "")
    h2 = story.get("headline_line2", "")
    image_path = story.get("image_path", "")
    draft_path = story.get("draft_path", "")

    # Use template_headline as the authoritative rendered text.
    # headline_line1/2 may differ from what's actually on the image card.
    full_headline = (story.get("template_headline") or "").strip() or f"{h1} {h2}".strip()

    if not full_headline or not image_path:
        send_msg(CHAT_ID, "⚠️ No headline or image found — generate image first.")
        set_keyboard(MAIN_KEYBOARD)
        return

    # Clear cached highlight so _render_current_template forces a fresh LLM pick
    story.pop("template_highlight", None)

    progress = tg("sendMessage", {"chat_id": CHAT_ID, "text": "\U0001f3a8 Re-picking highlights..."})
    progress_msg_id = progress.get("result", {}).get("message_id")

    # Pass full_headline as h1, empty h2 — renderer concatenates them anyway
    ok, highlight = _render_current_template(story, full_headline, "", image_path)
    if not ok:
        if progress_msg_id:
            tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id, "text": "❌ Render failed"})
        else:
            send_msg(CHAT_ID, "❌ Render failed.")
        set_keyboard(MAIN_KEYBOARD)
        return

    story["template_highlight"] = highlight or ""
    save_story(story)

    _edit_image(image_path, draft_path)
    hl_note = f": {highlight}" if highlight else ""
    if progress_msg_id:
        tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id,
                               "text": f"✅ Re-highlighted{hl_note}"})
    set_keyboard(MAIN_KEYBOARD)
    print(f"[REHIGHLIGHT] msg {MESSAGE_ID} → '{highlight}'", flush=True)


def handle_custom_headline():
    story = get_story()
    if not story:
        send_msg(CHAT_ID, "⚠️ Story data not found.")
        set_keyboard(MAIN_KEYBOARD)
        return

    # Register pending reply so the daemon dispatches the next text message to us
    _register_reply(CHAT_ID, "headline", MESSAGE_ID)

    tg("sendMessage", {
        "chat_id": CHAT_ID,
        "text": (
            "✏️ <b>Custom Headline</b>\n\n"
            "Type your headline. Use <code>|</code> for line breaks, "
            "<code>*word*</code> for highlighted words.\n\n"
            "Example: <code>*Samsung* Bonus Revolt | Spreads to *HBM* Delivery</code>\n\n"
            "<i>No | = auto-wrap. Highlights appear in pink on the card.</i>"
        ),
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"force_reply": True, "selective": True}),
    })
    set_keyboard(EDIT_KEYBOARD)
    log_telemetry("headline:custom_prompt", story=story)


def handle_edit_text(mode):
    story = get_story()
    if not story:
        send_msg(CHAT_ID, "⚠️ Story data not found.")
        set_keyboard(MAIN_KEYBOARD)
        return

    draft_path = story.get("draft_path")
    if not draft_path or not Path(draft_path).exists():
        send_msg(CHAT_ID, "⚠️ Draft file not found.")
        set_keyboard(MAIN_KEYBOARD)
        return

    MODE_LABELS = {"shorter": ("Shortening", "shorter style"), "punchier": ("Punching up", "punchier style"), "rewrite": ("Rewriting", "full rewrite"), "snarky": ("Snarkifying", "snarky style")}
    label, done_label = MODE_LABELS.get(mode, (mode.capitalize(), mode))
    progress = tg("sendMessage", {"chat_id": CHAT_ID, "text": f"\u270f\ufe0f {label}..."})
    progress_msg_id = progress.get("result", {}).get("message_id")

    original = Path(draft_path).read_text()
    new_text, err = call_llm(TEXT_PROMPTS[mode], original)

    if err:
        if progress_msg_id:
            tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id, "text": f"\u274c {label} failed: {err}"})
        else:
            send_msg(CHAT_ID, f"\u274c LLM rewrite failed: {err}")
        set_keyboard(MAIN_KEYBOARD)
        return

    if len(new_text) > 1024:
        if progress_msg_id:
            tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id,
               "text": f"\u274c {label} failed: rewrite is {len(new_text)} chars (Telegram caption limit is 1024). Try again or use Shorter first."})
        set_keyboard(MAIN_KEYBOARD)
        return

    edit_path = _derive_edit_path(draft_path)
    Path(edit_path).write_text(new_text)

    story["draft_path"] = edit_path
    save_story(story)

    edit_result = subprocess.run([
        "python3", str(NEWSROOM / "scripts/telegram_edit.py"),
        "--channel", "test",
        "--message-id", MESSAGE_ID,
        "--file", edit_path,
        "--caption",
    ], capture_output=True, text=True, env={**os.environ, "HOME": os.path.expanduser("~")})

    if edit_result.returncode != 0:
        err_detail = (edit_result.stderr or "").strip()[:200]
        if progress_msg_id:
            tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id,
               "text": f"\u274c {label} rewrite succeeded but Telegram edit failed: {err_detail}"})
        set_keyboard(MAIN_KEYBOARD)
        return

    if progress_msg_id:
        tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id, "text": f"\u2705 Done applying {done_label}"})

    log_telemetry(f"edit:{mode}", story=story)
    set_keyboard(MAIN_KEYBOARD)
    print(f"[{mode.upper()}] Applied to msg {MESSAGE_ID}", flush=True)


def handle_edit_opinion():
    """Show opinion style sub-menu."""
    set_keyboard(OPINION_KEYBOARD)


def handle_add_opinion(label_key):
    """Append an opinion block with the chosen label style."""
    story = get_story()
    if not story:
        send_msg(CHAT_ID, "⚠️ Story data not found.")
        set_keyboard(MAIN_KEYBOARD)
        return

    draft_path = story.get("draft_path")
    if not draft_path or not Path(draft_path).exists():
        send_msg(CHAT_ID, "⚠️ Draft file not found.")
        set_keyboard(MAIN_KEYBOARD)
        return

    emoji, label_text = OPINION_LABEL_MAP[label_key]
    label_html = f"{emoji} <b>{label_text}:</b>"

    prompt = f"""Add a 2-3 sentence opinion block to this Telegram news post.

Label to use (place on its own line): {label_html}
Insert immediately before the "Read more:" line.

{JACOB_EDITORIAL_LENS}

Rules:
- Each opinion sentence on its own line with a blank line between sentences
- No blank line between the label line and the first opinion sentence
- Full blank line before the label line
- No em-dashes. No banned AI words (delve, tapestry, leverage, etc.)
- Preserve all existing HTML tags and structure exactly
- Return the complete updated post only — no explanation"""

    original = Path(draft_path).read_text()
    new_text, err = call_llm(prompt, original)

    if err:
        send_msg(CHAT_ID, f"❌ Opinion generation failed: {err}")
        set_keyboard(MAIN_KEYBOARD)
        return

    edit_path = _derive_edit_path(draft_path)
    Path(edit_path).write_text(new_text)

    story["draft_path"] = edit_path
    save_story(story)

    edit_result = subprocess.run([
        "python3", str(NEWSROOM / "scripts/telegram_edit.py"),
        "--channel", "test",
        "--message-id", MESSAGE_ID,
        "--file", edit_path,
        "--caption",
    ], capture_output=True, text=True, env={**os.environ, "HOME": os.path.expanduser("~")})

    if edit_result.returncode != 0:
        err_detail = (edit_result.stderr or "").strip()[:200]
        send_msg(CHAT_ID, f"❌ Opinion append succeeded but Telegram edit failed: {err_detail}")
        set_keyboard(MAIN_KEYBOARD)
        return

    log_telemetry(f"opinion:{label_key}", story=story)
    set_keyboard(MAIN_KEYBOARD)
    print(f"[OPINION:{label_key}] Applied to msg {MESSAGE_ID}", flush=True)


def handle_factcheck():
    """Fact-check via Perplexity, apply corrections if needed, report what changed."""
    story = get_story()
    if not story:
        send_msg(CHAT_ID, "\u26a0\ufe0f Story data not found.")
        return

    draft_path = story.get("draft_path", "")
    title = story.get("title", "story")
    if not draft_path or not Path(draft_path).exists():
        send_msg(CHAT_ID, "\u26a0\ufe0f Draft file not found.")
        return

    progress = tg("sendMessage", {"chat_id": CHAT_ID, "text": "\U0001f50d Fact-checking via Perplexity..."})
    progress_msg_id = progress.get("result", {}).get("message_id")

    plain = re.sub(r'<[^>]+>', '', Path(draft_path).read_text()).strip()
    query = (
        "Fact-check these claims from a news post. For each claim, state whether it is "
        "ACCURATE, INACCURATE, EXAGGERATED, or UNVERIFIABLE. Be specific and cite evidence. "
        "At the end, give a one-line overall verdict: PASS (all accurate) or ISSUES FOUND.\n\n"
        f"Claims:\n\n{plain[:1500]}"
    )

    result = subprocess.run(
        ["pwm", "ask", query],
        capture_output=True, text=True, timeout=90,
        env={**os.environ, "HOME": os.path.expanduser("~")},
    )
    verdict_raw = (result.stdout or result.stderr or "No response").strip()

    # Post full verdict to NOTIF_GROUP (split if exceeds Telegram 4096 limit)
    header = f"<b>Fact-check: {title}</b>\n\n"
    max_first_chunk = 4096 - len(header) - 10
    if len(verdict_raw) <= max_first_chunk:
        send_msg(NOTIF_GROUP, header + verdict_raw)
    else:
        send_msg(NOTIF_GROUP, header + verdict_raw[:max_first_chunk])
        remainder = verdict_raw[max_first_chunk:]
        while remainder:
            chunk = remainder[:4000]
            remainder = remainder[4000:]
            send_msg(NOTIF_GROUP, chunk)

    # Use full verdict for issue detection, truncated for LLM correction prompt
    verdict = verdict_raw[:3000]

    # Determine if issues were found
    verdict_lower = verdict.lower()
    has_issues = any(w in verdict_lower for w in ["inaccurate", "exaggerated", "issues found", "incorrect", "misleading", "false"])

    if not has_issues:
        if progress_msg_id:
            tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id,
                "text": "\u2705 Fact check passed \u2014 no changes needed"})
        log_telemetry("factcheck:pass", story=story)
        print(f"[FACTCHECK:PASS] msg {MESSAGE_ID}", flush=True)
        return

    # Issues found — ask LLM to apply corrections
    if progress_msg_id:
        tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id,
            "text": "\U0001f50d Issues found, applying corrections..."})

    original_html = Path(draft_path).read_text()
    correction_prompt = (
        "You are editing a Telegram news post. A fact-checker found issues. "
        "Fix ONLY the factual inaccuracies. Do NOT change tone, style, or structure. "
        "Preserve all HTML tags exactly. Return the corrected post text, nothing else.\n\n"
        "FACT-CHECK FINDINGS:\n" + verdict[:1200] + "\n\n"
        "ORIGINAL POST:\n" + original_html
    )
    corrected_text, err = call_llm(correction_prompt, "")

    if err:
        if progress_msg_id:
            tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id,
                "text": "\u274c Fact check found issues but correction failed: " + err[:100]})
        log_telemetry("factcheck:correction_failed", story=story)
        return

    # Generate a brief summary of what changed
    summary_prompt = (
        "Compare the original and corrected versions below. "
        "In ONE sentence (max 120 chars), describe what factual corrections were made. "
        "Start with a verb (e.g., 'Corrected X claim', 'Removed exaggerated Y', 'Fixed Z stat'). "
        "Return ONLY that one sentence, no quotes.\n\n"
        "ORIGINAL:\n" + original_html[:800] + "\n\n"
        "CORRECTED:\n" + corrected_text[:800]
    )
    summary, _ = call_llm(summary_prompt, "")
    if not summary:
        summary = "Applied factual corrections"
    summary = summary.strip().strip('"')[:120]

    # Apply the correction
    edit_path = _derive_edit_path(draft_path)
    Path(edit_path).write_text(corrected_text)
    story["draft_path"] = edit_path
    save_story(story)

    subprocess.run([
        "python3", str(NEWSROOM / "scripts/telegram_edit.py"),
        "--channel", "test",
        "--message-id", MESSAGE_ID,
        "--file", edit_path,
        "--caption",
    ], capture_output=True, text=True, env={**os.environ, "HOME": os.path.expanduser("~")})

    if progress_msg_id:
        tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id,
            "text": f"\u2705 Done fact-checking \u2014 {summary}"})

    log_telemetry("factcheck:corrected", story=story)
    print(f"[FACTCHECK:CORRECTED] msg {MESSAGE_ID}: {summary}", flush=True)


def handle_newsource():
    """Search for alternative/additional sources for this story."""
    story = get_story()
    if not story:
        send_msg(CHAT_ID, "⚠️ Story data not found.")
        return

    title = story.get("title", "")
    source_url = story.get("source_url", "")
    if not title:
        send_msg(CHAT_ID, "⚠️ No title in story data.")
        return

    send_msg(CHAT_ID, "🔎 Searching for sources...")

    real_home = os.environ.get("ALEF_AGENT_REAL_HOME", "/Users/jbd")
    try:
        result = subprocess.run(
            ["gsearch", title, "--type", "news", "--time", "week", "--limit", "5"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "HOME": real_home},
        )
        output = (result.stdout or "").strip()
        if not output:
            output = result.stderr.strip() or "No results returned."
    except subprocess.TimeoutExpired:
        output = "Search timed out."
    except FileNotFoundError:
        output = "gsearch not found — check PATH."

    output = output[:2000]
    log_telemetry("newsource", story=story)
    current = f"\nCurrent source: {source_url}" if source_url else ""
    msg = f"<b>Sources for: {title}</b>{current}\n\n<pre>{output}</pre>"
    send_msg(CHAT_ID, msg)
    send_msg(NOTIF_GROUP, msg)
    print(f"[NEWSOURCE] msg {MESSAGE_ID}", flush=True)



def _register_reply(chat_id, action, message_id):
    """Register a pending reply expectation for the daemon's script reply dispatcher."""
    replies_path = Path(WORKSPACE) / "callbacks" / "pending_replies.json"
    try:
        data = json.loads(replies_path.read_text()) if replies_path.exists() else {}
    except Exception:
        data = {}
    data[chat_id] = {
        "prefix": "nr_",
        "action": action,
        "messageId": message_id,
        "expiresAt": int(time.time() * 1000) + 5 * 60_000,
    }
    replies_path.write_text(json.dumps(data, indent=2))


def handle_reply_headline():
    """Process a custom headline typed by the user. Parses *highlights* and | line breaks."""
    text = os.environ.get("CALLBACK_TEXT", "").strip()
    if not text:
        send_msg(CHAT_ID, "⚠️ No headline text received.")
        return

    story = get_story()
    if not story:
        send_msg(CHAT_ID, "⚠️ Story data not found for this post.")
        return

    image_path = story.get("image_path", "")
    if not image_path:
        send_msg(CHAT_ID, "⚠️ Image path not found.")
        return

    progress = tg("sendMessage", {"chat_id": CHAT_ID, "text": "✏️ Applying custom headline..."})
    progress_msg_id = progress.get("result", {}).get("message_id")

    highlights = re.findall(r"\*([^*]+)\*", text)
    clean_text = re.sub(r"\*([^*]+)\*", r"\1", text)
    # Convert | to <br> for forced line breaks in the rendered image
    headline_for_render = "<br>".join(part.strip() for part in clean_text.split("|"))
    # Also store clean version without <br> for data
    headline_clean = " ".join(clean_text.replace("|", " ").split())
    # Build highlight csv
    highlight_csv = ",".join(h.strip() for h in highlights) if highlights else ""

    # Render the image
    render_script = NEWSROOM / "skills/news-cards/render.mjs"
    if not render_script.exists():
        if progress_msg_id:
            tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id, "text": "❌ render.mjs not found"})
        return

    template_name = story.get("current_template", "dark-editorial")
    category = story.get("template_category") or _derive_category(story)
    subline = story.get("template_subline", "")
    if not subline:
        subline = _derive_subline(story.get("draft_path", ""))
        if subline:
            story["template_subline"] = subline

    cmd = [
        "node", str(render_script),
        "--template", template_name,
        "--category", category,
        "--headline", headline_for_render,
        "--subline", subline,
        "--output", image_path,
    ]
    if highlight_csv:
        cmd += ["--highlight", highlight_csv]

    result = subprocess.run(
        cmd, capture_output=True, text=True,
        env={**os.environ, "HOME": os.path.expanduser("~")},
    )

    if result.returncode != 0:
        err_msg = (result.stderr or result.stdout or "unknown error")[:150]
        if progress_msg_id:
            tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id, "text": f"❌ Render failed: {err_msg}"})
        return

    # Update story data
    story["headline_line1"] = headline_clean
    story["headline_line2"] = ""
    story["template_headline"] = headline_clean
    story["template_highlight"] = highlight_csv
    story.pop("awaiting_custom_headline", None)
    save_story(story)

    # Update the post image in Telegram
    _edit_image(image_path, story.get("draft_path"))

    hl_note = f" (highlighted: {highlight_csv})" if highlight_csv else ""
    if progress_msg_id:
        tg("editMessageText", {"chat_id": CHAT_ID, "message_id": progress_msg_id,
            "text": f"✅ Done applying custom headline{hl_note}"})

    set_keyboard(MAIN_KEYBOARD)
    log_telemetry("headline:custom_applied", story=story)
    print(f"[HEADLINE:custom] msg {MESSAGE_ID} \u2192 '{headline_clean}' hl={highlight_csv}", flush=True)


DISPATCH = {
    "nr_approve":       handle_approve,
    "nr_drop":          handle_drop,
    **{f"nr_drop_{k}": (lambda k=k: handle_drop_reason(k)) for k in DROP_REASON_LABELS},
    "nr_noop":          lambda: None,
    "nr_edit":          handle_edit,
    "nr_back":          handle_back,
    # Fact check + source
    "nr_factcheck":     handle_factcheck,
    "nr_newsource":     handle_newsource,
    # Image sub-menu
    "nr_edit_image":    handle_edit_image,
    "nr_img_classic":   handle_image_classic,
    "nr_img_t1":        lambda: handle_image_template("t1"),
    "nr_img_t2":        lambda: handle_image_template("t2"),
    "nr_img_t3":        lambda: handle_image_template("t3"),
    # Headline tools
    "nr_rethink_headline": handle_rethink_headline,
    "nr_rehighlight":      handle_rehighlight,
    "nr_custom_headline":  handle_custom_headline,
    "nr_reply_headline":   handle_reply_headline,
    # Text rewrites
    "nr_edit_shorter":  lambda: handle_edit_text("shorter"),
    "nr_edit_punchier": lambda: handle_edit_text("punchier"),
    "nr_edit_rewrite":  lambda: handle_edit_text("rewrite"),
    "nr_edit_snarky":   lambda: handle_edit_text("snarky"),
    # Opinion sub-menu
    "nr_edit_opinion":  handle_edit_opinion,
    "nr_op_matters":    lambda: handle_add_opinion("matters"),
    "nr_op_play":       lambda: handle_add_opinion("play"),
    "nr_op_real":       lambda: handle_add_opinion("real"),
    "nr_op_signal":     lambda: handle_add_opinion("signal"),
    "nr_op_room":       lambda: handle_add_opinion("room"),
    "nr_op_tell":       lambda: handle_add_opinion("tell"),
    # Buffer
    "nr_buf_queue":     lambda: handle_buffer("queue"),
    "nr_buf_publish":   lambda: handle_buffer("publish"),
    "nr_buf_draft":     lambda: handle_buffer("draft"),
    "nr_buf_skip":      handle_buffer_skip,
}

if __name__ == "__main__":
    if not TOKEN or not DATA:
        print("ERROR: Missing TELEGRAM_BOT_TOKEN or CALLBACK_DATA")
        sys.exit(1)

    handler = DISPATCH.get(DATA)
    if handler:
        handler()
    else:
        print(f"Unknown callback: {DATA}")
