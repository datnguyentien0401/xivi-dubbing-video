import { useVoiceOptions } from "@/hooks/useCharacters";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface VoicePickerProps {
  value: string | null;
  onChange: (voiceName: string) => void;
  className?: string;
  disabled?: boolean;
}

/** Lets a human pick directly from the 12 prebuilt TTS voices — no audio
 * sample, no Gemini classification call (see GET /characters/voice-options).
 * Used for manual voice assignment when there's no original recording for
 * Gemini to listen to and match automatically (specs/003-script-first-ingestion).
 *
 * Deliberately a plain controlled selector (value/onChange), not coupled to
 * any specific character_id or mutation — the same component works both for
 * a character that doesn't exist in the DB yet (script-first job creation,
 * where the choice is only submitted later as part of the whole script) and
 * for assigning a voice to an already-existing character (via
 * useAssignVoiceManually, wired up by the caller). */
export function VoicePicker({
  value,
  onChange,
  className,
  disabled,
}: VoicePickerProps) {
  const { data: options, isLoading, isError } = useVoiceOptions();

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading voices...</p>;
  }
  if (isError || !options) {
    return (
      <p className="text-sm text-destructive">Could not load voice options.</p>
    );
  }

  return (
    <Select
      value={value ?? undefined}
      onValueChange={onChange}
      disabled={disabled}
    >
      <SelectTrigger className={className}>
        <SelectValue placeholder="Select a voice..." />
      </SelectTrigger>
      <SelectContent>
        {options.map((option) => (
          <SelectItem key={option.voice_name} value={option.voice_name}>
            {option.voice_name} — {option.description}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
