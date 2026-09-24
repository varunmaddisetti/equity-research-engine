"""Polite HTTP client for NSE/BSE.

NSE rejects requests without browser-like headers and the cookies set by its home page,
and rate-limits aggressively. This client:
  - primes cookies by visiting the home page once per session,
  - sends a realistic User-Agent,
  - enforces a minimum interval between requests,
  - retries 401/403/429/5xx with exponential backoff, re-priming cookies on 401/403.
Archive hosts (nsearchives.nseindia.com) usually work without priming, but go through
the same client so rate limits are respected everywhere.
"""

from __future__ import annotations

import time

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
NSE_HOME = "https://www.nseindia.com"
RETRY_STATUSES = {401, 403, 429, 500, 502, 503, 504}


class RetryableHTTPError(Exception):
    pass


class ExchangeClient:
    def __init__(
        self,
        min_interval_s: float = 0.75,
        timeout_s: float = 30.0,
        prime_url: str | None = NSE_HOME,
    ) -> None:
        self._client = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": NSE_HOME + "/",
            },
            timeout=timeout_s,
            follow_redirects=True,
        )
        self._min_interval = min_interval_s
        self._last_request = 0.0
        self._prime_url = prime_url
        self._primed = False

    # -- internals ---------------------------------------------------------
    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _prime(self) -> None:
        if self._prime_url and not self._primed:
            self._throttle()
            self._client.get(self._prime_url)
            self._primed = True

    @retry(
        retry=retry_if_exception_type((RetryableHTTPError, httpx.TransportError)),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _get(self, url: str, params: dict | None) -> httpx.Response:
        self._prime()
        self._throttle()
        resp = self._client.get(url, params=params)
        if resp.status_code in (401, 403):
            self._primed = False  # cookies expired -> re-prime on the retry
        if resp.status_code in RETRY_STATUSES:
            raise RetryableHTTPError(f"{resp.status_code} for {url}")
        return resp

    # -- public API ----------------------------------------------------------
    def get_bytes(self, url: str, params: dict | None = None) -> bytes | None:
        """Return the body, or None on 404 (e.g. no bhavcopy for a market holiday)."""
        resp = self._get(url, params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    def get_json(self, url: str, params: dict | None = None) -> dict | list | None:
        body = self.get_bytes(url, params)
        return None if body is None else httpx.Response(200, content=body).json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ExchangeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
