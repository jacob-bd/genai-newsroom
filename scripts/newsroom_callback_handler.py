#!/usr/bin/env python3
"""
Deprecated long-polling callback handler.

Alef Agent now invokes callbacks per event via:
  /Users/jbd/.alef-agent/workspace/callbacks/nr_.py

That path is a symlink to:
  /Users/jbd/.alef-agent/workspace/newsroom/callbacks/nr_.py
"""

import sys

print(
    "ERROR: newsroom_callback_handler.py is deprecated. "
    "Use Alef Agent callback dispatch via workspace/callbacks/nr_.py.",
    file=sys.stderr,
)
sys.exit(1)
