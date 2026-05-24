#!/usr/bin/env python3
"""
telegram_edit.py - Reliable Telegram Message Editor

Edits existing messages via the Telegram Bot API. Same safety patterns
as telegram_post.py: no blind retries, clear error reporting, em-dash stripping.

Usage:
    python3 telegram_edit.py --channel <live|test|group|ID> --message-id <MSG_ID> --text "new text"
    python3 telegram_edit.py --channel <live|test|group|ID> --message-id <MSG_ID> --file /tmp/edited.txt
    python3 telegram_edit.py --channel <live|test|group|ID> --message-id <MSG_ID> --file /tmp/edited.txt --caption
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error

import requests


def get_channel_id(channel_arg):
    mapping = {
        "live": "-1003300061793",
        "test": "-1003889167143",
        "group": "-1003682312998"
    }
    return mapping.get(channel_arg, channel_arg)


def sanitize_text(text):
    """Strip em-dashes and en-dashes (AI writing giveaway).

    Uses a regex so any spaces surrounding the dash are absorbed into the
    replacement. A naive ``.replace("\u2014", ",")`` leaves the original
    spaces in place, producing artifacts like "expectations , forcing"
    when the source had "expectations \u2014 forcing".

    Mirrors the rule in telegram_post.py so post + edit stay consistent.
    """
    if not text:
        return text
    return re.sub(r"\s*[\u2014\u2013]\s*", ", ", text)


def edit_message(token, chat_id, message_id, text, parse_mode="HTML", is_caption=False, reply_markup=None):
    """Edit a Telegram message or caption. Returns True on success."""
    if is_caption:
        url = f"https://api.telegram.org/bot{token}/editMessageCaption"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": text,
            "parse_mode": parse_mode,
        }
    else:
        url = f"https://api.telegram.org/bot{token}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if body.get("ok"):
                msg_id = body.get("result", {}).get("message_id", message_id)
                print(f"MESSAGE_ID={msg_id}", file=sys.stderr)
                if is_caption:
                    _cache_caption(chat_id, message_id, text)
                return True
            else:
                desc = body.get("description", "unknown error")
                # Auto-fallback: message is a photo with caption, not a text message
                if not is_caption and "there is no text in the message" in desc:
                    print("INFO: No text found, retrying as caption edit...", file=sys.stderr)
                    return edit_message(token, chat_id, message_id, text, parse_mode, is_caption=True, reply_markup=reply_markup)
                print(f"ERROR: Telegram API returned ok=false: {desc}", file=sys.stderr)
                print("DO NOT RETRY without investigating.", file=sys.stderr)
                return False
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else "(no body)"
        # Also handle the fallback for HTTP 400 path
        if not is_caption and "there is no text in the message" in error_body:
            print("INFO: No text found, retrying as caption edit...", file=sys.stderr)
            return edit_message(token, chat_id, message_id, text, parse_mode, is_caption=True, reply_markup=reply_markup)
        print(f"ERROR: HTTP {e.code}: {error_body[:500]}", file=sys.stderr)
        print("DO NOT RETRY without investigating.", file=sys.stderr)
        return False
    except urllib.error.URLError as e:
        print(f"ERROR: Network error: {e.reason}", file=sys.stderr)
        print("DO NOT RETRY. Check if the edit actually went through first.", file=sys.stderr)
        return False
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("DO NOT RETRY without investigating.", file=sys.stderr)
        return False


CAPTION_CACHE = os.path.expanduser("~/.telegram_edit_captions.json")


def _load_caption_cache():
    try:
        with open(CAPTION_CACHE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_caption_cache(cache):
    with open(CAPTION_CACHE, "w") as f:
        json.dump(cache, f)


def _get_cached_caption(chat_id, message_id):
    cache = _load_caption_cache()
    return cache.get(str(chat_id), {}).get(str(message_id))


def _cache_caption(chat_id, message_id, text):
    cache = _load_caption_cache()
    cache.setdefault(str(chat_id), {})[str(message_id)] = text
    _save_caption_cache(cache)


def edit_message_media(token, chat_id, message_id, image_path, caption_text=None, parse_mode="HTML", reply_markup=None):
    """Replace photo media and optionally update caption in one call."""
    url = f"https://api.telegram.org/bot{token}/editMessageMedia"
    media = {
        "type": "photo",
        "media": "attach://photo",
    }
    if caption_text is None:
        caption_text = _get_cached_caption(chat_id, message_id)
    if caption_text is not None:
        media["caption"] = caption_text
        if parse_mode:
            media["parse_mode"] = parse_mode

    post_data = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "media": json.dumps(media),
    }
    if reply_markup is not None:
        post_data["reply_markup"] = json.dumps(reply_markup) if isinstance(reply_markup, dict) else reply_markup

    try:
        with open(image_path, "rb") as image_file:
            response = requests.post(
                url,
                data=post_data,
                files={"photo": image_file},
                timeout=60,
            )

        body = response.json()
        if body.get("ok"):
            msg_id = body.get("result", {}).get("message_id", message_id)
            print(f"MESSAGE_ID={msg_id}", file=sys.stderr)
            if caption_text:
                _cache_caption(chat_id, message_id, caption_text)
            return True

        desc = body.get("description", "unknown error")
        print(f"ERROR: Telegram API returned ok=false: {desc}", file=sys.stderr)
        print("DO NOT RETRY without investigating.", file=sys.stderr)
        return False
    except FileNotFoundError:
        print(f"ERROR: Image file not found: {image_path}", file=sys.stderr)
        return False
    except requests.RequestException as e:
        print(f"ERROR: Network error: {e}", file=sys.stderr)
        print("DO NOT RETRY. Check if the edit actually went through first.", file=sys.stderr)
        return False
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("DO NOT RETRY without investigating.", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Reliable Telegram Message Editor")
    parser.add_argument("--channel", required=True, help="Target channel ('live', 'test', 'group', or raw ID)")
    parser.add_argument("--message-id", required=True, type=int, help="Message ID to edit")
    parser.add_argument("--text", help="New text for the message")
    parser.add_argument("--file", help="File containing the new text")
    parser.add_argument("--image", help="Replace photo media with this local image path")
    parser.add_argument("--caption", action="store_true", help="Edit caption instead of text (for photo messages)")
    parser.add_argument("--no-html", action="store_true", help="Send as plain text (no HTML parsing)")
    parser.add_argument("--keyboard", help="Re-attach inline keyboard as JSON reply_markup (prevents keyboard disappearing on edit)")
    parser.add_argument("--newsroom-keyboard", action="store_true", dest="newsroom_keyboard",
                        help="Shorthand: re-attach standard newsroom Approve/Fact Check/New Source/Edit/Drop keyboard")
    parser.add_argument("--no-keyboard", action="store_true", dest="no_keyboard",
                        help="Explicitly remove keyboard (overrides auto-preserve for test channel)")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)

    if not args.text and not args.file and not args.image:
        print("ERROR: Must provide --text, --file, or --image.", file=sys.stderr)
        sys.exit(1)

    text = None
    if args.file:
        try:
            with open(args.file, "r") as f:
                text = f.read().strip()
        except FileNotFoundError:
            print(f"ERROR: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
    elif args.text:
        text = args.text

    chat_id = get_channel_id(args.channel)
    parse_mode = "HTML" if not args.no_html else None

    if text is not None:
        text = sanitize_text(text)

    NEWSROOM_KEYBOARD = {"inline_keyboard": [
        [{"text": "✅ Approve", "callback_data": "nr_approve"},
         {"text": "\U0001f5d1 Drop", "callback_data": "nr_drop"}],
        [{"text": "\U0001f50d Fact Check", "callback_data": "nr_factcheck"},
         {"text": "\U0001f50e New Source", "callback_data": "nr_newsource"}],
        [{"text": "✏️ Edit", "callback_data": "nr_edit"}],
    ]}

    reply_markup = None
    if args.no_keyboard:
        reply_markup = None  # explicit removal
    elif args.newsroom_keyboard:
        reply_markup = NEWSROOM_KEYBOARD
    elif args.keyboard:
        try:
            reply_markup = json.loads(args.keyboard)
        except json.JSONDecodeError as e:
            print(f"ERROR: --keyboard is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)
    elif chat_id == "-1003889167143" and not args.no_keyboard:
        # Auto-preserve: test channel edits always re-attach newsroom keyboard
        # unless --no-keyboard is explicitly passed.
        reply_markup = NEWSROOM_KEYBOARD
        print("INFO: Auto-attaching newsroom keyboard (test channel). Use --no-keyboard to suppress.", file=sys.stderr)

    if args.image:
        success = edit_message_media(token, chat_id, args.message_id, args.image, text, parse_mode, reply_markup=reply_markup)
    else:
        success = edit_message(token, chat_id, args.message_id, text, parse_mode, is_caption=args.caption, reply_markup=reply_markup)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
