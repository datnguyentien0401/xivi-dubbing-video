import { useParams } from "react-router-dom";
import { Eye } from "lucide-react";
import { PipelineStepper } from "@/components/PipelineStepper";
import { useJobStatus } from "@/hooks/useJobStatus";

/** Real-time pipeline tracking screen — see docs/frontend-spec.md section
 * 3.3. This is the screen that should be demoed live on stage (it shows the
 * multi-step agentic nature, not "a single API call that returns a result
 * immediately"). */
export default function PipelineStatus() {
  const { jobId } = useParams<{ jobId: string }>();
  const status = useJobStatus(jobId);

  return (
    <div className="mx-auto max-w-md p-6">
      <h1 className="mb-4 text-xl font-bold">Processing...</h1>
      <PipelineStepper status={status} />
      {status?.currentStage === "ready_for_review" && (
        <a
          href={`/jobs/${jobId}/review`}
          className="mt-4 flex items-center justify-center gap-1.5 rounded bg-green-600 p-2 text-center text-white"
        >
          <Eye size={16} />
          View Results & Review
        </a>
      )}
    </div>
  );
}
