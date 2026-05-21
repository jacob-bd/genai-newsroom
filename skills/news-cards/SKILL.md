---
name: news-cards
description: "Render scroll-stopping news card images from HTML templates using Puppeteer. Three rotating color themes (dark-editorial, hot-pink, cyan). Audited by Impeccable before render. Replaces gemcli image generation for newsroom posts."
user-invocable: false
---

# News Cards — HTML Image Pipeline

Renders news post images from parameterized HTML templates. No AI image generation needed.

## Templates

Three rotating themes in `templates/`:

| Template | File | Background | Best for |
|----------|------|-----------|----------|
| Dark Editorial | `dark-editorial.html` | Dark purple gradient | Funding, breaking news, major announcements |
| Hot Pink Split | `hot-pink-split.html` | Pink top + black data panel | Regulatory, policy, stats-heavy stories |
| Cyan Drenched | `cyan-drenched.html` | Full cyan surface | Open source, model releases, community |

## Rotation

Rotate templates per story so no two consecutive posts look the same:
```
Story 1 → dark-editorial
Story 2 → hot-pink-split
Story 3 → cyan-drenched
Story 4 → dark-editorial
...
```

## Render Command

```bash
HOME=/Users/jbd node /Users/jbd/.alef-agent/workspace/skills/news-cards/render.mjs \
  --template dark-editorial \
  --category "Breaking / Funding" \
  --headline "OpenAI Closes \$9 Billion Round" \
  --highlight "\$9 Billion" \
  --subline "Largest private funding in history. SoftBank leads." \
  --output /Users/jbd/.alef-agent/workspace/Media/2026-05-20_story.png
```

### Parameters

| Flag | Required | Description |
|------|----------|-------------|
| `--template` | Yes | Template name: `dark-editorial`, `hot-pink-split`, `cyan-drenched` |
| `--category` | Yes | Top label (e.g., "Breaking / Funding", "Open Source") |
| `--headline` | Yes | Main headline text (uppercase applied automatically) |
| `--highlight` | No | Word(s) in headline to highlight in pink |
| `--subline` | Yes | Supporting text below headline |
| `--output` | Yes | Output PNG path |
| `--source` | No | Source name (default: "Gen AI Spotlight") |
| `--stat-labels` | No | Hot-pink only: comma-separated stat labels |
| `--stat-values` | No | Hot-pink only: comma-separated stat values |

### Output

- 1280x720 PNG (16:9, matches newsroom standard)
- Audited by `npx impeccable detect` before render (warnings printed, not blocking)

## QA Checklist

Before posting:
- [ ] Headline readable at Telegram preview size
- [ ] Subline readable without squinting
- [ ] No text overlap or clipping
- [ ] Color theme rotated from previous post

## Adding New Templates

1. Create HTML file in `templates/` (1280x720 viewport)
2. Use `{{CATEGORY}}`, `{{HEADLINE}}`, `{{SUBLINE}}`, `{{SOURCE}}` placeholders
3. For highlight: wrap target word in `<span class="highlight">{{HIGHLIGHT}}</span>`
4. Test: `npx impeccable detect templates/your-template.html`
5. Add to rotation table above
