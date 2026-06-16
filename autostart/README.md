# MeetingBar — auto-start recording

Notion/Granola-style menu-bar app. When you join a Teams/Zoom/Slack call, a
native alert offers to start the `meeting` recorder. While recording, a 🎙 icon
sits in the menu bar — click it to stop. Stopping also kicks off transcription.
No Terminal involved.

## How it works

```
MeetingBar (one AppKit app)
  ├─ Core Audio listener  → mic goes live (debounced)
  ├─ policy               → meeting app present? not already recording? past cooldown?
  ├─ NSAlert              → "Aufnehmen" / "Ignorieren"  (real mic icon, not a folder)
  └─ Recorder             → spawns `meeting` with a stdin pipe;
                            menu-bar "Stoppen" writes a newline → clean stop → transcribe
```

- **Detection** reads only Core Audio device IO state
  (`kAudioDevicePropertyDeviceIsRunningSomewhere`) — **no microphone permission
  needed** just to detect. Virtual loopbacks (BlackHole) and Multi-Output devices
  are ignored so playing system audio isn't mistaken for a meeting; Aggregate
  mics (e.g. *Hollyland*) are watched.
- **Stop** writes `\n` to the recorder's stdin, releasing the `input()` that
  `meeting` blocks on ([../meeting_transcript/main.py](../meeting_transcript/main.py)) —
  the same stop path as manual use, minus the visible Terminal.
- Only a **rising edge** triggers a prompt; an already-live mic at launch is
  adopted silently (launching mid-call won't pop a dialog).

## Install

```bash
./install.sh              # build + install LaunchAgent + start
./install.sh --uninstall  # stop + remove
```

Look for **🎙 in the menu bar**. The first time you click *Aufnehmen*, macOS asks
to grant microphone access to the recorder — approve once.

## Config — `config.json` (picked up on next detection, no recompile)

| key | meaning |
|---|---|
| `meeting_bundle_ids` | apps whose presence + a live mic = a meeting |
| `meeting_command` | command run to record (default: full transcribe pipeline) |
| `record_only` | `true` → `meeting -r` (capture only, no transcription) |
| `whisper_model` | Whisper model passed via `WHISPER_MODEL` (default `large-v3-turbo`; language is auto-detected) |
| `cooldown_seconds` | min gap between prompts (suppresses re-prompt for same call) |
| `require_meeting_app` | if `true`, never prompt on bare mic activity |
| `ignore_device_substrings` | device-name substrings to never watch (loopbacks) |

Find an app's bundle ID: `osascript -e 'id of app "Microsoft Teams"'`.

## Why stop is manual (by design)

Once the recorder runs, *it* holds the mic — so "mic went idle" can't mean
"meeting ended". Reliable headless auto-stop needs a Teams/Zoom call-ended signal
independent of our own capture (a v2 item). For now you stop with one click.

## Files

| file | role |
|---|---|
| `MeetingBar.swift` | the app (detection + policy + recorder + menu bar) |
| `config.json` | policy config |
| `install.sh` | build + LaunchAgent management |
| `micwatch.swift` | optional standalone debug tool — prints `RUNNING`/`STOPPED` and the watched devices; run `./micwatch` to sanity-check detection without the app |

## Troubleshooting

- **App log**: `~/.meeting-autostart/launchd.err.log` (NSLog: watched devices,
  ignored events). **Recorder log**: `~/.meeting-autostart/recording.log`.
- **No prompt on a call**: confirm the app's bundle ID is in `meeting_bundle_ids`.
- **Prompt never reappears**: still inside `cooldown_seconds`, or a recording is
  still running.
- **Debug detection**: `swiftc -O micwatch.swift -o micwatch && ./micwatch`, then
  start/stop a call and watch the output.
