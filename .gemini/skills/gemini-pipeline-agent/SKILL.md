---
name: gemini-pipeline-agent
description: Create a new agent in the worker pipeline (backend/worker/agents/) following XIVI's standard interface, with a test included. Use when a new processing step needs to be added to the dubbing pipeline (e.g. an audio quality check agent, an auto song-detection agent).
---

# Skill: Create a new Gemini Pipeline Agent

## When to use
When you need to add a new processing step to the agent pipeline (see `docs/agent-pipeline.md`
for the existing agents in the pipeline and their run order).

## Required steps

1. **Determine its place in the pipeline**: which agent does the new one run after, and
   which one before? Update the `AGENTS` list in `backend/worker/pipeline.py`.
2. **Create the agent file** at `backend/worker/agents/<agent_name>_agent.py`, implementing
   the interface in `templates/agent_template.py` (see the sample file in this skill's
   `templates/` directory).
3. **Never call Gemini directly in the agent** — always go through
   `backend/worker/services/gemini_client.py`, adding a new method there if needed
   (to keep retry/cache logic centralized in one place).
4. **Prompts must live in `backend/shared/prompts.py`**, never hardcoded as a string
   in the agent file — this makes it easy to review/tune the prompt in isolation.
5. **Write a test** at `backend/worker/agents/test_<agent_name>_agent.py`, mocking
   the `gemini_client` response (never call the real API in a test), verifying the agent
   correctly handles the input/output `PipelineContext`.
6. **Update `docs/agent-pipeline.md`** adding a line describing the new agent to the summary table.
7. If the agent can fail and needs to flag something for a human reviewer (like the Safety Agent) —
   add the corresponding field to `PipelineContext` (`backend/worker/models/pipeline_context.py`)
   and the corresponding column in the DB (`dialogue_lines` or a new table), updating
   `docs/database-schema.md`.

## Checklist before considering it done
- [ ] The agent correctly implements the `Agent` protocol (`run(context) -> context`, async)
- [ ] There is no Gemini call anywhere outside `gemini_client.py`
- [ ] There's a test with at least 1 valid input case + 1 case where Gemini returns an error
- [ ] It's been added to the `AGENTS` list in `pipeline.py` in the correct order
- [ ] `docs/agent-pipeline.md` has been updated
