"""Polite HTTP client for NSE/BSE.

NSE rejects requests without browser-like headers and the cookies set by its home page,
and rate-limits aggressively. This client:
  - primes cookies by visiting the home page once per session (www.nseindia.com APIs),
  - sends a realistic User-Agent,
  - enforces a minimum interval between requests,
  - retries 401/403/429/5xx and network errors a few times with backoff,
  - enforces a HARD deadline per request (`max_seconds`, whole download included).

Why the hard deadline (learned on the first full XBRL run, Sept 2026): after a few hundred
quick downloads NSE's CDN can stop answering a client's connection without refusing it (a
"tarpit"). httpx's timeouts reset every time a byte arrives, so a trickling or silent
connection could wait almost indefinitely while a fresh connection (curl) was served in
0.06 s. So a stalled request is abandoned after `max_seconds`, the connection pool is thrown
away, and the retry goes out on a brand-new connection.
"""

from __future__ import annotations

import time

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
NSE_HOME = "https://www.nseindia.com"
RETRY_STATUSES = {401, 403, 429, 500, 502, 503, 504}


class RetryableHTTPError(Exception):
    pass


class StalledRequest(RetryableHTTPError):
    """The server stopped sending before the hard deadline."""


class ExchangeClient:
    def __init__(
        self,
        min_interval_s: float = 0.75,
        timeout_s: float = 15.0,
        prime_url: str | None = NSE_HOME,
        max_seconds: float = 30.0,
        attempts: int = 3,
        backoff_s: tuple[float, ...] = (2.0, 6.0, 15.0),
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 10.0))
        self._transport = transport
        self._client = self._new_client()
        self._min_interval = min_interval_s
        self._last_request = 0.0
        self._prime_url = prime_url
        self._primed = False
        self.max_seconds = max_seconds
        self.attempts = attempts
        self.backoff_s = backoff_s
        self.reconnects = 0

    # -- internals ---------------------------------------------------------
    def _new_client(self) -> httpx.Client:
        return httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": NSE_HOME + "/",
            },
            timeout=self._timeout,
            follow_redirects=True,
            transport=self._transport,
        )

    def _reconnect(self) -> None:
        """Drop every pooled connection and start over (also forgets cookies)."""
        try:
            self._client.close()
        finally:
            self._client = self._new_client()
            self._primed = False
            self.reconnects += 1

    def set_min_interval(self, seconds: float) -> None:
        self._min_interval = max(self._min_interval, seconds)

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _prime(self) -> None:
        if self._prime_url and not self._primed:
            self._throttle()
            try:
                self._client.get(self._prime_url, timeout=self._timeout)
            finally:
                self._primed = True

    def _fetch_once(self, url: str, params: dict | None) -> tuple[int, bytes]:
        """One request with a hard deadline on the whole download."""
        self._prime()
        self._throttle()
        start = time.monotonic()
        chunks: list[bytes] = []
        with self._client.stream("GET", url, params=params) as resp:
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                if time.monotonic() - start > self.max_seconds:
                    raise StalledRequest(f"no complete response within {self.max_seconds:.0f}s")
            status = resp.status_code
        if time.monotonic() - start > self.max_seconds:
            raise StalledRequest(f"no complete response within {self.max_seconds:.0f}s")
        return status, b"".join(chunks)

    def _get(self, url: str, params: dict | None) -> tuple[int, bytes]:
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                status, body = self._fetch_once(url, params)
                if status in (401, 403):
                    self._primed = False  # cookies expired -> re-prime on the retry
                if status in RETRY_STATUSES:
                    raise RetryableHTTPError(f"HTTP {status} for {url}")
                return status, body
            except (StalledRequest, httpx.TimeoutException, httpx.TransportError) as e:
                last = e
                self._reconnect()  # a stalled connection is never reused
            except RetryableHTTPError as e:
                last = e
            if attempt < self.attempts - 1:
                time.sleep(self.backoff_s[min(attempt, len(self.backoff_s) - 1)])
        raise last if last else RetryableHTTPError(f"failed: {url}")

    # -- public API ----------------------------------------------------------
    def get_bytes(self, url: str, params: dict | None = None) -> bytes | None:
        """Return the body, or None on 404 (e.g. no bhavcopy for a market holiday)."""
        status, body = self._get(url, params)
        if status == 404:
            return None
        if status >= 400:
            raise httpx.HTTPStatusError(f"HTTP {status} for {url}", request=None, response=None)
        return body

    def get_json(self, url: str, params: dict | None = None) -> dict | list | None:
        body = self.get_bytes(url, params)
        return None if body is None else httpx.Response(200, content=body).json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ExchangeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
