# -*- coding: utf-8 -*-
"""AFP MCP Binding (protocol doc 07): AFP primitives wrapped as MCP tools.

Five tools over the stdio transport (doc 07 section 3):
  afp_discover     -> agent-protocol metadata, same source as HTTP discover
  afp_lookup       -> Entity on found / partial-match, structured AFPError
                      on miss; partial-match warnings ride along in the
                      result payload (doc 07 section 5)
  afp_list         -> paginated entity list (limit default 20, max 100)
  afp_blob_resolve -> locator set for one sha256
  afp_fetch        -> capability_not_supported: this endpoint declares no
                      remote_fetch capability, so the tool must fail loudly
                      and never silently ignore the call (doc 07 section 7.3)

Handshake (doc 07 section 2): the initialize response carries an
`agentProtocol` extension inside capabilities, mirroring the HTTP discover
metadata so AFP-aware MCP clients consume AFP semantics directly.

Binding consistency: the same ComponentStore backs the HTTP and MCP
bindings — the same key returns the same Entity content, only the
per-request meta.trace_id may differ (doc 10 verification matrix item 9).

Tool input validation is AFP-semantic on purpose (validate_input=False):
every failure surfaces as a structured AFPError with isError=true
(doc 07 section 5), mirroring the HTTP binding's 422 -> 400 normalization.

Run: python mcp_binding.py  (stdio transport)
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Optional

import anyio
import mcp.types as types
from mcp.server import InitializationOptions, NotificationOptions, Server
from mcp.server.stdio import stdio_server

from afp_core import AFPError, Entity, ErrorCode, Meta
from server import (
    CAPABILITIES,
    PARTIAL_WARNING,
    SERVED_DOMAINS,
    Settings,
    build_discovery,
    is_valid_sha256,
)
from store import ComponentStore

SERVER_NAME = "afp-component-service"
SERVER_VERSION = "v0.2"

# doc 07 section 2: the agentProtocol capability extension advertised in the
# initialize response — fields mirror the HTTP discover document (same source)
AGENT_PROTOCOL_CAPABILITY = {
    "protocol": "Agent-First-Protocol",
    "protocol_version": "v0.2",
    "domains": SERVED_DOMAINS,
    "capabilities": CAPABILITIES,
}

# domain enum of the tool schemas (doc 07 section 4.1); a domain beyond the
# served list still resolves to a structured entity_missing AFPError at the
# semantic layer, exactly like the HTTP binding's ADR-I2 behavior
_SCHEMA_DOMAINS = ["component", "doc", "cad", "sensor"]

# doc 07 section 7.2: trace_id may be passed through request parameters; when
# absent the server generates one and returns it inside the result payload
_TRACE_PROPERTY = {
    "trace_id": {
        "type": ["string", "null"],
        "description": "可选 trace_id 透传（文档 07 §7.2），缺省由服务端生成",
    }
}


def _new_trace(trace_id: Optional[str]) -> str:
    return trace_id or f"trace-{uuid.uuid4().hex[:12]}"


def _ok(payload: dict[str, Any]) -> types.CallToolResult:
    """Successful tool result: structured payload + the same JSON as text."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
    )


def _err(err: AFPError) -> types.CallToolResult:
    """Failed tool result: isError=true + the AFPError JSON as text
    (doc 07 section 5: errors uniformly use isError + AFPError payload)."""
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(err.model_dump(mode="json"), ensure_ascii=False),
            )
        ],
        isError=True,
    )


def _invalid_param(trace: str, message: str) -> types.CallToolResult:
    return _err(
        AFPError(error_code=ErrorCode.INVALID_PARAM, human_readable=message, trace_id=trace)
    )


def _entity_missing(trace: str, message: str) -> types.CallToolResult:
    return _err(
        AFPError(error_code=ErrorCode.ENTITY_MISSING, human_readable=message, trace_id=trace)
    )


def _tool_definitions() -> list[types.Tool]:
    """The five AFP tools; inputSchemas follow doc 07 section 4."""
    return [
        types.Tool(
            name="afp_discover",
            description="返回 agent-protocol 元数据",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="afp_lookup",
            description="按 key 查询 AFP 实体",
            inputSchema={
                "type": "object",
                "required": ["domain", "key"],
                "properties": {
                    "domain": {"type": "string", "enum": _SCHEMA_DOMAINS},
                    "key": {"type": "string"},
                    "revision": {"type": ["string", "null"]},
                    **_TRACE_PROPERTY,
                },
            },
        ),
        types.Tool(
            name="afp_list",
            description="分页实体列表",
            inputSchema={
                "type": "object",
                "required": ["domain"],
                "properties": {
                    "domain": {"type": "string", "enum": _SCHEMA_DOMAINS},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    # no "maximum" here on purpose: an over-limit value is an
                    # AFP semantic error and must come back as a structured
                    # AFPError(invalid_param) (doc 04 section 2.3), not as a
                    # generic MCP schema-validation failure
                    "limit": {"type": "integer", "minimum": 1, "default": 20},
                    **_TRACE_PROPERTY,
                },
            },
        ),
        types.Tool(
            name="afp_blob_resolve",
            description="按 sha256 解析二进制资源定位器",
            inputSchema={
                "type": "object",
                "required": ["sha256"],
                "properties": {"sha256": {"type": "string"}, **_TRACE_PROPERTY},
            },
        ),
        types.Tool(
            name="afp_fetch",
            description="触发后端回源（本端点未声明 remote_fetch 能力）",
            inputSchema={
                "type": "object",
                "required": ["domain", "key"],
                "properties": {
                    "domain": {"type": "string", "enum": _SCHEMA_DOMAINS},
                    "key": {"type": "string"},
                    **_TRACE_PROPERTY,
                },
            },
        ),
    ]


class AFPBinding:
    """Tool-function layer for the five AFP MCP tools.

    Every method returns a types.CallToolResult directly — the lowlevel
    call_tool decorator passes such results through untouched — so tests can
    exercise the tool functions without a full MCP handshake.
    """

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings if settings is not None else Settings.from_env()
        # binding consistency (doc 10 matrix item 9): same store class on the
        # same database file as the HTTP binding => same Entity for same key
        self.store = ComponentStore(db_path=self.settings.db_path)
        self.blob_dir = Path(self.settings.blob_dir)

    # -- afp_discover -------------------------------------------------

    def discover(self) -> types.CallToolResult:
        """Agent-protocol metadata rendered from the same builder as the
        HTTP discover endpoint (single source: server.build_discovery)."""
        return _ok(build_discovery(self.settings))

    # -- afp_lookup ---------------------------------------------------

    def lookup(
        self,
        domain: str,
        key: str,
        revision: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> types.CallToolResult:
        """Three states (doc 04 section 2.2): found -> Entity, partial-match
        -> Entity + warnings (kept in the result, never dropped), miss ->
        structured AFPError(entity_missing)."""
        trace = _new_trace(trace_id)
        if not isinstance(domain, str) or not domain:
            return _invalid_param(trace, "domain must be a non-empty string")
        if not isinstance(key, str) or not key:
            return _invalid_param(trace, "key must be a non-empty string")
        if revision is not None and not isinstance(revision, str):
            return _invalid_param(trace, "revision must be a string or null")
        if domain not in SERVED_DOMAINS:
            # ADR-I2 (same as the HTTP binding): unserved domain -> entity_missing
            return _entity_missing(trace, f"domain '{domain}' not served by this endpoint")
        row = self.store.lookup_exact(key, revision)
        if row is not None:
            payload = json.loads(row["json_payload"])
            return _ok(
                Entity(
                    entity_id=row["part_number"],
                    domain=domain,
                    revision=row["revision"] or payload.get("revision"),
                    source_origin=row.get("source_file") or "unknown",
                    sha256=row.get("datasheet_sha256"),
                    payload=payload,
                    meta=Meta(trace_id=trace),
                ).model_dump(mode="json")
            )
        if revision is not None:
            # exact-revision miss is a plain miss: it must not degrade
            # into a partial-match candidate list
            return _entity_missing(
                trace, f"No entity {key} revision {revision} in domain {domain}"
            )
        cands = self.store.lookup_fuzzy(key)
        if cands:
            # partial-match: the warnings array must ride along in the
            # returned Entity so MCP clients can relay it upstream
            # (doc 07 section 5, doc 04 section 2.2)
            return _ok(
                Entity(
                    entity_id=key,
                    domain=domain,
                    revision=None,
                    source_origin=None,  # P-2: no status-marker misuse
                    sha256=None,
                    payload={"candidates": [c["part_number"] for c in cands]},
                    warnings=[PARTIAL_WARNING],
                    meta=Meta(trace_id=trace),
                ).model_dump(mode="json")
            )
        return _entity_missing(trace, f"No entity {key} in domain {domain}")

    # -- afp_list -----------------------------------------------------

    def list_entities(
        self,
        domain: str,
        offset: int = 0,
        limit: int = 20,
        trace_id: Optional[str] = None,
    ) -> types.CallToolResult:
        """Paginated entity list (doc 04 section 2.3): limit default 20,
        max 100; over-limit or negative offset => AFPError(invalid_param)."""
        trace = _new_trace(trace_id)
        if not isinstance(domain, str) or not domain:
            return _invalid_param(trace, "domain must be a non-empty string")
        if domain not in SERVED_DOMAINS:
            return _entity_missing(trace, f"domain '{domain}' not served by this endpoint")
        if not isinstance(offset, int) or offset < 0:
            return _invalid_param(trace, "offset must be an integer >= 0")
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            return _invalid_param(trace, "limit must be an integer within [1, 100]")
        rows, total = self.store.list_page(offset, limit)
        items = [
            Entity(
                entity_id=r["part_number"],
                domain=domain,
                revision=r.get("revision"),
                source_origin=r.get("source_file") or "unknown",
                sha256=r.get("datasheet_sha256"),
                payload={},
                meta=Meta(trace_id=trace),
            ).model_dump(mode="json")
            for r in rows
        ]
        return _ok(
            {
                "items": items,
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": (offset + limit) < total,
            }
        )

    # -- afp_blob_resolve ---------------------------------------------

    def blob_resolve(self, sha256: str, trace_id: Optional[str] = None) -> types.CallToolResult:
        """Locator set for one blob (doc 04 section 2.4): same structure as
        the HTTP binding's Accept: application/json branch."""
        trace = _new_trace(trace_id)
        if not isinstance(sha256, str) or not sha256:
            return _invalid_param(trace, "sha256 must be a non-empty string")
        if not is_valid_sha256(sha256):
            return _invalid_param(trace, f"invalid sha256: {sha256!r}")
        path = self.blob_dir / f"{sha256}.bin"
        if not path.is_file():
            return _entity_missing(trace, f"blob {sha256} not found")
        data = path.read_bytes()
        mime = "application/pdf" if data.startswith(b"%PDF-") else "application/octet-stream"
        return _ok(
            {
                "sha256": sha256,
                "mime_type": mime,
                "size_bytes": len(data),
                "locators": [
                    {"method": "http", "url": f"/api/v1/blob/{sha256}"},
                    {"method": "local_file", "path": f"data/{sha256}.bin"},
                ],
            }
        )

    # -- afp_fetch ----------------------------------------------------

    def fetch(
        self,
        domain: str,
        key: str,
        trace_id: Optional[str] = None,
    ) -> types.CallToolResult:
        """fetch_external mapping (doc 04 section 2.5): this endpoint does
        not declare the remote_fetch capability, so the call always fails
        loudly with capability_not_supported — never silently ignored
        (doc 07 section 7.3)."""
        trace = _new_trace(trace_id)
        return _err(
            AFPError(
                error_code=ErrorCode.CAPABILITY_NOT_SUPPORTED,
                human_readable=(
                    "remote_fetch is not implemented by this endpoint "
                    "(declared capabilities: entity_lookup, entity_list, blob_access)"
                ),
                trace_id=trace,
            )
        )


class AFPMCPServer(Server):
    """Lowlevel MCP server that advertises AFP metadata in the handshake.

    `create_initialization_options` is overridden so the initialize response
    carries the agentProtocol capability extension (doc 07 section 2) — also
    when the SDK's own in-memory test harness builds the options, because
    that helper calls create_initialization_options() with no arguments.
    """

    def create_initialization_options(
        self,
        notification_options: Optional[NotificationOptions] = None,
        experimental_capabilities: Optional[dict[str, dict[str, Any]]] = None,
    ) -> InitializationOptions:
        options = super().create_initialization_options(
            notification_options, experimental_capabilities
        )
        # ServerCapabilities is extra="allow": attach the AFP extension block
        # while preserving whatever standard capabilities were derived from
        # the registered handlers (doc 07 section 2)
        caps = options.capabilities.model_dump(exclude_none=True)
        caps["agentProtocol"] = AGENT_PROTOCOL_CAPABILITY
        options.capabilities = types.ServerCapabilities(**caps)
        return options


def create_mcp_server(binding: Optional[AFPBinding] = None) -> AFPMCPServer:
    """Wire the five AFP tools (doc 07 section 3) onto one MCP server."""
    binding = binding if binding is not None else AFPBinding()
    server = AFPMCPServer(SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def list_afp_tools() -> list[types.Tool]:
        return _tool_definitions()

    @server.call_tool(validate_input=False)
    async def call_afp_tool(
        name: str, arguments: Optional[dict[str, Any]]
    ) -> types.CallToolResult:
        # input validation is AFP-semantic here so every failure is a
        # structured AFPError (doc 07 section 5), mirroring the HTTP
        # binding's 422 -> 400 invalid_param normalization
        args = dict(arguments or {})
        if name == "afp_discover":
            return binding.discover()
        if name == "afp_lookup":
            return binding.lookup(
                domain=args.get("domain"),
                key=args.get("key"),
                revision=args.get("revision"),
                trace_id=args.get("trace_id"),
            )
        if name == "afp_list":
            return binding.list_entities(
                domain=args.get("domain"),
                offset=args.get("offset", 0),
                limit=args.get("limit", 20),
                trace_id=args.get("trace_id"),
            )
        if name == "afp_blob_resolve":
            return binding.blob_resolve(
                sha256=args.get("sha256"), trace_id=args.get("trace_id")
            )
        if name == "afp_fetch":
            return binding.fetch(
                domain=args.get("domain"),
                key=args.get("key"),
                trace_id=args.get("trace_id"),
            )
        return _invalid_param(_new_trace(None), f"unknown tool: {name}")

    return server


def main() -> None:
    """Run the AFP MCP binding over the stdio transport (doc 07)."""
    server = create_mcp_server()

    async def serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    anyio.run(serve)


if __name__ == "__main__":
    main()
