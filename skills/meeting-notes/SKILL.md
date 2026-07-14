---
name: meeting-notes
description: Clean up the latest meeting transcript from ~/Meetings (produced by the `meeting` CLI) and integrate it into the current project. Reads the newest raw.md/processed.md, strips Whisper artifacts, fixes speaker labels and grammar, writes a polished note into the project's meetings/ folder, then reads the relevant tracker tickets and proposes — locally in chat, without auto-writing — the changes/additions the meeting implies (inconsistencies, missing details), plus the user's own next steps, for the user to incorporate. Use when the user says things like "process/import/clean up the last meeting", "meeting notes into this project", "prettify the meeting transcript", "what was decided in the last meeting", or invokes /meeting-notes.
---

# Meeting Notes

Turn a raw meeting transcript into a clean, project-filed note and propose how its
content should update the project's knowledge.

## Workflow

### 1. Resolve the source transcript

```bash
python3 ~/.claude/skills/meeting-notes/scripts/latest_meeting.py
```

Prints `SOURCE` (the transcript path) plus stats. Default = newest meeting that has
a transcript, preferring `processed*.md` (Claude-smoothed) over `raw.md`.

- Specific meeting: `--meeting <dir|file>` · by date: `latest_meeting.py 2026-06-15` · list recent: `--list`
- `--raw` to use `raw.md` (maximal fidelity if `processed` looks over-smoothed)

Read the `SOURCE` file. Honor the user if they named a different meeting.

### 2. Prettify

The transcript is lines of `[123.4s] SPEAKER_01: text`. Clean it:

- **Scope to what's relevant to the current project.** A meeting is not all signal:
  check-in/icebreaker rounds, personal-favorite chatter, tooling/onboarding
  side-conversations, and off-topic tangents carry no project content. Read the
  project first (CLAUDE.md, docs, scope) so you know what "relevant" means *here*,
  then keep only the topics that are plausibly applicable to it and drop the rest.
  Name what you dropped in one line (e.g. "(Check-in-Runde + AWS-Onboarding-Side-Talk
  ausgelassen)") so the cut is transparent, not silent. If almost nothing is relevant,
  say so rather than padding the note. Speaker mapping only matters for the kept
  topics — don't chase names for content you're dropping.
- **Drop Whisper hallucinations.** Standalone fillers that cluster in silence/pauses
  carry no meaning — remove aggressively: repeated `"Vielen Dank."`, `"Untertitel…"`,
  `"Amara.org"`, `"Copyright …"`, lone `"Ja." / "Mhm."` spam, the long "Vielen Dank"
  runs at the very start.
- **Merge** consecutive turns by the same speaker.
- **Repair, don't invent.** Fix grammar and ASR mishears using meeting + project
  context (Whisper garbles domain terms — correct them from the project's vocabulary).
  Never add content that wasn't said.
- **Speakers.** Map `SPEAKER_NN` / `UNKNOWN` to real names when inferable from the
  discussion, the project, or the calendar (the `calendar` skill can list that day's
  meeting + attendees). If unclear, ask the user for a quick mapping
  (`SPEAKER_01=?, SPEAKER_02=?`) instead of guessing; keep labels if they decline.
- **Timestamps.** Keep coarse `mm:ss` anchors at topic boundaries; drop per-line seconds.
- **Language.** Keep the meeting's language (usually German). Do not translate.

### 3. Write the note into the current project

Pick the target directory by existing convention — first match wins:
`meetings/` → `docs/meetings/` → `notes/meetings/` → `docs/` → `notes/`.
If none exist, create `meetings/` at the project root.

Filename: `YYYY-MM-DD-<topic-slug>.md` (date from the meeting folder, slug from the main
topic). Never overwrite — suffix `-2`, `-3`, …

Structure (omit empty sections):

```markdown
---
date: 2026-06-15
source: ~/Meetings/2026-06-15_141509/processed.md
participants: [Name1, Name2]
duration: ~50min
---

# <Meeting title>

## TL;DR
2–4 sentences: what this was about and the upshot.

## Entscheidungen
- …

## Action Items
- [ ] <owner> — <task> (<due, if stated>)

## Offene Fragen
- …

## Diskussion
<readable, speaker-attributed narrative grouped by topic, with mm:ss anchors>
```

Report the written path.

### 4. Read the relevant tickets → propose changes locally (do **not** auto-write)

The note already holds the full content — do **not** mirror decisions/discussion back
into `CLAUDE.md`, specs, SoR docs, or a parallel local TODO. No info-doubling.

The integration step is **read + propose, never auto-update the tracker.** The goal is
to surface where the meeting **contradicts, supersedes, or fills a gap** in what the
tickets currently say, and hand the user crisp proposals they incorporate themselves.

1. **Identify + actually read the touched tickets.** From the project (CLAUDE.md, docs)
   find the tracker and the existing tickets the meeting touched, then **fetch and read
   their current content**. You cannot propose good additions blind — read first so the
   proposals target real inconsistencies and genuinely missing details, not duplicates.
2. **Per-ticket proposals, locally in chat.** For each ticket, list the concrete
   changes/additions the meeting implies (new/changed ACs, corrected definitions, scope
   changes) — phrased as verifiable done-criteria, grouped by ticket, each tied to what
   in the ticket it changes or adds. Follow the project's own tracker-writing skill /
   conventions for phrasing (e.g. an SDP/Jira skill) if one exists. If an outcome has no
   matching ticket, flag that a new ticket may be needed — do **not** invent a local
   tracker or doc section for it.
3. **The user's own next steps, beyond the tickets.** Hands-on work that isn't (and
   shouldn't be) ticket-tracked — data work, a draft, a person to ping. A plain checklist.

**Output is proposals only — no tracker write.** Present the per-ticket changes + the
personal list in chat; the user reviews and edits the tickets themselves. No Jira
round-trip from this skill (only do that if the user later explicitly asks you to apply).

## Edge cases

- **Not in a project** (empty/non-repo cwd): still write the prettified note (to
  `meetings/` or cwd), then skip step 4 and say why.
- **Mostly artifacts** (little real content after cleanup): say so plainly; don't
  fabricate substance to fill the template.
- **Large transcript**: process in sections, but produce a single output note.
- **Already imported** (a note for this meeting exists): offer to update it instead of
  creating a duplicate.
