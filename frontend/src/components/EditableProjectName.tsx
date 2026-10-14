import { useState } from "react";
import { Link } from "react-router-dom";
import { Pencil, X } from "lucide-react";
import { useUpdateProject, type Project } from "@/hooks/useProjects";

interface EditableProjectNameProps {
  project: Project;
  /** h1 on ProjectDetail.tsx (already on that page itself), h2 on
   * Dashboard.tsx (one of several project cards, not the page title) — see
   * call sites. */
  headingLevel: 1 | 2;
  /** When present, the name renders as a Link (Dashboard.tsx — the card is
   * clicked to navigate into the project page); when absent, it renders as
   * plain text (ProjectDetail.tsx — already on this exact page, no need to
   * link to itself). */
  linkTo?: string;
}

/** In-place project rename — shared between Dashboard.tsx (list of project
 * cards) and ProjectDetail.tsx (page title) to avoid duplicating the
 * draft/save/cancel logic in both places. Follows the same pattern as the
 * existing EditableTranslation in ReviewDashboard.tsx (local draft, only
 * calls the API on Save), with the difference that it requires an extra
 * CLICK on the pencil icon to enter edit mode — because here the project
 * name doubles as a navigation Link (Dashboard.tsx), so an always-on input
 * can't be left sitting on top of that link. */
export function EditableProjectName({
  project,
  headingLevel,
  linkTo,
}: EditableProjectNameProps) {
  const updateProject = useUpdateProject();
  const [isEditing, setIsEditing] = useState(false);
  const [draft, setDraft] = useState(project.name);

  function startEditing() {
    setDraft(project.name);
    setIsEditing(true);
  }

  function save() {
    const trimmed = draft.trim();
    // An empty name doesn't call the API (ProjectUpdate.name requires
    // min_length=1, the request would 422) — treat it as a cancelled edit
    // instead of showing an error for a harmless action. An unchanged name
    // is also skipped, to avoid an unnecessary empty PATCH.
    if (!trimmed || trimmed === project.name) {
      setIsEditing(false);
      return;
    }
    updateProject.mutate(
      { projectId: project.id, name: trimmed },
      { onSuccess: () => setIsEditing(false) },
    );
  }

  const headingClassName =
    headingLevel === 1 ? "text-xl font-bold" : "font-semibold";

  if (isEditing) {
    return (
      <div className="flex min-w-0 items-center gap-1.5">
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
            if (e.key === "Escape") setIsEditing(false);
          }}
          className={`min-w-0 rounded-md border bg-card px-1.5 py-0.5 ${headingClassName}`}
        />
        <button
          type="button"
          onClick={save}
          disabled={updateProject.isPending}
          className="shrink-0 text-xs text-primary hover:underline disabled:opacity-50"
        >
          Save
        </button>
        <button
          type="button"
          onClick={() => setIsEditing(false)}
          className="shrink-0 text-muted-foreground hover:text-foreground"
          aria-label="Cancel"
        >
          <X size={14} />
        </button>
        {updateProject.isError && (
          <p className="text-xs text-destructive">Could not rename.</p>
        )}
      </div>
    );
  }

  const Heading = headingLevel === 1 ? "h1" : "h2";
  const nameEl = <Heading className={headingClassName}>{project.name}</Heading>;

  return (
    <div className="flex min-w-0 items-center gap-1.5">
      {linkTo ? (
        <Link to={linkTo} className="min-w-0 truncate hover:underline">
          {nameEl}
        </Link>
      ) : (
        nameEl
      )}
      <button
        type="button"
        onClick={startEditing}
        className="shrink-0 text-muted-foreground hover:text-foreground"
        aria-label="Rename project"
      >
        <Pencil size={14} />
      </button>
    </div>
  );
}
