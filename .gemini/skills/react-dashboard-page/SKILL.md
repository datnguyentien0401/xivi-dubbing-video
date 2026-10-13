---
name: react-dashboard-page
description: Add a new page/screen to XIVI's React Studio Dashboard, following the project's routing, data fetching (TanStack Query), and state (Zustand) conventions.
---

# Skill: Add a new React Page

## Required conventions
- Pages live in `frontend/src/pages/`, as function components, no class components.
- Data fetching through a dedicated hook in `frontend/src/hooks/` using TanStack Query —
  no manual `useEffect` + `fetch` in the page.
- State that needs to be shared across pages (session, selected project) → a Zustand store
  in `frontend/src/stores/`, no prop-drilling through many levels.
- Realtime job/pipeline state → listen via a Firestore listener
  (`frontend/src/lib/firebase.ts`), no continuous REST polling.
- Style with TailwindCSS utility classes + the `shadcn/ui` components already in
  `frontend/src/components/ui/` — don't write a separate CSS module unless truly necessary.

## Steps
1. Create the file `frontend/src/pages/<PageName>.tsx`.
2. If it needs data from the backend, create/use a hook in `hooks/` (e.g. `useProjects.ts`).
3. Add the route to the main router (`App.tsx`).
4. If the page shows pipeline status → use the existing `PipelineStepper`
   component instead of re-implementing it.
5. For any operation taking >1s → show a clear loading state (see the UX principles
   in `docs/frontend-spec.md` section 5).

## Checklist
- [ ] No manual `useEffect` + `fetch` — use a TanStack Query hook
- [ ] Loading/error state present for every query
- [ ] Route added in `App.tsx`
- [ ] Used an existing `shadcn/ui` component before writing a new one
