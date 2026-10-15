"""Per-agent latency benchmark for the pipeline (see docs/agent-pipeline.md).

Runs a REAL job through the REAL Pipeline — real Gemini calls, real ffmpeg
mux if the job has a video — by calling worker/main.py's own
`_process_job` (the same function `handle_pubsub_push` uses), so the
numbers reflect actual processing latency, not a mocked estimate.

Does NOT create a job for you — bring your own, so the benchmark exercises
exactly the scenario you care about (e.g. a short vs. long episode,
script-first vs. video-first):
  1. Create a project + job as usual (e.g. via the API, or the sample script
     import at docs/sample-script-import.csv for a script-first job with no
     video/mux step).
  2. Note its job_id — current_stage must be resumable: "queued" for a fresh
     job, or any of worker/pipeline.py's _STAGE_ORDER values to re-benchmark
     just the remaining stages.
  3. Run: python -m scripts.benchmark_pipeline <job_id>

Each run appends one line to benchmark_results.jsonl (next to this script)
with the job_id, target language, timestamp, and per-stage seconds — so
multiple runs/episodes can be compared over time instead of only seeing the
latest one.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from worker.main import _process_job

_RESULTS_FILE = Path(__file__).parent / "benchmark_results.jsonl"


async def run_benchmark(job_id: str) -> None:
    status, stage_durations = await _process_job(job_id)
    if stage_durations is None:
        print(f"job {job_id}: nothing to benchmark (status={status!r})")
        print(
            "Pass a job_id whose current_stage is resumable (\"queued\" or one "
            "of worker/pipeline.py's _STAGE_ORDER values), not one already at "
            '"ready_for_review".'
        )
        return

    print(f"\nPer-agent latency for job {job_id} (slowest first):")
    for stage, seconds in sorted(stage_durations.items(), key=lambda item: -item[1]):
        print(f"  {stage:<16} {seconds:7.2f}s")
    print(f"  {'TOTAL':<16} {sum(stage_durations.values()):7.2f}s")

    with _RESULTS_FILE.open("a") as f:
        f.write(
            json.dumps(
                {
                    "job_id": job_id,
                    "ran_at": datetime.now(UTC).isoformat(),
                    "stage_durations": stage_durations,
                }
            )
            + "\n"
        )
    print(f"\nAppended to {_RESULTS_FILE}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.benchmark_pipeline <job_id>")
        sys.exit(1)
    asyncio.run(run_benchmark(sys.argv[1]))
