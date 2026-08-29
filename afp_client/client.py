# -*- coding: utf-8 -*-
"""AFP agent-side client SDK — M3 (protocol docs 03 section 6, 06).

Agent-side discovery flow (doc 03 section 6):

    meta <- discover()
    if meta.protocol != "Agent-First-Protocol": refuse the connection
    if "entity_lookup" not in meta.capabilities: report and stop
    record domains / auth / blob_access_methods
    every subsequent request carries an X-AFP-Trace-ID uuid

Rules encoded here (no hardcoded endpoint abilities):
- protocol mismatch => ProtocolMismatch, and the refusal is remembered so
  the client never issues further calls to that endpoint
- capability gating reads the LIVE discovery document only; a missing
  capability => CapabilityNotSupported raised before any request is sent
- lookup miss (404 entity_missing) => EntityMissing carrying the server
  AFPError fields (error_code / human_readable / trace_id / retry_after_sec)
- partial-match => LookupResult whose `warnings` attribute is always
  present and non-empty; require_exact() trips PartialMatchError so
  partial data is never consumed silently
- fetch_blob follows the http template advertised in blob_access_methods
  (never a hardcoded path) and re-computes sha256 over the downloaded
  bytes; any mismatch => BlobIntegrityError
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any, NoReturn, Optional

import httpx

PROTOCOL_NAME = "Agent-First-Protocol"
DISCOVER_PATH = "/.well-known/agent-protocol.json"
TRACE_HEADER = "X-AFP-Trace-ID"

_REQUIRED_DISCOVER_FIELDS = (
    "protocol",
    "protocol_version",
    "service_name",
    "capabilities",
    "domains",
)


class AFPClientError(Exception):
    """Base class for every AFP client-side failure."""


class ProtocolMismatch(AFPClientError):
    """The endpoint is not an Agent-First-Protocol service (doc 03 section 6)."""


class CapabilityNotSupported(AFPClientError):
    """Live discovery metadata lacks the capability this call needs."""


class PartialMatchError(AFPClientError):
    """Local guard: a partial-match result was used as if it were exact."""


class BlobIntegrityError(AFPClientError):
    """Downloaded blob does not hash to the requested sha256."""


class RemoteAFPError(AFPClientError):
    """Server answered with an AFPError; its fields are carried verbatim."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "unknown",
        human_readable: str = "",
        trace_id: str = "",
        retry_after_sec: int = 0,
        raw: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.human_readable = human_readable
        self.trace_id = trace_id
        self.retry_after_sec = retry_after_sec
        self.raw = raw or {}


class EntityMissing(RemoteAFPError):
    """lookup miss: HTTP 404 + AFPError(entity_missing) (doc 06 section 3.1)."""


@dataclass
class LookupResult:
    """Successful lookup payload.

    `warnings` is ALWAYS present on the type (non-empty on partial-match),
    so the partial state can never disappear into an untyped dict
    (doc 02 section 4: partial-match MUST carry warnings).
    """

    entity_id: str
    domain: str
    trace_id: str
    revision: Optional[str] = None
    source_origin: Optional[str] = None
    sha256: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_partial(self) -> bool:
        return bool(self.warnings)

    def require_exact(self) -> "LookupResult":
        """Trip PartialMatchError when this result carries warnings.

        The explicit guard makes silent partial-match consumption a
        hard, testable failure instead of a silent wrong-generation risk.
        """
        if self.warnings:
            raise PartialMatchError(
                f"partial-match result for {self.entity_id!r} with warnings: {self.warnings}"
            )
        return self


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


class AFPClient:
    """Synchronous AFP agent client over the HTTP-Web binding (doc 06)."""

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)
        self._metadata: Optional[dict[str, Any]] = None
        self._mismatch: Optional[str] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def __enter__(self) -> "AFPClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @property
    def metadata(self) -> Optional[dict[str, Any]]:
        """Cached discovery document; None until discover() succeeds."""
        return self._metadata

    # ------------------------------------------------------------------
    # per-request trace id (doc 06 section 4)
    # ------------------------------------------------------------------
    @staticmethod
    def _new_trace_id() -> str:
        return str(uuid.uuid4())

    def _headers(self) -> dict[str, str]:
        return {TRACE_HEADER: self._new_trace_id()}

    # ------------------------------------------------------------------
    # discovery (doc 03 sections 3 / 6)
    # ------------------------------------------------------------------
    def discover(self, *, force: bool = False) -> dict[str, Any]:
        """Fetch and validate /.well-known/agent-protocol.json.

        The document is cached (`force=True` re-fetches). Any deviation
        from the discovery schema — wrong `protocol` field, missing
        required fields, non-JSON reply, non-200 status — raises
        ProtocolMismatch, and the refusal is remembered: later
        lookup/fetch_blob calls re-raise without touching the network.
        """
        if self._mismatch is not None:
            raise ProtocolMismatch(self._mismatch)
        if self._metadata is not None and not force:
            return self._metadata
        try:
            resp = self._http.get(DISCOVER_PATH, headers=self._headers())
        except httpx.HTTPError as exc:
            raise AFPClientError(
                f"discover request to {self.base_url} failed: {exc}"
            ) from exc
        if resp.status_code != 200:
            self._refuse(
                f"discover endpoint returned HTTP {resp.status_code};"
                f" {self.base_url} is not an AFP service"
            )
        try:
            doc = resp.json()
        except ValueError:
            self._refuse("discover reply is not JSON; endpoint is not an AFP service")
        if not isinstance(doc, dict):
            self._refuse("discover reply is not a JSON object; endpoint is not an AFP service")
        missing = [name for name in _REQUIRED_DISCOVER_FIELDS if name not in doc]
        if missing:
            self._refuse(f"discover document misses required field(s): {', '.join(missing)}")
        if doc.get("protocol") != PROTOCOL_NAME:
            self._refuse(
                f"protocol mismatch: endpoint declares {doc.get('protocol')!r},"
                f" expected {PROTOCOL_NAME!r}"
            )
        self._metadata = doc
        return doc

    def _refuse(self, reason: str) -> NoReturn:
        # remember the refusal so no further call is ever made to this
        # endpoint (doc 03 section 6: "refuse the connection")
        self._mismatch = reason
        raise ProtocolMismatch(reason)

    def _ensure_ready(self) -> dict[str, Any]:
        if self._mismatch is not None:
            raise ProtocolMismatch(self._mismatch)
        if self._metadata is None:
            return self.discover()
        return self._metadata

    def _require_capability(self, capability: str) -> dict[str, Any]:
        """Gate a primitive on the LIVE capability list; never guess."""
        meta = self._ensure_ready()
        capabilities = meta.get("capabilities") or []
        if capability not in capabilities:
            raise CapabilityNotSupported(
                f"{self.base_url} does not advertise {capability!r}"
                f" (capabilities: {capabilities}); refusing to guess"
            )
        return meta

    # ------------------------------------------------------------------
    # entity_lookup (doc 06 section 2.1)
    # ------------------------------------------------------------------
    def lookup(self, domain: str, key: str, revision: Optional[str] = None) -> LookupResult:
        """Look up one entity; partial matches are never consumed silently."""
        self._require_capability("entity_lookup")
        params: dict[str, str] = {"key": key}
        if revision is not None:
            params["revision"] = revision
        resp = self._http.get(f"/api/v1/{domain}/lookup", params=params, headers=self._headers())
        payload = self._json_or_raise(resp, context=f"lookup {domain}/{key}")
        if resp.status_code != 200:
            self._raise_remote(payload, resp)
        if not isinstance(payload, dict):
            raise AFPClientError(f"lookup {domain}/{key}: reply is not a JSON object")
        return LookupResult(
            entity_id=str(payload.get("entity_id", key)),
            domain=str(payload.get("domain", domain)),
            revision=payload.get("revision"),
            source_origin=payload.get("source_origin"),
            sha256=payload.get("sha256"),
            payload=payload.get("payload") or {},
            warnings=payload.get("warnings") or [],
            trace_id=str((payload.get("meta") or {}).get("trace_id", "")),
        )

    # ------------------------------------------------------------------
    # blob_access (doc 06 section 2.2)
    # ------------------------------------------------------------------
    def fetch_blob(self, sha256: str) -> bytes:
        """Download a blob via the advertised http template; verify sha256.

        The download URL comes from blob_access_methods (discovery), never
        from a hardcoded path. Integrity is checked twice: against the
        X-Blob-Sha256 response header and by recomputing sha256 over the
        downloaded bytes; mismatch => BlobIntegrityError.
        """
        meta = self._require_capability("blob_access")
        if not _is_sha256(sha256):
            raise ValueError(f"invalid sha256 (expected 64 hex chars): {sha256!r}")
        template = self._http_blob_template(meta)
        resp = self._http.get(template.replace("{sha256}", sha256), headers=self._headers())
        if resp.status_code != 200:
            payload = self._json_or_raise(resp, context=f"fetch_blob {sha256[:12]}...")
            self._raise_remote(payload, resp)
        data = resp.content
        header_sha = resp.headers.get("X-Blob-Sha256")
        if header_sha is not None and header_sha.lower() != sha256.lower():
            raise BlobIntegrityError(
                f"blob header mismatch: X-Blob-Sha256={header_sha}, requested={sha256}"
            )
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256.lower():
            raise BlobIntegrityError(
                f"blob integrity failure: requested sha256={sha256}, recomputed sha256={actual}"
            )
        return data

    @staticmethod
    def _http_blob_template(meta: dict[str, Any]) -> str:
        """Pick the advertised http template; never guess a blob path."""
        for method in meta.get("blob_access_methods") or []:
            if (
                isinstance(method, dict)
                and method.get("method") == "http"
                and method.get("template")
            ):
                return str(method["template"])
        raise CapabilityNotSupported(
            "no http entry in blob_access_methods; refusing to guess a blob path"
        )

    # ------------------------------------------------------------------
    # error plumbing
    # ------------------------------------------------------------------
    @staticmethod
    def _json_or_raise(resp: httpx.Response, *, context: str) -> Any:
        try:
            return resp.json()
        except ValueError:
            raise AFPClientError(
                f"{context}: non-JSON reply with HTTP {resp.status_code}"
            ) from None

    @staticmethod
    def _raise_remote(payload: Any, resp: httpx.Response) -> NoReturn:
        """Map a server AFPError body onto typed client exceptions."""
        data = payload if isinstance(payload, dict) else {}
        error_code = str(data.get("error_code", "unknown"))
        human_readable = str(data.get("human_readable", ""))
        message = f"HTTP {resp.status_code}: {human_readable or error_code}"
        details = dict(
            error_code=error_code,
            human_readable=human_readable,
            trace_id=str(data.get("trace_id", "")),
            retry_after_sec=int(data.get("retry_after_sec") or 0),
            raw=data,
        )
        if error_code == "entity_missing" or resp.status_code == 404:
            raise EntityMissing(message, **details)
        raise RemoteAFPError(message, **details)
