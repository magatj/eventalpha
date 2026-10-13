from datetime import timedelta

import httpx
import pytest

from app.services.http import (
    DataUnavailable,
    HttpClient,
    MemoryCache,
    ProviderAccessDenied,
    ProviderError,
    RateLimiter,
    cache_key,
)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_rate_limiter_blocks_after_quota():
    clock = FakeClock()
    rl = RateLimiter(5, 60.0, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        assert rl.acquire() == 0
    waited = rl.acquire()  # 6th call must wait for the window to roll
    assert 59 < waited < 61


def make_client(handler, **kw):
    sleeps = []
    c = HttpClient("test", base_url="https://api.test", transport=httpx.MockTransport(handler),
                   sleep=sleeps.append, backoff_base=0.01, **kw)
    return c, sleeps


def test_retries_on_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"ok": True})

    c, sleeps = make_client(handler)
    assert c.get_json("/x") == {"ok": True}
    assert calls["n"] == 3 and len(sleeps) == 2
    assert sleeps[1] > sleeps[0]  # exponential backoff


def test_respects_retry_after_on_429():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "7"}) if calls["n"] == 1 else httpx.Response(200, json={})

    c, sleeps = make_client(handler)
    c.get_json("/x")
    assert sleeps[0] >= 7


def test_gives_up_after_max_retries():
    c, _ = make_client(lambda req: httpx.Response(500), max_retries=2)
    with pytest.raises(ProviderError) as e:
        c.get_json("/x")
    assert e.value.retryable


def test_network_errors_are_retried_then_raised():
    def handler(req):
        raise httpx.ConnectTimeout("boom")

    c, sleeps = make_client(handler, max_retries=1)
    with pytest.raises(ProviderError, match="network error"):
        c.get_json("/x")
    assert len(sleeps) == 1


def test_404_and_403_map_to_typed_errors():
    c, _ = make_client(lambda req: httpx.Response(404))
    with pytest.raises(DataUnavailable):
        c.get_json("/x")
    c, _ = make_client(lambda req: httpx.Response(403, json={"message": "NOT_AUTHORIZED"}))
    with pytest.raises(ProviderAccessDenied, match="NOT_AUTHORIZED"):
        c.get_json("/x")


def test_invalid_json_is_a_provider_error():
    c, _ = make_client(lambda req: httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderError, match="invalid JSON"):
        c.get_json("/x")


def test_cache_prevents_second_request():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"v": calls["n"]})

    c, _ = make_client(handler, cache=MemoryCache())
    assert c.get_json("/x", {"a": 1}, cache_ttl=timedelta(minutes=5)) == {"v": 1}
    assert c.get_json("/x", {"a": 1}, cache_ttl=timedelta(minutes=5)) == {"v": 1}
    assert calls["n"] == 1


def test_cache_key_excludes_secrets():
    assert cache_key("p", "/u", {"apiKey": "secret1", "q": 1}) == cache_key("p", "/u", {"apiKey": "other", "q": 1})
