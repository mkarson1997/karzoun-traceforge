from traceforge.gateway.rate_limit import ClientRateLimiter


def test_rate_limiter_enforces_sliding_window_per_client() -> None:
    now = [100.0]
    limiter = ClientRateLimiter(2, clock=lambda: now[0])

    assert limiter.allow("client-a")
    assert limiter.allow("client-a")
    assert not limiter.allow("client-a")
    assert limiter.allow("client-b")

    now[0] += 60.01
    assert limiter.allow("client-a")


def test_rate_limiter_bounds_client_tracking_and_recovers_after_expiry() -> None:
    now = [100.0]
    limiter = ClientRateLimiter(1, max_clients=2, clock=lambda: now[0])

    assert limiter.allow("client-a")
    assert limiter.allow("client-b")
    assert limiter.tracked_clients == 2
    assert not limiter.allow("client-c")

    now[0] += 60.01
    assert limiter.allow("client-c")
    assert limiter.tracked_clients == 1


def test_zero_rate_disables_limiter_without_tracking_clients() -> None:
    limiter = ClientRateLimiter(0)

    assert limiter.allow("client-a")
    assert limiter.allow("client-a")
    assert limiter.tracked_clients == 0
