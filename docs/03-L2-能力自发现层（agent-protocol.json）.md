# 03 · L2 能力自发现层（Discovery Layer）

> AFP‑v0.2 | 本层解决「Agent 如何知道端点能干什么」。不同传输载体入口不同，但**输出 Schema 完全一致**。

---

## 1. 核心原则

1. **自发现是强制，不是约定**：运行时能力，不依赖静态文档。
2. **输出一致**：无论走 HTTP 还是串口，discover 返回的元数据结构完全相同。
3. **禁止硬编码**：Agent 不得把端点能力写死在代码里，必须每次连接后 discover。

---

## 2. 各传输载体发现入口

| 载体 | 发现入口 |
|---|---|
| HTTP/Web | `GET /.well-known/agent-protocol.json` |
| MCP | 握手初始化阶段返回 `agent_protocol` 字段 |
| Local‑IPC / Unix‑socket | 第一条报文请求 `discover` 原语 |
| Embedded‑Serial | 首帧命令执行 `discover` |

---

## 3. agent-protocol 元数据 Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://agent-first-protocol.dev/schema/discovery.json",
  "title": "AFPDiscAvery",
  "type": "object",
  "required": ["protocol", "protocol_version", "service_name", "capabilities", "domains"],
  "properties": {
    "protocol": { "const": "Agent-First-Protocol" },
    "protocol_version": { "type": "string" },
    "service_name": { "type": "string" },
    "capabilities": {
      "type": "array",
      "items": { "enum": ["entity_lookup","entity_list","blob_access","remote_fetch"] }
    },
    "domains": { "type": "array", "items": { "type": "string" } },
    "auth": {
      "type": "object",
      "properties": {
        "mode": { "enum": ["none","api_key","oauth2_bearer"] },
        "header_name": { "type": "string" },
        "token_endpoint": { "type": "string" }
      }
    },
    "blob_access_methods": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "method": { "enum": ["http","local_file","mcp_blob","ipc"] },
          "template": { "type": "string" }
        }
      }
    },
    "human_view": {
      "type": "object",
      "properties": {
        "available": { "type": "boolean" },
        "transport": { "type": "string" },
        "prefix": { "type": "string" }
      }
    },
    "extensions": { "type": "object" }
  }
}
```

---

## 4. 元数据示例（元器件服务）

```json
{
  "protocol": "Agent-First-Protocol",
  "protocol_version": "v0.2",
  "service_name": "Component-Datasheet-Service",
  "capabilities": ["entity_lookup", "entity_list", "blob_access", "remote_fetch"],
  "domains": ["component", "doc"],
  "auth": { "mode": "none" },
  "blob_access_methods": [
    { "method": "http",     "template": "/api/v1/blob/{sha256}" },
    { "method": "local_file" },
    { "method": "mcp_blob" }
  ],
  "human_view": {
    "available": true,
    "transport": "http",
    "prefix": "/browser"
  },
  "extensions": { "service_owner": "zhuhai-lab" }
}
```

---

## 5. 字段语义

| 字段 | 说明 |
|---|---|
| `capabilities` | 能力集合；`remote_fetch` 表示支持后端回源 |
| `domains` | 该端点提供的业务域 |
| `auth.mode` | `none` / `api_key` / `oauth2_bearer`；**不使用 Cookie/Session** |
| `blob_access_methods` | 二进制获取方式列表，Agent 按序择优 |
| `human_view` | 仅标记人类浏览视图存在；**Agent 业务逻辑禁止使用该通道** |

---

## 6. Agent 侧发现流程伪代码

```text
agent 连接端点
  meta ← discover()
  if meta.protocol != "Agent-First-Protocol":
      拒绝连接，报告协议不兼容
  if "entity_lookup" not in meta.capabilities:
      报告该端点不支持实体查询，终止
  agent 记录：
      domains、auth、blob_access_methods
  # 后续所有 lookup 都带 trace_id
```

---

## 7. llms.txt 索引（可选，推荐）

`/llms.txt`：纯文本实体索引，供大模型语义检索。

```text
# Component Datasheet Service

## Entity Index
- MAX30011 | ADI | EEG analog front-end | wlp
- ADS1299 | TI | 8-ch EEG amplifier | tqfp
- OPA2340 | TI | rail-to-rail op-amp | sot23

## Query Hints
Use entity_lookup with part number.
```
