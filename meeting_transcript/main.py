#!/usr/bin/env python3
"""Meeting transcription with MLX-Whisper, FluidAudio diarization, and Claude smoothing."""

import argparse
import os
import sys
import re
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
load_dotenv()

import anyio

# Lazy imports for heavy dependencies
LightningWhisperMLX = None


def lazy_import_whisper():
    global LightningWhisperMLX
    if LightningWhisperMLX is None:
        from lightning_whisper_mlx import LightningWhisperMLX as LWMLX
        LightningWhisperMLX = LWMLX
    return LightningWhisperMLX


MEETINGS_DIR = Path.home() / "Meetings"

# ANSI colors
DIM = "\033[90m"
RESET = "\033[0m"

SYSTEM_PROMPT = """Transkript-Glättung. Regeln:
- Füllwörter weg (ähm, äh, uhm, ahm, also, quasi, halt, sozusagen)
- Wiederholungen/Satzabbrüche weg
- Unvollständige → vollständige Sätze
- Speaker-Labels behalten

AUSGABE: NUR der bereinigte Text. KEINE Einleitung wie "Hier ist...", KEINE Erklärungen, KEINE Kommentare. Direkt mit dem Text starten."""


# =============================================================================
# Audio Recording (System Audio via scap + Mic via ffmpeg, merged)
# =============================================================================

def record_audio(output_path: Path) -> Path:
    """Record system audio (scap) + microphone (ffmpeg) separately, then merge.

    This avoids the ScreenCaptureKit bug where --enable-microphone causes
    mic audio to be played back through speakers (feedback loop).
    """
    video_path = output_path.parent / "recording.mov"
    mic_path = output_path.parent / "mic.wav"
    system_audio_path = output_path.parent / "system.wav"

    print(f"🎤 Recording... {DIM}[Enter to stop]{RESET}")

    # Start system audio recording (scap WITHOUT --enable-microphone)
    scap_proc = subprocess.Popen(
        ["scap", "--output", str(video_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    # Start microphone recording (ffmpeg via AVFoundation)
    # Using :default for system default input device
    # stdin=DEVNULL prevents ffmpeg from capturing keyboard input
    mic_proc = subprocess.Popen(
        ["ffmpeg", "-y", "-f", "avfoundation", "-i", ":default",
         "-ar", "16000", "-ac", "1", str(mic_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    try:
        input()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        # Stop both recordings
        scap_proc.send_signal(signal.SIGINT)
        mic_proc.send_signal(signal.SIGINT)
        scap_proc.wait(timeout=5)
        mic_proc.wait(timeout=5)

    if not video_path.exists():
        raise RuntimeError("System audio recording failed")
    if not mic_path.exists():
        raise RuntimeError("Microphone recording failed")

    # Extract system audio from video
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-ar", "16000", "-ac", "1", str(system_audio_path)],
        capture_output=True, check=True
    )

    # Merge system audio + mic into single file (both as mono, mixed together)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(system_audio_path), "-i", str(mic_path),
         "-filter_complex", "amix=inputs=2:duration=longest", "-ar", "16000", "-ac", "1", str(output_path)],
        capture_output=True, check=True
    )

    # Cleanup temp files
    video_path.unlink()
    mic_path.unlink()
    system_audio_path.unlink()

    return output_path


# =============================================================================
# Transcription (Lightning Whisper MLX)
# =============================================================================

_whisper_instance = None
_whisper_model = None


def transcribe_audio(audio_path: Path, model: str = "distil-large-v3", batch_size: int = 12, language: str = "en") -> dict:
    """Transcribe audio using Lightning Whisper MLX."""
    global _whisper_instance, _whisper_model

    WhisperClass = lazy_import_whisper()

    # lightning-whisper-mlx uses hardcoded relative paths (./mlx_models/)
    # so we need to chdir to a fixed location for model caching
    model_cache_dir = Path(__file__).parent / "mlx_models"
    model_cache_dir.mkdir(exist_ok=True)
    original_cwd = os.getcwd()
    os.chdir(Path(__file__).parent)

    try:
        if _whisper_instance is None or _whisper_model != model:
            _whisper_instance = WhisperClass(model=model, batch_size=batch_size)
            _whisper_model = model

        return _whisper_instance.transcribe(audio_path=str(audio_path), language=language)
    finally:
        os.chdir(original_cwd)


def get_audio_duration(audio_path: Path) -> float:
    """Get audio duration using ffprobe."""
    import subprocess
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


def convert_to_wav(audio_path: Path) -> Path:
    """Convert audio to WAV format using ffmpeg."""
    if audio_path.suffix.lower() == ".wav":
        return audio_path

    import subprocess
    wav_path = audio_path.parent / f"{audio_path.stem}_converted.wav"
    print(f"🔄 Konvertiere {audio_path.suffix} → WAV...")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(audio_path), "-ar", "16000", "-ac", "1", str(wav_path)],
        capture_output=True, check=True
    )
    return wav_path


# =============================================================================
# Diarization (FluidAudio)
# =============================================================================

FLUIDAUDIO_PATH = "/tmp/FluidAudio/.build/release/fluidaudio"


def diarize_audio(audio_path: Path) -> list[dict]:
    """Run speaker diarization using FluidAudio (Swift/CoreML)."""
    import json
    import tempfile

    wav_path = convert_to_wav(audio_path)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        output_path = f.name

    try:
        subprocess.run(
            [FLUIDAUDIO_PATH, "process", str(wav_path), "--output", output_path],
            capture_output=True, text=True, check=True
        )

        with open(output_path) as f:
            data = json.load(f)

        segments = []
        for seg in data.get("segments", []):
            segments.append({
                "start": seg.get("startTimeSeconds", 0),
                "end": seg.get("endTimeSeconds", 0),
                "speaker": f"SPEAKER_{seg.get('speakerId', '0')}"
            })
        return segments

    finally:
        Path(output_path).unlink(missing_ok=True)


# =============================================================================
# Merge Transcription + Diarization
# =============================================================================

def merge_transcript_with_speakers(transcript: dict, diarization: list[dict]) -> str:
    """Merge whisper transcript with speaker labels."""
    raw_segments = transcript.get("segments", [])
    HOP_LENGTH = 160
    SAMPLE_RATE = 16000

    def normalize_segment(seg):
        if isinstance(seg, list):
            return {
                "start": seg[0] * HOP_LENGTH / SAMPLE_RATE,
                "end": seg[1] * HOP_LENGTH / SAMPLE_RATE,
                "text": seg[2] if len(seg) > 2 else ""
            }
        return seg

    segments = [normalize_segment(s) for s in raw_segments]

    if not diarization:
        return "\n".join(
            f"[{seg['start']:.1f}s] {seg['text'].strip()}"
            for seg in segments
        )

    def get_speaker_at(time: float) -> str:
        for d in diarization:
            if d["start"] <= time <= d["end"]:
                return d["speaker"]
        return "UNKNOWN"

    lines = []
    current_speaker = None
    current_text = []
    current_start = 0

    for segment in segments:
        seg_mid = (segment["start"] + segment["end"]) / 2
        speaker = get_speaker_at(seg_mid)

        if speaker != current_speaker:
            if current_text:
                lines.append(f"[{current_start:.1f}s] {current_speaker}: {' '.join(current_text)}")
            current_speaker = speaker
            current_text = [segment["text"].strip()]
            current_start = segment["start"]
        else:
            current_text.append(segment["text"].strip())

    if current_text:
        lines.append(f"[{current_start:.1f}s] {current_speaker}: {' '.join(current_text)}")

    return "\n".join(lines)


# =============================================================================
# Claude Protocol Generation
# =============================================================================

async def generate_protocol(transcript: str) -> str:
    """Generate smoothed transcript using Claude."""
    from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        max_turns=1,
        model="haiku"
    )

    protocol_parts = []
    async for message in query(prompt=transcript, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    protocol_parts.append(block.text)

    return "\n".join(protocol_parts)


# =============================================================================
# Main Pipeline
# =============================================================================

def create_meeting_dir() -> Path:
    """Create timestamped meeting directory."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    meeting_dir = MEETINGS_DIR / timestamp
    meeting_dir.mkdir(parents=True, exist_ok=True)
    return meeting_dir


def get_next_processed_path(meeting_dir: Path) -> Path:
    """Get next available processed filename (processed.md, processed_1.md, ...)."""
    base = meeting_dir / "processed.md"
    if not base.exists():
        return base

    i = 1
    while True:
        path = meeting_dir / f"processed_{i}.md"
        if not path.exists():
            return path
        i += 1


async def process_audio(audio_path: Path, meeting_dir: Path, language: str = "en") -> Path:
    """Full pipeline: transcribe, diarize, generate protocol."""
    import time
    pipeline_start = time.time()

    # distil-large-v3 is English-only, use large-v3 for other languages
    default_model = "distil-large-v3" if language == "en" else "large-v3"
    model = os.environ.get("WHISPER_MODEL", default_model)
    duration = get_audio_duration(audio_path)

    print(f"⏳ Processing {duration/60:.1f}min audio {DIM}({language}, {model}){RESET}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_transcript = executor.submit(transcribe_audio, audio_path, model, 12, language)
        future_diarization = executor.submit(diarize_audio, audio_path)
        transcript = future_transcript.result()
        diarization = future_diarization.result()

    # Merge and save raw transcript
    merged = merge_transcript_with_speakers(transcript, diarization)
    raw_path = meeting_dir / "raw.md"
    with open(raw_path, "w") as f:
        f.write(merged)

    # Generate smoothed version
    processed = await generate_protocol(merged)
    processed_path = get_next_processed_path(meeting_dir)
    with open(processed_path, "w") as f:
        f.write(processed)

    total_time = time.time() - pipeline_start
    print(f"✅ {processed_path} {DIM}({total_time:.1f}s){RESET}")

    return processed_path


async def reprocess_raw(raw_path: Path) -> Path:
    """Re-process an existing raw transcript."""
    meeting_dir = raw_path.parent

    with open(raw_path) as f:
        raw_text = f.read()

    processed = await generate_protocol(raw_text)
    processed_path = get_next_processed_path(meeting_dir)
    with open(processed_path, "w") as f:
        f.write(processed)
    print(f"✅ {processed_path}")

    return processed_path


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Meeting transcription with speaker diarization")
    parser.add_argument("file", nargs="?", help="Audio/video file or raw.md to process")
    parser.add_argument("-l", "--lang", "--language", dest="language", default="en",
                       choices=["en", "de"], help="Transcription language (default: en)")
    return parser.parse_args()


async def async_main():
    """Main entry point."""
    args = parse_args()
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)

    # Determine mode based on arguments
    if args.file:
        input_path = Path(args.file)

        if not input_path.exists():
            print(f"❌ Datei nicht gefunden: {input_path}")
            sys.exit(1)

        # Re-process mode: raw.md file
        if input_path.name == "raw.md" or input_path.suffix == ".md":
            await reprocess_raw(input_path)

        # Process existing audio/video
        else:
            meeting_dir = create_meeting_dir()
            await process_audio(input_path, meeting_dir, args.language)

    else:
        # Record mode
        meeting_dir = create_meeting_dir()
        audio_path = meeting_dir / "recording.wav"

        record_audio(audio_path)
        await process_audio(audio_path, meeting_dir, args.language)


def main():
    """Entry point."""
    import asyncio
    try:
        anyio.run(async_main)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
