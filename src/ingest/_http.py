"""공통 HTTP 헬퍼. tenacity 기반 retry + 단순 rate limiter."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


class RateLimiter:
    """간단한 sliding window 리미터. 멀티 스레드 안전."""

    def __init__(self, requests_per_period: int, period_seconds: float = 1.0):
        self.rps = requests_per_period
        self.period = period_seconds
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > self.period:
                self._calls.popleft()
            if len(self._calls) >= self.rps:
                sleep_for = self.period - (now - self._calls[0]) + 0.001
            else:
                sleep_for = 0.0
            # 슬립은 lock 밖에서? — 다른 thread가 동시에 acquire 시 race 가능.
            # 단순화: lock 안에서 sleep. 처리량 약간 저하되지만 정확함.
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._calls.append(time.monotonic())


def _is_retryable(exc: BaseException) -> bool:
    """네트워크 + 5xx + 429 만 retry. 다른 4xx (401/403/404)는 즉시 fail."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or 500 <= code < 600
    return False


_RETRY = retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception(_is_retryable),
)


class HttpClient:
    def __init__(
        self,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        rps: int = 5,
        timeout: float = 30.0,
        follow_redirects: bool = False,
        period_seconds: float = 1.0,
    ) -> None:
        self.client = httpx.Client(
            base_url=base_url,
            headers=headers or {},
            timeout=timeout,
            follow_redirects=follow_redirects,
        )
        self.limiter = RateLimiter(rps, period_seconds=period_seconds)

    @_RETRY
    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.limiter.acquire()
        resp = self.client.get(url, **kwargs)
        if resp.status_code in (429, 503):
            logger.warning("Backoff: %s -> %d", url, resp.status_code)
            resp.raise_for_status()
        resp.raise_for_status()
        return resp

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
