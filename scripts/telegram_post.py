import re
import os
import sys
import argparse
import requests
import json
import subprocess
from contextlib import ExitStack
from datetime import datetime

ALLOWED_REACTIONS = {
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢",
    "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊️", "🤡", "🥱", "🥴", "😍", "🐳",
    "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾",
    "💋", "🖕", "😈", "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇",
    "😨", "🤝", "🤗", "🫡", "🎅", "🎄", "☃️", "💅", "🤪", "🗿", "🆒", "💘",
    "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷", "😡",
}


def get_channel_id(channel_arg):
    mapping = {
        "live": "-1003300061793",
        "test": "-1003889167143",
        "group": "-1003682312998"
    }
    # If it's a known alias, return the ID, else assume it's a raw numeric ID
    return mapping.get(channel_arg, channel_arg)


def probe_video_metadata(video_path):
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-print_format", "json",
                "-show_entries", "stream=width,height:format=duration",
                video_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
        meta = {}
        if stream.get("width"):
            meta["width"] = int(stream["width"])
        if stream.get("height"):
            meta["height"] = int(stream["height"])
        if fmt.get("duration"):
            meta["duration"] = max(0, round(float(fmt["duration"])))
        return meta
    except Exception:
        return {}


def react_to_message(base_url, chat_id, message_id, emoji):
    endpoint = base_url + "setMessageReaction"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reaction": json.dumps([{"type": "emoji", "emoji": emoji}]),
    }
    response = requests.post(endpoint, data=payload, timeout=20)
    try:
        data = response.json()
    except ValueError:
        return False, f"Invalid JSON response from Telegram while reacting: {response.text}"
    if data.get("ok"):
        return True, None
    return False, data.get("description", "Unknown reaction error")


def main():
    parser = argparse.ArgumentParser(description="Reliable Telegram Poster")
    parser.add_argument("--channel", required=True, help="Target channel ('live', 'test', 'group', or raw ID)")
    parser.add_argument("--text", help="Raw text for the post")
    parser.add_argument("--file", help="File containing the text for the post")
    parser.add_argument("--image", help="Path to image file (optional)")
    parser.add_argument("--video", help="Path to video file (optional)")
    parser.add_argument("--thumbnail", help="Path to JPEG thumbnail for video uploads (optional)")
    parser.add_argument("--copy-from-chat", help="Chat ID to copy a message from")
    parser.add_argument("--copy-msg-id", type=int, help="Message ID to copy")
    parser.add_argument("--parse-mode", default="HTML", help="Parse mode (default: HTML)")
    parser.add_argument("--reply-to", type=int, help="Optional message ID to reply to")
    parser.add_argument("--react", help="Optional emoji reaction to apply to the posted message")
    parser.add_argument("--react-to", type=int, help="React to an existing message (standalone, no posting). Requires --react and --channel.")
    parser.add_argument("--source-url", help="Source article URL (for channel history indexing)")
    parser.add_argument("--title", help="Post title override for channel history indexing (used when --copy-from-chat omits text)")
    parser.add_argument("--log", help="Path to write the returned message ID after a successful post")
    parser.add_argument("--draft-mode", action="store_true", help="Attach draft review buttons (Approve / Edit / Drop) as inline keyboard")

    args = parser.parse_args()
    
    media_args = sum(bool(x) for x in [args.image, args.video, args.copy_msg_id])
    if media_args > 1:
        print("ERROR: Cannot pass more than one of --image, --video, or --copy-msg-id.")
        sys.exit(1)
        
    if args.copy_msg_id and not args.copy_from_chat:
        print("ERROR: Must provide --copy-from-chat when using --copy-msg-id.")
        sys.exit(1)
        
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN environment variable not set.")
        sys.exit(1)
        
    chat_id = get_channel_id(args.channel)

    if args.react and args.react not in ALLOWED_REACTIONS:
        print(f"WARN: Reaction emoji '{args.react}' is not in the Telegram-allowed list. Falling back to 🔥")
        args.react = "🔥"

    # Standalone react-to mode: react to an existing message without posting
    if args.react_to:
        if not args.react:
            print("ERROR: --react-to requires --react <EMOJI>")
            sys.exit(1)
        base_url = f"https://api.telegram.org/bot{token}/"
        ok, error = react_to_message(base_url, chat_id, args.react_to, args.react)
        if ok:
            print(f"SUCCESS: Reacted with {args.react} to message {args.react_to} in {args.channel} ({chat_id})")
            sys.exit(0)
        else:
            print(f"REACTION_ERROR: {error}")
            sys.exit(1)
    
    # Get text
    post_text = ""
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                post_text = f.read()
        except Exception as e:
            print(f"ERROR: Could not read file {args.file}: {e}")
            sys.exit(1)
    elif args.text:
        post_text = args.text
    else:
        # Text is optional ONLY if we are copying a message and keeping its original caption
        if not args.copy_msg_id:
            print("ERROR: Must provide either --text or --file")
            sys.exit(1)
        
    # Enforce Em-Dash Rule silently if text exists
    if post_text:
        post_text = re.sub(r"\s*[\u2014\u2013]\s*", ", ", post_text)
    
    url = f"https://api.telegram.org/bot{token}/"

    draft_keyboard = None
    if args.draft_mode:
        draft_keyboard = json.dumps({"inline_keyboard": [
            [{"text": "✅ Approve", "callback_data": "nr_approve"},
             {"text": "🗑 Drop", "callback_data": "nr_drop"}],
            [{"text": "✏️ Edit", "callback_data": "nr_edit"}],
        ]})

    try:
        if args.image:
            endpoint = url + "sendPhoto"
            if not os.path.exists(args.image):
                print(f"ERROR: Image file not found at {args.image}")
                sys.exit(1)

            with open(args.image, "rb") as media_file:
                payload = {
                    "chat_id": chat_id,
                    "caption": post_text,
                    "parse_mode": args.parse_mode
                }
                if args.reply_to:
                    payload["reply_to_message_id"] = args.reply_to
                if draft_keyboard:
                    payload["reply_markup"] = draft_keyboard

                files = {"photo": media_file}
                # Use a generous timeout since image uploads can be slow
                response = requests.post(endpoint, data=payload, files=files, timeout=45)
                
        elif args.video:
            endpoint = url + "sendVideo"
            if not os.path.exists(args.video):
                print(f"ERROR: Video file not found at {args.video}")
                sys.exit(1)
            if args.thumbnail and not os.path.exists(args.thumbnail):
                print(f"ERROR: Thumbnail file not found at {args.thumbnail}")
                sys.exit(1)

            video_meta = probe_video_metadata(args.video)
            with ExitStack() as stack:
                media_file = stack.enter_context(open(args.video, "rb"))
                payload = {
                    "chat_id": chat_id,
                    "caption": post_text,
                    "parse_mode": args.parse_mode,
                    "supports_streaming": True,
                }
                if video_meta.get("width"):
                    payload["width"] = video_meta["width"]
                if video_meta.get("height"):
                    payload["height"] = video_meta["height"]
                if video_meta.get("duration") is not None:
                    payload["duration"] = video_meta["duration"]
                if args.reply_to:
                    payload["reply_to_message_id"] = args.reply_to
                if draft_keyboard:
                    payload["reply_markup"] = draft_keyboard

                files = {"video": media_file}
                if args.thumbnail:
                    thumb_file = stack.enter_context(open(args.thumbnail, "rb"))
                    files["thumbnail"] = thumb_file

                # Use a VERY generous timeout for video uploads (300 seconds / 5 mins)
                response = requests.post(endpoint, data=payload, files=files, timeout=300)
                
        elif args.copy_msg_id:
            endpoint = url + "copyMessage"
            payload = {
                "chat_id": chat_id,
                "from_chat_id": get_channel_id(args.copy_from_chat),
                "message_id": args.copy_msg_id,
                "parse_mode": args.parse_mode
            }
            if post_text:
                payload["caption"] = post_text
            if args.reply_to:
                payload["reply_to_message_id"] = args.reply_to
            if draft_keyboard:
                payload["reply_markup"] = draft_keyboard

            response = requests.post(endpoint, data=payload, timeout=20)

        else:
            endpoint = url + "sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": post_text,
                "parse_mode": args.parse_mode
            }
            if args.reply_to:
                payload["reply_to_message_id"] = args.reply_to
            if draft_keyboard:
                payload["reply_markup"] = draft_keyboard

            response = requests.post(endpoint, data=payload, timeout=20)
            
        try:
            data = response.json()
        except ValueError:
            print(f"ERROR: Invalid JSON response from Telegram: {response.text}")
            print("CRITICAL: Message may or may not have posted. DO NOT blindly retry!")
            sys.exit(1)
            
        if data.get("ok"):
            # copyMessage returns the message_id differently
            if "message_id" in data["result"]:
                msg_id = data["result"]["message_id"]
            else:
                msg_id = data["result"] # copyMessage returns just { "message_id": X } inside result
                if isinstance(msg_id, dict) and "message_id" in msg_id:
                    msg_id = msg_id["message_id"]
                    
            print(f"SUCCESS: Posted to {args.channel} ({chat_id})")
            print(f"MESSAGE_ID: {msg_id}")
            if args.react:
                ok, error = react_to_message(url, chat_id, msg_id, args.react)
                if ok:
                    print(f"REACTION: {args.react}")
                else:
                    print(f"REACTION_ERROR: {error}")
            # Auto-index in channel history — live channel only
            if args.channel == 'live':
                try:
                    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                    from dedup_db import DedupDB
                    # --title takes priority; fall back to first line of post_text
                    # NOTE: --copy-msg-id path leaves post_text empty — always pass --title
                    # when copying to live, or the post will be silently skipped in the index.
                    title = (getattr(args, 'title', '') or
                             (post_text or "")[:200].split('\n')[0])
                    # Skip indexing rather than write a useless "Post XXXX" stub
                    if title:
                        db = DedupDB()
                        db.record_published(
                            title=title,
                            date=datetime.now().strftime("%Y-%m-%d"),
                            message_id=msg_id,
                            telegram_link=f"https://t.me/genaispot/{msg_id}",
                            source_url=getattr(args, 'source_url', '') or '',
                        )
                except Exception:
                    pass  # Never let indexing failure affect posting

            sys.stderr.write(str(msg_id) + "\n")
            if args.log:
                try:
                    with open(args.log, "w") as lf:
                        lf.write(str(msg_id) + "\n")
                except Exception as e:
                    print(f"WARN: Could not write msg_id to log file {args.log}: {e}")
            sys.exit(0)
        else:
            print(f"API ERROR: {data.get('description')}")
            sys.exit(1)
            
    except requests.exceptions.RequestException as e:
        print(f"NETWORK/TIMEOUT ERROR: {e}")
        print("CRITICAL WARNING: The request may have succeeded despite the timeout.")
        print("DO NOT blindly retry! Check the channel first.")
        sys.exit(1)

if __name__ == "__main__":
    main()
