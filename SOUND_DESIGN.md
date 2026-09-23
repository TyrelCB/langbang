# LangBang Sound Design — Mega Man X Terminal HUD

**Status: GENERATED (2026-09-23).** All 19 cues render from the `audio_sfx`
tool on the all-media MCP server (`spark-ee93:8005`, stable-audio-sfx
backend): fixed seeds, then ffmpeg auto-trim to the audible content (last
frame above render-peak − 35 dB, + 30 ms pad, 15 ms fade-out),
peak-normalization (−3 dB; thinking −12 dB bed), ogg vorbis q3 @ 44.1 kHz
stereo. Bundle ~216 KB (budget < 500 KB). Renders are full-scale at the
backend, so the design's relative levels live in the manifest `gain` field
(`web/sfx.js` applies it). `thinking-loop` is a best-effort loop: full 3 s
with 10 ms edge fades, not a true crossfade seam.

Regeneration is a committed tool: `server/sfxgen.py` holds the machine cue
table (this doc is the human brief) and runs the full path — `audio_sfx` job →
`media_job_wait` → fetch `/files/...` → trim/normalize → ogg, writing
atomically into `web/sounds/`. Two front doors: CONFIG → SOUNDBOARD (▶
audition, ↻ REGEN re-renders one cue with a fresh seed and swaps it into the
live client cache) and `python -m server.sfxgen <slot> [--fresh]` for
deliberate CLI passes. Job durations must be >= 1.0 s (API minimum; short cues
are trimmed down from 1 s renders).

## Style brief
Mega Man X (SNES) menu/UI audio: short, punchy, 16-bit "digital rock".
Think screen-transition zaps, menu blips, the intro's synth stabs.
All one-shots <= 1.2s; `thinking` may be a 2-4s seamless loop.
Sample-accurate starts, tail decay < 80ms (keeps chat snappy).

## Slots (see manifest.json for exact trigger events)
| slot | trigger | brief |
|---|---|---|
| boot | page load | 0.8s rising arpeggio zap, X-ignition feel |
| click | buttons | 60ms square blip, mid pitch |
| hover | thread hover | 40ms soft tick, quieter than click |
| message_sent | user transmit | 150ms upward chirp (sending up the line) |
| thinking | stream start, loops | 3s low pulse loop, quiet, -18dB bed |
| message_received | assistant done | 300ms two-note confirm (perfect 4th up) |
| tool_start | tool call begins | 120ms metallic servo whir-up |
| tool_end | tool result | 120ms servo whir-down + click |
| error | stream/backend error | 400ms descending buzz, X-damage flavor |
| thread_new | new chat | 250ms crisp double blip |
| settings_saved | config saved | 200ms satisfying latch/lock sound |
| game_shoot | X-SIM blaster fires | 80ms bright pew, X-buster flavor |
| game_jump | X-SIM jump / wall-kick | 100ms soft boot-whoosh |
| game_dash | X-SIM dash | 150ms whoosh + afterburner tick |
| game_hurt | X-SIM player damage | 200ms harsh zap, same family as `error` |
| game_kill | X-SIM enemy destroyed | 250ms small explosion crumble |
| game_death | X-SIM player death | 600ms big crumble + descending whine |
| game_clear | X-SIM stage door reached | 800ms victory arpeggio sting |
| game_charge_full | X-SIM charge hits max | 120ms bright shimmer/ping |

The `game_*` slots belong to the built-in X-SIM platformer (web/game.js) —
same rule: imitate the Mega Man X *genre*, never rip game audio.

## Ground rules (still hold for any regeneration)
- Mega Man X is copyrighted — imitate the *genre* (SNES UI chiptune), do not
  rip assets. Prompts describe the sound, never the franchise.
- Keep total bundle < 500 KB; chat cues stay snappy (short audible tail —
  auto-trim enforces this).
