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
