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
# Audio Recording - ROBUST VERSION
# =============================================================================
# Strategy for bulletproof recording:
# 1. Raw PCM format (no header = no corruption possible)
# 2. ffmpeg with graceful 'q' shutdown (more reliable than sox SIGINT)
# 3. Redundant recording: ffmpeg as primary, sox as backup
# 4. Recovery: if primary fails, use backup; raw PCM always recoverable
# =============================================================================

# Recording constants
SAMPLE_RATE = 48000
CHANNELS_MONO = 1
CHANNELS_STEREO = 2
BITS = 32  # s32le format


def _get_raw_audio_duration(raw_path: Path, sample_rate: int, channels: int, bits: int = 32) -> float:
    """Calculate duration of raw PCM file from file size."""
    if not raw_path.exists():
        return 0.0
    bytes_per_sample = bits // 8
    bytes_per_second = sample_rate * channels * bytes_per_sample
    return raw_path.stat().st_size / bytes_per_second


def _convert_raw_to_wav(raw_path: Path, wav_path: Path, sample_rate: int, channels: int) -> bool:
    """Convert raw PCM to WAV. Returns True on success."""
    if not raw_path.exists() or raw_path.stat().st_size == 0:
        return False

    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "s32le", "-ar", str(sample_rate), "-ac", str(channels),
         "-i", str(raw_path), "-c:a", "pcm_s32le", str(wav_path)],
        capture_output=True
    )
    return result.returncode == 0 and wav_path.exists()


def _stop_ffmpeg_gracefully(proc: subprocess.Popen, name: str, timeout: int = 5) -> bool:
    """Stop ffmpeg by sending 'q' to stdin. Returns True if stopped cleanly."""
    if proc.poll() is not None:
        return True  # Already stopped

    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
        proc.wait(timeout=timeout)
        return True
    except Exception:
        pass

    # Fallback: SIGTERM then SIGKILL
    try:
        proc.terminate()
        proc.wait(timeout=3)
        return True
    except subprocess.TimeoutExpired:
        print(f"  ⚠️  {name} didn't stop, killing...")
        proc.kill()
        proc.wait()
        return False


def _stop_sox_gracefully(proc: subprocess.Popen, name: str, timeout: int = 5) -> bool:
    """Stop sox with SIGINT. Returns True if stopped cleanly."""
    import signal

    if proc.poll() is not None:
        return True

    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        print(f"  ⚠️  {name} didn't stop, killing...")
        proc.kill()
        proc.wait()
        return False


def record_audio(output_path: Path, downsample: bool = True) -> Path:
    """Record system audio (BlackHole) + microphone with redundant, crash-proof recording.

    ROBUST RECORDING STRATEGY:
    - Primary: ffmpeg recording to raw PCM (no header = no corruption)
    - Backup: sox recording to WAV (traditional, but can corrupt)
    - Graceful shutdown: ffmpeg via 'q' stdin, sox via SIGINT
    - Recovery: raw PCM is always recoverable, even after kill

    Requires Multi-Output Device setup in Audio MIDI Setup:
    - Create Multi-Output combining speakers + BlackHole 2ch
    - Set as system output
    """
    import time
    import signal

    recording_dir = output_path.parent

    # Primary recordings (raw PCM - crash-proof)
    mic_raw = recording_dir / "mic.raw"
    system_raw = recording_dir / "system.raw"

    # Backup recordings (WAV via sox)
    mic_wav_backup = recording_dir / "mic_backup.wav"
    system_wav_backup = recording_dir / "system_backup.wav"

    # Final WAV files
    mic_wav = recording_dir / "mic.wav"
    system_wav = recording_dir / "system.wav"

    print(f"🎤 Recording... {DIM}[Enter to stop]{RESET}")

    # === PRIMARY: ffmpeg to raw PCM (crash-proof, no headers) ===
    # Mic: mono
    ffmpeg_mic = subprocess.Popen(
        ["ffmpeg", "-y", "-f", "avfoundation", "-i", ":default",
         "-f", "s32le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS_MONO),
         str(mic_raw)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # System audio (BlackHole): stereo
    ffmpeg_system = subprocess.Popen(
        ["ffmpeg", "-y", "-f", "avfoundation", "-i", ":BlackHole 2ch",
         "-f", "s32le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS_STEREO),
         str(system_raw)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # === BACKUP: sox to WAV (traditional, may corrupt on kill) ===
    sox_mic = subprocess.Popen(
        ["sox", "-q", "-t", "coreaudio", "default", str(mic_wav_backup)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    sox_system = subprocess.Popen(
        ["sox", "-q", "-t", "coreaudio", "BlackHole 2ch", str(system_wav_backup)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    recording_start = time.time()

    # Wait for user to stop recording
    try:
        input()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        recording_duration = time.time() - recording_start
        print(f"  ⏹️  Stopping recording ({recording_duration:.0f}s)...")

        # Stop all processes gracefully
        ffmpeg_mic_ok = _stop_ffmpeg_gracefully(ffmpeg_mic, "ffmpeg-mic")
        ffmpeg_system_ok = _stop_ffmpeg_gracefully(ffmpeg_system, "ffmpeg-system")
        sox_mic_ok = _stop_sox_gracefully(sox_mic, "sox-mic")
        sox_system_ok = _stop_sox_gracefully(sox_system, "sox-system")

    # === RECOVERY: Choose best available source for each track ===

    def recover_audio(raw_path: Path, backup_path: Path, final_path: Path,
                      sample_rate: int, channels: int, name: str) -> bool:
        """Try to recover audio from primary (raw) or backup (wav)."""

        # Try primary (raw PCM) first - always recoverable
        raw_duration = _get_raw_audio_duration(raw_path, sample_rate, channels)
        if raw_duration > 1.0:  # At least 1 second
            if _convert_raw_to_wav(raw_path, final_path, sample_rate, channels):
                print(f"  ✓ {name}: recovered from raw ({raw_duration:.0f}s)")
                return True

        # Try backup (sox WAV)
        if backup_path.exists() and backup_path.stat().st_size > 1000:
            # Validate WAV is not corrupt
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(backup_path)],
                capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                backup_duration = float(result.stdout.strip())
                # Check if duration is reasonable (within 50% of expected)
                if backup_duration > recording_duration * 0.5:
                    # Copy backup to final location
                    subprocess.run(["cp", str(backup_path), str(final_path)], check=True)
                    print(f"  ✓ {name}: using backup ({backup_duration:.0f}s)")
                    return True

        print(f"  ✗ {name}: FAILED - no valid recording")
        return False

    # Recover mic and system audio
    mic_ok = recover_audio(mic_raw, mic_wav_backup, mic_wav, SAMPLE_RATE, CHANNELS_MONO, "mic")
    system_ok = recover_audio(system_raw, system_wav_backup, system_wav, SAMPLE_RATE, CHANNELS_STEREO, "system")

    # At minimum, we need system audio (others' voices in the call)
    if not system_ok:
        raise RuntimeError("Recording failed - no system audio recovered. Is BlackHole set up?")

    if not mic_ok:
        print(f"  ⚠️  Mic recording failed - continuing with system audio only")
        # Create silent mic track to allow merge
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=mono",
             "-t", str(recording_duration), "-c:a", "pcm_s32le", str(mic_wav)],
            capture_output=True, check=True
        )

    # === MERGE: Combine mic + system audio ===
    if downsample:
        # Convert to 16kHz mono for Whisper
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(system_wav), "-i", str(mic_wav),
             "-filter_complex", "[0:a]aresample=16000[a0];[1:a]aresample=16000[a1];[a0][a1]amix=inputs=2:duration=longest:normalize=0",
             "-ar", "16000", "-ac", "1", str(output_path)],
            capture_output=True, check=True
        )
    else:
        # Keep native quality
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(system_wav), "-i", str(mic_wav),
             "-filter_complex", "amix=inputs=2:duration=longest:normalize=0",
             str(output_path)],
            capture_output=True, check=True
        )

    # Cleanup raw files
    for raw_file in [mic_raw, system_raw]:
        if raw_file.exists():
            raw_file.unlink()

    # Cleanup backup files
    for backup_file in [mic_wav_backup, system_wav_backup]:
        if backup_file.exists():
            backup_file.unlink()

    # Delete intermediate WAVs (only recording.wav needed for transcription)
    for wav_file in [mic_wav, system_wav]:
        if wav_file.exists():
            wav_file.unlink()

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


def transcribe_audio(audio_path: Path, model: str = "large-v3-turbo", batch_size: int = 12, language: str | None = None) -> dict:
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


async def process_audio(audio_path: Path, meeting_dir: Path, language: str | None = None) -> Path:
    """Full pipeline: transcribe, diarize, generate protocol."""
    import time
    import shutil
    pipeline_start = time.time()

    duration = get_audio_duration(audio_path)
    if duration < MIN_AUDIO_DURATION:
        print(f"⏭️  Audio too short ({duration:.1f}s < {MIN_AUDIO_DURATION:.0f}s), skipping")
        shutil.rmtree(meeting_dir)
        return None

    # large-v3-turbo: multilingual, ~5x faster than large-v3, quality ~equivalent
    model = os.environ.get("WHISPER_MODEL", "large-v3-turbo")

    print(f"⏳ Processing {duration/60:.1f}min audio {DIM}({language or 'auto'}, {model}){RESET}")

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

    # Delete all audio files after successful transcription
    for f in meeting_dir.glob("*.wav"):
        f.unlink()

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
    parser.add_argument("-l", "--lang", "--language", dest="language", default=None,
                       help="Force language (e.g. de, en). Default: auto-detect")
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
