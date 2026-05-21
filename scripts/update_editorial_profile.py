#!/usr/bin/env python3
"""
Editorial Profile Updater
Analyzes approval/rejection patterns and updates the editorial profile.

Usage: python3 update_editorial_profile.py [--dry-run]

Reads: memory/editorial_decisions.md
Updates: memory/editorial_profile.md (Approval History Stats section)

Set OPENCLAW_WORKSPACE env var or defaults to ~/.openclaw/workspace
"""
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from datetime import datetime

WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE",
                                os.path.expanduser("~/.alef-agent/workspace")))
DECISIONS_PATH = WORKSPACE / "memory" / "editorial_decisions.md"
PROFILE_PATH = WORKSPACE / "memory" / "editorial_profile.md"


def parse_decisions():
    if not DECISIONS_PATH.exists():
        return []
    decisions = []
    with open(DECISIONS_PATH) as f:
        for line in f:
            m = re.match(
                r"(.*?)\s*\|\s*(APPROVED|SKIPPED|MANUAL_DRAFT|BOOSTED)\s*\|\s*(.*?)\s*\|\s*(.*?)(?:\s*\|\s*(.*))?$",
                line.strip()
            )
            if m:
                decisions.append({
                    "timestamp": m.group(1),
                    "action": m.group(2),
                    "title": m.group(3),
                    "url": m.group(4),
                    "category": (m.group(5) or "").strip(),
                })
    return decisions


def analyze_patterns(decisions):
    if not decisions:
        return "- No decisions logged yet.\n- Tracking begins when you approve/skip stories.\n"

    category_stats = defaultdict(lambda: {"approved": 0, "skipped": 0, "manual_draft": 0, "boosted": 0})
    total_approved = 0
    total_skipped = 0
    total_manual = 0
    total_boosted = 0

    for d in decisions:
        cat = d["category"] or "uncategorized"
        action = d["action"].lower()
        category_stats[cat][action] += 1
        if action == "approved":
            total_approved += 1
        elif action == "manual_draft":
            total_manual += 1
        elif action == "boosted":
            total_boosted += 1
        else:
            total_skipped += 1

    total = total_approved + total_skipped + total_manual + total_boosted
    approval_rate = (total_approved / max(total_approved + total_skipped, 1) * 100)

    report = f"- Total decisions: {total}\n"
    report += f"- Scanner approved: {total_approved} ({approval_rate:.0f}% of scanner stories)\n"
    report += f"- Scanner skipped: {total_skipped}\n"
    report += f"- Manual drafts: {total_manual} (stories you sought out yourself)\n"
    report += f"- Boosted: {total_boosted} (wrongly-filtered stories flagged for re-scoring)\n"
    report += "- Last updated: " + datetime.now().strftime("%Y-%m-%d") + "\n\n"
    report += "Category breakdown:\n"
    for cat, stats in sorted(category_stats.items(),
                             key=lambda x: -(x[1]["approved"] + x[1]["manual_draft"] + x[1]["boosted"])):
        a = stats["approved"]
        s = stats["skipped"]
        m = stats["manual_draft"]
        b = stats["boosted"]
        scanner_total = a + s
        rate = (a / scanner_total * 100) if scanner_total > 0 else 0
        parts = []
        if a:
            parts.append(f"{a} approved")
        if s:
            parts.append(f"{s} skipped")
        if m:
            parts.append(f"{m} manual")
        if b:
            parts.append(f"{b} boosted")
        if b > 0:
            report += f"  - {cat}: {', '.join(parts)} — boost signal: increase weight for this topic\n"
        elif scanner_total > 0:
            report += f"  - {cat}: {', '.join(parts)} ({rate:.0f}% scanner approval)\n"
        else:
            report += f"  - {cat}: {', '.join(parts)} (manual only)\n"

    # Blind spot analysis
    blind_spots = []
    for cat, stats in category_stats.items():
        m = stats["manual_draft"]
        a = stats["approved"]
        if m > 0 and m > a:
            blind_spots.append((cat, m, a))

    if blind_spots:
        report += "\n## Scanner Blind Spots\n"
        report += "Topics you manually seek out but the scanner rarely catches:\n"
        for cat, manual_count, scanner_count in sorted(blind_spots, key=lambda x: -x[1]):
            report += f"  - **{cat}**: {manual_count} manual draft(s) vs {scanner_count} scanner catch(es). "
            if scanner_count == 0:
                report += "Scanner never found this topic.\n"
            else:
                report += "Consider adding more RSS feeds or keywords.\n"

    return report


def update_profile(analysis, dry_run=False):
    profile = PROFILE_PATH.read_text()
    marker = "## Approval History Stats"
    blind_marker = "## Scanner Blind Spots"

    if blind_marker in profile:
        profile = profile[:profile.index(blind_marker)]

    if marker in profile:
        before = profile[:profile.index(marker)]
        new_section = f"{marker}\n{analysis}"
        updated = before + new_section
    else:
        updated = profile + f"\n{marker}\n{analysis}"

    if dry_run:
        print("DRY RUN -- would update profile with:")
        print(new_section if marker in profile else f"\n{marker}\n{analysis}")
    else:
        PROFILE_PATH.write_text(updated)
        print(f"Updated {PROFILE_PATH}")


def main():
    dry_run = "--dry-run" in sys.argv
    decisions = parse_decisions()
    analysis = analyze_patterns(decisions)
    print(analysis)
    update_profile(analysis, dry_run)


if __name__ == "__main__":
    main()
