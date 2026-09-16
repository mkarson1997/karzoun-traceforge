from traceforge.benchmark import run_privacy_benchmark


def test_privacy_benchmark_detects_no_known_synthetic_leaks() -> None:
    result = run_privacy_benchmark(iterations=5)

    assert result["mode"] == "privacy"
    assert result["iterations"] == 5
    assert result["privacy_leaks"] == 0
    assert result["leaked_markers"] == []
    assert result["spans_scrubbed"] == 20
    assert result["privacy_findings"] > 0
    assert result["requests_per_second"] > 0
