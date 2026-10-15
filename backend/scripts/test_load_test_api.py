"""Unit tests for scripts/load_test_api.py's pure summary math — no running
server needed, these just check summarize()/_percentile() against
hand-computed results."""

from scripts.load_test_api import summarize


def test_summarize_reports_percentiles_and_throughput_with_no_errors():
    # 10 requests, latencies 0.01s..0.10s, all status 200 — total_seconds
    # chosen so throughput comes out to a round number for an easy assert.
    results = [(seconds / 100, 200) for seconds in range(1, 11)]

    summary = summarize(results, total_seconds=2.0)

    assert summary["requests"] == 10
    assert summary["throughput_rps"] == 5.0
    # p50 of 10 sorted values [0.01..0.10] at index int(10*0.5)=5 -> 0.06s
    assert summary["p50_ms"] == 60.0
    assert summary["max_ms"] == 100.0
    assert summary["error_count"] == 0
    assert summary["error_samples"] == []


def test_summarize_counts_4xx_5xx_and_connection_errors_as_errors():
    results = [
        (0.01, 200),
        (0.01, 201),
        (0.02, 404),
        (0.02, 500),
        (0.03, "ConnectError: all connection attempts failed"),
    ]

    summary = summarize(results, total_seconds=1.0)

    assert summary["error_count"] == 3
    assert 404 in summary["error_samples"]
    assert 500 in summary["error_samples"]
    assert "ConnectError: all connection attempts failed" in summary["error_samples"]


def test_summarize_handles_empty_results_without_dividing_by_zero():
    summary = summarize([], total_seconds=0.0)

    assert summary["requests"] == 0
    assert summary["throughput_rps"] == 0.0
    assert summary["p50_ms"] == 0.0
    assert summary["max_ms"] == 0.0
    assert summary["error_count"] == 0
