"""Agent 7 — Educational Content Generator (see docs/agent-pipeline.md).

Runs in parallel, NOT on the video-publish critical path — if it errors, it
MUST NOT fail the whole pipeline (see docs/agent-pipeline.md MVP scope section).

Input: context.dialogue_lines (all translated_text), context.target_language
Output: dict {"vocabulary": [...], "quiz": [...]} — saved to the vocabulary_items table
"""

from worker.models.pipeline_context import PipelineContext
from worker.services.gemini_client import GeminiClient


class EducationalAgent:
    def __init__(self, gemini: GeminiClient):
        self._gemini = gemini

    async def run(self, context: PipelineContext) -> dict:
        full_transcript = "\n".join(
            line.translated_text for line in context.dialogue_lines if line.translated_text
        )
        if not full_transcript.strip():
            return {"vocabulary": [], "quiz": []}

        try:
            return await self._gemini.generate_educational_content(
                full_transcript=full_transcript, target_lang=context.target_language
            )
        except Exception:  # noqa: BLE001 - must not fail the main pipeline
            # TODO: log the error properly (Cloud Logging) instead of
            # silently swallowing the exception
            return {"vocabulary": [], "quiz": []}
