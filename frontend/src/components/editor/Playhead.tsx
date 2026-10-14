interface PlayheadProps {
  positionPx: number;
}

/** The playhead marker — positioned absolutely in px
 * (`playheadSeconds * pixelsPerSecond`, computed in the parent component
 * Timeline.tsx). The parent component is responsible for placing it inside a
 * `position: relative` container that spans the full height of the tracks. */
export function Playhead({ positionPx }: PlayheadProps) {
  return (
    <div
      className="pointer-events-none absolute bottom-0 top-0 z-10 w-px bg-red-500"
      style={{ left: positionPx }}
    >
      <div className="absolute -left-1.5 -top-1 h-3 w-3 rounded-full bg-red-500" />
    </div>
  );
}
