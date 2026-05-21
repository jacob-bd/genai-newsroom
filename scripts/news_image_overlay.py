#!/usr/bin/env python3
"""
news_image_overlay.py — Pillow text overlay for Gen AI Spotlight news images.

Usage (direct args):
  python3 news_image_overlay.py <input> <output> "<line1>" "<line2>" [--color1 HEX] [--color2 HEX]

Usage (text file — avoids shell expansion of $, &, etc):
  python3 news_image_overlay.py <input> <output> --text-file <path> [--color1 HEX] [--color2 HEX]
  File format: line 1 = first text line, line 2 = second text line

Defaults:
  --color1 #F000E7  (hot pink bar, line 1)
  --color2 #0CD9EA  (cyan bar, line 2)

Dollar sign handling:
  Dollar signs are ALWAYS auto-restored. If the shell stripped "$400M" to "400M",
  the script adds it back automatically. Already-present dollar signs are never doubled.
  --auto-dollar flag is kept for backward compatibility but is now a no-op.
"""

import argparse
import os
import re
import sys
from PIL import Image, ImageDraw, ImageFont

FONT_SIZE = 100
LEFT_MARGIN = 30
PAD_X = 18       # horizontal padding inside the highlight bar
PAD_Y = 10       # vertical padding inside the highlight bar
LINE_GAP = 6     # pixels between the two lines
BOTTOM_MARGIN = 35
SHADOW_OFFSET = 4
FONT_PATHS = [
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/Library/Fonts/Impact.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
]


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def draw_line(draw, text, x, y, font, bar_rgb):
    bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    # Adjust for font baseline offset
    ty_offset = -bbox[1]

    # Highlight bar (fully opaque)
    draw.rectangle(
        [x - PAD_X, y, x + tw + PAD_X, y + th + PAD_Y * 2],
        fill=bar_rgb + (255,)
    )

    text_y = y + PAD_Y + ty_offset

    # Drop shadow — draw at multiple small offsets for soft shadow
    for dx, dy in [(SHADOW_OFFSET, SHADOW_OFFSET), (SHADOW_OFFSET+1, SHADOW_OFFSET+1)]:
        draw.text((x + dx, text_y + dy), text, font=font, fill=(0, 0, 0, 180))

    # White text
    draw.text((x, text_y), text, font=font, fill=(255, 255, 255, 255))

    return y + th + PAD_Y * 2  # return bottom of bar


# Consume optional backslash + optional $ so \$1.25B, $1.25B, and 1.25B all become $1.25B
DOLLAR_RE = re.compile(r'\\?\$?(\b\d+(?:\.\d+)?[BMKmbk]\b)')

def fix_dollar(text):
    return DOLLAR_RE.sub(r'$\1', text)

def overlay(input_path, output_path, line1, line2, color1="#F000E7", color2="#0CD9EA"):
    img = Image.open(input_path).convert("RGBA")
    width, height = img.size

    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    font = None
    for fp in FONT_PATHS:
        if os.path.exists(fp):
            font = ImageFont.truetype(fp, FONT_SIZE)
            break
    if font is None:
        raise RuntimeError("Impact font not found. Install msttcorefonts or specify a bold font path.")

    c1 = hex_to_rgb(color1)
    c2 = hex_to_rgb(color2)

    # Calculate total height of both lines to anchor from bottom
    bbox1 = font.getbbox(line1)
    bbox2 = font.getbbox(line2)
    h1 = bbox1[3] - bbox1[1] + PAD_Y * 2
    h2 = bbox2[3] - bbox2[1] + PAD_Y * 2
    total_h = h1 + LINE_GAP + h2

    y1 = height - BOTTOM_MARGIN - total_h
    bottom_of_line1 = draw_line(draw, line1, LEFT_MARGIN, y1, font, c1)
    draw_line(draw, line2, LEFT_MARGIN, bottom_of_line1 + LINE_GAP, font, c2)

    result = Image.alpha_composite(img, layer)
    result.convert("RGB").save(output_path, "PNG")
    print(f"Saved: {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("line1", nargs="?", default=None)
    parser.add_argument("line2", nargs="?", default=None)
    parser.add_argument("--text-file", help="Read two text lines from file (bypasses shell expansion)")
    parser.add_argument("--auto-dollar", action="store_true", help="[No-op: dollar restoration is now always active]")
    parser.add_argument("--color1", default="#F000E7")
    parser.add_argument("--color2", default="#0CD9EA")
    args = parser.parse_args()

    if args.text_file:
        with open(args.text_file) as f:
            lines = [l.rstrip("\n") for l in f.readlines()]
        if len(lines) < 2:
            sys.exit("Error: --text-file must contain at least 2 lines")
        line1, line2 = lines[0], lines[1]
    else:
        if args.line1 is None or args.line2 is None:
            sys.exit("Error: provide line1/line2 positional args or --text-file")
        line1, line2 = args.line1, args.line2

    line1 = fix_dollar(line1)
    line2 = fix_dollar(line2)

    overlay(args.input, args.output, line1, line2, args.color1, args.color2)
