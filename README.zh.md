# Agent-First-Protocol（AFP）

**给 AI Agent 定的一套通用"普通话"——任何服务说这套话，任何 Agent 插上即用。**

[English](README.md)

---

## 解决什么问题

现在各种对外接口、服务五花八门：查文档、查数据库、硬件传感器、CAD 模型、业务数据、知识库、第三方能力。

**现状**：

- 每个服务说话的规矩都不一样，AI Agent 想用就要专门写一份适配胶水代码。
- 有的走 HTTP，有的走 MCP，有的是本地程序，有的是硬件串口；返回格式各搞各的，报错五花八门。
- 很多服务本来就是给人看的网页，AI 还要去扒网页内容，网页一改直接崩。

**AFP 就是一套通用"普通话"标准**：不管什么业务、不管怎么连线，业务语义全部同一套规矩，底层传输渠道随便换。

## 五条核心规矩

1. **先自我介绍**：Agent 连上先问 discover，服务返回标准能力清单。不用硬记地址、不用死记接口。
2. **只给结构化数据**：不给 AI 扔网页 HTML。人看的页面放独立前缀，Agent 永不接触。
3. **文件用哈希指纹**：sha256 内容寻址。文件放哪无所谓，指纹对得上就是同一个，还能防篡改。
4. **报错全统一**：8 种错误码 + trace_id 追踪号，跨传输一致。Agent 的错误处理代码写一次到处用。
5. **脏活服务自己扛**：爬虫、验证码等外部抓取由服务内部消化，Agent 只见规整结果。（回源是可选能力——没有照样合规，声明里不许虚报。）

## 两条"防 AI 犯错"铁律

"Agent-First" 的真正由来——一切设计围绕"别让 AI 犯错"：

- **查不到就老实说查不到**：404 + entity_missing，禁止包在 200 里装成功。
- **模糊命中必须带警告**：partial-match 强制携带 warnings，禁止静默糊弄，防 AI 一本正经胡编。
- 配套原则：**能力声明必须诚实**——只声明真实实现了的能力。

## 四层架构

| 层 | 名字 | 干什么的 |
|---|---|---|
| L1 | 实体数据模型 | "回执单"长什么样：Entity / 统一错误对象（8 种错误码） |
| L2 | 能力自发现 | `/.well-known/agent-protocol.json`——对方是谁、能干什么 |
| L3 | 操作原语 | 5 个固定动词：discover / lookup / list / blob_resolve / fetch |
| L4 | 传输绑定 | 同一套话用多种"方言"：HTTP / MCP / 命名管道 / 串口 |

## 仓库内容

| 位置 | 说明 |
|---|---|
| `docs/` | 协议文档族 01–11（协议正文，权威） |
| `afp_service/` | 参考实现：HTTP + MCP + IPC 三绑定，SQLite 存储 |
| `afp_client/` | Agent 客户端 SDK：能力校验、哈希校验、警告强制暴露 |
| `tests/` | 108 项测试验证实现守规矩 |
| `COMPLIANCE.txt` | 合规自评：FULL（除回源） |

## 快速开始

```bash
pip install -r afp_service/requirements.txt

# HTTP 服务（端口 47100）
cd afp_service && python -m uvicorn server:app --port 47100

# MCP 绑定
cd afp_service && python mcp_binding.py

# 本机 IPC（Windows 命名管道）
cd afp_service && python ipc_binding.py

# 跑全部测试
python -m pytest tests -v
```

验证：

```bash
curl http://127.0.0.1:47100/.well-known/agent-protocol.json
curl "http://127.0.0.1:47100/api/v1/component/lookup?key=MAX30011"
```

## 状态

合规自评：**FULL（除回源）** | 绑定：**HTTP + MCP + IPC** | 测试 108/108 通过。

延后项：`fetch_external` 回源（协议定位为可选能力）、嵌入式串口绑定（文档 09，暂无参考实现）。

## 许可证

[MIT](LICENSE)
