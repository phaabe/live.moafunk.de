---
name: vue-patterns
description: Use whenever writing or reviewing Vue code in this repo's admin SPA (frontend/src/admin). Covers Vue 3 Composition API, `<script setup lang="ts">`, Pinia stores, Vue Router, composables, Vite, and common anti-patterns.
---

# Vue Patterns Skill (live.moafunk.de admin SPA)

The admin dashboard lives in `frontend/src/admin/` — **Vue 3 + Vite + Pinia + Vue Router**, TypeScript strict.
The public pages are vanilla TS (no framework). Apply this skill to the admin SPA.

## Defaults

- **`<script setup lang="ts">`** for every component. Composition API only — no Options API in new code.
- TypeScript `strict`. Type props with `defineProps<{ ... }>()` (type-based), emits with `defineEmits<{ ... }>()`. Never `any`.
- `ref` for primitives, `reactive` only for grouped object state; prefer `ref` + `.value` for predictability.
- `computed` for derived state — never duplicate state you can derive.
- One component = one responsibility. Extract reusable logic into **composables** (`useXxx`) under `frontend/src/admin/composables/`.
- Co-locate component-scoped styles with `<style scoped>`.

## Reactivity rules (the real ones)

- Don't destructure `props` (loses reactivity) — use `toRefs(props)` or reference `props.x` directly. For store state use `storeToRefs(store)`.
- `watch` explicit sources; `watchEffect` only when the dependency set is obvious. Clean up listeners/timers in `onUnmounted` (or the `watch`/`watchEffect` cleanup callback) — critical for the WebSocket/stream and wavesurfer/flv.js views.
- Keep `ref`s stable; never reassign a `reactive` object wholesale (mutate fields or use a `ref`).

## State — when to reach for what

| Need | Use |
|------|-----|
| Local component state | `ref` / `computed` |
| Reusable stateful logic | a composable (`useStream`, `useUpload`, …) |
| Cross-view shared/app state | a **Pinia** store (`frontend/src/admin/stores/`) |
| Server data | the API client in `frontend/src/admin/api/` (see `ApiClient`); keep fetch logic in composables/stores, not components |
| Auth/route guards | Vue Router guards + the auth store (`router.ts`) |

### Pinia
- `defineStore('name', () => { ... })` (setup-store style) to match Composition API. Return `ref`/`computed`/actions.
- Read reactive state in components via `storeToRefs(store)`; call actions directly.
- Stores own side-effects (API calls); components stay declarative.

## Async & media views
- This SPA drives streaming/recording UIs (wavesurfer.js, flv.js, WebSocket to the Axum backend). Always tear down players/sockets in `onUnmounted` to avoid leaks across route changes.
- Guard async-after-unmount: capture an `isActive` flag or check the component is still mounted before mutating state in a resolved promise.

## Anti-patterns to flag in review
- Destructured `props`/store state (broken reactivity).
- `reactive()` reassignment, or mixing Options + Composition API.
- Business logic / fetch calls inline in templates or `mounted` instead of composables/stores.
- Missing cleanup for sockets, intervals, `wavesurfer`/`flv.js` instances.
- `any`, non-null `!` abuse, or untyped `defineProps`/`defineEmits`.
- Watchers that duplicate what a `computed` could derive.

## Tooling (run from `frontend/`)
- `npm run dev` · `npm run build` · `npm run typecheck` (tsc) · `npm run lint` / `lint:fix` (ESLint) · `npm run format` (Prettier) · `npm test` (Vitest).
- Before reviewing a Vue change, prefer `gitnexus_query`/`gitnexus_context` (clusters: Pages, Composables, Components) over grepping.
