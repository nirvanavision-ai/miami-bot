# Project: The Don Julio 1942 Pour Meter

## Tech Stack
- Frontend: Next.js (App Router, Strict TypeScript)
- Styling: Tailwind CSS
- 3D/Graphics: Three.js / WebGL
- Animation Orchestration: GSAP + Framer Motion
- Audio: Native Web Audio API
- Data Persistence: IndexedDB (local-first) -> Supabase

## Luxury UI/UX Conventions
- **Visual Aesthetic:** DO NOT use plain white backgrounds or standard generic boxes. Default to an ultra-luxe dark mode (deep obsidian, amber gold, metallic finishes).
- **Layouts:** Use high-end bento-box grid layouts with heavy, moody shadows and frosted glass filters.
- **Component Rules:** Default to React Server Components for performance. Strictly isolate all Three.js, GSAP, and Web Audio API logic into Client Components (`"use client"`).
- **Animation Routing:** Use Framer Motion for simple UI spring physics (modals, task cards). Use GSAP exclusively for sequencing complex timelines (e.g., the exact sequence of card snap -> cork pop -> pour -> crystal clink).

## Repository Layout
- `miami_bot/`, `tests/`, `main.py`, `config.yaml`: an unrelated Python rental-listing pipeline. Do not modify it while building the Pour Meter; its CI (`ruff check .` + pytest) must stay green.
- `pour-meter/index.html`: the current zero-dependency prototype (raw WebGL 3D bottle, Web Audio SFX, LocalStorage). Treat it as the reference implementation for mechanics, shaders, and SFX when scaffolding the Next.js app.
- The Next.js app, when scaffolded, lives in its own directory (e.g. `app/` or `pour-meter-next/`) — never at the repo root next to the Python package.
