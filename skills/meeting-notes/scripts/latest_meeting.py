#!/usr/bin/env python3
"""Resolve a meeting transcript from ~/Meetings for the meeting-notes skill.

Default: newest meeting that has a transcript, preferring the processed version.

  latest_meeting.py                 # newest meeting
  latest_meeting.py --list [N]      # list the N most recent meetings (default 10)
  latest_meeting.py --meeting PATH  # a specific meeting dir or .md file
  latest_meeting.py 2026-06-15      # newest meeting whose folder starts with this
  latest_meeting.py --raw           # prefer raw.md over processed*.md

Prints a small header (SOURCE path + stats). The skill reads SOURCE itself.
"""
import argparse
import os
import re
import sys
from pathlib import Path

MEETINGS_DIR = Path(os.environ.get("MEETINGS_DIR", str(Path.home() / "Meetings")))


def meeting_dirs():
    if not MEETINGS_DIR.exists():
        return []
    ds = [d for d in MEETINGS_DIR.iterdir() if d.is_dir()]
    return sorted(ds, key=lambda d: d.stat().st_mtime, reverse=True)


def pick_source(d: Path, prefer_raw=False):
    """Return the best transcript in a meeting dir, or None."""
    procs = sorted(d.glob("processed*.md"))  # processed.md, processed_1.md, ...
    raw = d / "raw.md"
    order = ([raw] + procs) if prefer_raw else (procs + [raw])
    for p in order:
        if p.exists():
            return p
    others = sorted(d.glob("*.md"))
    return others[0] if others else None


def stats(path: Path):
    text = path.read_text(errors="replace")
    speakers = sorted(set(re.findall(r"\b(?:SPEAKER_\d+|UNKNOWN)\b", text)))
    ts = re.findall(r"\[(\d+(?:\.\d+)?)s\]", text)
    dur = float(ts[-1]) if ts else 0.0
    return len(text.splitlines()), len(text), speakers, dur


def fmt_dur(s: float):
    return "{}:{:02d}".format(int(s // 60), int(s % 60))


def print_meeting(src: Path):
    d = src.parent
    n_lines, n_chars, speakers, dur = stats(src)
    kind = "raw" if src.name.startswith("raw") else "processed"
    print("SOURCE: {}".format(src))
    print("MEETING_DIR: {}".format(d))
    print("DATE: {}".format(d.name))
    print("KIND: {}".format(kind))
    print("DURATION: ~{}".format(fmt_dur(dur)))
    print("LINES: {}  CHARS: {}".format(n_lines, n_chars))
    print("SPEAKERS: {}".format(", ".join(speakers) or "none detected"))


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("selector", nargs="?", help="date prefix or path")
    ap.add_argument("--meeting", help="specific meeting dir or .md file")
    ap.add_argument("--list", nargs="?", const=10, type=int, metavar="N")
    ap.add_argument("--raw", action="store_true", help="prefer raw.md")
    args = ap.parse_args()

    dirs = meeting_dirs()

    if args.list is not None:
        if not dirs:
            print("No meetings found in {}".format(MEETINGS_DIR))
            return
        for d in dirs[: args.list]:
            src = pick_source(d, args.raw)
            if not src:
                continue
            kind = "raw" if src.name.startswith("raw") else "processed"
            size = src.stat().st_size // 1024
            print("{}  [{}, {}KB]  {}".format(d.name, kind, size, d))
        return

    target = args.meeting or args.selector
    if target:
        p = Path(target).expanduser()
        if p.is_file():
            print_meeting(p)
            return
        if p.is_dir():
            src = pick_source(p, args.raw)
            if not src:
                sys.exit("No .md transcript in {}".format(p))
            print_meeting(src)
            return
        matches = [d for d in dirs if d.name.startswith(target)]
        if not matches:
            sys.exit("No meeting matching '{}' in {}".format(target, MEETINGS_DIR))
        src = pick_source(matches[0], args.raw)
        if not src:
            sys.exit("No .md transcript in {}".format(matches[0]))
        print_meeting(src)
        return

    # default: newest dir that actually has a transcript
    for d in dirs:
        src = pick_source(d, args.raw)
        if src:
            print_meeting(src)
            return
    sys.exit("No meeting transcript found in {}".format(MEETINGS_DIR))


if __name__ == "__main__":
    main()
