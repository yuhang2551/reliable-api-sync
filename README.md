# Reliable API Sync

**中文** | [English](README_EN.md)

一个小型 Python 集成：校验分页商品 API，用 SQLite 持久化状态，并通过事务性 outbox 发送变更通知。

[详细案例：问题、数据、方法、证据与局限（英文）](CASE_STUDY.md)

![API 同步演示](portfolio-cover.png)

**这是一个独立的工程演示，使用合成商品数据和本地回环 HTTP 服务器，不是真实店铺或消息账号。**

## 本地复现

需要 Python 3.10+，只用标准库。集成测试需要有权限绑定本地回环端口。

    python -m unittest -v
    python demo.py --output demo-output

测试服务器只监听 127.0.0.1，演示结束后自动关闭。不需要 API key，也不会访问任何外部服务。

如果要把文档里的商品接口约定套用到你有权使用的 API 上：

    python api_sync.py --base-url https://your-authorized-api.example --db state.sqlite

这条命令只保存变更，不发送通知。要真正投递，必须显式加上 `--send-to https://your-authorized-receiver.example/events`。源 API 的 token 可以用 `--token-env` 从已有的环境变量读取，它永远不会被转发给事件接收方。上线生产前，必须针对目标系统实现接收方的身份认证。

## API 约定

`GET /products` 返回 `items` 和 `next_cursor`，后续请求通过 cursor 查询参数翻页。每个 item 包含 `id`、正整数 `revision`、非负的十进制字符串 `price`，以及在白名单内的 `currency`（本演示中为 USD/GBP/CNY/AED）。

    {"items":[{"id":"DEMO-01","revision":1,"price":"10.00","currency":"USD"}],"next_cursor":null}

接收方必须用 `{"accepted": true, "event_id": "the-matching-id"}` 确认 POST，并对 `Idempotency-Key` 请求头做持久化去重。首次基线同步不会产生变更事件。

## 已验证的集成场景

| 步骤 | 观察到的结果 |
| --- | --- |
| 模拟 HTTP 429 后的首次同步 | 3 页共 12 个商品落库 |
| 关闭并重新打开 SQLite 后，修改 3 个价格 | 生成 3 条 outbox 事件 |
| 重放相同的商品 revision | 0 条新事件 |
| 接收方已处理事件但先返回 HTTP 503 | 用同一个幂等键重试同一事件 |
| 投递总计 | 4 次 HTTP POST 尝试，接收方收到 3 个不重复事件 |
| 重新加载 SQLite 状态并重放投递 | 0 次新投递，0 条待处理事件 |

`sample_evidence.json` 保存了真实的本地运行证据。20 个自动化测试覆盖：schema 和十进制校验、分页死循环与上限、revision 冲突、事务、有上限的重试、不可重试的 401 响应、错误的确认响应，以及真实的回环 HTTP 场景。

数据库是在同一个 Python 进程内关闭再重新打开的，这不是跨进程重启、崩溃或断电测试。接收方一直在运行，且只在内存中去重。`verification.json` 记录了测试方法、运行时间和源码哈希。

## 可靠性与局限

- 有上限的尝试次数和超时、封顶的退避时间；除回环地址外强制 HTTPS，禁用重定向。
- 先拉取并校验所有分页，再开启数据库事务。格式错误或互相冲突的记录会让本次运行失败，原有状态保持不变。
- 商品值变化时 revision 必须递增。旧的 revision 会被忽略；重复使用且内容冲突的 revision 会被拒绝。
- outbox 提供的是**至少一次（at-least-once）投递尝试**，不是通用的恰好一次投递。接收方必须实现持久化幂等。演示用接收方只在内存中去重。
- 只支持单个发送者/worker。不包含分布式锁、生产级接收方认证、调度、监控、全量刷新时的删除、针对具体商城的适配器或 SLA。
- Retry-After 只支持秒数形式，不支持 HTTP 日期格式。响应大小和分页数量有意设置了上限。
- 真实部署仍需要服务方的访问授权、凭证、接口约定、接收方配置，以及在目标环境中的验收测试。

## 项目说明

源码、接口约定和记录下来的测试共同说明了演示的能力。演示中没有使用第三方账号、商城爬取、真实商品数据或外部通知。
