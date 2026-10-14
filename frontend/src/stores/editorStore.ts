import { create } from "zustand";

/** Matches TimelineState/Track/Clip on the backend 1-to-1 (see
 * backend/api/schemas/schemas.py, specs/002-video-editor) — snake_case
 * because this is the JSON shape returned directly from the API, not the
 * usual TS convention. */
export interface Clip {
  id: string;
  dialogue_line_id: string | null;
  timeline_start: number;
  source_start: number;
  source_end: number;
  volume: number;
  fade_in_seconds: number;
  fade_out_seconds: number;
}

export interface Track {
  id: string;
  type: "video" | "audio_original" | "dialogue";
  character_id: string | null;
  muted: boolean;
  solo: boolean;
  volume: number;
  clips: Clip[];
}

export interface TimelineState {
  schema_version: number;
  duration_seconds: number;
  tracks: Track[];
}

const MAX_UNDO_STEPS = 50;
const MIN_PIXELS_PER_SECOND = 10;
const MAX_PIXELS_PER_SECOND = 400;
const DEFAULT_PIXELS_PER_SECOND = 80;
// Minimum distance (seconds) from the split point to either end of the clip
// — prevents splitClip from producing an almost-empty fragment that can't be
// clicked/dragged (US2).
const MIN_SPLIT_PIECE_SECONDS = 0.1;

interface EditorState {
  /** The current working draft for this editor session — null when no job
   * is open yet (VideoEditor.tsx calls loadTimeline() right after
   * useJobTimeline() finishes fetching). This is the SINGLE source of data
   * read/written by every Timeline/TrackRow/ClipBlock component — there's no
   * other copy passed through props. */
  timeline: TimelineState | null;
  /** Undo/redo history lives ONLY within the working session (zustand, not
   * the server) — see US5 in spec.md: only the FINAL snapshot needs to
   * survive a reload (autosaved to the server), the step-by-step edit
   * history does not. */
  past: TimelineState[];
  future: TimelineState[];
  selectedClipId: string | null;
  playheadSeconds: number;
  pixelsPerSecond: number;

  /** Initializes/replaces the current working timeline — wipes the old
   * undo/redo history (a different job, or a freshly re-fetched one from the
   * server, shouldn't get mixed up with the edit history of the previous
   * job). */
  loadTimeline: (timeline: TimelineState) => void;
  /** Applies a change to the current timeline via a pure updater function
   * (takes the old state, returns the new state) — pushes the old state onto
   * `past`, clears `future` (a new branch has been taken, the old redo is no
   * longer valid), following the standard undo/redo stack convention. */
  applyEdit: (updater: (timeline: TimelineState) => TimelineState) => void;
  undo: () => void;
  redo: () => void;
  selectClip: (clipId: string | null) => void;
  setPlayhead: (seconds: number) => void;
  setZoom: (pixelsPerSecond: number) => void;
  /** Patches a clip by id within a specific track — used by ClipBlock.tsx
   * (drag/trim-edge, US3) and ClipInspector.tsx (volume/fade, US3). Each call
   * creates exactly one undo step (via applyEdit) — ClipBlock itself batches
   * an entire drag gesture into a single call at pointerup, rather than
   * calling per pixel moved. */
  updateClip: (trackId: string, clipId: string, patch: Partial<Clip>) => void;
  /** Patches track properties (muted/solo/volume) — used by Timeline.tsx (US3). */
  updateTrack: (trackId: string, patch: Partial<Track>) => void;
  /** Splits one clip into two at `atSeconds` — used by US2 (split video at
   * the playhead, see ClipInspector.tsx), but not restricted to any specific
   * track at the store level (the UI decides when to show the button).
   * No-op if `atSeconds` is too close to either end of the clip (see
   * MIN_SPLIT_PIECE_SECONDS) or if the track/clip isn't found. */
  splitClip: (trackId: string, clipId: string, atSeconds: number) => void;
  /** Deletes a clip on `trackId` — every other clip (on any track) that
   * overlaps the exact [timeline_start, timeline_start+duration) range of
   * the deleted clip is also affected: one FULLY contained within that range
   * is deleted outright (a dialogue line tied to a video/audio segment that
   * no longer exists has no reason to be kept — US2, spec.md: "dialogue
   * clips within the cut range are handled according to the corresponding
   * ripple mode"); one overlapping only PARTIALLY (e.g. original audio
   * extending past the exact cut range) is TRIMMED instead of deleted
   * outright, keeping the portion outside the cut range. `ripple=true`:
   * shifts every remaining clip AFTER that range (on any track) earlier, and
   * shrinks `duration_seconds`. `ripple=false`: only leaves a gap on the
   * exact track of the deleted clip (the backend fills in a black frame for
   * the video track automatically, see
   * worker/services/editor_export_service.py _build_silent_video) — other
   * tracks keep their absolute positions, `duration_seconds` stays the
   * same. */
  deleteClip: (trackId: string, clipId: string, ripple: boolean) => void;
}

export const useEditorStore = create<EditorState>((set, get) => ({
  timeline: null,
  past: [],
  future: [],
  selectedClipId: null,
  playheadSeconds: 0,
  pixelsPerSecond: DEFAULT_PIXELS_PER_SECOND,

  loadTimeline: (timeline) =>
    set({
      timeline,
      past: [],
      future: [],
      selectedClipId: null,
      playheadSeconds: 0,
    }),

  applyEdit: (updater) => {
    const current = get().timeline;
    if (!current) return;
    const next = updater(current);
    set((state) => ({
      timeline: next,
      // Capped at MAX_UNDO_STEPS — prevents the past array from growing
      // unbounded over a long editing session; no need to keep history
      // further back than what's practically useful.
      past: [...state.past, current].slice(-MAX_UNDO_STEPS),
      future: [],
    }));
  },

  undo: () => {
    const { past, timeline, future } = get();
    if (past.length === 0 || !timeline) return;
    const previous = past[past.length - 1];
    set({
      timeline: previous,
      past: past.slice(0, -1),
      future: [timeline, ...future],
    });
  },

  redo: () => {
    const { future, timeline, past } = get();
    if (future.length === 0 || !timeline) return;
    const next = future[0];
    set({
      timeline: next,
      past: [...past, timeline],
      future: future.slice(1),
    });
  },

  selectClip: (clipId) => set({ selectedClipId: clipId }),
  setPlayhead: (seconds) => set({ playheadSeconds: Math.max(0, seconds) }),
  setZoom: (pixelsPerSecond) =>
    set({
      pixelsPerSecond: Math.min(
        MAX_PIXELS_PER_SECOND,
        Math.max(MIN_PIXELS_PER_SECOND, pixelsPerSecond),
      ),
    }),

  updateClip: (trackId, clipId, patch) => {
    get().applyEdit((timeline) => ({
      ...timeline,
      tracks: timeline.tracks.map((track) =>
        track.id !== trackId
          ? track
          : {
              ...track,
              clips: track.clips.map((clip) =>
                clip.id !== clipId ? clip : { ...clip, ...patch },
              ),
            },
      ),
    }));
  },

  updateTrack: (trackId, patch) => {
    get().applyEdit((timeline) => ({
      ...timeline,
      tracks: timeline.tracks.map((track) =>
        track.id !== trackId ? track : { ...track, ...patch },
      ),
    }));
  },

  splitClip: (trackId, clipId, atSeconds) => {
    get().applyEdit((timeline) => ({
      ...timeline,
      tracks: timeline.tracks.map((track) => {
        if (track.id !== trackId) return track;
        return {
          ...track,
          clips: track.clips.flatMap((clip) => {
            if (clip.id !== clipId) return [clip];
            const clipEnd =
              clip.timeline_start + (clip.source_end - clip.source_start);
            if (
              atSeconds <= clip.timeline_start + MIN_SPLIT_PIECE_SECONDS ||
              atSeconds >= clipEnd - MIN_SPLIT_PIECE_SECONDS
            )
              return [clip];
            const splitSourcePos =
              clip.source_start + (atSeconds - clip.timeline_start);
            return [
              { ...clip, source_end: splitSourcePos },
              {
                ...clip,
                id: crypto.randomUUID(),
                timeline_start: atSeconds,
                source_start: splitSourcePos,
              },
            ];
          }),
        };
      }),
    }));
  },

  deleteClip: (trackId, clipId, ripple) => {
    get().applyEdit((timeline) => {
      const track = timeline.tracks.find((t) => t.id === trackId);
      const clip = track?.clips.find((c) => c.id === clipId);
      if (!track || !clip) return timeline;
      const cutStart = clip.timeline_start;
      const cutEnd = cutStart + (clip.source_end - clip.source_start);
      const cutDuration = cutEnd - cutStart;

      const tracks = timeline.tracks.map((t) => ({
        ...t,
        clips: t.clips
          .flatMap((c): Clip[] => {
            const cStart = c.timeline_start;
            const cEnd = cStart + (c.source_end - c.source_start);
            if (cEnd <= cutStart || cStart >= cutEnd) return [c]; // no overlap — keep as-is
            if (cStart >= cutStart && cEnd <= cutEnd) return []; // FULLY contained within the cut range (including the clip being deleted itself) — drop entirely
            if (cStart < cutStart && cEnd > cutEnd) {
              // Overlaps on BOTH sides (rare — a clip spans the entire cut
              // range, usually only happens with original audio/a track
              // that's never been split). MVP: only keep the portion BEFORE
              // the cut, drop the portion after — equivalent to a trim-end at
              // cutStart; doesn't automatically generate a second clip for
              // the remaining portion afterward (kept simple — see splitClip
              // if the user needs to keep both portions).
              return [
                { ...c, source_end: c.source_start + (cutStart - cStart) },
              ];
            }
            if (cStart < cutStart) {
              // Overlaps on the TRAILING side of the clip — trim it short,
              // ending exactly at cutStart (e.g.: original audio extending
              // past the range that was just deleted).
              return [
                { ...c, source_end: c.source_start + (cutStart - cStart) },
              ];
            }
            // Overlaps on the LEADING side of the clip — trim the start,
            // shifting both timeline_start and source_start to cutEnd
            // (keeps the clip at the "same source length", just starting
            // later, matching the trim-start semantics already used in
            // ClipBlock.tsx).
            const trimAmount = cutEnd - cStart;
            return [
              {
                ...c,
                timeline_start: c.timeline_start + trimAmount,
                source_start: c.source_start + trimAmount,
              },
            ];
          })
          .map((c) =>
            ripple && c.timeline_start >= cutEnd
              ? { ...c, timeline_start: c.timeline_start - cutDuration }
              : c,
          ),
      }));

      return {
        ...timeline,
        duration_seconds: ripple
          ? timeline.duration_seconds - cutDuration
          : timeline.duration_seconds,
        tracks,
      };
    });
  },
}));
