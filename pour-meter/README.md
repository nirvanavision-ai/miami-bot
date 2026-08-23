# The 1942 Pour Meter

A single-file productivity app built around **urgency**. Your day is a bottle:
finishing tasks pours golden liquid in; letting a time block close on unfinished
work drains it out — audibly.

Zero dependencies, zero build step, zero audio assets. Open
[`index.html`](index.html) in a browser and it runs.

## The mechanics

| Mechanic | Rule |
|---|---|
| The bottle | Every task planned today is an equal share of 100%. Complete a task → its share pours in (pour + crystal-clink SFX, gold particle burst). |
| Time blocks | The day is scheduled in blocks (`Deep Work 9–11`, …). A task assigned to a block is **due when the block ends**. |
| The spill | When a block closes with unfinished tasks, their shares are **spilled** — the liquid drains with a comic glug-glug and a cheeky toast from the maître d'. Spilled shares cannot be re-poured: you can still tick the task off, but the pour is gone. |
| Last call | 15 minutes before a block closes with work outstanding, the block pulses ember and Last Call is announced. |
| Anytime tasks | Tasks without a block are due at midnight and settle at the day rollover. |
| Rewards | Fill tiers engraved on the gauge (Reposado 25 · Añejo 50 · Extra Añejo 75 · **1942** 100). Finishing the bottle triggers a fanfare and a starred bottle in the Cellar. |
| The Cellar | The last 7 days shelved as mini bottles with their final fill levels. |
| Streaks | A day ending at ≥ 80% extends your streak; best streak is kept. |
| Rituals | Tasks marked *Daily Ritual* re-appear automatically each morning. |
| Closed-app decay | Deadlines are settled retroactively on load — "While you were away, the angels took 2 pours." |

## Technical notes

- **State**: LocalStorage (`pourmeter.v1`), local-first. Day rollover at local
  midnight archives the bottle to history and resets ritual tasks.
- **Audio**: all SFX (cork pop, pour, clink, glug, fanfare) are synthesized live
  with the Web Audio API — no asset files. Audio unlocks on first user gesture
  per browser autoplay policy; mute toggle persists.
- **Rendering**: a dependency-free raw-WebGL 3D engine. The bottle is lathe
  geometry (a 1942-style tapered cone) with a fresnel/specular glass shader
  and a canvas-generated label texture that spins with the glass; drag to
  orbit with inertia (auto-rotates when idle). The liquid is the same mesh
  shader-clipped by a **world-horizontal plane**, so it stays level while
  the bottle spins and tilts, with spring-damper slosh, waves, GL-point
  bubbles, a cork that pops off during pours, and a projected 2D overlay
  for pour streams, droplets, and confetti. Falls back to a flat 2D bottle
  without WebGL. Pink-studio palette with blue accents; custom cursor,
  magnetic buttons, liquid-fill intro. `prefers-reduced-motion` disables
  ambient animation and the idle spin.
- **No trademark trade dress**: the bottle silhouette and mark are original;
  "1942" is used as a number.

## Roadmap

- IndexedDB (Dexie) behind a storage adapter for sync-readiness
- PWA + notifications ("Your '42 is evaporating, darling")
- Wager mode: pledge a task to a time inside Last Call for an overfill bonus
- Weighted task shares and week-long bottles
