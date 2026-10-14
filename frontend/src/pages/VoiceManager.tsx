import { useState } from "react";
import { useParams } from "react-router-dom";
import { Plus, Save, Sparkles, Upload } from "lucide-react";
import { AudioPlayer } from "@/components/AudioPlayer";
import {
  useCharacters,
  useClassifyVoiceSample,
  useCreateCharacter,
  useCreateVoiceSampleUploadUrl,
  useUpdateCharacter,
  type Character,
} from "@/hooks/useCharacters";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

function EditableName({
  character,
  onSave,
}: {
  character: Character;
  onSave: (name: string) => void;
}) {
  const [draft, setDraft] = useState(character.name);
  const isDirty = draft.trim() !== "" && draft !== character.name;

  return (
    <div className="flex items-center gap-2">
      <Input
        className="h-8 w-56 text-sm"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
      />
      {isDirty && (
        <Button size="sm" className="h-8" onClick={() => onSave(draft)}>
          <Save size={12} />
          Save
        </Button>
      )}
    </div>
  );
}

/** Adds a new character with a self-uploaded voice sample — distinct from
 * characters that Agent 2 detects automatically from the video, used when
 * wanting to add/fix a voice without depending on automatic diarization
 * results (see AGENTS.md: Gemini is still the core voice-picking logic via
 * select_voice_for_speaker, only the sample audio source differs). */
function AddVoiceForm({ projectId }: { projectId: string }) {
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [step, setStep] = useState<
    "idle" | "creating" | "uploading" | "classifying"
  >("idle");
  const [error, setError] = useState<string | null>(null);

  const createCharacter = useCreateCharacter(projectId);
  const createUploadUrl = useCreateVoiceSampleUploadUrl();
  const classifyVoiceSample = useClassifyVoiceSample(projectId);

  const isSubmitting = step !== "idle";
  const canSubmit = name.trim() !== "" && file !== null && !isSubmitting;

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!file) return;
    setError(null);
    // Set step BEFORE the first await — same reasoning as NewProject.tsx:
    // avoids double-submit while waiting on the network.
    setStep("creating");

    try {
      const character = await createCharacter.mutateAsync({ name });

      setStep("uploading");
      const { upload_url } = await createUploadUrl.mutateAsync({
        characterId: character.id,
        filename: file.name,
        content_type: file.type || "audio/wav",
      });
      const putResponse = await fetch(upload_url, {
        method: "PUT",
        headers: { "Content-Type": file.type || "audio/wav" },
        body: file,
      });
      if (!putResponse.ok) {
        throw new Error(
          `Voice sample upload to GCS failed (${putResponse.status})`,
        );
      }

      setStep("classifying");
      await classifyVoiceSample.mutateAsync(character.id);

      setName("");
      setFile(null);
      setStep("idle");
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Something went wrong, please try again.",
      );
      setStep("idle");
    }
  }

  return (
    <Card className="mb-6">
      <CardContent className="pt-6">
        <form
          onSubmit={handleSubmit}
          className="flex flex-wrap items-end gap-3"
        >
          <div>
            <Label className="mb-1.5 block text-xs text-muted-foreground">
              Character Name
            </Label>
            <Input
              className="h-9 w-48"
              placeholder="e.g. Squirrel"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={isSubmitting}
            />
          </div>
          <div>
            <Label className="mb-1.5 flex items-center gap-1 text-xs text-muted-foreground">
              <Upload size={12} />
              Voice Sample (audio)
            </Label>
            <input
              type="file"
              accept="audio/*"
              className="flex h-9 items-center text-sm text-foreground file:mr-3 file:rounded-md file:border file:border-input file:bg-card file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-foreground"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              disabled={isSubmitting}
            />
          </div>
          <Button type="submit" disabled={!canSubmit}>
            <Plus size={14} />
            {step === "idle" && "Add Voice"}
            {step === "creating" && "Creating..."}
            {step === "uploading" && "Uploading..."}
            {step === "classifying" && "Analyzing voice..."}
          </Button>
          {error && (
            <Alert variant="destructive" className="w-full py-2">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
        </form>
      </CardContent>
    </Card>
  );
}

function SelectedVoiceCell({ character }: { character: Character }) {
  if (character.voice_id) {
    return (
      <Badge variant="outline" title={character.voice_id} className="gap-1">
        <Sparkles size={12} className="text-primary" />
        Custom Voice (Voice Design)
      </Badge>
    );
  }
  if (character.voice_name) {
    return <span>{character.voice_name}</span>;
  }
  return (
    <span className="text-muted-foreground">
      {character.voice_sample_gcs_uri ? "Analyzing..." : "—"}
    </span>
  );
}

// Character Voice Manager — see docs/frontend-spec.md section 3.5. Names
// characters (kept consistent across episodes — this is the core value prop,
// see AGENTS.md section 1), lets you preview the original voice sample, and
// lets you add a new character/voice by uploading audio directly (not
// depending on Agent 1/2's automatic diarization results).
export default function VoiceManager() {
  const { projectId } = useParams<{ projectId: string }>();
  const { data: characters, isLoading } = useCharacters(projectId);
  const updateCharacter = useUpdateCharacter(projectId);

  if (isLoading) return <p className="p-6 text-muted-foreground">Loading...</p>;

  return (
    <div className="p-6">
      <h1 className="mb-4 font-display text-2xl font-semibold">
        Character Voice Manager
      </h1>

      {projectId && <AddVoiceForm projectId={projectId} />}

      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Character</TableHead>
                <TableHead>Voice Sample</TableHead>
                <TableHead>Selected TTS Voice</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {characters?.map((character) => (
                <TableRow key={character.id}>
                  <TableCell className="align-top">
                    <EditableName
                      character={character}
                      onSave={(name) =>
                        updateCharacter.mutate({
                          characterId: character.id,
                          name,
                        })
                      }
                    />
                  </TableCell>
                  <TableCell className="align-top">
                    {character.voice_sample_gcs_uri ? (
                      <AudioPlayer
                        gcsUri={character.voice_sample_gcs_uri}
                        signedUrlPath={`/characters/${character.id}/voice-sample-url`}
                      />
                    ) : (
                      <span className="text-muted-foreground">
                        No sample yet
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="align-top">
                    <SelectedVoiceCell character={character} />
                  </TableCell>
                </TableRow>
              ))}
              {characters?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={3} className="text-muted-foreground">
                    No characters yet — they'll appear automatically once Agent
                    1/2 processes the video, or add a new voice above.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
