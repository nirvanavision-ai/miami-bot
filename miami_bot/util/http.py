"""A single shared HTTP client with retries, per-host rate limiting and an
optional on-disk response cache.

Every outbound call in the project goes through :class:`HttpClient` so that
retry policy, timeouts, quota-friendly pacing and error taxonomy are uniform
across the RapidAPI wrappers, RealtyAPI, ScrapingBee, the AVM providers and the
county GIS endpoints.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .logging import get_logger

log = get_logger(__name__)


class HttpError(RuntimeError):
    """Base class for transport failures surfaced to callers."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class RateLimited(HttpError):
    """429 / quota exhaustion. Callers may degrade instead of failing hard."""


class AuthError(HttpError):
    """401 / 403 -- a bad or missing key. Never worth retrying."""


class NotFound(HttpError):
    """404 -- resource absent. Usually a soft outcome for enrichment lookups."""


@dataclass
class _HostState:
    last_request_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class HttpClient:
    """Thread-safe requests wrapper.

    Parameters mirror the ``HTTP_*`` environment variables so the pipeline can
    build one client from settings and hand it to every adapter.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        rate_limit_seconds: float = 1.0,
        cache_enabled: bool = False,
        cache_dir: str = "data/cache",
        cache_ttl_seconds: int = 900,
        user_agent: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.rate_limit_seconds = rate_limit_seconds
        self.cache_enabled = cache_enabled
        self.cache_dir = Path(cache_dir)
        self.cache_ttl_seconds = cache_ttl_seconds

        self._hosts: dict[str, _HostState] = {}
        self._hosts_lock = threading.Lock()

        self.session = requests.Session()
        # Retry only on transport-level and server-side faults. 429 is retried
        # here too because most wrapper APIs use short, burst-shaped windows.
        retry = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            status=max_retries,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST", "HEAD"}),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {
                "User-Agent": user_agent
                or "MiamiCondoMonitor/1.0 (+https://github.com/nirvanavision-ai/miami-bot)",
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            }
        )

    # ------------------------------------------------------------------ pacing
    def _throttle(self, url: str) -> None:
        if self.rate_limit_seconds <= 0:
            return
        host = urlparse(url).netloc
        with self._hosts_lock:
            state = self._hosts.setdefault(host, _HostState())
        with state.lock:
            elapsed = time.monotonic() - state.last_request_at
            wait = self.rate_limit_seconds - elapsed
            if wait > 0:
                time.sleep(wait)
            state.last_request_at = time.monotonic()

    # ------------------------------------------------------------------- cache
    def _cache_key(self, method: str, url: str, params: Mapping[str, Any] | None,
                   body: Any | None) -> Path:
        payload = json.dumps(
            {"m": method, "u": url, "p": _sortable(params), "b": _sortable(body)},
            sort_keys=True,
            default=str,
        )
        digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()
        host = urlparse(url).netloc.replace(":", "_") or "unknown"
        return self.cache_dir / host / f"{digest}.json"

    def _cache_read(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > self.cache_ttl_seconds:
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _cache_write(self, path: Path, status: int, text: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"status": status, "text": text}), "utf-8")
        except OSError as exc:  # cache failures must never break a run
            log.debug("cache write failed for %s: %s", path, exc)

    # ----------------------------------------------------------------- request
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        data: Any | None = None,
        auth: tuple[str, str] | None = None,
        timeout: float | None = None,
        allow_cache: bool = True,
    ) -> requests.Response:
        """Perform a request, raising the typed errors above on failure."""
        cache_path = None
        if self.cache_enabled and allow_cache and method.upper() == "GET":
            cache_path = self._cache_key(method, url, params, json_body or data)
            cached = self._cache_read(cache_path)
            if cached is not None:
                log.debug("cache hit %s %s", method, url)
                return _synthetic_response(url, cached["status"], cached["text"])

        self._throttle(url)
        log.debug("%s %s params=%s", method, url, _sortable(params))

        try:
            response = self.session.request(
                method.upper(),
                url,
                params=params,
                headers=dict(headers or {}),
                json=json_body,
                data=data,
                auth=auth,
                timeout=timeout or self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise HttpError(f"timeout after {timeout or self.timeout}s: {url}") from exc
        except requests.exceptions.RequestException as exc:
            raise HttpError(f"transport error for {url}: {exc}") from exc

        body_preview = (response.text or "")[:400]
        if response.status_code in (401, 403):
            raise AuthError(
                f"auth rejected ({response.status_code}) for {url}",
                status=response.status_code,
                body=body_preview,
            )
        if response.status_code == 404:
            raise NotFound(f"not found: {url}", status=404, body=body_preview)
        if response.status_code == 429:
            raise RateLimited(f"rate limited: {url}", status=429, body=body_preview)
        if response.status_code >= 400:
            raise HttpError(
                f"HTTP {response.status_code} for {url}",
                status=response.status_code,
                body=body_preview,
            )

        if cache_path is not None:
            self._cache_write(cache_path, response.status_code, response.text)
        return response

    def get_json(self, url: str, **kwargs: Any) -> Any:
        response = self.request("GET", url, **kwargs)
        return _decode_json(response, url)

    def post_json(self, url: str, **kwargs: Any) -> Any:
        response = self.request("POST", url, **kwargs)
        return _decode_json(response, url)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _decode_json(response: requests.Response, url: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise HttpError(
            f"non-JSON response from {url}", status=response.status_code,
            body=(response.text or "")[:400],
        ) from exc


def _sortable(value: Any) -> Any:
    """Make params/bodies stable for cache keys and safe for debug logs."""
    if isinstance(value, Mapping):
        return {str(k): _sortable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_sortable(v) for v in value]
    return value


def _synthetic_response(url: str, status: int, text: str) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = text.encode("utf-8")  # noqa: SLF001 -- constructing a stub
    response.url = url
    response.encoding = "utf-8"
    return response
