# LangBang Sound Design — Mega Man X Terminal HUD

**Status: NOT GENERATED.** On 2026-09-14 the single Spark node is VRAM-tight
(Qwen3.8-Next-Flash NVFP4 holds the GPU). Audio generation is deferred.
The frontend is already wired: `web/sfx.js` + `web/sounds/manifest.json` play
any file that appears — just drop assets in and flip their `TODO-` entries.

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

## Generation plan (later)
- Prefer SFX-oriented synth/sampler: chiptune SFX generator or
  Stable Audio / MusicGen short-prompt renders, batch of 11, ogg @ 44.1kHz.
- Keep total bundle < 500KB; loop `thinking` from <= 64KB.
- Mega Man X is copyrighted — imitate the *genre* (SNES UI chiptune), do not
  rip assets.
