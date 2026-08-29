# Agent-First-Protocol (AFP)

**One standard "language" for AI agents to talk to any external service.**

Agents today have to adapt to every service individually — REST APIs with bespoke response shapes, MCP tool servers, local programs, serial devices, and worst of all, HTML pages scraped with fragile glue code. AFP flips this: **services speak one common protocol, so agents plug in with zero per-service adaptation code.**

[中文说明 (Chinese)](README.zh.md)

---

## Why

Every external service speaks its own dialect: different transports (HTTP / MCP / local IPC / serial), different response formats, different error conventions. Every agent needs custom glue code for each one, and page-scraping integrations break whenever the HTML changes.

AFP is a transport-agnostic interaction standard. Whatever the business domain — component datasheets, legal texts, sensor readings, 3D models, library catalogs, database rows — and whatever the wire (HTTP, MCP tools, named pipes, UART), the **business semantics stay identical** while the transport underneath can be swapped freely.

## Five Rules

1. **Introduce yourself first.** An agent connects and asks `discover` — the service returns a standard capability manifest. No hard-coded endpoints, no memorized API shapes.
2. **Structured data only.** Services return well-formed entities, never HTML for the agent to parse. Human-facing pages may exist, but they live behind a separate prefix and agents never touch them.
3. **Files are content-addressed.** Binary resources are identified by sha256. Location doesn't matter — same fingerprint, same file — and tampering is detectable.
4. **Errors are uniform.** A fixed set of error codes, warnings, and a `trace_id` for tracing, identical across remote and local calls. Agents write error-handling logic exactly once.
5. **Dirty work stays inside the service.** If a service must fetch data from the outside world (crawling, CAPTCHAs), it handles that internally. The agent only ever sees clean results. *(Remote fetch is an optional capability — a service without it is still compliant, as long as its capability manifest says so honestly.)*

## The Agent-First Iron Rules

The name comes from the design philosophy: **everything is built so the agent cannot fool itself.**

- **A miss is an honest miss** — `404 + entity_missing`, never a 200-wrapped "success".
- **A partial match must carry warnings** — no silent best-effort guesses that let an agent hallucinate with confidence.
- **Capability declarations must be honest** — a service only advertises what it actually implements.

## Four Layers

| Layer | Name | Role |
|---|---|---|
| L1 | Entity Model | The shape of every response: `Entity` on hit, one unified error object (8 codes) on miss |
| L2 | Capability Discovery | `/.well-known/agent-protocol.json` — who am I, what can I do |
| L3 | Operation Primitives | Five fixed verbs: `discover` / `lookup` / `list` / `blob_resolve` / `fetch` |
| L4 | Transport Bindings | The same semantics over HTTP, MCP, local IPC (named pipes), or serial |

## Repository Layout

```
docs/           Protocol specification documents 01–11 (Chinese, authoritative)
afp_service/    Reference implementation: HTTP + MCP + IPC bindings, SQLite store
afp_client/     Agent-side SDK: capability gating, hash verification, warning exposure
tests/          108 tests proving the implementation follows the spec
COMPLIANCE.txt  Self-assessment: FULL (except remote fetch)
```

## Quick Start

```bash
pip install -r afp_service/requirements.txt

# HTTP service (port 47100)
cd afp_service && python -m uvicorn server:app --port 47100

# MCP binding
cd afp_service && python mcp_binding.py

# Local IPC (Windows named pipe)
cd afp_service && python ipc_binding.py

# Run the full test suite
python -m pytest tests -v
```

Then point any agent at it:

```bash
curl http://127.0.0.1:47100/.well-known/agent-protocol.json
curl "http://127.0.0.1:47100/api/v1/component/lookup?key=MAX30011"
```

## Status

Compliance self-assessment: **FULL (except remote fetch)** | Bindings: **HTTP + MCP + IPC** | 108/108 tests passing.

Deferred: `fetch_external` remote fetching (optional capability per spec) and the embedded serial binding (spec doc 09; no reference implementation yet).

## License

[MIT](LICENSE)
