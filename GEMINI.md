@AGENTS.md

## Gemini CLI-specific additions

- When creating a new pipeline agent (`backend/worker/agents/`), use the
  `gemini-pipeline-agent` skill (`.gemini/skills/gemini-pipeline-agent/SKILL.md`) —
  it ensures the interface matches the standard and comes with tests from the start.
- When adding a new FastAPI endpoint, use the `fastapi-agent-endpoint` skill (`.gemini/skills/fastapi-agent-endpoint/SKILL.md`).
- When adding a new React page/screen, use the `react-dashboard-page` skill (`.gemini/skills/react-dashboard-page/SKILL.md`).
- Before opening a PR or considering a task done, run the `code-reviewer` subagent
  (`.gemini/agents/code-reviewer.md`) to self-review against the project checklist.
- When editing a prompt that calls Gemini (translation, safety, dubbing), always run the
  `gemini-prompt-reviewer` subagent (`.gemini/agents/gemini-prompt-reviewer.md`) — this directly affects the competition's "Meaningful
  use of Gen AI" score, so it needs closer scrutiny than regular code.
- Use the Spec Kit slash commands (`.gemini/commands/speckit.*.toml`) when starting a
  major new feature that isn't yet in `specs/`: `/speckit.specify` → `/speckit.plan`
  → `/speckit.tasks` → `/speckit.implement`. For small changes/bug fixes, code directly
  without going through this process.
- Hooks already configured in `.gemini/settings.json` automatically run `format.sh` after
  editing a file and `verify.sh` before ending a session — no need to remind yourself to
  run these manually unless a hook reports an error.
