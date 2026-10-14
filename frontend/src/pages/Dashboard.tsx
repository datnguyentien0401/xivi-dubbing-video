import { Link } from "react-router-dom";
import { FileText, Mic, Plus, Trash2 } from "lucide-react";
import { useDeleteProject, useProjects } from "@/hooks/useProjects";
import { EditableProjectName } from "@/components/EditableProjectName";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

export default function Dashboard() {
  const { data: projects, isLoading, error } = useProjects();
  const deleteProject = useDeleteProject();

  if (isLoading) return <p className="p-6 text-muted-foreground">Loading...</p>;
  if (error)
    return <p className="p-6 text-destructive">Failed to load projects.</p>;

  function handleDelete(projectId: string, projectName: string) {
    if (
      !window.confirm(
        `Delete project "${projectName}"? All videos, translations, and characters in this project will be removed from the system.`,
      )
    ) {
      return;
    }
    deleteProject.mutate(projectId);
  }

  return (
    <div className="p-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="font-display text-2xl font-semibold">Projects</h1>
        <div className="flex items-center gap-2">
          <Button asChild>
            <Link to="/projects/new">
              <Plus size={16} />
              New Project
            </Link>
          </Button>
          <Button asChild variant="outline">
            <Link to="/projects/new-script">
              <FileText size={16} />
              New from Script
            </Link>
          </Button>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {projects?.map((project) => (
          <Card
            key={project.id}
            className="p-4 transition-shadow hover:shadow-md"
          >
            <div className="flex items-start justify-between gap-2">
              <EditableProjectName
                project={project}
                headingLevel={2}
                linkTo={`/projects/${project.id}`}
              />
              <button
                type="button"
                onClick={() => handleDelete(project.id, project.name)}
                disabled={
                  deleteProject.isPending &&
                  deleteProject.variables === project.id
                }
                className="flex shrink-0 items-center gap-1 text-sm text-destructive hover:underline disabled:opacity-50"
              >
                <Trash2 size={14} />
                Delete
              </button>
            </div>
            <p className="text-sm text-muted-foreground">
              {project.source_language}
            </p>
            <Link
              to={`/projects/${project.id}/voices`}
              className="mt-2 inline-flex items-center gap-1 text-sm text-primary hover:underline"
            >
              <Mic size={14} />
              Manage Character Voices
            </Link>
          </Card>
        ))}
      </div>
    </div>
  );
}
