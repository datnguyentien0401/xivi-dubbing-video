import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Eye, EyeOff } from "lucide-react";
import { useJob } from "@/hooks/useDialogueLines";
import { useEducationalContent } from "@/hooks/useEducationalContent";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

function QuizCard({ question, answer }: { question: string; answer: string }) {
  const [revealed, setRevealed] = useState(false);

  return (
    <Card>
      <CardContent className="flex items-start justify-between gap-3 pt-6">
        <div>
          <p className="font-medium">{question}</p>
          {revealed && (
            <p className="mt-1 text-sm text-muted-foreground">{answer}</p>
          )}
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={() => setRevealed((value) => !value)}
        >
          {revealed ? <EyeOff size={14} /> : <Eye size={14} />}
          {revealed ? "Hide answer" : "Show answer"}
        </Button>
      </CardContent>
    </Card>
  );
}

// Educational Content Preview — see docs/frontend-spec.md section 3.6. A
// read-only view of Agent 7's output (vocabulary flashcards + review
// questions, worker/agents/educational_agent.py); runs off the pipeline's
// critical path, so it may still be empty right after the job reaches
// ready_for_review (no separate "still generating" status, see
// api/routers/jobs.py::get_educational_content).
export default function EduPreview() {
  const { jobId } = useParams<{ jobId: string }>();
  const { data: job } = useJob(jobId);
  const { data: content, isLoading } = useEducationalContent(jobId);

  if (isLoading) return <p className="p-6 text-muted-foreground">Loading...</p>;

  const vocabulary = content?.vocabulary ?? [];
  const quiz = content?.quiz ?? [];

  return (
    <div className="p-6">
      <Link
        to={`/jobs/${jobId}/review`}
        className="mb-1 flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft size={14} />
        Back to Review
      </Link>
      <h1 className="mb-6 font-display text-2xl font-semibold">
        Educational Content{job ? ` — ${job.target_language}` : ""}
      </h1>

      <section className="mb-8">
        <h2 className="mb-3 text-lg font-semibold">Vocabulary</h2>
        {vocabulary.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No vocabulary generated yet — this runs in the background and may
            still be in progress, or the episode didn't yield any suitable
            words.
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {vocabulary.map((item) => (
              <Card key={item.id}>
                <CardHeader>
                  <CardTitle>{item.term}</CardTitle>
                </CardHeader>
                <CardContent className="space-y-1 text-sm">
                  <p>{item.meaning}</p>
                  <p className="italic text-muted-foreground">
                    {item.example_sentence}
                  </p>
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold">Review Questions</h2>
        {quiz.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No review questions generated yet.
          </p>
        ) : (
          <div className="space-y-3">
            {quiz.map((item) => (
              <QuizCard
                key={item.id}
                question={item.question}
                answer={item.answer}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
