---
name: newsroom
description: >
  Full news publishing workflow for Gen AI Spotlight. Use when Jacob says
  "Draft [topic]", "Draft #N", or explicitly invokes /newsroom with a specific
  story to write. Covers research, drafting, image generation, test-channel
  posting, review/edit cycle, live publishing, and Buffer distribution.
type: tool
status: approved
---

# Newsroom — Gen AI Spotlight Publishing Workflow

**Guard:** Confirm Jacob has provided a specific story topic, draft number (e.g. "Draft #3"), or approved story. If missing, ask: "Which story would you like me to draft?"

> **Gate enforcement:** All gates below are hard. Do not advance to the next phase until every item is checked. The only valid stops before test post are: stale story, duplicate detected, or image QA failure (2 attempts exhausted).

**Execution scope:** Draft requests (including edit comments) authorize the full workflow through Test-channel delivery. Execute continuously: research → draft → image → test-channel post. "Approved" triggers live post + Buffer queue push immediately.

**If Jacob says "draft #N" (a number):**
1. Read `~/.alef-agent/workspace/newsroom/data/last_scan_picks.json`
2. Skip `_scan_meta`, find the object where `"rank": N`
3. Use that story's `title`, `url`, and `summary` as the drafting topic
4. If not found, check `last_scan_picks_prev.json`
5. If still not found, ask Jacob to clarify

When a valid story is confirmed, derive a short slug from the story title (e.g. "tiktok-finland", "anthropic-mythos") — used for all file names in this workflow.

Announce: "Newsroom loaded. Checking recency before drafting."

### Gate: Story Confirmed
- [ ] Story topic, URL, or draft #N confirmed from Jacob
- [ ] Slug derived (e.g. `tiktok-finland`)

---

## 🛡️ SHELL SAFETY (The Dollar Sign Rule)

🛑 **CRITICAL TECHNICAL GUARD**: To avoid the common shell expansion bug where dollar signs followed by numbers (e.g., `$400`, `$1.2B`) are stripped or corrupted by the bash shell:

1.  **HIGH RISK STEPS**: This applies to **Initial Test Posts**, **Inline Edits**, and **Manual Buffer Pushes** where raw text is passed to the script.
2.  **THE FIX**: **NEVER** use the `--text` argument for `telegram_post.py`, `telegram_edit.py`, or `buffer_push.py` during these steps. 
3.  **WORKFLOW**: Always write the post content to a temporary file first (e.g., `~/.alef-agent/workspace/newsroom/tmp/<slug>_draft.txt`) and use the `--file` argument.

*Note: Publishing to the Live channel using `--copy-from-chat` is safe as it does not involve shell text expansion.*

---

## File Organization

**All files must use the slug-based naming below. Never write to the workspace root.**

| Purpose | Path |
|---------|------|
| Draft text | `~/.alef-agent/workspace/newsroom/tmp/<slug>_draft.txt` |
| Edited draft | `~/.alef-agent/workspace/newsroom/tmp/<slug>_edit.txt` |
| Live msg_id log | `~/.alef-agent/workspace/newsroom/tmp/<slug>_live_msg_id.txt` |
| Generated image | `~/.alef-agent/workspace/newsroom/media/YYYY-MM-DD_<slug>.png` |

**Cleanup (Phase 3 gate):** After live post is confirmed, delete `~/.alef-agent/workspace/newsroom/tmp/<slug>_draft.txt`, `~/.alef-agent/workspace/newsroom/tmp/<slug>_edit.txt`, `~/.alef-agent/workspace/newsroom/tmp/<slug>_live_msg_id.txt`.

**Never** save files directly to `~/.alef-agent/workspace/` root.

---

## Phase 0: Whiteboard Handoff Check (first)

Read `~/.alef-agent/workspace/newsroom/data/newsroom_whiteboard.md` immediately.

- If a block exists for this story: load its state (draft HTML, image path, test channel msg_id) and resume from the last saved milestone.
- If no matching block: proceed normally. Create one after drafting.

### Gate: Phase 0 Complete
- [ ] Whiteboard read
- [ ] Resuming existing block OR starting fresh (state declared)

---

## Phase 0.5: Validate Recency

1. Use `gsearch-mcp` to confirm publication date of each source. Fallback: Perplexity quick/Sonar + direct source fetch.
2. Older than 14 days → STOP, report: "Story from [DATE], [N] days old. Proceed?"
3. Sources that are stale, historical, or outside the story window → STOP. Do not salvage with old links.
4. Current (≤14 days) → announce "Story confirmed recent. Drafting."

### Gate: Phase 0.5 Complete
- [ ] Publication date verified on each source
- [ ] All sources ≤14 days old (or Jacob approved override)

---

## Phase 0.6: Check Prior Coverage (required)

1. Identify the key company or topic (e.g., "Nvidia", "Karpathy", "AI regulation")
2. Run: `python3 ~/.alef-agent/workspace/newsroom/scripts/dedup_db.py --search "<company or topic>"`
3. Entity alias expansion is automatic ("Nvidia" also catches "Jensen Huang", "DLSS", "Nemotron", etc.)


- 
- 
- 
- **Duplicate gate (runs first — silent pass is the happy path):**
- For batch requests (multiple stories): run this check for ALL stories before spawning any agents
- For a clean batch: proceed directly into research, draft, fact validation, image generation, and Test-channel posting
- For a mixed batch: report only the flagged duplicates, then continue only on the clean stories unless Jacob gives an override

**Callback weaving (only if not a duplicate):**
4. If relevant prior coverage exists within ~14 days (different angle, not a dupe):
   - Note the dates and Telegram links
   - Weave a one-line callback into the draft WITH a hyperlink to the previous post
   - Do not use phrases like "our coverage" or "we covered". Instead, hyperlink the relevant contextual text directly with the previous post's URL.
   - Example HTML: `Nvidia previously announced an <a href="https://t.me/genaispot/NNN">orbital compute push</a> last week.`
   - Linked text must be descriptive (the topic), not "this post" or "here"
5. Only include when it genuinely adds context. Never fabricate prior coverage.

### Gate: Phase 0.6 Complete
- [ ] Dedup DB checked
- [ ] No duplicate found (or Jacob override received)
- [ ] Callback link identified if prior coverage exists within 14 days

---

## Phase 1: Research & Draft

### Step 1: Source Gathering

**Tooling:** Load `gsearch-mcp` skill first. Use `gsearch ai` for story context synthesis and AI Overview. Use `gsearch search --type news` for primary source discovery. Fallback to Perplexity for fact-checking after draft (Step 3).

**If Jacob provides a URL:**
1. Load `gsearch-mcp` skill
2. Use `gsearch fetch <URL>` to extract key facts, claims, quotes, pub date
3. Run `gsearch ai "<topic>" --time week` for story context and corroborating sources
4. Confirm story is real, current, not retracted
5. If the URL is stale or unsupported, STOP

**If Jacob provides a topic (no URL):**
1. Load `gsearch-mcp` skill
2. Run `gsearch ai "<topic>" --time week` for AI-synthesized story context and citations
3. Run `gsearch "<topic>" --type news --time day` for primary and corroborating sources
4. Validate publish dates. Current (≤14 days) → continue. Claim unproven → kill the story.

Continue directly into drafting unless blocked by stale sources or ambiguity.

### Gate: Step 1 Complete
- [ ] gsearch-mcp skill loaded
- [ ] AI Overview (`gsearch ai`) run for story context and citations
- [ ] Primary source found and validated (live, not retracted)
- [ ] Corroborating source(s) found
- [ ] Publish dates confirmed on all sources

### Step 2: Draft in Current Backend

1. Load `jbd-my-voice` — mandatory for every news-related Telegram draft and edit. If unavailable, STOP and notify Jacob.
2. Draft a Telegram-style news post following `jbd-my-voice` Telegram mode exactly
3. All post text uses HTML formatting only: `<b>text</b>`, `<a href="URL">anchor</a>`. No Markdown.
4. Write a background-only image prompt following the Gen AI Spotlight image standard: vivid story-specific scene, not heavy text in image. Also decide the two headline lines (6–8 words total). See context/image-standard.md.

Do not spawn a separate writer sub-agent. Draft in the active backend.

### Gate: Step 2 Complete
- [ ] jbd-my-voice skill loaded
- [ ] Post drafted in HTML only (zero Markdown)
- [ ] Image prompt written (branded, 16:9, story-specific)

### Step 3: Fact Validation & Edit Pass

After the draft is complete:
1. Activate the `perplexity` skill
2. Send the full draft for fact validation
3. Check for: inaccurate claims, exaggerated statements, missing context, outdated info
4. Re-check every linked source — must be current and support the exact claim in the draft
5. If evidence breaks, the story dies. Do not weaken by swapping in links that prove something else.
6. If issues found: edit the draft directly, preserving the jbd-my-voice style
7. Review the image prompt: must match the story, include correct brand elements, be specific not generic

### Gate: Step 3 Complete
- [ ] Perplexity fact check complete
- [ ] Every claim backed by a current source
- [ ] Draft edited if needed, jbd-my-voice style preserved
- [ ] Image prompt updated if story changed during fact check

### Step 4: Image Generation & QA

**TWO MODES — Jacob controls which one per story.**

| Mode | When to use | Command |
|------|-------------|---------|
| **template** | **DEFAULT** — all stories | `news-cards` skill → Puppeteer render (dark-editorial unless specified) |
| **classic** | Only when Jacob says "use classic" or "use Pillow" | gemcli/gptcli background → Pillow overlay |

Default is **dark-editorial template**. Only other option is **classic** (say "use classic" or "use Pillow").

**Category tag taxonomy — always pick the most specific match:**

| Category | Use for |
|----------|---------|
| `AI / AGENTS` | Agentic tools, agent platforms, multi-agent frameworks, autonomous AI |
| `AI / INFRASTRUCTURE` | Compute deals, GPU clusters, data centers, cloud contracts, hardware |
| `AI / RESEARCH` | Papers, model releases, benchmark results, academic findings |
| `AI / POLICY` | Regulation, government, legislation, lawsuits, compliance |
| `AI / BUSINESS` | Funding, acquisitions, IPOs, layoffs, revenue, partnerships |
| `AI / TOOLS` | Dev tools, IDEs, APIs, SDKs, platforms for builders |
| `AI / MODELS` | New LLM/image/video model launches or updates |
| `AI` | Only when no subcategory fits |

**Template mode render command:**
```bash
HOME=/Users/jbd node /Users/jbd/.alef-agent/workspace/newsroom/skills/news-cards/render.mjs \
  --template dark-editorial \
  --category "CATEGORY" \
  --headline "FULL HEADLINE" \
  --highlight "KEYWORD" \
  --subline "Subline text here." \
  --output ~/.alef-agent/workspace/newsroom/media/YYYY-MM-DD_<slug>.png
```
- `--highlight` wraps one keyword in hot pink. Optional.
- Output goes directly to `Media/` — no Pillow overlay step needed. Skip Step 4b.

If using template mode, skip to Gate: Step 4 Complete after the render succeeds.

---

**Classic mode instructions follow (default).**

Images are built in two steps. See `context/image-standard.md` for full spec.

**Prerequisite: Load the appropriate image skill before generating.**

**Primary: gemcli (Gemini)**
Load `gemini-web-skill` first. This skill contains auth recovery protocols, Chrome session management, and troubleshooting steps needed before running gemcli. If the image generation fails with any auth error, follow the gemini-web-skill's Authentication Error Recovery protocol (run `gemcli login`, retry once).

**Fallback: gptcli (ChatGPT) — use when gemcli fails OR Jacob asks for gptcli**
Load `gpt-web-skill` first. Use `gptcli image generate` for background generation. Same prompt template, same QA checklist. See `context/image-standard.md` for full fallback instructions.

**🛑 CRITICAL: NEVER run image generation commands in parallel.** Both gemcli and gptcli share a Chrome/CDP session. Running two `image generate` commands simultaneously causes prompt cross-contamination — one story's image overwrites the other's. Always generate images one at a time, sequentially, and verify each before starting the next.

**Step 4a: Generate background (NO text in prompt)**

Write a story-specific 16:9 background prompt — vivid, photorealistic, not heavy text. Use the background-only template from `context/image-standard.md`.

**With gemcli (primary):**
```bash
HOME=/Users/jbd /Users/jbd/.alef-agent/workspace/newsroom/scripts/gemcli_image.sh "<BACKGROUND PROMPT>" -o ~/.alef-agent/workspace/newsroom/media/YYYY-MM-DD_<slug>_clean.png
```

**With gptcli (fallback):**
```bash
HOME=/Users/jbd gptcli image generate "<BACKGROUND PROMPT>" -o ~/.alef-agent/workspace/newsroom/media/YYYY-MM-DD_<slug>_clean.png
```

Background QA before proceeding:
- [ ] Image generation ran SEQUENTIALLY (no parallel gemcli/gptcli commands)
- [ ] Scene is story-specific (not generic tech)
- [ ] No text or letters visible in image
- [ ] No malformed faces, hands, or objects
- [ ] Vision-verified: image content matches the story (not a different story's image)

If gemcli fails twice: switch to gptcli fallback automatically — do NOT stop and ask Jacob. If both fail → STOP, notify Jacob.

**Step 4b: Stamp headline overlay with Pillow (NEVER overwrite the clean background)**

After background passes QA, ALWAYS save it as a separate clean file. Apply the overlay to a COPY so the clean original is never modified.

Use `_clean.png` suffix for the original background and `<slug>.png` for the overlaid version:

```bash
HOME=/Users/jbd python3 /Users/jbd/.alef-agent/workspace/newsroom/scripts/news_image_overlay.py \
  ~/.alef-agent/workspace/newsroom/media/YYYY-MM-DD_<slug>_clean.png \
  ~/.alef-agent/workspace/newsroom/media/YYYY-MM-DD_<slug>.png \
  'Line 1 Text' 'Line 2 Text'
```

🛑 **DOLLAR SIGN WARNING**: Bash expands `$` in double quotes. If your headline contains a dollar value (e.g., `$200M`, `$2B`), you MUST use single quotes or escape the `$` with `\$`. Example:
```bash
  'Anthropic Commits $200M' 'To Gates Foundation'
```
If the text contains an apostrophe (e.g., "Altman's"), use: `'Altman'\''s $2B'`

If overlay edits are needed later, always regenerate from the clean background (`_clean.png`), never from the already-overlaid file. This avoids double-overlay artifacts.

Headline rules: 6–8 words total, 3–4 words per line, states the news fact, no em dashes.

### Gate: Step 4 Complete
- [ ] Background generated and saved to `workspace/newsroom/media/YYYY-MM-DD_<slug>.png`
- [ ] Background is story-specific with no text artifacts
- [ ] Pillow overlay applied — both headline lines visible, colors correct
- [ ] Text readable at Telegram preview size

### Step 5: Post to Test Channel

🛑 **MANDATORY PRE-FLIGHT CHECKLIST (Must pass all before posting)**
- [ ] Are there blank lines between *every* sentence? (Including list-like content: each component, point, or item gets its own line)
- [ ] Is there zero markdown? (Only `<b>` and `<a href>`)
- [ ] Are there zero numbered or emoji lists?
- [ ] Is the opinion label formatted correctly using `<b>` tags?
- [ ] Was the image generated and passed to the script?

1. Save draft to `~/.alef-agent/workspace/newsroom/tmp/<slug>_draft.txt`
2. Post using the atomic wrapper — this ONE command handles posting, pending.json, whiteboard, and update-group notification:
```bash
HOME=/Users/jbd python3 ~/.alef-agent/workspace/newsroom/scripts/newsroom_post.py \
  --slug "<slug>" \
  --draft ~/.alef-agent/workspace/newsroom/tmp/<slug>_draft.txt \
  --image ~/.alef-agent/workspace/newsroom/media/YYYY-MM-DD_<slug>.png \
  --clean-bg ~/.alef-agent/workspace/newsroom/media/YYYY-MM-DD_<slug>_clean.png \
  --headline1 "LINE ONE TEXT" \
  --headline2 "LINE TWO TEXT" \
  --emoji "<EMOJI>" \
  --source-url "https://..." \
  --title "Full story headline"
```
   - `--emoji` accepts any story-appropriate emoji — the script auto-sanitizes to the nearest Telegram-allowed equivalent. Never hard-code 🔥 unless it truly fits.
   - On success, outputs: `SUCCESS: msg_id=XXXX url=https://t.me/c/3889167143/XXXX`
   - **Do NOT call `telegram_post.py` directly for newsroom drafts** — use this wrapper.
3. The Alef Agent daemon handles button taps automatically via `callbacks/nr_.py`. **Do not manually copy to live unless Jacob says there is a problem with the buttons.**
4. If Jacob requests manual edits via text: use `telegram_edit.py --caption` as before.

### Gate: Step 5 Complete
- [ ] Draft saved to `~/.alef-agent/workspace/newsroom/tmp/<slug>_draft.txt`
- [ ] `newsroom_post.py` ran successfully (outputs `SUCCESS: msg_id=...`)
- [ ] Jacob notified in News Update Group (done automatically by the script)

---

## Phase 2: Review and Edit

**Primary flow:** Jacob taps ✅ Approve, ✏️ Edit, or 🗑 Drop on the test channel post. The callback handler executes automatically — no agent involvement needed.

**Manual edit flow (Jacob types edit instructions in News Update Group):**
1. Apply edits directly to the existing Test draft using `telegram_edit.py`:
```bash
python3 ~/.alef-agent/workspace/newsroom/scripts/telegram_edit.py \
  --channel test \
  --message-id <MSG_ID> \
  --file ~/.alef-agent/workspace/newsroom/tmp/<slug>_edit.txt \
  --caption
```
   The test channel (`--channel test`) **automatically re-attaches the Approve/Edit/Drop keyboard** on every edit. No extra flag needed. To explicitly remove the keyboard, pass `--no-keyboard`.
   All newsroom posts are photo+caption, so `--caption` is always required. Omit only for rare text-only messages.
2. Update `newsroom_pending.json` `draft_path` field to point to the latest edited file after each manual edit.
3. Image/media changes: run Step 4b overlay script from `_clean.png`, then `telegram_edit.py --image` (keyboard auto-preserved for test channel).
4. News Update Group: text-only communication (no files, no images).

**If Jacob says "approved", "post live", or equivalent via text:** Execute Phase 3 manually using `--copy-from-chat` (same as before). The callback handler has not been triggered in this case.

**Custom Headline flow (ForceReply):**
When Jacob taps ✏️ Custom Headline in the Edit menu, the bot sends a ForceReply prompt asking for `Line1 | Line2`. Jacob's reply arrives here as a regular message. When you see a message in this format with a `|` separator AND `newsroom_pending.json` has `"awaiting_custom_headline": true` for any story:
1. Parse: `h1 = text before |`, `h2 = text after |` (trimmed)
2. Validate: neither part starts with lowercase (split-word guard)
3. Re-render using `render.mjs` with the current template from `story["current_template"]` (default: `dark-editorial`)
4. Update pending.json: `headline_line1`, `headline_line2`, `template_headline = h1 + " " + h2`, clear `awaiting_custom_headline`
5. Push updated image via `telegram_edit.py --image`
6. Restore EDIT_KEYBOARD via `telegram_edit.py` (test channel auto-attaches it)

### Gate: Phase 2 Complete (before Phase 3)
- [ ] All requested edits applied (via button or manual telegram_edit.py)
- [ ] newsroom_pending.json updated if draft_path changed
- [ ] Whiteboard updated after each manual edit
- [ ] Explicit approval received (button tap OR text from Jacob)

---

## Phase 3: Publish Live

**Primary flow (button tap):** Jacob taps ✅ Approve on the test post. The daemon executes `callbacks/nr_.py` automatically: posts live, stores the live msg_id, then shows the Buffer keyboard (⚡ Publish Now / 📅 Queue / 📝 Draft). Jacob picks a Buffer destination. The callback handler clears the whiteboard and pending state. **No agent action needed in the primary flow.**

**Manual fallback (Jacob types approval):** Only triggered when Jacob explicitly types "approved", "post live", or equivalent in chat. In that case:

1. Copy from test channel — never post from file:
```bash
python3 ~/.alef-agent/workspace/newsroom/scripts/telegram_post.py \
  --channel live \
  --copy-from-chat -1003889167143 \
  --copy-msg-id <TEST_MSG_ID> \
  --title "<Full Story Headline>" \
  --react <EMOJI> \
  --log ~/.alef-agent/workspace/newsroom/tmp/<slug>_live_msg_id.txt
```
**CRITICAL: You MUST include the `--react <EMOJI>` flag. Run foreground only, never `&`.**

2. Ask Jacob: "Queue, Publish Now, or Draft?" and push to Buffer per his answer (default: --queue for image posts, --draft for video).

3. Log to `newsroom/data/news_log.md`:
   `YYYY-MM-DD HH:MM TZ | POSTED | Full Story Title | msg_id:NNN | https://t.me/genaispot/NNN | https://original-article-url.com`
4. Delete this story's block from the newsroom whiteboard.
5. Clean up temp files: `rm -f ~/.alef-agent/workspace/newsroom/tmp/<slug>_draft.txt ~/.alef-agent/workspace/newsroom/tmp/<slug>_edit.txt ~/.alef-agent/workspace/newsroom/tmp/<slug>_live_msg_id.txt`
6. Confirm to Jacob in News Update Group: "Posted live. [link]"

### Gate: Phase 3 Complete (manual fallback only — button flow is fully automatic)
- [ ] Copied from test channel using --copy-from-chat (NOT posted from file)
- [ ] Live msg_id captured
- [ ] Buffer push completed per Jacob's choice
- [ ] news_log.md updated
- [ ] Whiteboard block deleted
- [ ] Temp files cleaned up
- [ ] Jacob notified in News Update Group

---

## Phase 4: Push to Buffer

**Primary flow (button tap):** After Jacob taps ✅ Approve, the live post goes out and the Buffer keyboard appears automatically. Jacob selects ⚡ Publish Now, 📅 Queue, or 📝 Draft. The callback handler pushes to Buffer using the live msg_id as source, then clears the whiteboard. **No agent action needed.**

For video posts: only 📝 Draft is offered (no video — Jacob uploads manually).

**Manual fallback (typed approval path):** Only when Phase 3 was executed manually. Use the live msg_id as source:

```bash
# Text/image post:
HOME=/Users/jbd python3 ~/.alef-agent/workspace/newsroom/scripts/buffer_push.py \
  --telegram-msg <LIVE_MSG_ID> \
  --image ~/.alef-agent/workspace/newsroom/media/YYYY-MM-DD_<slug>.png \
  --queue

# Video post (draft, no video):
HOME=/Users/jbd python3 ~/.alef-agent/workspace/newsroom/scripts/buffer_push.py \
  --telegram-msg <LIVE_MSG_ID> \
  --draft
```

### Gate: Phase 4 Complete (manual fallback only)
- [ ] Buffer push successful
- [ ] Correct routing flag applied (--queue for text/image, --draft for video)

---

## Writing Contract

`jbd-my-voice` owns all Telegram tone, structure, and formatting. `newsroom` owns workflow, validation, and channel routing. If conflict: `jbd-my-voice` wins.

---

## Image Standard

All images use the Gen AI Spotlight two-step pipeline. See `context/image-standard.md` for full spec.

**Summary:**
1. Generate a clean 16:9 background — story-specific, vivid, not heavy text in image
   - Primary: gemcli via `gemcli_image.sh` wrapper (load `gemini-web-skill` first)
   - Fallback: gptcli `image generate` if gemcli fails OR Jacob requests it (load `gpt-web-skill` first)
   - Auto-switch to fallback after 2 gemcli failures — do not stop to ask
2. Pillow stamps the headline: two lines, Impact 100pt white, hot pink bar (`#F000E7`) on line 1, cyan bar (`#0CD9EA`) on line 2, drop shadow, left-aligned bottom quarter
- 6–8 words total, 3–4 per line, states the fact, no em dashes
- Use logos, brand colors, and recognizable personas when possible
- QA after overlay: both lines readable, colors correct, background story-relevant

---

## Workflow Continuity

🛑 **HARD CONTINUITY RULE**: Do not stop between phases. Once a story enters Phase 1, you MUST execute continuously through Phase 1 -> Phase 2 -> Image Generation -> Test-channel post (Phase 1 Step 5) without pausing for approval. 

The ONLY valid reasons to stop before posting the draft and image to the test channel are:
1. **Stale story**: Source is >14 days old.
2. **Duplicate**: Story was posted in the last 7 days.
3. **Image QA failure**: Failed to generate a usable image after 2 attempts.
4. **Ambiguity**: The requested topic is completely unclear.

If none of these blockers exist, you must deliver the fully formatted text and the generated image to the test channel in one continuous execution flow.4. **Ambiguity**: The requested topic is completely unclear.

If none of these blockers exist, you must deliver the fully formatted text and the generated image to the test channel in one continuous execution flow.
**Link validation (required before test-channel post):**
Before calling `telegram_post.py`, verify every URL in the draft:
- `curl -sI <url>` for direct links, or `gsearch-mcp` / `pwm` for news sources
- 4xx/5xx or redirect to homepage → replace or kill the link, do not post broken URLs
## Gen AI Spotlight Style Rules
- Headlines: punchy, snarky, high-energy — no neutral or corporate phrasing
- No extra blank lines between sections in post body
- Images: logo-branded, story-relevant; use brand logo overlay on generated images