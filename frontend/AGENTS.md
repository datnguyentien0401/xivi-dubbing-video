# AGENTS.md — frontend/

> React-specific conventions. Read `../AGENTS.md` (root) first — this file only adds to it.

## Conventions
- Function components + hooks only, no class components.
- Data fetching via TanStack Query hooks in `src/hooks/`, not manual `useEffect`+`fetch`.
- State shared across multiple pages → Zustand (`src/stores/`).
- Realtime job/pipeline state → Firestore listener (`src/lib/firebase.ts`), not polling.
- Styling: TailwindCSS utility classes + `shadcn/ui`. See the `react-dashboard-page`
  skill when adding a new page.
- Any operation taking >1s must have a clear loading indicator (see docs/frontend-spec.md section 5).
