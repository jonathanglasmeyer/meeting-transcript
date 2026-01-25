# meeting-transcript

Local meeting transcription CLI for Apple Silicon. MLX-Whisper + Senko diarization + Claude smoothing.

## Usage

```bash
meeting                    # Record → Enter → process
meeting video.mp4          # Process existing audio/video
meeting ~/Meetings/.../raw.md  # Re-process → processed_1.md
meeting -r                 # Record only (no transcription)
meeting -l de audio.mp3    # German transcription
```

Output: `~/Meetings/<datetime>/raw.md` + `processed.md`

## Architecture

- **Recording**: Crash-proof dual-redundant (BlackHole 2ch + default mic → merged 16kHz mono)
  - Primary: ffmpeg → raw PCM (headerless = survives kill -9)
  - Backup: sox → WAV
  - Auto-recovery picks best source per track
- **Transcription**: mlx-whisper (distil-large-v3 for EN, large-v3 for others, word-level timestamps)
- **Diarization**: Senko (CoreML accelerated, ~30x realtime on Apple Silicon)
- **Smoothing**: Claude Haiku (chunked for large transcripts, up to 15K chars/chunk)

## Setup

1. **BlackHole**: `brew install blackhole-2ch`
2. **Multi-Output Device**: Audio MIDI Setup → Create Multi-Output combining speakers + BlackHole 2ch → Set as system output
3. **sox + ffmpeg**: `brew install sox ffmpeg ffprobe`
4. **Project**: `uv sync` (installs all deps including senko)

## Dev

```bash
uv run meeting <file>      # Run from repo
uv tool install .          # Install globally as `meeting`
```

## Config

- `WHISPER_MODEL`: Override model (default: distil-large-v3 for EN, large-v3 for others)
- `.env`: Not required (Senko caches models locally)
