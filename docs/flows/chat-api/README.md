# Chat API（chat-api）

用户对话请求从 Muse 网页会话，经兼容协议流式或非流式回传客户端。

## 边界

- 包含：
- 不包含（归属哪个域）：

## 流程

| 流程 ID | 名称 | 触发入口 | 关键词 | 文件 | 状态 |
|---|---|---|---|---|---|
| CHATMSG | Claude Messages 网页对话回传 | （待补） | Anthropic messages、Claude Code、stream、links、tool use | chatmsg.md | planned |
| CHATSESS | Claude 会话绑定 Muse thread | （待补） | session、thread、X-Claude-Code-Session-Id、passHeaders、compact、绑定 | chatsess.md | planned |
| CHATSRC | 网页回复采集（事件流优先，DOM 兜底） | （待补） | websocket、onEvent、text_append、message_done、markdown、首字、DOM | chatsrc.md | planned |
| CHATPRUN | 旁聊数量清理（保留最近 50 个） | （待补） | sidechat、session.delete、sessions.list、清理、删除、保留50 | chatprune.md | planned |

## 关联域

| 方向 | 域 | 关联流程 | 说明 |
|---|---|---|---|
| 上游 / 下游 / 相关 | | | |

## 域内共用不变量

-

## 存疑 / 陈旧文档（位于图谱之外，不作为流程事实依据）

> 已确认的业务需求文档可作需求语义依据；其余旧图、README、设计说明、ADR 只作业务线索。与代码冲突时把两处位置整理成待确认问题，不由模型无依据选边。

| 位置 | 对应流程 | 与图谱 / 代码的差异 | 处理状态 |
|---|---|---|---|
