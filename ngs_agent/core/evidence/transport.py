"""HTTP transport for evidence adapters.

The transport is an injectable seam for three reasons:

1. **Deterministic tests.** Adapter behaviour is verified against recorded
   responses, never against a live endpoint that can change under us.
2. **Customer-controlled egress.** A deployment can substitute a transport that
   routes through a corporate proxy, an air-gapped mirror, or a local cache
   server, without any adapter changing.
3. **Honest failure.** Transport failures raise; they do not return empty
   payloads that an adapter might misread as "variant absent".

``httpx`` is imported lazily so that the signed classification path — which
never needs the network — has no hard dependency on it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ngs_agent.core.errors import AdapterError
from ngs_agent.core.hashing import sha256_bytes

#: Base URL of the NCBI E-utilities service used by ClinVar and other NCBI
#: databases. Declared here (rather than in an adapter) so recording loaders can
#: reconstruct request keys without importing adapter code.
EUTILS_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


class TransportError(AdapterError):
    """The request could not be completed. Never means "no data found"."""


@dataclass(frozen=True)
class HttpResponse:
    """A transport-level response, reduced to what an adapter needs."""

    status_code: int
    body: bytes
    url: str
    elapsed_ms: int

    @property
    def body_sha256(self) -> str:
        return sha256_bytes(self.body)

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError(f"response from {self.url} is not valid JSON: {exc}") from exc

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class HttpTransport(Protocol):
    """The seam every network-backed adapter is constructed with."""

    def get(self, url: str, *, params: dict[str, str] | None = None) -> HttpResponse:
        """Perform a GET. Raise :class:`TransportError` on any failure."""


class HttpxTransport:
    """Default transport built on ``httpx``."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        user_agent: str = "NGS-Agent/1.0 (evidence-adapter; research use only)",
        headers: dict[str, str] | None = None,
        client: Any = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.headers = {
            "User-Agent": user_agent,
            "Accept": "application/json",
            **(headers or {}),
        }
        self._client = client

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - depends on install extras
                raise TransportError(
                    "httpx is required for network evidence retrieval. Install with "
                    '`pip install "ngs-agent[llm]"` or supply an offline evidence pack.'
                ) from exc
            self._client = (
                httpx.Client(timeout=self.timeout_seconds, headers=self.headers,
                    follow_redirects=True)
            )
        return self._client

    def get(self, url: str, *, params: dict[str, str] | None = None) -> HttpResponse:
        client = self._ensure_client()
        started = time.monotonic()
        try:
            response = client.get(url, params=params)
        except Exception as exc:  # noqa: BLE001 - httpx raises a wide exception family
            raise TransportError(f"GET {url} failed: {type(exc).__name__}: {exc}") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)
        body = response.content if isinstance(response.content, bytes) else bytes(response.content)
        return HttpResponse(
            status_code=int(response.status_code),
            body=body,
            url=str(response.url),
            elapsed_ms=elapsed_ms,
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class RecordedTransport:
    """A transport that replays pre-recorded responses keyed by URL.

    This is how every adapter test runs: the fixture *is* a real recorded
    response (see ``tests/fixtures/clinvar/``), so the test exercises the same
    parsing code a live call would, without depending on the network or on
    NCBI's current data.

    Unknown URLs raise rather than returning 404, because a silently unmatched
    URL in a test is a test that is not testing anything.
    """

    #: Read by adapters so the audit trail says where a response actually
    #: came from. A recording served from disk is not an HTTP retrieval, and
    #: an audit record that claims otherwise misdescribes the provenance of
    #: every criterion derived from it.
    transport_label = "recorded_fixture"

    def __init__(self, responses: dict[str, HttpResponse | tuple[int, bytes]]) -> None:
        self._responses: dict[str, HttpResponse] = {}
        for url, value in responses.items():
            if isinstance(value, HttpResponse):
                self._responses[url] = value
            else:
                status, body = value
                self._responses[url] = HttpResponse(
                    status_code=status, body=body, url=url, elapsed_ms=0)
        self.calls: list[str] = []

    def get(self, url: str, *, params: dict[str, str] | None = None) -> HttpResponse:
        key = canonical_url(url, params)
        self.calls.append(key)
        if key in self._responses:
            return self._responses[key]
        # Also accept a match on the bare URL when the fixture was recorded
        # without query parameters.
        if url in self._responses and not params:
            return self._responses[url]
        raise TransportError(
            f"no recorded response for {key}. Recorded keys: {sorted(self._responses)}"
        )


#: Parameters that identify the *caller* rather than the request. Excluded from
#: fixture keys so a recording made without an API key still matches a call made
#: with one.
_NON_SEMANTIC_PARAMS = frozenset({"tool", "email", "api_key"})


def canonical_url(
    url: str, params: dict[str, str] | None, *, ignore: frozenset[str] = _NON_SEMANTIC_PARAMS
) -> str:
    """Build the key a :class:`RecordedTransport` matches on.

    Caller-identifying parameters (``tool``, ``email``, ``api_key``) are
    excluded: they say who asked, not what was asked, and a fixture recorded
    without an API key must still match a deployment that has one.
    """
    if not params:
        return url
    significant = {key: value for key, value in params.items() if key not in ignore}
    if not significant:
        return url
    query = "&".join(f"{key}={significant[key]}" for key in sorted(significant))
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{query}"
