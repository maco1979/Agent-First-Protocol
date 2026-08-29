# 02 · L1 实体数据模型层（Entity Model Layer）

> AFP‑v0.2 | 本层是协议最核心部分，**与传输完全无关**。任何载体（HTTP/MCP/IPC/串口）传递的数据都必须遵守本层 Schema。

---

## 1. Entity 实体对象（强制）

### 1.1 Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://agent-first-protocol.dev/schema/entity.json",
  "title": "AFPEntity",
  "type": "object",
  "required": ["entity_id", "domain", "payload", "meta"],
  "properties": {
    "entity_id":   { "type": "string", "description": "该域下唯一业务标识" },
    "domain":      { "type": "string", "description": "业务域，如 component/doc/cad/sensor" },
    "revision":    { "type": ["string", "null"], "description": "修订版本；同一 entity_id 允许多版本并存" },
    "source_origin": { "type": ["string", "null"], "description": "溯源来源字符串" },
    "sha256":      { "type": ["string", "null"], "description": "关联二进制资源哈希；无附件为 null" },
    "payload":     { "type": "object", "description": "业务自定义结构体" },
    "warnings":    { "type": "array", "items": { "type": "string" }, "description": "风险告警数组；partial-match 必填" },
    "meta": {
      "type": "object",
      "required": ["trace_id", "timestamp"],
      "properties": {
        "trace_id":   { "type": "string", "description": "全链路追踪 ID，跨进程透传" },
        "timestamp":  { "type": "string", "format": "date-time", "description": "ISO-8601 时间戳" }
      }
    }
  }
}
```

### 1.2 示例

```json
{
  "entity_id": "MAX30011",
  "domain": "component",
  "revision": "Rev-C",
  "source_origin": "ADI official datasheet",
  "sha256": "a94a8fe5ccb19ba61c4c0873d391e987982fbbd3",
  "payload": {
    "part_number": "MAX30011",
    "manufacturer": "ADI",
    "package": "WLP",
    "pins": [ { "pin_num": "1", "pin_name": "VDD", "pin_type": "power_in" } ],
    "absolute_max_rating": [ { "param": "VDD", "max": "3.6", "unit": "V" } ],
    "forbidden_connect": ["NC 引脚禁止外接信号"]
  },
  "warnings": [],
  "meta": {
    "trace_id": "trace-20260829-001",
    "timestamp": "2026-08-29T08:00:00Z"
  }
}
```

### 1.3 字段语义说明

| 字段 | 说明 | 约束 |
|---|---|---|
| `entity_id` | 业务主键 | 必填，域内唯一 |
| `domain` | 业务域 | 必填，建议注册制 |
| `revision` | 版本 | 允许空；多版本并存时不覆盖 |
| `source_origin` | 溯源 | 审计用，尽量填写 |
| `sha256` | 资源哈希 | 无附件必须为 `null`，不得留空字符串 |
| `warnings` | 告警 | `partial-match` 时必须非空 |
| `meta.trace_id` | 追踪 | 必填 |

---

## 2. Blob 资源抽象模型（强制）

> 本协议**不规定存储路径**，只定义身份与元信息；获取方式由自发现层告知。

| 属性 | 类型 | 说明 |
|---|---|---|
| `sha256` | string | 资源唯一身份（内容寻址） |
| `mime_type` | string | 媒体类型，如 `application/pdf` |
| `size_bytes` | integer | 字节数 |
| `locator` | object | 访问定位器，由绑定层提供 |

### locator 示例（多载体共存）

```json
{
  "locator": {
    "http":  "https://svc.local/api/v1/blob/a94a...",
    "local_file": "/data/mirror/a94a...pdf",
    "mcp_blob": "blob://a94a..."
  }
}
```

---

## 3. 统一错误对象（强制）

> 任何传输载体返回错误，必须复用此结构。

### 3.1 Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://agent-first-protocol.dev/schema/error.json",
  "title": "AFPError",
  "type": "object",
  "required": ["error_code", "trace_id"],
  "properties": {
    "error_code":       { "type": "string" },
    "human_readable":   { "type": "string" },
    "warnings":         { "type": "array", "items": { "type": "string" } },
    "trace_id":         { "type": "string" },
    "retry_after_sec":  { "type": "integer", "minimum": 0 }
  }
}
```

### 3.2 error_code 枚举（强制）

| error_code | 语义 | 建议 HTTP 映射 |
|---|---|---|
| `ok` | 成功（一般不出现在错误对象） | 200 |
| `entity_missing` | 实体不存在（miss） | 404 |
| `partial_match` | 模糊命中，需看 warnings | 200 + warnings |
| `rate_limit` | 触发限流 | 429 |
| `invalid_param` | 参数非法 | 400 |
| `fetch_remote_failed` | 回源拉取外部失败 | 502/503 |
| `auth_failed` | 鉴权失败 | 401 |
| `capability_not_supported` | 端点不支持该能力 | 501 |

### 3.3 示例

```json
{
  "error_code": "entity_missing",
  "human_readable": "No entity MAX30011 in domain component",
  "warnings": [],
  "trace_id": "trace-20260829-002",
  "retry_after_sec": 0
}
```

---

## 4. 查询状态枚举（强制）

| 状态 | 语义 | 使用要求 |
|---|---|---|
| `found` | 精确命中实体 | 可正常使用 |
| `miss` | 端点无此实体 | 返回错误对象 `entity_missing` |
| `partial-match` | 模糊部分匹配 | **必须**携带 `warnings`，Agent 禁止静默用于生产生成 |

### 状态 → 数据结构对应表

| 状态 | 返回内容 |
|---|---|
| `found` | `Entity`（warnings 可为空） |
| `miss` | `AFPError`（error_code=entity_missing） |
| `partial-match` | `Entity` + `warnings` 非空；可附候选列表 |

---

## 5. 业务域注册建议

| domain | 示例 entity | 说明 |
|---|---|---|
| `component` | MAX30011 | 元器件/器件 |
| `doc` | 协议文档 | 文档知识库 |
| `cad` | 3D 模型 | CAD 实体 |
| `sensor` | 传感器节点 | 硬件设备 |
| `model` | Qwen3-4B | 模型权重/元数据 |

> 新域建议在发现层 `domains` 数组中声明，并补充 `llms.txt` 索引。
