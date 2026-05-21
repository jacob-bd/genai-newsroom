#!/usr/bin/env python3
"""
Buffer Push Script
Takes a Telegram-formatted post and pushes it to X/Twitter and LinkedIn via Buffer MCP.

Usage:
  python3 scripts/buffer_push.py --text "post text" [--image /path/to/image_or_url] [--video /path/to/video.mp4] [--draft]
  python3 scripts/buffer_push.py --file /path/to/post.txt [--image /path/to/image_or_url] [--video /path/to/video.mp4] [--draft]

The script:
  1. Strips HTML tags (converts <a href="URL">Text</a> to "Text: URL")
  2. Applies Unicode bold to headline and "Why this matters:"
  3. Appends the CTA footer with Telegram channel link
  4. Posts to both X/Twitter and LinkedIn channels via mcporter
"""

import argparse
import re
import subprocess
import sys
import json
import os
import urllib.request
import urllib.parse

# Buffer Channel IDs
TWITTER_CHANNEL = "69a852783f3b94a12115b160"
LINKEDIN_CHANNEL = "69a8523b3f3b94a12115b077"

# CTA Footer
CTA_SEPARATOR = "───"
CTA_LINE1_RAW = "For more real-time AI news, join our Telegram channel:"
CTA_URL = "https://t.me/genaispot"

# Buffer-specific CTA: no URL in body (link goes in first comment to avoid algo suppression)
BUFFER_CTA_RAW = 'For more AI news, search "GenAISpot" on Telegram'

# Telegram channel username (for post URLs in first comments)
TELEGRAM_CHANNEL_USERNAME = "genaispot"


def to_unicode_bold(text):
    """Convert ASCII text to Unicode Mathematical Bold Sans-Serif."""
    result = []
    for ch in text:
        if 'A' <= ch <= 'Z':
            result.append(chr(0x1D5D4 + ord(ch) - ord('A')))
        elif 'a' <= ch <= 'z':
            result.append(chr(0x1D5EE + ord(ch) - ord('a')))
        elif '0' <= ch <= '9':
            result.append(chr(0x1D7EC + ord(ch) - ord('0')))
        else:
            result.append(ch)
    return ''.join(result)


def strip_html_links(text):
    """Strip hyperlinks from body — keep anchor text, drop URL entirely.
    Inline links hurt reach on LinkedIn/X; URLs belong in first comment only."""
    pattern = r"<a\s+href=['\"][^'\"]+['\"]>([^<]+)</a>"
    return re.sub(pattern, r"\1", text)


def strip_html_tags(text):
    """Remove any remaining HTML tags (bold, italic, etc.)."""
    return re.sub(r'<[^>]+>', '', text)


def strip_read_more_line(text):
    """Remove 'Read more' lines — links go in first comment for LinkedIn/X, not post body."""
    lines = text.split('\n')
    filtered = [l for l in lines if not re.match(r'^\s*[Rr]ead\s+more', l.strip())]
    return '\n'.join(filtered)


def extract_source_lines(text):
    """Extract and convert source lines from Telegram HTML format.
    Returns (text_without_sources, source_lines_raw)
    """
    lines = text.split('\n')
    source_lines = []
    other_lines = []
    
    for line in lines:
        stripped = line.strip()
        # Match "Source: <a href='...'>...</a>" patterns
        if stripped.lower().startswith('source:') or stripped.lower().startswith('sources:'):
            # For source lines, convert to "Name: URL" format
            converted = re.sub(r"<a\s+href=['\"]([^'\"]+)['\"][^>]*>([^<]+)</a>", lambda m: f"{m.group(2)}: {m.group(1)}", stripped)
            # Remove the "Source: " prefix and rebuild
            converted = re.sub(r'^[Ss]ources?:\s*', '', converted)
            # Split multiple sources (separated by | or ,)
            parts = re.split(r'\s*\|\s*', converted)
            for part in parts:
                part = part.strip()
                if part:
                    # "Name (URL)" → "Name: URL" (entity-resolved path)
                    name_url_match = re.match(r'^(.+?)\s+\((https?://[^\)]+)\)', part)
                    if name_url_match:
                        part = f"{name_url_match.group(1).strip()}: {name_url_match.group(2)}"
                    source_lines.append(part)
        else:
            other_lines.append(line)
    
    return '\n'.join(other_lines).rstrip(), source_lines


def apply_bold_headline(text):
    """Apply Unicode bold to the first line (headline) of the post."""
    lines = text.split('\n')
    if not lines:
        return text
    
    # Find the first non-empty line (headline)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped:
            # Preserve leading emoji if present
            emoji_match = re.match(r'^([\U0001F300-\U0001FAFF\u2600-\u27BF\u2700-\u27BF⚡🔥💡🚨🤖💰🎮📱🧪✨🦞]+\s*)', stripped)
            if emoji_match:
                emoji_prefix = emoji_match.group(1)
                rest = stripped[len(emoji_prefix):]
                lines[i] = emoji_prefix + to_unicode_bold(rest)
            else:
                lines[i] = to_unicode_bold(stripped)
            break
    
    return '\n'.join(lines)


def apply_bold_why_matters(text):
    """Apply Unicode bold to 'Why this matters:' line."""
    lines = text.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Match "💡 Why this matters:" with or without emoji
        why_match = re.match(r'^(💡\s*)?[Ww]hy\s+this\s+matters:?\s*$', stripped)
        if why_match:
            emoji = '💡 ' if '💡' in stripped else ''
            lines[i] = emoji + to_unicode_bold('Why this matters:')
    
    return '\n'.join(lines)


def format_for_buffer(telegram_text):
    """
    Transform a Telegram-formatted post into Buffer-ready format for X and LinkedIn.
    """
    # Step 0: Remove "Read more" lines — source link goes in first comment
    telegram_text = strip_read_more_line(telegram_text)

    # Step 1: Extract sources before stripping HTML
    text, source_lines = extract_source_lines(telegram_text)
    
    # Step 1.5: Convert inline <a href> links to "text (URL)" before stripping tags
    text = strip_html_links(text)

    # Step 2: Strip remaining HTML
    text = strip_html_tags(text)
    
    # Step 3: Apply Unicode bold to headline
    text = apply_bold_headline(text)
    
    # Step 4: Apply Unicode bold to "Why this matters:"
    text = apply_bold_why_matters(text)
    
    # Step 5: Clean up any double blank lines
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    
    # Step 7: Append CTA footer (no URL — link goes in first comment to avoid algo suppression)
    cta_bold = to_unicode_bold(BUFFER_CTA_RAW)
    text += f"\n\n{CTA_SEPARATOR}\n🤖 {cta_bold}"
    
    return text, source_lines


def resolve_entities_in_caption(caption, entities):
    """Resolve Telegram caption_entities (text_link type) into a plain-text caption."""
    if not entities or not caption:
        return caption

    caption_utf16 = caption.encode('utf-16-le')
    result = caption
    link_entities = sorted(
        [e for e in entities if e.get('type') == 'text_link'],
        key=lambda e: e['offset'],
        reverse=True
    )

    for entity in link_entities:
        url = entity.get('url', '')
        if not url: continue
        offset = entity['offset']
        length = entity['length']
        byte_start = offset * 2
        byte_end = (offset + length) * 2
        linked_text = caption_utf16[byte_start:byte_end].decode('utf-16-le')
        before = caption_utf16[:byte_start].decode('utf-16-le')
        after = caption_utf16[byte_end:].decode('utf-16-le')
        caption = before + linked_text + after
        caption_utf16 = caption.encode('utf-16-le')

    return caption


def resolve_telegram_message(msg_id, chat_id="-1003300061793"):
    """Extract photo/video URL AND caption from a Telegram channel message."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        return None, None, None

    base = f"https://api.telegram.org/bot{bot_token}"

    try:
        test_chat_id = "-1003889167143" 
        data = urllib.parse.urlencode({
            "chat_id": test_chat_id,
            "from_chat_id": chat_id,
            "message_id": msg_id,
            "disable_notification": "true"
        }).encode()

        req = urllib.request.Request(f"{base}/forwardMessage", data=data)
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())

        if not result.get("ok"):
            print(f"ERROR: forwardMessage to test channel failed: {result}", file=sys.stderr)
            return None, None, None

        fwd_msg = result.get("result", {})
        forwarded_msg_id = fwd_msg.get("message_id")
        
        photos = fwd_msg.get("photo", [])
        video = fwd_msg.get("video") or fwd_msg.get("document")
        caption = fwd_msg.get("caption", "") or fwd_msg.get("text", "")
        entities = fwd_msg.get("caption_entities", []) or fwd_msg.get("entities", [])

        if forwarded_msg_id:
            del_data = urllib.parse.urlencode({"chat_id": test_chat_id, "message_id": forwarded_msg_id}).encode()
            try: urllib.request.urlopen(urllib.request.Request(f"{base}/deleteMessage", data=del_data), timeout=15)
            except Exception: pass

        if caption and entities:
            caption = resolve_entities_in_caption(caption, entities)

        image_local = None
        video_local = None

        if photos:
            largest = max(photos, key=lambda p: p.get("file_size", 0))
            file_id = largest["file_id"]
            req2 = urllib.request.Request(f"{base}/getFile?file_id={file_id}")
            with urllib.request.urlopen(req2, timeout=30) as resp2:
                file_result = json.loads(resp2.read())
            if file_result.get("ok"):
                file_path = file_result["result"]["file_path"]
                dl_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
                ext = file_path.split(".")[-1] if "." in file_path else "jpg"
                tmp_file = f"/tmp/tg_image_{msg_id}.{ext}"
                with urllib.request.urlopen(dl_url, timeout=60) as image_resp, open(tmp_file, "wb") as image_out:
                    image_out.write(image_resp.read())
                image_local = tmp_file

        if video and not image_local:
            file_id = video["file_id"]
            req2 = urllib.request.Request(f"{base}/getFile?file_id={file_id}")
            with urllib.request.urlopen(req2, timeout=30) as resp2:
                file_result = json.loads(resp2.read())
            if file_result.get("ok"):
                file_path = file_result["result"]["file_path"]
                dl_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
                ext = file_path.split(".")[-1] if "." in file_path else "mp4"
                tmp_file = f"/tmp/tg_video_{msg_id}.{ext}"
                with urllib.request.urlopen(dl_url, timeout=120) as video_resp, open(tmp_file, "wb") as video_out:
                    video_out.write(video_resp.read())
                video_local = tmp_file

        return image_local, video_local, caption or None

    except Exception as e:
        print(f"ERROR resolving Telegram message: {e}", file=sys.stderr)
        return None, None, None


def push_to_buffer(text, channel_id, image_url=None, video_url=None, draft=False, mode="shareNow", source_lines=None):
    """Push a post to a Buffer channel via mcporter."""
    args = {
        "channelId": channel_id,
        "schedulingType": "automatic",
        "text": text,
    }

    if source_lines:
        sources_text = "\n".join(source_lines)
        if CTA_SEPARATOR in args["text"]:
            parts = args["text"].split(CTA_SEPARATOR)
            args["text"] = parts[0].strip() + "\n\n" + sources_text + "\n\n" + CTA_SEPARATOR + parts[1]
        else:
            args["text"] += "\n\n" + sources_text
    
    if draft:
        args["saveToDraft"] = True
    else:
        args["mode"] = mode
    
    if image_url or video_url:
        args["assets"] = []
        if image_url:
            args["assets"].append({"image": {"url": image_url, "metadata": {"altText": "News article image"}}})
        if video_url:
            args["assets"].append({"video": {"url": video_url}})

    cmd = ["mcporter", "call", "buffer.create_post", "--args", json.dumps(args)]
    print(f"DEBUG ARGS: {json.dumps(args)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    
    if result.returncode != 0:
        print(f"ERROR posting to channel {channel_id}: {result.stderr}", file=sys.stderr)
        return None
    
    try:
        return json.loads(result.stdout)
    except Exception:
        print(f"ERROR parsing response: {result.stdout}", file=sys.stderr)
        return None


def main():
    parser = argparse.ArgumentParser(description="Push Telegram post to Buffer (X + LinkedIn)")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--text", help="Post text (Telegram HTML format)")
    group.add_argument("--file", help="Path to file containing post text")
    parser.add_argument("--image", help="Image path or public URL")
    parser.add_argument("--video", help="Video path or public URL")
    parser.add_argument("--telegram-msg", help="Telegram message ID to extract media from")
    parser.add_argument("--telegram-chat", default="-1003300061793", help="Telegram chat ID")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--draft", action="store_true", default=True, help="Save as draft (DEFAULT)")
    mode_group.add_argument("--queue", action="store_true", help="Add to Buffer queue")
    mode_group.add_argument("--publish", action="store_true", help="Publish immediately")
    parser.add_argument("--twitter-only", action="store_true", help="Post to X/Twitter only")
    parser.add_argument("--linkedin-only", action="store_true", help="Post to LinkedIn only")
    parser.add_argument("--dry-run", action="store_true", help="Show formatted text without posting")
    parser.add_argument("--allow-no-image", action="store_true", help="Allow posting without an image (bypasses image guard)")
    
    args = parser.parse_args()

    if not args.text and not args.file and not args.telegram_msg:
        parser.error("one of --text, --file, or --telegram-msg is required")

    if args.file:
        with open(args.file, 'r') as f: telegram_text = f.read()
    else:
        telegram_text = args.text or ""
    
    image_url = args.image
    video_url = args.video

    if args.telegram_msg:
        print(f"📸 Resolving media + caption from Telegram msg {args.telegram_msg}...", flush=True)
        tg_image, tg_video, tg_caption = resolve_telegram_message(args.telegram_msg, args.telegram_chat)

        # Only use Telegram media if no manual override was provided (None)
        if image_url is None and tg_image:
            image_url = tg_image
            print(f"   ✅ Got image from Telegram")
        if video_url is None and tg_video:
            video_url = tg_video
            print(f"   ✅ Got video from Telegram")

        if not telegram_text and tg_caption:
            telegram_text = tg_caption
            print(f"   ✅ Got caption from Telegram")

    # If user passed empty string, explicitly set to None to skip upload/attachment
    if not image_url: image_url = None
    if not video_url: video_url = None


    imgur_client_id = os.environ.get("IMGUR_CLIENT_ID", "546c25a59c58ad7")
    for media_type, current_url in [("image", image_url), ("video", video_url)]:
        if current_url and os.path.isfile(current_url):
            print(f"📤 Uploading local {media_type} to imgur...", flush=True)
            try:
                result = subprocess.run(
                    ["curl", "-s", "--max-time", "120",
                     "-H", f"Authorization: Client-ID {imgur_client_id}",
                     "-F", f"image=@{current_url}",
                     "https://api.imgur.com/3/upload"],
                    capture_output=True, text=True, timeout=130
                )
                resp_json = json.loads(result.stdout)
                if resp_json.get("success") and resp_json.get("data", {}).get("link"):
                    new_url = resp_json["data"]["link"]
                    if media_type == "image": image_url = new_url
                    else: video_url = new_url
                    print(f"   ✅ Public {media_type} URL: {new_url}")
                else:
                    print(f"   ⚠️ {media_type} upload failed: {result.stdout.strip()}", file=sys.stderr)
            except Exception as e:
                print(f"   ⚠️ {media_type} upload error: {e}", file=sys.stderr)

    # 🛡️ Image guard: block push without a valid image unless explicitly allowed
    if not args.allow_no_image:
        if not image_url:
            print(file=sys.stderr)
            print("🚨 ERROR: No image available for Buffer push.", file=sys.stderr)
            print("   Buffer posts without images get dramatically less engagement.", file=sys.stderr)
            print("   Provide --image <path> or --allow-no-image to skip this guard.", file=sys.stderr)
            print(file=sys.stderr)
            sys.exit(1)
        if os.path.isfile(image_url):
            # Local file path was provided but doesn't exist as a file
            # (could be a stale path, deleted file, etc.)
            pass  # Will be uploaded to imgur below
        elif not image_url.startswith(('http://', 'https://')):
            print(file=sys.stderr)
            print(f"🚨 ERROR: Image path does not exist: {image_url}", file=sys.stderr)
            print("   Provide --allow-no-image to push without an image.", file=sys.stderr)
            print(file=sys.stderr)
            sys.exit(1)

    buffer_text, source_lines = format_for_buffer(telegram_text)

    
    if args.dry_run:
        print("=" * 60)
        print(buffer_text)
        print("=" * 60)
        print(f"Image: {image_url or 'None'}")
        print(f"Video: {video_url or 'None'}")
        return
    
    channels = []
    if args.twitter_only: channels = [("X/Twitter", TWITTER_CHANNEL)]
    elif args.linkedin_only: channels = [("LinkedIn", LINKEDIN_CHANNEL)]
    else: channels = [("X/Twitter", TWITTER_CHANNEL), ("LinkedIn", LINKEDIN_CHANNEL)]
    
    for name, channel_id in channels:
        print(f"📤 Posting to {name}...", end=" ", flush=True)
        is_draft = not args.publish and not args.queue
        mode = "addToQueue" if args.queue else "shareNow"
        response = push_to_buffer(
            text=buffer_text,
            channel_id=channel_id,
            image_url=image_url,
            video_url=video_url,
            draft=is_draft,
            mode=mode,
            source_lines=source_lines,
        )
        if response and response.get("id"):
            print(f"✅ (id: {response.get('id')})")
        else:
            print(f"❌ FAILED")

if __name__ == "__main__":
    main()
