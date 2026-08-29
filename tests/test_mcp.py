# -*- coding: utf-8 -*-
"""M4 MCP binding tests (doc 07; doc 10 verification matrix item 9, MCP half).

Three layers:
- tool functions: call AFPBinding methods directly and assert the Entity /
  AFPError structures they return (doc 07 section 5)
- one in-memory MCP session (mcp SDK memory transport): the handshake
  carries the agentProtocol capability extension (doc 07 section 2) and
  tools/list exposes the five tools with the doc 07 section 4 schemas
- binding consistency: the same key queried over HTTP and MCP returns
  identical entity content — only the per-request meta.trace_id differs
  (doc 10 verification matrix item 9)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
import pytest
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from mcp_binding import AFPBinding, create_mcp_server
from seed import ensure_seed
from server import Settings, create_app


def _payload(result: types.CallToolResult) -> dict:
    """Parse the JSON payload of a tool result's first text block."""
    assert result.content, "tool result carries no content"
    return json.loads(result.content[0].text)


def _content(entity: dict) -> dict:
    """Entity content without the per-request meta block."""
    return {k: v for k, v in entity.items() if k != "meta"}


def _isolated_settings(tmp_path: Path) -> Settings:
    """Isolated db + blob dir + audit log (same pattern as test_full)."""
    db_path = tmp_path / "afp.db"
    blob_dir = tmp_path / "data"
    ensure_seed(str(db_path), str(blob_dir))
    return Settings(
        db_path=str(db_path),
        blob_dir=str(blob_dir),
        audit_log_path=str(tmp_path / "audit.jsonl"),
    )


@pytest.fixture()
def binding(tmp_path: Path) -> AFPBinding:
    return AFPBinding(_isolated_settings(tmp_path))


# ---------- afp_lookup: found / miss / partial (doc 07 section 5) ----------

class TestLookupTool:
    def test_found_returns_entity_structure(self, binding: AFPBinding):
        result = binding.lookup(domain="component", key="MAX30011")
        assert result.isError is False
        entity = _payload(result)
        assert entity["entity_id"] == "MAX30011"
        assert entity["domain"] == "component"
        assert entity["revision"] == "Rev-C"
        assert entity["source_origin"] == "ADI official datasheet"
        assert entity["sha256"] and len(entity["sha256"]) == 64
        assert entity["payload"]["part_number"] == "MAX30011"
        assert "pins" in entity["payload"]
        assert "absolute_max_rating" in entity["payload"]
        assert "forbidden_connect" in entity["payload"]
        assert entity["warnings"] == []
        assert entity["meta"]["trace_id"]
        assert entity["meta"]["timestamp"]
        # the structuredContent channel mirrors the text payload
        assert result.structuredContent == entity

    def test_miss_returns_structured_entity_missing(self, binding: AFPBinding):
        result = binding.lookup(domain="component", key="GHOST_PART_404")
        assert result.isError is True
        err = _payload(result)
        assert err["error_code"] == "entity_missing"
        assert "GHOST_PART_404" in err["human_readable"]
        assert err["trace_id"]

    def test_partial_match_transfers_warnings(self, binding: AFPBinding):
        # doc 07 section 5: partial-match returns Entity + warnings; the
        # warnings array must ride along in the tool result, never dropped
        result = binding.lookup(domain="component", key="MAX3001")
        assert result.isError is False
        entity = _payload(result)
        assert entity["warnings"] != []
        assert "MAX30011" in entity["payload"]["candidates"]
        assert entity["source_origin"] is None  # P-2: no status-marker misuse
        assert entity["sha256"] is None
        assert entity["meta"]["trace_id"]

    def test_revision_semantics(self, binding: AFPBinding):
        found = binding.lookup(domain="component", key="MAX30011", revision="Rev-C")
        assert found.isError is False
        assert _payload(found)["revision"] == "Rev-C"
        # exact-revision miss is a plain miss, not a candidate list
        miss = binding.lookup(domain="component", key="MAX30011", revision="Rev-Z")
        assert miss.isError is True
        assert _payload(miss)["error_code"] == "entity_missing"

    def test_unserved_domain_is_entity_missing(self, binding: AFPBinding):
        # ADR-I2 (same as the HTTP binding): unserved domain => entity_missing
        result = binding.lookup(domain="doc", key="whatever")
        assert result.isError is True
        err = _payload(result)
        assert err["error_code"] == "entity_missing"
        assert "domain" in err["human_readable"]

    def test_missing_key_is_invalid_param(self, binding: AFPBinding):
        result = binding.lookup(domain="component", key="")
        assert result.isError is True
        assert _payload(result)["error_code"] == "invalid_param"

    def test_trace_id_passthrough_and_generation(self, binding: AFPBinding):
        passed = binding.lookup(domain="component", key="MAX30011", trace_id="trace-mcp-1")
        assert _payload(passed)["meta"]["trace_id"] == "trace-mcp-1"
        generated = binding.lookup(domain="component", key="MAX30011")
        assert _payload(generated)["meta"]["trace_id"].startswith("trace-")


# ---------- afp_fetch: no remote_fetch capability (doc 07 section 7.3) -----

class TestFetchTool:
    def test_fetch_is_capability_not_supported(self, binding: AFPBinding):
        result = binding.fetch(domain="component", key="MAX30011")
        assert result.isError is True  # never silently ignored
        err = _payload(result)
        assert err["error_code"] == "capability_not_supported"
        assert err["trace_id"]
        assert "remote_fetch" in err["human_readable"]


# ---------- afp_discover: same source as HTTP discover ---------------------

class TestDiscoverTool:
    def test_discover_same_document_as_http(self, binding: AFPBinding):
        # the MCP tool and the HTTP endpoint render the very same document
        # (single source: server.build_discovery)
        http = TestClient(create_app(binding.settings))
        http_meta = http.get("/.well-known/agent-protocol.json").json()
        assert http_meta["protocol"] == "Agent-First-Protocol"
        result = binding.discover()
        assert result.isError is False
        assert _payload(result) == http_meta
        assert "remote_fetch" not in _payload(result)["capabilities"]


# ---------- afp_list: pagination + invalid_param (doc 04 section 2.3) -------

class TestListTool:
    def test_defaults_offset0_limit20(self, binding: AFPBinding):
        result = binding.list_entities(domain="component")
        assert result.isError is False
        page = _payload(result)
        assert page["total"] == 5
        assert page["offset"] == 0
        assert page["limit"] == 20
        assert len(page["items"]) == 5
        assert page["has_more"] is False
        assert page["items"][0]["entity_id"]
        assert page["items"][0]["meta"]["trace_id"]

    def test_pagination(self, binding: AFPBinding):
        page = _payload(binding.list_entities(domain="component", offset=0, limit=2))
        assert len(page["items"]) == 2
        assert page["has_more"] is True
        rest = _payload(binding.list_entities(domain="component", offset=2, limit=2))
        ids = [i["entity_id"] for i in page["items"] + rest["items"]]
        assert len(ids) == len(set(ids))

    def test_limit_above_100_is_invalid_param(self, binding: AFPBinding):
        result = binding.list_entities(domain="component", limit=101)
        assert result.isError is True
        assert _payload(result)["error_code"] == "invalid_param"

    def test_negative_offset_is_invalid_param(self, binding: AFPBinding):
        result = binding.list_entities(domain="component", offset=-1)
        assert result.isError is True
        assert _payload(result)["error_code"] == "invalid_param"

    def test_unserved_domain_is_entity_missing(self, binding: AFPBinding):
        result = binding.list_entities(domain="cad")
        assert result.isError is True
        assert _payload(result)["error_code"] == "entity_missing"


# ---------- afp_blob_resolve: locator set (doc 04 section 2.4) --------------

class TestBlobResolveTool:
    def _max_sha(self, binding: AFPBinding) -> str:
        return _payload(binding.lookup(domain="component", key="MAX30011"))["sha256"]

    def test_returns_locator_set(self, binding: AFPBinding):
        sha = self._max_sha(binding)
        result = binding.blob_resolve(sha256=sha)
        assert result.isError is False
        loc = _payload(result)
        assert loc["sha256"] == sha
        assert loc["mime_type"] == "application/pdf"
        assert loc["size_bytes"] > 0
        methods = {item["method"] for item in loc["locators"]}
        assert {"http", "local_file"} <= methods

    def test_missing_blob_is_entity_missing(self, binding: AFPBinding):
        result = binding.blob_resolve(sha256="0" * 64)
        assert result.isError is True
        assert _payload(result)["error_code"] == "entity_missing"

    def test_malformed_hash_is_invalid_param(self, binding: AFPBinding):
        result = binding.blob_resolve(sha256="not-a-hash")
        assert result.isError is True
        assert _payload(result)["error_code"] == "invalid_param"


# ---------- binding consistency: HTTP vs MCP (doc 10 matrix item 9) ---------

class TestBindingConsistency:
    def test_same_key_same_entity_http_vs_mcp(self, tmp_path: Path):
        settings = _isolated_settings(tmp_path)
        http = TestClient(create_app(settings))
        binding = AFPBinding(settings)

        # found state: identical entity content across bindings
        http_entity = http.get("/api/v1/component/lookup", params={"key": "ADS1299"}).json()
        mcp_entity = _payload(binding.lookup(domain="component", key="ADS1299"))
        assert _content(mcp_entity) == _content(http_entity)
        # meta keeps the same structure; trace_id is per-request and may
        # (and does) differ between the two bindings
        assert set(mcp_entity["meta"]) == set(http_entity["meta"]) == {"trace_id", "timestamp"}
        assert mcp_entity["meta"]["trace_id"] != http_entity["meta"]["trace_id"]
        assert mcp_entity["meta"]["trace_id"] and http_entity["meta"]["trace_id"]

        # partial-match state: warnings and candidates identical on both sides
        http_partial = http.get("/api/v1/component/lookup", params={"key": "MAX3001"}).json()
        mcp_partial = _payload(binding.lookup(domain="component", key="MAX3001"))
        assert _content(mcp_partial) == _content(http_partial)

        # miss state: the same structured error_code on both bindings
        http_miss = http.get("/api/v1/component/lookup", params={"key": "GHOST_PART_404"})
        mcp_miss = binding.lookup(domain="component", key="GHOST_PART_404")
        assert http_miss.status_code == 404
        assert http_miss.json()["error_code"] == "entity_missing"
        assert _payload(mcp_miss)["error_code"] == "entity_missing"


# ---------- full MCP stack over the in-memory transport --------------------

class TestMCPHandshake:
    """Handshake + tools/list + real tool calls through the mcp SDK
    in-memory session (doc 07 sections 2/4/5)."""

    def test_handshake_agent_protocol_and_tool_calls(self, binding: AFPBinding):
        captured: dict[str, Any] = {}

        async def scenario() -> None:
            server = create_mcp_server(binding)
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                client_read, client_write = client_streams
                server_read, server_write = server_streams
                async with anyio.create_task_group() as tg:
                    tg.start_soon(
                        lambda: server.run(
                            server_read,
                            server_write,
                            server.create_initialization_options(),
                        )
                    )
                    try:
                        async with ClientSession(client_read, client_write) as session:
                            init = await session.initialize()
                            tools = await session.list_tools()
                            found = await session.call_tool(
                                "afp_lookup", {"domain": "component", "key": "MAX30011"}
                            )
                            miss = await session.call_tool(
                                "afp_lookup", {"domain": "component", "key": "GHOST_PART_404"}
                            )
                            captured["capabilities"] = init.capabilities.model_dump(
                                exclude_none=True
                            )
                            captured["tools"] = [
                                t.model_dump(exclude_none=True) for t in tools.tools
                            ]
                            captured["found"] = found.model_dump(exclude_none=True)
                            captured["miss"] = miss.model_dump(exclude_none=True)
                    finally:
                        tg.cancel_scope.cancel()

        anyio.run(scenario)

        # doc 07 section 2: agentProtocol extension inside capabilities
        caps = captured["capabilities"]
        assert caps["agentProtocol"] == {
            "protocol": "Agent-First-Protocol",
            "protocol_version": "v0.2",
            "domains": ["component"],
            "capabilities": ["entity_lookup", "entity_list", "blob_access"],
        }
        assert "tools" in caps  # standard MCP capability alongside the extension

        # doc 07 section 3: exactly the five tools
        names = {t["name"] for t in captured["tools"]}
        assert names == {
            "afp_discover",
            "afp_lookup",
            "afp_list",
            "afp_blob_resolve",
            "afp_fetch",
        }

        # doc 07 section 4: tool inputSchemas
        lookup = next(t for t in captured["tools"] if t["name"] == "afp_lookup")
        assert lookup["inputSchema"]["required"] == ["domain", "key"]
        props = lookup["inputSchema"]["properties"]
        assert props["domain"]["enum"] == ["component", "doc", "cad", "sensor"]
        assert props["key"] == {"type": "string"}
        assert props["revision"]["type"] == ["string", "null"]
        blob_tool = next(t for t in captured["tools"] if t["name"] == "afp_blob_resolve")
        assert blob_tool["inputSchema"]["required"] == ["sha256"]

        # full-stack tool calls behave like the function layer
        assert captured["found"]["isError"] is False
        assert captured["found"]["structuredContent"]["entity_id"] == "MAX30011"
        assert json.loads(captured["found"]["content"][0]["text"])["entity_id"] == "MAX30011"
        assert captured["miss"]["isError"] is True
        assert (
            json.loads(captured["miss"]["content"][0]["text"])["error_code"] == "entity_missing"
        )
