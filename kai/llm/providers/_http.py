"""
Shared HTTP transport for the cloud provider adapters.

The Anthropic and OpenAI adapters speak different wire formats, but their
plumbing — build a urllib request, POST/GET, map errors to ProviderError, read
the body or iterate SSE lines — is identical. It lives here so a transport fix
(timeout, error wrapping, proxy) is made once, and so ``ProviderError`` is a
single class both adapters share (a per-module copy would mean ``except
ProviderError`` against one silently missing the other).

An adapter subclasses ``BaseHTTPProvider``, sets ``self.base_url``, and overrides
``_headers()``; everything API-specific (payload building, response/stream
parsing, the public chat surface) stays in the adapter.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Generator


class ProviderError(RuntimeError):
    """A cloud call failed (auth, rate limit, network). Carries an HTTP status
    when there is one so the caller can decide whether to fall back to local."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class BaseHTTPProvider:
    """urllib-based transport shared by the cloud adapters.

    Subclasses must set ``self.base_url`` (trailing slash stripped) and override
    ``_headers()``. The ``_open`` / ``_post_json`` / ``_post_stream`` /
    ``_get_json`` methods are the "HTTP seams" that tests monkeypatch on the
    instance, so their names and signatures are part of the contract.
    """

    base_url: str

    def _headers(self) -> dict:
        raise NotImplementedError

    def _open(self, path: str, payload: dict | None, method: str, timeout: int):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=self._headers(),
            method=method,
        )
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")[:500]
            except Exception:
                pass
            raise ProviderError(
                f"{self.base_url}{path} → HTTP {e.code}: {body}", status=e.code
            ) from e
        except Exception as e:
            raise ProviderError(f"{self.base_url}{path} unreachable: {e}") from e

    def _post_json(self, path: str, payload: dict) -> dict:
        with self._open(path, payload, "POST", 120) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post_stream(self, path: str, payload: dict) -> Generator[str, None, None]:
        with self._open(path, payload, "POST", 300) as r:
            for raw in r:
                yield raw.decode("utf-8") if isinstance(raw, bytes) else raw

    def _get_json(self, path: str) -> dict:
        with self._open(path, None, "GET", 15) as r:
            return json.loads(r.read().decode("utf-8"))
