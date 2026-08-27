---
paths:
  - "src/audio/**/*.ts"
  - "src/hooks/useAudio.ts"
---

# Procedural Audio Synthesizer Guidelines

- **Web Audio API Only:** Do not install external sound libraries like Howler.js.
- **Synthesis:** Synthesize the "glug glug" draining penalty, "cork pop", and "crystal clink" programmatically using `AudioContext` oscillators and buffer manipulation.
- **Dynamic Reactivity:** Always expose parameters for pitch shifting and panning so the sound can react dynamically to the app's state (e.g., pitching down the drain sound based on the number of overdue tasks).
- **Reference implementation:** the synth recipes in `pour-meter/index.html` (`sfxPour`, `sfxGlug`, `sfxCork`, `sfxClink`, `sfxFanfare`) are proven — port them into typed modules rather than reinventing the envelopes.
