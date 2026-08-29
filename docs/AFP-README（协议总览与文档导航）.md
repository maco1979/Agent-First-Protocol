# Agent‑First‑Protocol (AFP) 文档族

> **版本**：v0.2‑draft
> **状态**：草案（源自 datasheet 器件工程实践提炼）
> **性质**：一套传输无关的 AI Agent 通用交互协议族标准。**不是网站、不是程序、不是 Web 框架**；HTTP‑Web 只是其中一种传输绑定实现。

---

## 一、这套文档是什么

AFP 解决的核心问题：Agent 对接异构外部资源时，需要为每个服务写胶水、解析 HTML、对抗反爬/验证码、硬编码接口地址。

AFP 给出的答案：

1. **实体优先，载体无关** —— 交互核心是「结构化 Entity + 二进制 Blob」，不绑定 HTTP / MCP / IPC / 串口。
2. **强制自发现** —— Agent 连接端点后先执行 `discover()`，能力自动获知，不写死地址。
3. **HTML 禁止作为业务数据源** —— HTML 只是人类兼容视图。
4. **全局 SHA‑256 内容寻址** —— 二进制资源身份由哈希决定，与存储位置无关。
5. **错误 / 告警 / 追踪语义统一** —— 本地与远程调用格式完全一致。
6. **回源可选，Agent 透明** —— 外网抓取对抗全部封装在端点内部。

> MCP 是工具调用传输层；AFP 在其上定义实体业务语义，二者互补、互不替代。

---

## 二、文档族目录

| 编号 | 文件 | 内容 | 阅读顺序 |
|---|---|---|---|
| 00 | `README.md` | 本导航文件 | 1 |
| 01 | `01-Protocol-Core.md` | 协议核心：顶层原则、四层架构、硬性约束 | 2 |
| 02 | `02-L1-Entity-Model.md` | L1 实体数据模型层：Entity / Blob / 统一错误 / 状态枚举 + JSON Schema | 3 |
| 03 | `03-L2-Discovery-Layer.md` | L2 能力自发现层：agent-protocol.json + 各传输入口 | 4 |
| 04 | `04-L3-Operation-Primitives.md` | L3 操作原语层：discover / lookup / list / blob_resolve / fetch_external | 5 |
| 05 | `05-L4-Transport-Bindings.md` | L4 传输绑定层：总览与选择矩阵 | 6 |
| 06 | `06-HTTP-Web-Binding.md` | HTTP‑Web 绑定详细规范（端点、状态码、缓存、限流、鉴权） | 7 |
| 07 | `07-MCP-Binding.md` | MCP 绑定映射规范 | 8 |
| 08 | `08-Local-IPC-Binding.md` | 本地 IPC / Unix‑socket 绑定规范 | 9 |
| 09 | `09-Embedded-Serial-Binding.md` | 嵌入式串口绑定规范 | 10 |
| 10 | `10-Compliance-Checklist.md` | 合规实现检查清单 + 验证矩阵 | 11 |
| 11 | `11-Reference-Implementation.md` | 参考实现：Python FastAPI 最小原型 | 12 |

> 最小可读路径：`01 + 02 + 03 + 10` 即可理解协议并评估合规。

---

## 三、快速上手（30 秒理解）

```text
Agent 连接端点
  → discover()                     # 拿到 agent-protocol 元数据
  → lookup("component","MAX30011") # 拿 Entity（引脚/参数/警告）
  → blob_resolve(sha256)           # 拿二进制资源定位器
  → 处理 Entity.payload            # 业务数据全部在结构化实体里
```

- 状态：`found` / `miss` / `partial-match`
- 错误：统一 `error_code` 枚举 + `trace_id`
- 资源：`sha256` 内容寻址

---

## 四、版本记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-08-29 | 草案：实体优先、自发现、四层架构首次成型 |
| v0.2 | 2026-08-29 | 补齐 JSON Schema、统一错误枚举、三种传输绑定规范、合规清单、参考实现 |

---

## 五、术语表

| 术语 | 含义 |
|---|---|
| AFP | Agent‑First‑Protocol，本协议族 |
| Entity | 结构化业务实体对象（协议 L1 核心） |
| Blob | 二进制资源抽象（PDF / bin / 模型权重等） |
| 端点 (Endpoint) | 实现 AFP 的服务实例（可多传输绑定共存） |
| 绑定 (Binding) | 把 AFP 语义映射到某一种真实传输方式 |
| 回源 (fetch_external) | 端点在实体缺失时内部拉取外部资源的可选能力 |

---

## 六、实现状态（参考实现收尾 · 2026-08-29）

> 本仓库除文档族（01–11）外，还包含可运行的参考实现：`afp_service/`（服务端三绑定）、`afp_client/`（Agent 客户端）与 `tests/`（108 项测试）。合规自评声明文件见根目录 `COMPLIANCE.txt`。

### 6.1 合规等级

```text
AFP Compliance: FULL(除回源)
Bindings: HTTP + MCP + IPC(Windows命名管道)
Domains: component
```

- 最小合规（文档 10 §1）：全部满足。
- 完整合规（文档 10 §2）：除 `fetch_external()` 回源外全部实现——`list()` 分页、限流（429 + retry_after_sec）、API Key 鉴权（401 auth_failed）、审计日志、多绑定共存、`X-Blob-Sha256`、`/llms.txt`、revision 并存不覆盖。
- 延后项：`fetch_external` 回源（协议定位为可选能力，含限速 / 黑名单要求）；Serial 绑定（文档 09，无参考实现）。

### 6.2 测试

```bash
python -m pytest tests -v
# 108 项全绿（2026-08-29 验证）
```

测试文件分工：`test_minimal.py`（最小合规 + blob / revision / trace_id）、`test_full.py`（分页 / 限流 / 鉴权 / 审计 / revision）、`test_e2e.py`（客户端全链路：discover 缓存、能力闸门、blob 模板跟随、完整性校验）、`test_mcp.py`（MCP 绑定 + HTTP/MCP 一致性）、`test_ipc.py`（Windows 命名管道 IPC 绑定）。

### 6.3 启动方式

| 绑定 | 启动命令 |
|---|---|
| HTTP | `cd afp_service && python -m uvicorn server:app --port 47100` |
| MCP | `cd afp_service && python mcp_binding.py` |
| IPC | `cd afp_service && python ipc_binding.py` |

### 6.4 实现裁决记录（ADR）

| 编号 | 裁决 | 依据 |
|---|---|---|
| ADR-I1 | blob 端点支持内容协商：请求头携带 `Accept: application/json` 时返回 locator 集合（含 sha256 / mime_type / size_bytes / locators），而非二进制本体 | 文档 04 §2.4；实现于 `afp_service/server.py` |
| ADR-I2 | 未知 domain 一律返回 `404 + AFPError(entity_missing, "domain not served")`，不与「domain 存在但 key 缺失」区分 | 实现于 `server.py` / `mcp_binding.py` / `ipc_binding.py`，三绑定行为一致 |
