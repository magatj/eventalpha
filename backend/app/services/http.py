"""Shared HTTP plumbing for external providers: rate limiting, retries with
exponential backoff, timeouts, response caching and typed provider errors."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """A provider call failed. `retryable` tells callers whether trying later may help."""

    def __init__(self, provider: str, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.message = message
        self.status = status
        self.retryable = retryable


class ProviderNotConfigured(ProviderError):
    def __init__(self, provider: str, message: str):
        super().__init__(provider, message, retryable=False)


class DataUnavailable(ProviderError):
    """The provider answered, but has no data for this request (unknown ticker, no coverage...)."""


class ProviderAccessDenied(ProviderError):
    """The plan/key does not cover this endpoint (e.g. 403 NOT_AUTHORIZED on a free tier)."""


class RateLimiter:
    """Thread-safe sliding-window limiter: at most `max_calls` per `period` seconds."""

    def __init__(self, max_calls: int, period: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.max_calls = max(1, max_calls)
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep

    def acquire(self) -> float:
        """Block until a call is allowed. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return waited
                wait = self.period - (now - self._calls[0]) + 0.01
            self._sleep(wait)
            waited += wait


class ResponseCache(Protocol):
    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, provider: str, url: str, value: Any, ttl: timedelta) -> None: ...


class MemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[datetime, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item and item[0] > datetime.now(UTC):
            return item[1]
        return None

    def set(self, key: str, provider: str, url: str, value: Any, ttl: timedelta) -> None:
        self._data[key] = (datetime.now(UTC) + ttl, value)


class DbCache:
    """Response cache persisted in PostgreSQL so it is shared by API, worker and scheduler."""

    def get(self, key: str) -> Any | None:
        from app.db import session_scope
        from app.models import HttpCache

        try:
            with session_scope() as s:
                row = s.get(HttpCache, key)
                if row and row.expires_at > datetime.now(UTC):
                    return row.response
        except Exception as exc:  # cache must never break a request
            log.warning("cache read failed: %s", exc)
        return None

    def set(self, key: str, provider: str, url: str, value: Any, ttl: timedelta) -> None:
        from sqlalchemy.dialects.postgresql import insert

        from app.db import session_scope
        from app.models import HttpCache

        now = datetime.now(UTC)
        try:
            with session_scope() as s:
                stmt = insert(HttpCache).values(
                    key=key, provider=provider, url=url, response=value, fetched_at=now, expires_at=now + ttl
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["key"],
                    set_={"response": value, "fetched_at": now, "expires_at": now + ttl},
                )
                s.execute(stmt)
        except Exception as exc:
            log.warning("cache write failed: %s", exc)


SENSITIVE_PARAMS = {"apikey", "api_key", "token", "key"}


def cache_key(provider: str, url: str, params: dict[str, Any] | None) -> str:
    clean = {k: v for k, v in sorted((params or {}).items()) if k.lower() not in SENSITIVE_PARAMS}
    raw = f"{provider}|{url}|{json.dumps(clean, sort_keys=True, default=str)}"
    return hashlib.sha256(raw.encode()).hexdigest()


class HttpClient:
    def __init__(
        self,
        provider: str,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        rate_limiter: RateLimiter | None = None,
        timeout: float = 20.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        cache: ResponseCache | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.provider = provider
        self.rate_limiter = rate_limiter
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.cache = cache
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=base_url, headers=headers or {}, timeout=httpx.Timeout(timeout),
            transport=transport, follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def get_json(self, url: str, params: dict[str, Any] | None = None, cache_ttl: timedelta | None = None) -> Any:
        return self._get(url, params, cache_ttl, as_json=True)

    def get_text(self, url: str, params: dict[str, Any] | None = None, cache_ttl: timedelta | None = None) -> str:
        return self._get(url, params, cache_ttl, as_json=False)

    def _get(self, url: str, params: dict[str, Any] | None, cache_ttl: timedelta | None, as_json: bool) -> Any:
        key = cache_key(self.provider, str(self._client.base_url) + url, params)
        if cache_ttl and self.cache:
            hit = self.cache.get(key)
            if hit is not None:
                return hit if as_json else hit.get("text", "")

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if self.rate_limiter:
                self.rate_limiter.acquire()
            try:
                resp = self._client.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = ProviderError(self.provider, f"network error: {type(exc).__name__}", retryable=True)
                self._backoff(attempt, None)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = ProviderError(
                    self.provider, f"HTTP {resp.status_code} from provider", resp.status_code, retryable=True
                )
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            if resp.status_code in (401, 403):
                raise ProviderAccessDenied(
                    self.provider,
                    f"HTTP {resp.status_code}: not authorized (check API key / plan coverage). {_snippet(resp)}",
                    resp.status_code,
                )
            if resp.status_code == 404:
                raise DataUnavailable(self.provider, f"not found: {url}", 404)
            if resp.status_code >= 400:
                raise ProviderError(self.provider, f"HTTP {resp.status_code}: {_snippet(resp)}", resp.status_code)

            if as_json:
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise ProviderError(self.provider, "invalid JSON in response") from exc
                payload: Any = data
            else:
                payload = {"text": resp.text}
            if cache_ttl and self.cache:
                self.cache.set(key, self.provider, url, payload, cache_ttl)
            return payload if as_json else payload["text"]

        assert last_exc is not None
        raise last_exc

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if attempt >= self.max_retries:
            return
        delay = self.backoff_base * (2**attempt) + random.uniform(0, 0.25 * self.backoff_base)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        delay = min(delay, 65.0)
        log.info("%s: retrying in %.1fs (attempt %d)", self.provider, delay, attempt + 1)
        self._sleep(delay)


def _snippet(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("message") or body.get("error") or body.get("status")
        return str(msg)[:200] if msg else ""
    except Exception:
        return resp.text[:200]
