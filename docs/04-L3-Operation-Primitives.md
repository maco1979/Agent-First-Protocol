# 04 · L3 操作原语层（Operation Primitives）

> AFP‑v0.2 | 本层定义一组**逻辑操作原语**，语义固定，与传输无关。由 L4 绑定层映射为真实调用（HTTP 接口 / MCP tools / IPC 函数 / 串口帧）。

---

## 1. 原语总表

| 原语 | 参数 | 返回 | 是否可选 |
|---|---|---|---|
| `discover()` | 无 | 发现元数据（L2 Schema） | 必选 |
| `lookup(domain, key, revision=null)` | 域、查询 key、可选版本 | Entity 或 AFPError | 必选 |
| `list(domain, filter, offset, limit)` | 域、过滤条件、分页 | Entity 数组 + 分页信息 | 可选 |
| `blob_resolve(sha256)` | 资源哈希 | locator 集合（多载体） | 必选（若声明 blob_access） |
| `fetch_external(domain, key)` | 域、key | Entity（回源后） | 可选（capability=remote_fetch） |

---

## 2. 原语详细语义

### 2.1 discover()

- 语义：获取端点协议元数据。
- 返回：L2 发现元数据对象。
- 约束：Agent 连接后第一步调用。

### 2.2 lookup(domain, key, revision)

- 语义：按 key 查询实体。
- 返回：
  - `found` → Entity
  - `miss` → AFPError(entity_missing)
  - `partial-match` → Entity + 非空 warnings（可附候选列表）
- 约束：
  - `revision` 传 null 表示取最新版本。
  - `partial-match` 的 warnings 必须被上层 Agent 透传，不得静默使用。

### 2.3 list(domain, filter, offset, limit)

- 语义：域内实体分页列表。
- 返回：`{ "items": [Entity...], "total": int, "offset": int, "limit": int }`
- 约束：`limit` 默认 20，最大 100；超限应报 `invalid_param`。

### 2.4 blob_resolve(sha256)

- 语义：给定哈希，返回二进制资源的所有可用访问定位器。
- 返回：
```json
{
  "sha256": "a94a...",
  "mime_type": "application/pdf",
  "size_bytes": 123456,
  "locators": [
    { "method": "http",       "url": "https://svc.local/api/v1/blob/a94a..." },
    { "method": "local_file", "path": "/data/mirror/a94a....pdf" },
    { "method": "mcp_blob",   "handle": "blob://a94a..." }
  ]
}
```

### 2.5 fetch_external(domain, key)

- 语义：实体本地缺失时，触发端点内部回源拉取并入库，然后返回实体。
- 约束：
  - 仅当 discover 元数据中 `capabilities` 含 `remote_fetch` 时可用，否则返回 `capability_not_supported`。
  - 回源过程对 Agent 完全透明；Agent 只看到「miss → 重试 → found」。
  - 回源失败返回 `fetch_remote_failed`。

---

## 3. 分页信息对象

```json
{
  "items": [],
  "total": 42,
  "offset": 20,
  "limit": 20,
  "has_more": true
}
```

---

## 4. 原语 → 绑定映射总览

| 原语 | HTTP Binding | MCP Binding | Local-IPC Binding |
|---|---|---|---|
| discover | GET /.well-known/agent-protocol.json | 握手返回 agent_protocol | 报文 op=discover |
| lookup | GET /api/v1/{domain}/lookup | tool: afp_lookup | 报文 op=lookup |
| list | GET /api/v1/{domain}/list | tool: afp_list | 报文 op=list |
| blob_resolve | GET /api/v1/blob/{sha256} | tool: afp_blob_resolve | 报文 op=blob_resolve |
| fetch_external | POST /api/v1/{domain}/fetch | tool: afp_fetch | 报文 op=fetch |

> 详细映射见各绑定文档（06/07/08/09）。
