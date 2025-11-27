# meeting-transcript

Local meeting transcription CLI for Apple Silicon. MLX-Whisper + FluidAudio diarization + Claude smoothing.

## Usage

```bash
meeting                    # Record → Ctrl+C → confirm → process
meeting video.mp4          # Process existing audio/video
meeting ~/Meetings/.../raw.md  # Re-process → processed_1.md
```

Output: `~/Meetings/<datetime>/raw.md` + `processed.md`

## Architecture

- **Recording**: SwiftCapture (ScreenCaptureKit) - System Audio + Mikrofon
- **Transcription**: lightning-whisper-mlx (distil-large-v3, batch_size=12)
- **Diarization**: FluidAudio Swift CLI (`/tmp/FluidAudio/.build/release/fluidaudio`)
- **Smoothing**: Claude Haiku via claude-agent-sdk

Whisper + FluidAudio run parallel (ThreadPoolExecutor). FluidAudio ~200x realtime; Whisper ~25s for 37min.

## Dependencies

- SwiftCapture: `brew tap GlennWong/swiftcapture && brew install swiftcapture`
- FluidAudio must be built: `cd /tmp/FluidAudio && swift build -c release`
- Requires ffmpeg/ffprobe for audio conversion
- Rust required for tiktoken build (lightning-whisper-mlx dep)
- Screen Recording + Microphone permissions required

## Dev Commands

```bash
uv sync                    # Install deps
uv run meeting <file>      # Run locally
uv tool install .          # Install globally as `meeting`
```

## Key Files

- `main.py:252`: Claude prompt for smoothing
- `main.py:149`: FluidAudio binary path
- `main.py:33`: Output directory (~/Meetings)
