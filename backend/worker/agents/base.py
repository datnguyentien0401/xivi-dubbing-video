"""Standard interface for every pipeline agent. See the gemini-pipeline-agent
skill (.gemini/skills/gemini-pipeline-agent/SKILL.md) when creating a new agent."""

from typing import Protocol

from worker.models.pipeline_context import PipelineContext


class Agent(Protocol):
    async def run(self, context: PipelineContext) -> PipelineContext:
        """Process the context, return the updated context. MUST NOT raise an
        unhandled exception — catch errors and record them in the context if
        needed, so the pipeline can correctly decide whether to resume/stop
        (see pipeline.py)."""
        ...
