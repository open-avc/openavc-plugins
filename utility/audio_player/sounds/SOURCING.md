# Sound Sourcing — Audio Player Plugin

Every built-in sound shipped with this plugin must be MIT-compatible. Acceptable licenses:

- CC0 1.0 (Public Domain Dedication) — preferred
- MIT
- 0BSD
- Unlicense

**Not acceptable:** CC-BY (attribution requirements complicate distribution), CC-BY-SA, CC-NC, GPL family.

## Process

1. Find a candidate at one of the sources below.
2. Verify the license is on the acceptable list above. Screenshot the license page if it's only displayed at upload time (Freesound license badges are particularly easy to misread).
3. Download the source file, convert to MP3 if needed (LAME `-V 4` is a good balance for short notification sounds).
4. Place the file in this directory under the filename listed in `manifest.json` (e.g., `chime_soft.mp3`).
5. Update the corresponding entry in `manifest.json`:
   - Set `license` to the SPDX-style identifier (`CC0-1.0`, `MIT`, etc.)
   - Set `source_url` to the page where you obtained it
   - Set `license_audit` to `"verified"`
   - Fill in `duration_seconds` (rounded to one decimal)
6. Bump the plugin `version` in `audio_player_plugin.py`, `plugin.json`, and the catalog entry.

The plugin will not present sounds with `license_audit: "pending"` to users (the panel element checks this before playing).

## Recommended Sources

- **freesound.org** — filter by "Creative Commons 0" license. Most reliable source. Link to the user profile in `source_url`.
- **mixkit.co/free-sound-effects/** — Mixkit license is essentially CC0 for sound effects. Always re-verify the current license terms.
- **pixabay.com/sound-effects/** — Pixabay Content License, which is permissive but not exactly CC0 — read the terms before using.
- **Bensound, ZapSplat free tier** — free tiers often carry attribution requirements. Avoid unless you find an explicitly CC0/public-domain track.

## Generating Programmatic Sounds

Some of the planned sounds (especially `silence_1s`, `countdown_beep`, simple sine-tone variations) can be generated rather than sourced. Generated audio falls under your own license — set `license: "MIT"` and `source_url: "generated"` for those entries.

A 1-second silence MP3 can be generated with:

```bash
ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo -t 1 -b:a 64k silence_1s.mp3
```

A short beep:

```bash
ffmpeg -f lavfi -i "sine=frequency=880:duration=0.15" -af "afade=t=in:d=0.01,afade=t=out:st=0.13:d=0.02" -b:a 96k countdown_beep.mp3
```

## Targeted Sound Set

Confirm the final set with Aaron before bulk-sourcing. The current targets in `manifest.json` cover the common AV control use cases (chimes for meeting starts, bells for class changes, alerts for emergencies, feedback tones for button presses, fun sounds for awards, silence as a no-op utility).
