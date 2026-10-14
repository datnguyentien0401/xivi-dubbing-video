import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Sparkles, UploadCloud } from "lucide-react";
import { VoicePicker } from "@/components/VoicePicker";
import {
  useCreateScriptJobs,
  type ScriptLineImport,
} from "@/hooks/useDialogueLines";
import { useCreateProject } from "@/hooks/useProjects";
import {
  SOURCE_LANGUAGES,
  TARGET_LANGUAGE_GROUPS,
} from "@/lib/targetLanguages";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const REQUIRED_COLUMNS = ["segment_code", "character_name", "text"];

/** A minimal, correct CSV-line parser (quoted fields, "" as an escaped
 * quote inside a quoted field) — no new dependency added, per plan.md's
 * suggestion, since the script format is a fixed 3-column shape. */
function parseCsvLine(line: string): string[] {
  const fields: string[] = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    if (inQuotes) {
      if (char === '"') {
        if (line[i + 1] === '"') {
          current += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        current += char;
      }
    } else if (char === '"') {
      inQuotes = true;
    } else if (char === ",") {
      fields.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  fields.push(current);
  return fields;
}

/** Parses the uploaded script into rows — mirrors ScriptLineImport
 * (backend/api/schemas/schemas.py) exactly: segment_code, character_name,
 * text. Throws a clear error (shown to the user) rather than silently
 * dropping/misreading rows, matching US1's "shows me a clear error if
 * something doesn't parse" requirement. */
function parseScriptCsv(text: string): ScriptLineImport[] {
  const lines = text.split(/\r?\n/).filter((line) => line.trim() !== "");
  if (lines.length === 0) {
    throw new Error("The file is empty.");
  }
  const [headerLine, ...rowLines] = lines;
  const columns = parseCsvLine(headerLine).map((c) => c.trim().toLowerCase());
  const indices = REQUIRED_COLUMNS.map((name) => columns.indexOf(name));
  if (indices.some((i) => i === -1)) {
    throw new Error(
      `The header row must have these columns: ${REQUIRED_COLUMNS.join(", ")}`,
    );
  }
  const [segmentIdx, characterIdx, textIdx] = indices;
  if (rowLines.length === 0) {
    throw new Error("The script has no data rows.");
  }

  const lineByCode = new Map<string, ScriptLineImport>();
  rowLines.forEach((rowLine, rowIndex) => {
    const fields = parseCsvLine(rowLine);
    // A row with MORE fields than the header means an unescaped comma
    // inside a value (e.g. unquoted dialogue text containing a comma) —
    // reading by index alone would silently truncate the value at that
    // comma instead of erroring, exactly the "silently dropped content"
    // US1 explicitly requires avoiding (see docs/manual-dubbing-process.docx's
    // own "preserve 100% of rows" rule, which this mirrors).
    if (fields.length !== columns.length) {
      throw new Error(
        `Row ${rowIndex + 2} has ${fields.length} column(s), expected ${columns.length} — ` +
          "check for a comma inside a value that needs to be wrapped in quotes.",
      );
    }
    const segment_code = (fields[segmentIdx] ?? "").trim();
    const character_name = (fields[characterIdx] ?? "").trim();
    const lineText = (fields[textIdx] ?? "").trim();
    if (!segment_code || !character_name || !lineText) {
      throw new Error(`Row ${rowIndex + 2} is missing a required value.`);
    }
    if (lineByCode.has(segment_code)) {
      throw new Error(
        `Duplicate segment_code "${segment_code}" on row ${rowIndex + 2}.`,
      );
    }
    lineByCode.set(segment_code, {
      segment_code,
      character_name,
      text: lineText,
    });
  });
  return [...lineByCode.values()];
}

// New Script-First Project (specs/003-script-first-ingestion) — upload a
// locked script (CSV) instead of a video, assign each character's voice
// manually (no audio sample needed), choose target languages, submit ->
// POST /projects/{id}/script-jobs -> navigate to Pipeline Status. Mirrors
// NewProject.tsx's flow/conventions closely, just swapping "upload video"
// for "upload script + assign voices".
export default function NewScriptProject() {
  const [name, setName] = useState("");
  const [sourceLanguage, setSourceLanguage] = useState("en");
  const [lines, setLines] = useState<ScriptLineImport[]>([]);
  const [parseError, setParseError] = useState<string | null>(null);
  const [voiceByCharacter, setVoiceByCharacter] = useState<
    Record<string, string>
  >({});
  const [targetLanguages, setTargetLanguages] = useState<string[]>([]);
  const [step, setStep] = useState<
    "idle" | "creating-project" | "creating-jobs"
  >("idle");
  const [error, setError] = useState<string | null>(null);

  const createProject = useCreateProject();
  const createScriptJobs = useCreateScriptJobs();
  const navigate = useNavigate();

  const characterNames = [...new Set(lines.map((line) => line.character_name))];
  const everyCharacterHasAVoice =
    characterNames.length > 0 &&
    characterNames.every((name) => voiceByCharacter[name]);

  const isSubmitting = step !== "idle";
  const canSubmit =
    name.trim() !== "" &&
    lines.length > 0 &&
    everyCharacterHasAVoice &&
    targetLanguages.length > 0 &&
    !isSubmitting;

  function toggleTargetLanguage(code: string) {
    setTargetLanguages((prev) =>
      prev.includes(code) ? prev.filter((c) => c !== code) : [...prev, code],
    );
  }

  async function handleFileChange(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setParseError(null);
    try {
      const text = await file.text();
      const parsedLines = parseScriptCsv(text);
      setLines(parsedLines);
      // Carry over any voice already picked for a character name that's
      // still present after reparsing (e.g. the user fixed a typo in a
      // different row and re-uploaded) — only drop choices for names that
      // no longer exist.
      const newCharacterNames = new Set(
        parsedLines.map((line) => line.character_name),
      );
      setVoiceByCharacter((prev) =>
        Object.fromEntries(
          Object.entries(prev).filter(([charName]) =>
            newCharacterNames.has(charName),
          ),
        ),
      );
    } catch (err) {
      setLines([]);
      setParseError(
        err instanceof Error ? err.message : "Could not parse this file.",
      );
    }
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!everyCharacterHasAVoice || targetLanguages.length === 0) return;
    setError(null);
    // Set step BEFORE the first await — same reasoning as NewProject.tsx
    // (avoid a double-click triggering a second parallel submit).
    setStep("creating-project");

    try {
      const project = await createProject.mutateAsync({
        name,
        source_language: sourceLanguage,
      });

      setStep("creating-jobs");
      const character_voice_assignments = Object.fromEntries(
        characterNames.map((charName) => [
          charName,
          { voice_name: voiceByCharacter[charName] },
        ]),
      );
      const jobs = await createScriptJobs.mutateAsync({
        projectId: project.id,
        lines,
        target_languages: targetLanguages,
        character_voice_assignments,
      });

      navigate(`/jobs/${jobs[0].id}/status`);
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
    <div className="p-6">
      <Card className="mx-auto max-w-xl">
        <CardHeader>
          <CardTitle className="text-2xl">New Project from Script</CardTitle>
          <p className="text-sm text-muted-foreground">
            Start localization directly from a locked script — no video required
            yet. Translation and dubbing can run while the video is still being
            edited; attach the finished video afterward.
          </p>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <Label htmlFor="project-name" className="mb-1.5 block">
                Project Name
              </Label>
              <Input
                id="project-name"
                placeholder="e.g. Froggy - Episode 1"
                value={name}
                onChange={(e) => setName(e.target.value)}
                disabled={isSubmitting}
              />
            </div>

            <div>
              <Label className="mb-1.5 block">Script Language</Label>
              <Select
                value={sourceLanguage}
                onValueChange={setSourceLanguage}
                disabled={isSubmitting}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SOURCE_LANGUAGES.map((lang) => (
                    <SelectItem key={lang.code} value={lang.code}>
                      {lang.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div>
              <Label htmlFor="script-file" className="mb-1.5 block">
                Script (CSV: segment_code, character_name, text)
              </Label>
              <label
                htmlFor="script-file"
                className={`flex items-center gap-2 rounded-md border border-dashed px-3 py-3 text-sm transition-colors ${
                  isSubmitting
                    ? "cursor-not-allowed opacity-50"
                    : "cursor-pointer hover:border-primary hover:bg-accent/40"
                }`}
              >
                <UploadCloud
                  size={18}
                  className="shrink-0 text-muted-foreground"
                />
                <span className="truncate text-muted-foreground">
                  {lines.length > 0 && !parseError
                    ? "Click to choose a different CSV file"
                    : "Click to choose a CSV file"}
                </span>
                <input
                  id="script-file"
                  type="file"
                  accept=".csv,text/csv"
                  className="hidden"
                  onChange={handleFileChange}
                  disabled={isSubmitting}
                />
              </label>
              {parseError && (
                <Alert variant="destructive" className="mt-2">
                  <AlertDescription>{parseError}</AlertDescription>
                </Alert>
              )}
              {lines.length > 0 && !parseError && (
                <p className="mt-2 text-sm text-muted-foreground">
                  Parsed {lines.length} line{lines.length === 1 ? "" : "s"}{" "}
                  across {characterNames.length} character
                  {characterNames.length === 1 ? "" : "s"}.
                </p>
              )}
            </div>

            {characterNames.length > 0 && (
              <div>
                <Label className="mb-1.5 block">
                  Assign a voice to each character
                </Label>
                <div className="space-y-2">
                  {characterNames.map((charName) => (
                    <div key={charName} className="flex items-center gap-2">
                      <span className="w-32 shrink-0 truncate text-sm">
                        {charName}
                      </span>
                      <VoicePicker
                        value={voiceByCharacter[charName] ?? null}
                        onChange={(voiceName) =>
                          setVoiceByCharacter((prev) => ({
                            ...prev,
                            [charName]: voiceName,
                          }))
                        }
                        disabled={isSubmitting}
                        className="flex-1"
                      />
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div>
              <Label className="mb-1.5 block">
                Target Languages (select at least 1)
              </Label>
              <div className="space-y-3">
                {TARGET_LANGUAGE_GROUPS.map((group) => (
                  <div key={group.region}>
                    <p className="mb-1.5 text-xs font-semibold text-muted-foreground">
                      {group.region}
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {group.languages.map((lang) => {
                        const checked = targetLanguages.includes(lang.code);
                        return (
                          <label
                            key={lang.code}
                            className={`flex cursor-pointer items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-sm transition-colors ${
                              checked
                                ? "border-primary bg-accent text-accent-foreground"
                                : "hover:bg-accent/40"
                            }`}
                          >
                            <input
                              type="checkbox"
                              className="accent-[hsl(var(--primary))]"
                              checked={checked}
                              onChange={() => toggleTargetLanguage(lang.code)}
                              disabled={isSubmitting}
                            />
                            <span>{lang.flag}</span>
                            <span>{lang.label}</span>
                          </label>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {error && (
              <Alert variant="destructive">
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}

            <Button type="submit" disabled={!canSubmit} className="w-full">
              <Sparkles size={16} />
              {step === "idle" && "Create Project"}
              {step === "creating-project" && "Creating project..."}
              {step === "creating-jobs" && "Initializing pipeline..."}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
