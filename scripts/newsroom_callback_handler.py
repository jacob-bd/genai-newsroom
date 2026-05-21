#!/usr/bin/env python3
"""
newsroom_callback_handler.py - Newsroom draft review button handler.

Long-polls Telegram for callback_query events from inline keyboard buttons
attached to draft posts in the test channel. Handles:
  nr_approve     → copy to live + Buffer push
  nr_drop        → remove buttons, clear pending state
  nr_edit        → show edit sub-menu
  nr_back        → restore main review keyboard
  nr_edit_image  → regenerate image overlay from clean background
  nr_edit_shorter  → trim post text via LLM
  nr_edit_punchier → punch up post text via LLM
  nr_edit_rewrite  → full redraft via LLM

State is persisted in memory/newsroom_pending.json:
  { "<message_id>": { slug, title, draft_path, image_path, clean_bg_path,
                      headline_line1, headline_line2, source_url, emoji,
                      chat_id, created_at } }

Start: python3 newsroom_callback_handler.py
Stop:  kill $(cat ~/.alef-agent/workspace/newsroom/tmp/callback_handler.pid)
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
BASE = f"https://api.telegram.org/bot{TOKEN}/"
WORKSPACE = Path("/Users/jbd/.alef-agent/workspace")
PENDING_FILE = WORKSPACE / "memory/newsroom_pending.json"
OFFSET_FILE = WORKSPACE / "tmp/callback_offset.txt"
PID_FILE = WORKSPACE / "tmp/callback_handler.pid"
TEST_CHAT = "-1003889167143"
LIVE_CHAT = "-1003300061793"
UPDATE_GROUP = "-1003682312998"

MAIN_KEYBOARD = [
    [{"text": "✅ Approve", "callback_data": "nr_approve"},
     {"text": "🗑 Drop", "callback_data": "nr_drop"}],
    [{"text": "✏️ Edit", "callback_data": "nr_edit"}],
]
EDIT_KEYBOARD = [
    [{"text": "🖼 New image", "callback_data": "nr_edit_image"},
     {"text": "✂️ Shorter", "callback_data": "nr_edit_shorter"}],
    [{"text": "🔥 Punchier", "callback_data": "nr_edit_punchier"},
     {"text": "📰 Rewrite", "callback_data": "nr_edit_rewrite"}],
    [{"text": "« Back", "callback_data": "nr_back"}],
]


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_pending():
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except Exception:
            pass
    return {}


def save_pending(data):
    PENDING_FILE.write_text(json.dumps(data, indent=2))


def load_offset():
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def save_offset(offset):
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(offset))


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def tg(method, payload, timeout=15):
    try:
        r = requests.post(BASE + method, json=payload, timeout=timeout)
        return r.json()
    except Exception as e:
        print(f"[TG ERROR] {method}: {e}", flush=True)
        return {}


def answer_cb(cb_id, text="", alert=False):
    tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": text, "show_alert": alert})


def set_keyboard(chat_id, message_id, keyboard=None):
    markup = {"inline_keyboard": keyboard} if keyboard else {"inline_keyboard": []}
    tg("editMessageReplyMarkup", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": json.dumps(markup),
    })


def send_msg(chat_id, text):
    tg("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})


# ---------------------------------------------------------------------------
# LLM rewrite (OpenRouter → claude-sonnet)
# ---------------------------------------------------------------------------

def llm_rewrite(text, mode):
    import openai

    prompts = {
        "shorter": (
            "Trim this Telegram news post to about 70% of its current length. "
            "Keep all key facts and the main insight. Preserve all HTML tags "
            "(<b>, <a href=...>) exactly. Return only the rewritten post text, nothing else."
        ),
        "punchier": (
            "Rewrite this Telegram news post to be more punchy, high-energy, and impactful. "
            "Keep the same facts and structure. Preserve all HTML tags exactly. "
            "Return only the rewritten post text, nothing else."
        ),
        "rewrite": (
            "Fully rewrite this Telegram news post with a fresh angle and fresh opening. "
            "Keep the same story facts. Preserve all HTML tags exactly. "
            "Return only the rewritten post text, nothing else."
        ),
    }

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    client = openai.OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )
    resp = client.chat.completions.create(
        model="anthropic/claude-sonnet-4-5",
        max_tokens=2048,
        messages=[{"role": "user", "content": f"{prompts[mode]}\n\n{text}"}],
    )
    return resp.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def handle_approve(chat_id, message_id, cb_id):
    pending = load_pending()
    story = pending.get(str(message_id))
    if not story:
        answer_cb(cb_id, "Story data not found. Post live manually.", alert=True)
        return

    answer_cb(cb_id, "Posting live...")
    set_keyboard(chat_id, message_id)

    slug = story.get("slug", "story")
    log_path = str(WORKSPACE / f"tmp/{slug}_live_msg_id.txt")
    emoji = story.get("emoji", "🔥")
    title = story.get("title", "")

    cmd = [
        "python3", str(WORKSPACE / "scripts/telegram_post.py"),
        "--channel", "live",
        "--copy-from-chat", TEST_CHAT,
        "--copy-msg-id", str(message_id),
        "--log", log_path,
        "--react", emoji,
    ]
    if title:
        cmd += ["--title", title]

    result = subprocess.run(
        cmd, capture_output=True, text=True,
        env={**os.environ, "HOME": "/Users/jbd"},
    )

    if "SUCCESS" not in result.stdout:
        send_msg(chat_id, f"Live post failed:\n<code>{result.stdout[:300]}</code>")
        set_keyboard(chat_id, message_id, MAIN_KEYBOARD)
        return

    live_msg_id = None
    for line in result.stdout.splitlines():
        if line.startswith("MESSAGE_ID:"):
            live_msg_id = line.split(":", 1)[1].strip()

    image_path = story.get("image_path", "")
    if image_path and Path(image_path).exists():
        subprocess.run([
            "python3", str(WORKSPACE / "scripts/buffer_push.py"),
            "--telegram-msg", str(message_id),
            "--telegram-chat", TEST_CHAT,
            "--image", image_path,
            "--queue",
        ], capture_output=True, text=True, env={**os.environ, "HOME": "/Users/jbd"})

    pending.pop(str(message_id), None)
    save_pending(pending)

    link = f"https://t.me/genaispot/{live_msg_id}" if live_msg_id else "(check channel)"
    send_msg(UPDATE_GROUP, f"Posted live: {link}")


def handle_drop(chat_id, message_id, cb_id):
    answer_cb(cb_id, "Story dropped.")
    set_keyboard(chat_id, message_id)
    pending = load_pending()
    pending.pop(str(message_id), None)
    save_pending(pending)
    print(f"[DROP] msg {message_id} dropped", flush=True)


def handle_edit(chat_id, message_id, cb_id):
    answer_cb(cb_id)
    set_keyboard(chat_id, message_id, EDIT_KEYBOARD)


def handle_back(chat_id, message_id, cb_id):
    answer_cb(cb_id)
    set_keyboard(chat_id, message_id, MAIN_KEYBOARD)


def handle_edit_text(chat_id, message_id, cb_id, mode):
    pending = load_pending()
    story = pending.get(str(message_id))
    if not story:
        answer_cb(cb_id, "Story data not found.", alert=True)
        return

    draft_path = story.get("draft_path")
    if not draft_path or not Path(draft_path).exists():
        answer_cb(cb_id, "Draft file not found.", alert=True)
        set_keyboard(chat_id, message_id, MAIN_KEYBOARD)
        return

    answer_cb(cb_id, "Rewriting...")
    original = Path(draft_path).read_text()

    try:
        new_text = llm_rewrite(original, mode)
        edit_path = re.sub(r"_draft\.txt$", "_edit.txt", draft_path)
        if edit_path == draft_path:
            edit_path = draft_path.replace(".txt", "_edit.txt")
        Path(edit_path).write_text(new_text)
        story["draft_path"] = edit_path
        save_pending(pending)

        subprocess.run([
            "python3", str(WORKSPACE / "scripts/telegram_edit.py"),
            "--channel", "test",
            "--message-id", str(message_id),
            "--file", edit_path,
            "--caption",
        ], capture_output=True, text=True, env={**os.environ, "HOME": "/Users/jbd"})

    except Exception as e:
        print(f"[REWRITE ERROR] {e}", flush=True)
        answer_cb(cb_id, f"Rewrite failed: {str(e)[:80]}", alert=True)

    set_keyboard(chat_id, message_id, MAIN_KEYBOARD)


def handle_edit_image(chat_id, message_id, cb_id):
    pending = load_pending()
    story = pending.get(str(message_id))
    if not story:
        answer_cb(cb_id, "Story data not found.", alert=True)
        return

    clean_bg = story.get("clean_bg_path", "")
    image_path = story.get("image_path", "")
    line1 = story.get("headline_line1", "")
    line2 = story.get("headline_line2", "")

    if not clean_bg or not Path(clean_bg).exists():
        answer_cb(cb_id, "Clean background not found. Cannot regenerate.", alert=True)
        set_keyboard(chat_id, message_id, MAIN_KEYBOARD)
        return

    answer_cb(cb_id, "Regenerating image...")

    result = subprocess.run([
        "python3", str(WORKSPACE / "scripts/news_image_overlay.py"),
        clean_bg, image_path, line1, line2,
    ], capture_output=True, text=True, env={**os.environ, "HOME": "/Users/jbd"})

    if result.returncode != 0:
        print(f"[IMAGE ERROR] {result.stderr[:200]}", flush=True)
        set_keyboard(chat_id, message_id, MAIN_KEYBOARD)
        return

    subprocess.run([
        "python3", str(WORKSPACE / "scripts/telegram_edit.py"),
        "--channel", "test",
        "--message-id", str(message_id),
        "--image", image_path,
    ], capture_output=True, text=True, env={**os.environ, "HOME": "/Users/jbd"})

    set_keyboard(chat_id, message_id, MAIN_KEYBOARD)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

DISPATCH = {
    "nr_approve": handle_approve,
    "nr_drop": handle_drop,
    "nr_edit": handle_edit,
    "nr_back": handle_back,
    "nr_edit_image": handle_edit_image,
    "nr_edit_shorter": lambda c, m, cb: handle_edit_text(c, m, cb, "shorter"),
    "nr_edit_punchier": lambda c, m, cb: handle_edit_text(c, m, cb, "punchier"),
    "nr_edit_rewrite": lambda c, m, cb: handle_edit_text(c, m, cb, "rewrite"),
}


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def poll():
    offset = load_offset()
    print(f"[START] Newsroom callback handler. Offset: {offset}", flush=True)

    while True:
        try:
            resp = requests.post(BASE + "getUpdates", json={
                "offset": offset,
                "timeout": 30,
                "allowed_updates": ["callback_query"],
            }, timeout=35)
            data = resp.json()

            if not data.get("ok"):
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                save_offset(offset)

                cq = update.get("callback_query")
                if not cq:
                    continue

                cb_id = cq["id"]
                msg = cq["message"]
                chat_id = str(msg["chat"]["id"])
                message_id = msg["message_id"]
                action = cq.get("data", "")

                print(f"[CB] {action} from {chat_id} msg {message_id}", flush=True)

                handler = DISPATCH.get(action)
                if handler:
                    try:
                        handler(chat_id, message_id, cb_id)
                    except Exception as e:
                        print(f"[HANDLER ERROR] {action}: {e}", flush=True)
                        answer_cb(cb_id, f"Error: {str(e)[:80]}", alert=True)
                else:
                    answer_cb(cb_id)

        except requests.RequestException as e:
            print(f"[NETWORK] {e}", flush=True)
            time.sleep(10)
        except Exception as e:
            print(f"[POLL ERROR] {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    try:
        poll()
    finally:
        PID_FILE.unlink(missing_ok=True)
