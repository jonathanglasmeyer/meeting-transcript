#!/usr/bin/env python3
"""Meeting transcription with MLX-Whisper, FluidAudio diarization, and Claude smoothing."""

import argparse
import os
import sys
import re
import subprocess
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

import anyio

MEETINGS_DIR = Path.home() / "Meetings"

# ANSI colors
DIM = "\033[90m"
RESET = "\033[0m"

SYSTEM_PROMPT = """Du bist ein Transkript-Glätter. Der User gibt dir ein Meeting-Transkript und du gibst NUR die geglättete Version zurück.

Regeln:
- NUR echte Füllwörter entfernen: ähm, äh, uhm, ahm, hm, mhm
- "also/quasi/halt/sozusagen" NUR entfernen wenn sie als Füllwort verwendet werden, NICHT wenn sie Bedeutung tragen
- Wiederholungen entfernen (z.B. "wir wir" → "wir")
- Abgebrochene Satzanfänge entfernen (z.B. "Das ist, also das war, ich meine das Projekt...")
- ALLE Zeitstempel BEHALTEN (auch [0.0s] am Anfang!)
- Im Zweifel: Text NICHT ändern

SPEAKER-KORREKTUR:
- Wenn innerhalb eines Blocks OFFENSICHTLICH mehrere Sprecher sind (z.B. Frage + Antwort), dann aufteilen
- Verwende konsistente Speaker-Labels (SPEAKER_1, SPEAKER_2, etc.)
- UNKNOWN durch passendes Label ersetzen wenn aus Kontext klar

WICHTIG: Gib NUR das geglättete Transkript aus. KEINE Einleitung, KEINE Erklärungen, KEINE Fragen, KEINE Kommentare, KEINE Markdown-Codeblöcke (```). Beginne direkt mit dem ersten Speaker-Label."""


# =============================================================================
# Audio Recording (System Audio via BlackHole + Mic via ffmpeg, merged)
# =============================================================================

def record_audio(output_path: Path, downsample: bool = True) -> Path:
    """Record system audio (BlackHole) + microphone separately, then merge.

    Requires Multi-Output Device setup in Audio MIDI Setup:
    - Create Multi-Output combining speakers + BlackHole 2ch
    - Set as system output
    This routes audio to speakers (you hear) AND BlackHole (for recording).

    Args:
        downsample: If True, convert to 16kHz mono for Whisper. If False, keep native quality.
    """
    mic_path = output_path.parent / "mic.wav"
    system_audio_path = output_path.parent / "system.wav"

    print(f"🎤 Recording... {DIM}[Enter to stop]{RESET}")

    # Record system audio from BlackHole via sox (ffmpeg has crackling issues)
    # Use DEVNULL for stdout/stderr to prevent pipe buffer blocking
    system_proc = subprocess.Popen(
        ["sox", "-q", "-t", "coreaudio", "BlackHole 2ch", str(system_audio_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Record mic via sox
    mic_proc = subprocess.Popen(
        ["sox", "-q", "-t", "coreaudio", "default", str(mic_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    import time
    recording_start = time.time()

    try:
        input()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        recording_duration = time.time() - recording_start
        # Stop recordings - sox needs SIGINT to write WAV headers properly
        import signal
        for proc in [system_proc, mic_proc]:
            if proc.poll() is None:  # Still running
                proc.send_signal(signal.SIGINT)
        for proc in [system_proc, mic_proc]:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print(f"⚠️  sox didn't stop cleanly, killing...")
                proc.kill()
                proc.wait()

    if not system_audio_path.exists():
        raise RuntimeError("System audio recording failed - is BlackHole set up?")
    if not mic_path.exists():
        raise RuntimeError("Microphone recording failed")

    # Validate WAV files are not corrupt
    for wav_file in [system_audio_path, mic_path]:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(wav_file)],
            capture_output=True, text=True
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(f"Recording failed - {wav_file.name} is corrupt. sox may not have stopped cleanly.")

        # Check duration is reasonable (within 50% of expected)
        wav_duration = float(result.stdout.strip())
        if wav_duration < recording_duration * 0.5 or wav_duration > recording_duration * 1.5:
            raise RuntimeError(
                f"Recording failed - {wav_file.name} duration ({wav_duration:.1f}s) doesn't match "
                f"recording time ({recording_duration:.1f}s). WAV header may be corrupt."
            )

    # Merge system audio + mic
    if downsample:
        # Convert to 16kHz mono for Whisper
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(system_audio_path), "-i", str(mic_path),
             "-filter_complex", "[0:a]aresample=16000[a0];[1:a]aresample=16000[a1];[a0][a1]amix=inputs=2:duration=longest:normalize=0",
             "-ar", "16000", "-ac", "1", str(output_path)],
            capture_output=True, check=True
        )
    else:
        # Keep native quality, mix to mono
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(system_audio_path), "-i", str(mic_path),
             "-filter_complex", "amix=inputs=2:duration=longest:normalize=0",
             str(output_path)],
            capture_output=True, check=True
        )

    # Keep temp files for debugging (TODO: remove later)
    # mic_path.unlink()
    # system_audio_path.unlink()

    return output_path


# =============================================================================
# Transcription (mlx-whisper)
# =============================================================================

# Model name mapping for mlx-whisper (uses HuggingFace model IDs)
MLX_WHISPER_MODELS = {
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "distil-large-v3": "mlx-community/distil-whisper-large-v3",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
}


def transcribe_audio(audio_path: Path, model: str = "distil-large-v3", batch_size: int = 12, language: str = "en") -> dict:
    """Transcribe audio using mlx-whisper with word-level timestamps."""
    import mlx_whisper

    model_id = MLX_WHISPER_MODELS.get(model, model)
    return mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=model_id,
        language=language,
        word_timestamps=True,
    )


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
# Diarization (Senko - CoreML accelerated)
# =============================================================================

_senko_diarizer = None


def get_senko_diarizer():
    """Lazy-load Senko diarizer."""
    global _senko_diarizer
    if _senko_diarizer is None:
        import senko
        _senko_diarizer = senko.Diarizer(device='auto', warmup=True, quiet=True)
    return _senko_diarizer


def diarize_audio(audio_path: Path) -> list[dict]:
    """Run speaker diarization using Senko (CoreML accelerated)."""
    wav_path = convert_to_wav(audio_path)
    diarizer = get_senko_diarizer()

    result = diarizer.diarize(str(wav_path))

    return [
        {"start": seg["start"], "end": seg["end"], "speaker": seg["speaker"]}
        for seg in result.get("merged_segments", [])
    ]


# =============================================================================
# Merge Transcription + Diarization
# =============================================================================

def merge_transcript_with_speakers(transcript: dict, diarization: list[dict]) -> str:
    """Merge whisper transcript with speaker labels using word-level timestamps."""
    segments = transcript.get("segments", [])

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
        # Use word-level timestamps for better speaker assignment
        words = segment.get("words", [])
        if words:
            # Assign speaker based on first word timestamp
            seg_time = words[0].get("start", segment["start"])
        else:
            seg_time = (segment["start"] + segment["end"]) / 2

        speaker = get_speaker_at(seg_time)

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

MAX_CHUNK_SIZE = 15000  # ~4K tokens, safe for Haiku output limit


def split_transcript(transcript: str, max_size: int = MAX_CHUNK_SIZE) -> list[str]:
    """Split transcript into chunks at speaker boundaries."""
    lines = transcript.split("\n")
    chunks = []
    current_chunk = []
    current_size = 0

    for line in lines:
        line_size = len(line) + 1  # +1 for newline
        if current_size + line_size > max_size and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_size = 0
        current_chunk.append(line)
        current_size += line_size

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


async def generate_protocol_chunk(chunk: str, chunk_idx: int) -> tuple[int, str]:
    """Process a single chunk and return (index, result)."""
    from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        max_turns=1,
        model="haiku"
    )

    parts = []
    async for message in query(prompt=chunk, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)

    return (chunk_idx, "\n".join(parts))


async def generate_protocol(transcript: str) -> str:
    """Generate smoothed transcript using Claude, with chunking for large transcripts."""
    import asyncio

    # Small transcripts: process directly
    if len(transcript) <= MAX_CHUNK_SIZE:
        _, result = await generate_protocol_chunk(transcript, 0)
        return result

    # Large transcripts: split and process in parallel
    chunks = split_transcript(transcript)
    print(f"  📦 Split into {len(chunks)} chunks for parallel processing")

    # Process all chunks in parallel
    tasks = [generate_protocol_chunk(chunk, i) for i, chunk in enumerate(chunks)]
    results = await asyncio.gather(*tasks)

    # Sort by index and join
    results.sort(key=lambda x: x[0])
    return "\n".join(result for _, result in results)


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


MIN_AUDIO_DURATION = 3.0  # seconds - minimum for meaningful transcription


async def process_audio(audio_path: Path, meeting_dir: Path, language: str = "en") -> Path:
    """Full pipeline: transcribe, diarize, generate protocol."""
    import time
    import shutil
    pipeline_start = time.time()

    duration = get_audio_duration(audio_path)
    if duration < MIN_AUDIO_DURATION:
        print(f"⏭️  Audio too short ({duration:.1f}s < {MIN_AUDIO_DURATION:.0f}s), skipping")
        shutil.rmtree(meeting_dir)
        return None

    # distil-large-v3 is English-only, use large-v3 for other languages
    default_model = "distil-large-v3" if language == "en" else "large-v3"
    model = os.environ.get("WHISPER_MODEL", default_model)

    print(f"⏳ Processing {duration/60:.1f}min audio {DIM}({language}, {model}){RESET}")

    # Run sequentially - Senko uses numba which conflicts with ThreadPoolExecutor
    t1 = time.time()
    print(f"  📝 Transcribing...", end="", flush=True)
    transcript = transcribe_audio(audio_path, model, 12, language)
    print(f" {DIM}({time.time()-t1:.1f}s){RESET}")

    t2 = time.time()
    print(f"  👥 Diarizing...", end="", flush=True)
    diarization = diarize_audio(audio_path)
    print(f" {DIM}({time.time()-t2:.1f}s){RESET}")

    # Merge and save raw transcript
    merged = merge_transcript_with_speakers(transcript, diarization)
    raw_path = meeting_dir / "raw.md"
    with open(raw_path, "w") as f:
        f.write(merged)
    print(f"  💾 Saved {raw_path.name} {DIM}({len(merged)//1024}KB){RESET}")

    # Generate smoothed version (auto-chunks large transcripts)
    t3 = time.time()
    print(f"  ✨ Smoothing with Claude...", end="", flush=True)
    processed = await generate_protocol(merged)
    processed_path = get_next_processed_path(meeting_dir)
    with open(processed_path, "w") as f:
        f.write(processed)
    print(f" {DIM}({time.time()-t3:.1f}s){RESET}")

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
    parser.add_argument("-r", "--record-only", action="store_true",
                       help="Record audio only, skip transcription")
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

        if args.record_only:
            record_audio(audio_path, downsample=False)
            print(f"✅ {audio_path}")
            subprocess.run(["open", str(audio_path)])
        else:
            record_audio(audio_path, downsample=True)
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
