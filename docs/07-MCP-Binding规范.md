# 07 · MCP Binding 规范

> AFP‑v0.2 | 把 AFP 语义封装为 MCP（Model Context Protocol）工具，使 MCP Client（如 OpenClaw、各类智能体）可直接调用 AFP 端点。

---

## 1. 定位

- MCP 解决「工具如何被调用」的传输问题。
- AFP 解决「实体、资源、溯源、发现」的业务语义。
- 本绑定 = 把 AFP 原语映射为 MCP tools，**在 MCP 传输之上传递 AFP 实体语义**。

---

## 2. 发现机制

MCP 握手初始化阶段，服务端必须在 capabilities 或初始化响应中携带：

```json
{
  "protocolVersion": "2026-08-29",
  "capabilities": {
    "tools": { "listChanged": true },
    "agentProtocol": {
      "protocol": "Agent-First-Protocol",
      "protocol_version": "v0.2",
      "domains": ["component"],
      "capabilities": ["entity_lookup", "blob_access"]
    }
  }
}
```

> 兼容 AFP 的 MCP Client 读取 `agentProtocol` 字段后，即按 AFP 语义消费工具返回。

---

## 3. 工具映射

| AFP 原语 | MCP Tool | 说明 |
|---|---|---|
| discover | `afp_discover` | 返回 agent-protocol 元数据 |
| lookup | `afp_lookup` | 返回 Entity / AFPError |
| list | `afp_list` | 分页实体列表 |
| blob_resolve | `afp_blob_resolve` | 返回 locator 集合 |
| fetch_external | `afp_fetch` | 触发后端回源 |

---

## 4. 工具 Schema（示例）

### 4.1 afp_lookup

```json
{
  "name": "afp_lookup",
  "description": "按 key 查询 AFP 实体",
  "inputSchema": {
    "type": "object",
    "required": ["domain", "key"],
    "properties": {
      "domain": { "type": "string", "enum": ["component","doc","cad","sensor"] },
      "key": { "type": "string" },
      "revision": { "type": ["string","null"] }
    }
  }
}
```

### 4.2 afp_blob_resolve

```json
{
  "name": "afp_blob_resolve",
  "description": "按 sha256 解析二进制资源定位器",
  "inputSchema": {
    "type": "object",
    "required": ["sha256"],
    "properties": { "sha256": { "type": "string" } }
  }
}
```

---

## 5. 返回内容语义

- `found`：工具结果直接返回 `Entity` JSON（含 payload、meta.trace_id）。
- `partial-match`：返回 Entity + `warnings` 数组；MCP Client 应把 warnings 透传上层大模型。
- `miss`：返回结构化的 AFPError（error_code=entity_missing）。
- 错误统一使用 `isError: true` + AFPError 文本。

---

## 6. 二进制资源获取

- `afp_blob_resolve` 返回 locator。
- MCP 环境若支持 blob 资源，可直接用 `blob://` 句柄；否则按 locator 中 http/local_file 方式获取。

---

## 7. 约束

1. MCP 绑定的鉴权沿用 AFP discover 元数据中的 `auth` 声明。
2. trace_id 通过 MCP 上下文或请求参数透传；无则服务端生成并在结果返回。
3. 未实现的工具（如端点无 `remote_fetch` 能力）必须返回 `capability_not_supported`，不得静默忽略。
