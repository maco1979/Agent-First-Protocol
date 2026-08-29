# 06 · HTTP‑Web Binding 规范

> AFP‑v0.2 | 把 AFP 语义映射到 HTTP/1.1、HTTPS。**这是 AFP 最常用的绑定，也是参考实现使用的绑定。**

---

## 1. 端点映射

| AFP 原语 | HTTP 端点 | 方法 |
|---|---|---|
| discover | `/.well-known/agent-protocol.json` | GET |
| lookup | `/api/v1/{domain}/lookup` | GET |
| list | `/api/v1/{domain}/list` | GET |
| blob_resolve | `/api/v1/blob/{sha256}` | GET |
| fetch_external | `/api/v1/{domain}/fetch` | POST |
| llms.txt | `/llms.txt` | GET |

---

## 2. 请求约定

### 2.1 lookup 请求

```
GET /api/v1/component/lookup?key=MAX30011&revision=Rev-C
```

参数：
- `key`：必填，实体标识
- `revision`：可选，缺省返回最新
- `domain` 在 URL 路径中

### 2.2 blob 请求

```
GET /api/v1/blob/a94a8fe5ccb19ba61c4c0873d391e987982fbbd3
```

响应：原始二进制流。
响应头必须包含：`X-Blob-Sha256: <hex>`，供 Agent 下载后完整性校验。

### 2.3 fetch_external 请求

```
POST /api/v1/component/fetch
Content-Type: application/json

{ "key": "MAX30011", "revision": null }
```

返回：回源后的 Entity（状态 found）或 AFPError。

---

## 3. 响应语义与状态码

### 3.1 状态码映射

| HTTP 状态码 | 场景 |
|---|---|
| 200 | found（Entity）或 partial-match（Entity + warnings） |
| 400 | 参数非法（invalid_param） |
| 401 | 鉴权失败（auth_failed） |
| 404 | 实体不存在（entity_missing） |
| 429 | 限流（rate_limit，返回 retry_after_sec） |
| 501 | 能力不支持（capability_not_supported） |
| 502/503 | 回源失败 / 服务暂不可用 |

### 3.2 业务错误响应体（统一 AFPError）

```json
{
  "error_code": "rate_limit",
  "human_readable": "Too many requests",
  "warnings": [],
  "trace_id": "trace-20260829-003",
  "retry_after_sec": 30
}
```

> **禁止**把业务错误包装在 200 里。

---

## 4. trace_id 透传约定

- Agent 请求可选带请求头：`X-AFP-Trace-ID: <uuid>`
- 端点若收到，必须在响应 meta.trace_id 原样回传；未收到则自行生成并返回。
- 服务日志必须记录 trace_id 与调用方。

---

## 5. 鉴权

### 5.1 三种模式（与 discover 元数据一致）

| mode | 实现 |
|---|---|
| none | 无鉴权（本机/内网） |
| api_key | 请求头 `X-AFP-Api-Key: <key>` |
| oauth2_bearer | 请求头 `Authorization: Bearer <token>` |

### 5.2 约束

- 不使用 Cookie / Session。
- 鉴权失败返回 401 + AFPError(auth_failed)。

---

## 6. 缓存语义

| 资源类型 | Cache-Control |
|---|---|
| 实体元数据 | `public, max-age=3600` |
| Blob 静态资源（不可变） | `public, immutable, max-age=31536000` |
| discover 元数据 | `public, max-age=300` |

> sha256 内容寻址使 Blob 可安全长期缓存。

---

## 7. 限流与审计

1. 对外实例必须实现限流；内网实例可关闭但配置项必须存在。
2. 限流响应 429 + `retry_after_sec`。
3. 每个请求记录 trace_id、调用方、查询 key、是否触发回源、回源耗时。
4. 回源子系统独立限流：**只处理被显式请求的实体，禁止全站遍历爬取**；配置回源黑名单域名。

---

## 8. 人类兼容层（human_view）

- 全部人类页面统一前缀 `/browser/**`。
- Agent 禁止从 `/browser/**` 页面解析业务数据。
- 可配置 `human_view.available=false` 完全关闭前端页面，仅保留机器 API。

---

## 9. 示例交互（curl）

```bash
# discover
curl -s http://127.0.0.1:47100/.well-known/agent-protocol.json

# lookup
curl -s "http://127.0.0.1:47100/api/v1/component/lookup?key=MAX30011"

# blob
curl -s -o MAX30011.pdf \
  -H "X-AFP-Trace-ID: trace-demo-1" \
  http://127.0.0.1:47100/api/v1/blob/a94a8fe5ccb19ba61c4c0873d391e987982fbbd3
```
