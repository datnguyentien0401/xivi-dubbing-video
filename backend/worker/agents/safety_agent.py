"""Agent 4 — Cultural Safety Checker (see docs/agent-pipeline.md).

Input: context.dialogue_lines (translated_text), context.target_language
Output: each dialogue_line gets safety_status ("ok"/"flagged") + safety_reasoning

Does NOT automatically block content — only flags + explains, the human
reviewer makes the final call (see AGENTS.md principle #5).
"""

from collections.abc import Awaitable, Callable

from worker.models.pipeline_context import PipelineContext
from worker.services.gemini_client import GeminiClient

# Returns {target_market: combined_rule_text}, read from the cultural_safety_rules
# table — do NOT hardcode rules in code (AGENTS.md principle #6). worker/main.py
# wires up the real implementation (queries the DB); tests pass a fake async callable.
RulesLoader = Callable[[], Awaitable[dict[str, str]]]


class SafetyAgent:
    def __init__(self, gemini: GeminiClient, rules_loader: RulesLoader):
        self._gemini = gemini
        self._rules_loader = rules_loader

    async def run(self, context: PipelineContext) -> PipelineContext:
        rules_by_market = await self._rules_loader()
        market_rules = rules_by_market.get(context.target_language, "")
        for line in context.dialogue_lines:
            if not line.translated_text:
                continue
            result = await self._gemini.check_cultural_safety(
                text=line.translated_text,
                target_market=context.target_language,
                market_rules=market_rules,
            )
            line.safety_status = result["safety_status"]
            line.safety_reasoning = result["reasoning"]
        context.current_stage = "safety_checked"
        return context
