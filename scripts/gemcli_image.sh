#!/usr/bin/env bash
# gemcli_image.sh — Reliable image generation wrapper.
# Performs a full Chrome cycle before every generation to prevent
# Token Factory session decay after repeated auth cycles.
#
# Usage: gemcli_image.sh "prompt" -o /path/to/output.png [additional gemcli flags]
#
# Examples:
#   gemcli_image.sh "A futuristic AI robot" -o /tmp/out.png
#   gemcli_image.sh "prompt" -o /tmp/out.png --aspect-ratio 16:9

set -euo pipefail

export HOME=/Users/jbd

if [[ $# -lt 1 ]]; then
  echo "Usage: gemcli_image.sh \"prompt\" -o output.png [flags]" >&2
  exit 1
fi

echo "[gemcli_image] Resetting Chrome session..." >&2
gemcli chrome stop 2>/dev/null || true
gemcli chrome start > /dev/null 2>&1
gemcli login > /dev/null 2>&1
echo "[gemcli_image] Chrome ready. Generating image..." >&2

# Concatenate arguments into a single command string to avoid
# shell-word-splitting issues with gemcli's argument parser.
# Build: gemcli image <prompt> -o <path> [flags...]
PROMPT="$1"
shift
bash -c "HOME=/Users/jbd gemcli image '${PROMPT}' $*"
