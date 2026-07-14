# meeting-transcript

Local meeting transcription CLI for Apple Silicon. Records system audio + mic, transcribes with MLX-Whisper, diarizes speakers with Senko, and smooths with Claude.

**Input:** Meeting recording or audio file
**Output:** Speaker-labeled transcript with timestamps + Claude-smoothed version

## Quick Start

```bash
# Record a meeting
meeting

# Record with German transcription
meeting -l de

# Process existing audio
meeting video.mp4
meeting audio.wav

# Process with German transcription
meeting -l de audio.mp3

# Re-smooth existing transcript
meeting ~/Meetings/2026-01-22_110000/raw.md
```

## Install

```bash
git clone https://github.com/jonathanglasmeyer/meeting-transcript.git
cd meeting-transcript
uv sync
uv tool install .
```

### Dependencies

**macOS only** — requires:
- BlackHole 2ch: `brew install blackhole-2ch`
- sox + ffmpeg: `brew install sox ffmpeg`
- Audio MIDI Setup: Create Multi-Output combining speakers + BlackHole

See [CLAUDE.md](CLAUDE.md) for detailed setup.

## Usage

| Command | Effect |
|---------|--------|
| `meeting` | Record → transcribe → diarize → smooth |
| `meeting file.mp4` | Process audio/video file |
| `meeting path/raw.md` | Re-smooth transcript |
| `meeting -l de file.mp3` | German transcription |
| `meeting -r` | Record only, no transcription |

**Output:** `~/Meetings/<YYYY-MM-DD_HHMMSS>/raw.md` (raw) + `processed.md` (smoothed)

## Auto-start (menu bar)

Don't want to remember to hit `meeting` before every call? [`autostart/`](autostart/) ships
**MeetingBar** — a native menu-bar app that notices when you join a Teams/Zoom/Slack call and
offers to start the recorder. A 🎙 in the menu bar means it's recording; click to stop, and
transcription kicks off automatically. No Terminal involved.

```bash
cd autostart && ./install.sh
```

Detection reads only Core Audio device state, so **no microphone permission is needed just to
detect** a call. Which apps count as a meeting, which Whisper model to use, and the re-prompt
cooldown are all configurable in [`autostart/config.json`](autostart/config.json) without a
recompile.

See [autostart/README.md](autostart/README.md) for how it works, the full config table, and
troubleshooting.

## Recommended: the `/meeting-notes` Claude Code skill

The CLI gets you an accurate transcript — but it's still a transcript: Whisper artifacts,
`SPEAKER_01` labels, no structure. [`skills/meeting-notes/`](skills/meeting-notes/) is a
[Claude Code](https://claude.com/claude-code) skill that turns it into something you'd actually
file: it picks up the newest meeting, strips the hallucinated filler, maps speakers to real
names, keeps only what's relevant to the project you're in, and writes a note with TL;DR,
decisions, action items and open questions into the project's `meetings/` folder. It then reads
the tickets the meeting touched and proposes — in chat, without writing anything — what the
meeting implies for them.

```bash
cp -r skills/meeting-notes ~/.claude/skills/
```

Then say *"process the last meeting"* (or `/meeting-notes`) in a project.

## Architecture

- **Recording** — sox captures system audio (BlackHole) + microphone, mixed to 16kHz mono
- **Transcription** — mlx-whisper (distil-large-v3) with word-level timestamps
- **Diarization** — Senko (CoreML) for speaker attribution, ~30x realtime
- **Smoothing** — Claude Haiku processes transcript chunks, removes filler words, fixes broken sentences

## Features

- ✅ Parallel recording (system + mic, with validation)
- ✅ Word-level timestamps for accurate speaker assignment
- ✅ Multi-language support (EN, DE)
- ✅ Large transcript chunking (auto-splits >15K chars for parallel Claude processing)
- ✅ Zero config (models cached locally)

## Development

```bash
uv run meeting <file>      # Run from repo
uv run pytest              # Run tests (if added)
```

## License

MIT
