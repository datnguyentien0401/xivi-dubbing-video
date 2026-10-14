"""Orchestrates running the agents sequentially + checkpoint/resume (see
docs/architecture.md section 5 — "State is lost if the worker crashes
mid-run").

When adding a new agent, update the AGENTS list here (see the
gemini-pipeline-agent skill).
"""

import logging
import tempfile

from worker.agents.dubbing_agent import DubbingAgent
from worker.agents.educational_agent import EducationalAgent
from worker.agents.ingestion_agent import IngestionAgent
from worker.agents.safety_agent import SafetyAgent
from worker.agents.script_benchmark_agent import ScriptBenchmarkAgent
from worker.agents.translation_agent import TranslationAgent
from worker.agents.voice_profiling_agent import VoiceProfilingAgent
from worker.models.pipeline_context import PipelineContext
from worker.services.mux_service import mux_for_job
from worker.services.storage import StorageClient

logger = logging.getLogger(__name__)

# Fixed order — see docs/agent-pipeline.md for why this exact order matters
# (e.g. Safety must run after Translation because it needs translated_text to
# review; "muxed" is not a Gemini agent — see worker/services/mux_service.py)
_STAGE_ORDER = [
    "transcribed",
    "voice_profiled",
    "translated",
    "safety_checked",
    "dubbed",
    "muxed",
]


def _stage_index(current_stage: str) -> int:
    if current_stage in ("queued", ""):
        return 0
    try:
        return _STAGE_ORDER.index(current_stage) + 1
    except ValueError:
        return 0


class Pipeline:
    def __init__(
        self,
        ingestion: IngestionAgent,
        script_benchmark: ScriptBenchmarkAgent,
        voice_profiling: VoiceProfilingAgent,
        translation: TranslationAgent,
        safety: SafetyAgent,
        dubbing: DubbingAgent,
        educational: EducationalAgent,
        gcs: StorageClient,
    ):
        # The "transcribed" stage's agent is NOT fixed here — see run() below,
        # which picks between ingestion/script_benchmark per-context instead
        # (specs/003-script-first-ingestion). Keep both instances, plus the
        # fixed remainder of the pipeline (same agent regardless of how
        # "transcribed" was reached — VoiceProfilingAgent/TranslationAgent/
        # SafetyAgent/DubbingAgent all consume dialogue_lines identically
        # either way).
        self._ingestion = ingestion
        self._script_benchmark = script_benchmark
        # Each element: (stage_name_after_it_runs, agent_instance)
        self._rest_agents = [
            ("voice_profiled", voice_profiling),
            ("translated", translation),
            ("safety_checked", safety),
            ("dubbed", dubbing),
        ]
        self._educational = educational
        self._gcs = gcs

    async def _run_mux_stage(self, context: PipelineContext) -> PipelineContext:
        with tempfile.TemporaryDirectory(prefix=f"mux-{context.job_id}-") as tmp_dir:
            context.muxed_video_gcs_uri = await mux_for_job(context, self._gcs, tmp_dir)
        return context

    async def run(
        self,
        context: PipelineContext,
        save_checkpoint,  # async callable(context) -> None — writes to Postgres
        update_realtime_status,  # async callable(context) -> None — writes to Firestore
    ) -> tuple[PipelineContext, dict]:
        start_index = _stage_index(context.current_stage)
        # A script-first job (no real recording, specs/003-script-first-
        # ingestion) has nothing for IngestionAgent to transcribe —
        # ScriptBenchmarkAgent establishes the timing benchmark from text
        # alone instead. This choice is always derivable from `context`
        # alone, so it's safe across a mid-pipeline crash/resume (the same
        # variant is picked again deterministically on retry, regardless of
        # which process/session is running).
        transcribe_agent = (
            self._script_benchmark if context.source_video_gcs_uri is None else self._ingestion
        )
        stages = [("transcribed", transcribe_agent), *self._rest_agents]
        # Likewise, there's no video to mux until one is later attached (see
        # POST /jobs/{id}/attach-video) — omit the stage entirely rather than
        # running it against a job with nothing to mux. The loop below then
        # naturally stops after "dubbed", and the unconditional tail
        # (current_stage = "ready_for_review") below still runs regardless,
        # reaching exactly the audio-only review state a script-first job
        # needs with no new stage name required.
        if context.source_video_gcs_uri is not None:
            stages = [*stages, ("muxed", None)]

        for stage_name, agent in stages[start_index:]:
            logger.info("job %s: running stage %s", context.job_id, stage_name)
            if stage_name == "muxed":
                context = await self._run_mux_stage(context)
            else:
                context = await agent.run(context)
            context.current_stage = stage_name
            await save_checkpoint(context)
            await update_realtime_status(context)

        # Educational content runs in parallel/last, does NOT block the
        # critical path and MUST NOT fail the whole job if it errors (see
        # educational_agent.py)
        educational_result = await self._educational.run(context)
        context.current_stage = "ready_for_review"
        await save_checkpoint(context)
        await update_realtime_status(context)

        return context, educational_result
