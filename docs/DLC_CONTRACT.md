# Persona Presence DLC 契约

> 资料片（DLC）是可选能力，不是缩小版本体。关闭时不改变本体行为；开启时只做加法。

## 1. 本体边界

本体拥有“一条消息的完整生命周期”：

```
事件进入 -> scope/安全过滤 -> 解析人格 -> 参与判断 -> 生成回复 -> 发送 -> 写历史 -> 失败处理
```

本体负责：

- 指令、黑名单、@、媒体、takeover 等入口边界；
- 当前会话的人格、provider、时区、群/私聊 scope；
- 参与判断、正式回复生成、发送、去重、历史保存和失败策略；
- 给资料片提供只读的 `FeatureContext`，并接收 `Proposal`。

资料片负责：

- 观察（它看到了什么）；
- 贡献上下文（它知道/在意什么）；
- 提议（它想额外说一句）；
- 从本体实际发送的回复中学习。

资料片不得：

- 调用 `context.send_message` 直接发消息；
- 自己解析人格、provider 或时区；
- 自己决定群 scope 或写全局聊天历史；
- 绕过本体的内容过滤、重复过滤和并发闸门。

## 2. 接口

`utils/modules/base.py` 定义了最小契约：

```python
@dataclass(frozen=True)
class FeatureContext:
    event: Any
    umo: str
    group_id: str
    is_private: bool
    chat_key: str
    persona: str        # 已解析的当前会话人格
    provider_id: str    # 已解析的当前会话模型
    now: datetime       # 已解析时区的当前时间

@dataclass(frozen=True)
class Proposal:
    text: str
    source: str
    reason: str = ""
    group_id: str = ""
    created_at: float = 0.0
```

生命周期钩子：

- `on_event(ctx)`：只记录/观察，不做副作用。
- `contribute_context(ctx)`：返回一段背景文本，由本体决定是否注入。
- `propose(ctx)`：返回至多一条 `Proposal`，由本体决定是否发送。
- `on_reply(ctx, reply_text)`：从本体已经发出的回复中学习。
- `on_proposal_sent(ctx, proposal)`：本体确认提议已发送后，资料片更新自己的冷却/计数。
- `close()`：落盘。

## 3. 发送闸门

所有 `Proposal` 都经过 `PersonaPresence._send_feature_proposal`：

1. 清洗泄漏标签和机械前缀；
2. 同一会话有正式回复在跑时拒绝；
3. 本会话刚发过言时拒绝；
4. 与近期回复重复时拒绝；
5. 通过后由本体发送，并写入平台历史；
6. 通知资料片 `on_proposal_sent`。

资料片自己返回 proposal 不等于发送成功。

## 4. 当前迁移状态

- 主动发言：已迁移为 `on_event` + `propose`，只在参与判断明确由 Persona 拒绝（`source=ai/legacy`）后提议；硬策略拒绝（节流、话轮、疲劳、addressed 模式）不会触发。群聊缓冲只用于判断时机，**不进入发言 prompt**；prompt 里只有人格、兴趣、最近经历和好奇心知识，让它分享自己的事而不是接群聊话题。全局冷却、群里安静窗口（90–900 秒）和近期话题去重负责社交节奏；状态、门槛原因、最近动作和 no-send 试跑在插件页「主动发言」中可见。
- 记忆本：已按群标记 `group_id`，并给黑话加 `cross_group`。`internet` 类通用梗默认跨群可用；`group`/`person` 类默认只在本群注入，需要跨群引用时打开 `notebook_cross_group_jargon`，条目会带“来自其他群”的出处标记。旧版 `group:manual` 条目按通用黑话处理。群限定黑话采用三阶段遗忘（14 天宽限、7 天周期、三个空周期删除，重新出现/匹配使用重置），通用梗默认保护。下一步迁移到 `contribute_context` / `on_reply` 接口。
- 好奇心：已接入 `FeatureContext` 的 persona/provider/time；按群缓冲，每 15 条消息或 45 分钟让 Persona 结合自己的兴趣反思“我到底想弄明白什么”，保存搜索词并直接查，不再先用 known/slang/meh 分类把话题筛掉。待查和查到的知识放在独立的 `curiosity.json`，不进入记忆网络；记忆本只提供兴趣。明确的“X 是什么”问题会立即入队；未配置 Tavily key 时不调用模型；连续两次空结果或总结明确说没查到信息时会丢弃。后台按每日预算（默认 5 条、最短间隔 10 分钟）逐步清待查，不依赖新消息；明确问题优先。待查默认保留 7 天；知识采用三阶段遗忘（14 天宽限、7 天一个周期、连续三个空周期删除，被注入/主动使用后重置），可选硬 TTL 默认关闭。插件页支持逐条编辑/删除并按固定高度分页。
- LifeSight：已作为本体的“感官”接入 `_build_background_block`（只在活动类问题注入；泛问不带标题/陈旧观看，追问具体视频才带标题）和主动发言的 `state_provider`；默认关闭，数据目录可配置。

## 5. 验收

- 全部资料片关闭时，本体核心测试通过且行为不变。
- 只能由本体发送消息、写历史；资料片只贡献/提议。
- 资料片的异常、超时或空输出不改变本体的参与判断。
- 资料片数据带群/私聊 scope，不跨群泄漏。
- 每个资料片可以单独关闭；主动发言可按群限制。
