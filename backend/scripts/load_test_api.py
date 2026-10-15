"""HTTP load test for the API service (see docs/backend-spec.md) — fires
concurrent requests at one endpoint and reports latency percentiles,
throughput, and error rate.

Needs the API running (locally via docker-compose, or any deployed URL).
Sends no Authorization header — for local dev, run the API with
AUTH_DEV_BYPASS=true (api/core/config.py) so authenticated endpoints don't
need a real Firebase token; against a deployed environment, pass a real
token with --header "Authorization: Bearer <token>".

Run:
  python -m scripts.load_test_api GET /projects
  python -m scripts.load_test_api GET /projects/<id>/jobs -c 50 -n 500
  python -m scripts.load_test_api GET /jobs/<id>/dialogue-lines -c 20 -n 200

CAUTION: this hits whatever endpoint/method you point it at exactly as
given — pointing it at an endpoint that triggers real work (e.g. POST /jobs,
which publishes to Pub/Sub and gets picked up by the real worker pipeline)
enqueues that many REAL pipeline runs, each making REAL Gemini calls and
costing real quota. Start with a small -n on write endpoints.
"""

import argparse
import asyncio
import json
import time

import httpx

DEFAULT_BASE_URL = "http://localhost:8010"  # API's host-published port in docker-compose.yml

# (latency_seconds, outcome) — outcome is the HTTP status code on a
# completed request, or the exception's str() if the request itself failed
# (connection refused, timeout, ...).
_Result = tuple[float, int | str]


async def _fire(
    client: httpx.AsyncClient, method: str, path: str, body: dict | None
) -> _Result:
    started_at = time.perf_counter()
    try:
        response = await client.request(method, path, json=body)
        return time.perf_counter() - started_at, response.status_code
    except httpx.HTTPError as exc:
        return time.perf_counter() - started_at, str(exc)


def _percentile(sorted_latencies: list[float], p: float) -> float:
    if not sorted_latencies:
        return 0.0
    index = min(int(len(sorted_latencies) * p), len(sorted_latencies) - 1)
    return sorted_latencies[index]


def _is_error(outcome: int | str) -> bool:
    return not isinstance(outcome, int) or outcome >= 400


def summarize(results: list[_Result], total_seconds: float) -> dict:
    """Pure function (no I/O) so the percentile/error-rate math can be unit
    tested without a running server — see test_load_test_api.py."""
    latencies = sorted(seconds for seconds, _outcome in results)
    error_outcomes = sorted(
        {outcome for _seconds, outcome in results if _is_error(outcome)}, key=str
    )
    return {
        "requests": len(results),
        "total_seconds": total_seconds,
        "throughput_rps": len(results) / total_seconds if total_seconds > 0 else 0.0,
        "p50_ms": _percentile(latencies, 0.50) * 1000,
        "p95_ms": _percentile(latencies, 0.95) * 1000,
        "p99_ms": _percentile(latencies, 0.99) * 1000,
        "max_ms": (max(latencies) if latencies else 0.0) * 1000,
        "error_count": sum(1 for _seconds, outcome in results if _is_error(outcome)),
        "error_samples": error_outcomes[:5],
    }


def _print_summary(method: str, path: str, concurrency: int, summary: dict) -> None:
    print(f"\n{method} {path} — {summary['requests']} requests, concurrency={concurrency}")
    print(f"  total time     {summary['total_seconds']:7.2f}s")
    print(f"  throughput     {summary['throughput_rps']:7.1f} req/s")
    print(f"  p50 latency    {summary['p50_ms']:7.0f}ms")
    print(f"  p95 latency    {summary['p95_ms']:7.0f}ms")
    print(f"  p99 latency    {summary['p99_ms']:7.0f}ms")
    print(f"  max latency    {summary['max_ms']:7.0f}ms")
    print(f"  errors         {summary['error_count']}/{summary['requests']}")
    if summary["error_samples"]:
        print(f"  error samples  {summary['error_samples']}")


async def run_load_test(
    base_url: str,
    method: str,
    path: str,
    concurrency: int,
    requests: int,
    body: dict | None,
    headers: dict[str, str],
) -> None:
    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(client: httpx.AsyncClient) -> _Result:
        async with semaphore:
            return await _fire(client, method, path, body)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, headers=headers) as client:
        started_at = time.perf_counter()
        results = await asyncio.gather(*(_bounded(client) for _ in range(requests)))
        total_seconds = time.perf_counter() - started_at

    _print_summary(method, path, concurrency, summarize(results, total_seconds))


def _parse_headers(raw_headers: list[str]) -> dict[str, str]:
    headers = {}
    for raw in raw_headers:
        name, _sep, value = raw.partition(":")
        headers[name.strip()] = value.strip()
    return headers


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("method", help="HTTP method, e.g. GET/POST")
    parser.add_argument("path", help="API path, e.g. /projects or /jobs/<id>/dialogue-lines")
    parser.add_argument("-c", "--concurrency", type=int, default=10)
    parser.add_argument("-n", "--requests", type=int, default=100)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--body", default=None, help="JSON request body for POST/PATCH")
    parser.add_argument(
        "--header", action="append", default=[], help='Extra header, e.g. "Authorization: Bearer x"'
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(
        run_load_test(
            args.base_url,
            args.method.upper(),
            args.path,
            args.concurrency,
            args.requests,
            json.loads(args.body) if args.body else None,
            _parse_headers(args.header),
        )
    )
