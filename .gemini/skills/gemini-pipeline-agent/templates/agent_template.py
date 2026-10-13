"""Skeleton template for a new pipeline agent. Copy, rename the class, fill in the logic."""
from worker.agents.base import Agent
from worker.models.pipeline_context import PipelineContext
from worker.services.gemini_client import GeminiClient


class TemplateAgent(Agent):
    """TODO: brief description of what this agent does and why it needs to be separate."""

    def __init__(self, gemini_client: GeminiClient):
        self._gemini = gemini_client

    async def run(self, context: PipelineContext) -> PipelineContext:
        # TODO: read the data you need from context (e.g. context.dialogue_lines)
        # TODO: call self._gemini.<new_method>(...) — add the method to gemini_client.py if it doesn't exist yet
        # TODO: write the result back to context, do NOT mutate unrelated fields
        return context
