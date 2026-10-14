import { create } from "zustand";

interface SessionState {
  currentProjectId: string | null;
  setCurrentProjectId: (id: string | null) => void;
}

/** Zustand store for state shared across multiple pages — NOT used for
 * server data (project list, job status, etc.), since that's the job of
 * TanStack Query / Firestore listeners, see AGENTS.md frontend. */
export const useSessionStore = create<SessionState>((set) => ({
  currentProjectId: null,
  setCurrentProjectId: (id) => set({ currentProjectId: id }),
}));
