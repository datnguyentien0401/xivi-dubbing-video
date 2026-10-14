"""Agent 3 — Translation & Transcreation (see docs/agent-pipeline.md).

Input: context.dialogue_lines (original_text), context.target_language
Output: each dialogue_line gets translated_text — uses transcreation if is_song=true
"""

from worker.models.pipeline_context import DialogueLine, PipelineContext
from worker.services.gemini_client import GeminiClient

# Number of adjacent lines taken as context on each side (before/after) —
# dialogue_lines are auto-split by timestamp, so one sentence can be split
# into 2-3 lines because of a pause while speaking; 2 lines on each side is
# enough to cover most common sentence-splitting cases without pulling too
# much unrelated text into the translation prompt.
_CONTEXT_WINDOW = 2


def _build_context(dialogue_lines: list[DialogueLine], index: int) -> str:
    """Build a context block made of the lines adjacent to
    `dialogue_lines[index]`, marking the line to be translated with ">>>" —
    see gemini_client.py:translate_dialogue for why this context is needed
    (the bug "a sentence split into pieces made the sentence's overall
    meaning wrong": translating each line independently isn't enough for
    Gemini to understand the correct meaning when one complete sentence is
    split mid-way into multiple dialogue_lines).

    An adjacent line isn't necessarily spoken by the SAME speaker as the line
    being translated — back-and-forth dialogue between 2 characters is
    normal (e.g. question/answer) — so we clearly annotate which line is the
    other person's turn (found via gemini-prompt-reviewer: without this
    annotation, Gemini might blend the meaning of two different characters'
    turns into one sentence, creating a NEW translation error instead of
    fixing the split-sentence error)."""
    start = max(0, index - _CONTEXT_WINDOW)
    end = min(len(dialogue_lines), index + _CONTEXT_WINDOW + 1)
    current_speaker = dialogue_lines[index].speaker_tag
    lines = []
    for i in range(start, end):
        line = dialogue_lines[i]
        marker = ">>> " if i == index else "    "
        note = ""
        if i != index:
            if line.speaker_tag != current_speaker:
                note = "  [another speaker's turn — do NOT blend meaning with the >>> line]"
            elif line.is_song:
                note = "  [song lyrics, not regular dialogue]"
        lines.append(f"{marker}{line.original_text}{note}")
    return "\n".join(lines)


class TranslationAgent:
    def __init__(self, gemini: GeminiClient):
        self._gemini = gemini

    async def run(self, context: PipelineContext) -> PipelineContext:
        for index, line in enumerate(context.dialogue_lines):
            line.translated_text = await self._gemini.translate_dialogue(
                text=line.original_text,
                source_lang=context.source_language,
                target_lang=context.target_language,
                is_song=line.is_song,
                # Songs are transcreated as a whole from the original lyrics
                # (see SONG_TRANSCREATION_PROMPT), they don't need/use
                # adjacent-line context — only build context for normal dialogue.
                context="" if line.is_song else _build_context(context.dialogue_lines, index),
            )
        context.current_stage = "translated"
        return context
