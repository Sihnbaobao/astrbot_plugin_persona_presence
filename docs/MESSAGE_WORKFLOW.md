# Persona Presence 消息工作流程

本文按事件到达顺序说明 Persona Presence（人格自主参与）如何决定一次消息是否进入正式回复。核心选择来自 Persona 的兴趣与当下意愿，而不是随机概率或固定的 @ 必回规则。

[← 返回 README](../README.md) | [架构指南](ARCHITECTURE.md) | [配置参考](CONFIG_REFERENCE.md) | [项目结构](PROJECT_STRUCTURE.md)

## 流程总览

消息事件
  |
  +-- 平台空事件与重复事件过滤
  |
  +-- 群聊/私聊开关、群组/用户范围、黑名单、命令过滤
  |
  +-- 当前发送者、目标 signal、媒体和回复对象提取
  |
  +-- 图片、转发、表情和戳一戳整理
  |
  +-- Smart anchor/follower 批次协调
  |
  +-- 明显 noise 快速过滤
  |
  +-- DecisionAI.evaluate：事实分类 + Persona 意愿
  |
  +-- ParticipationDecision 本地硬边界
  |
  +-- open/side 参与预算
  |
  +-- ReplyHandler 正式生成、发送、保存

## Phase 0：平台事件过滤

插件先处理平台重复投递、无法取得消息正文的真空事件和已处理 message ID。这一阶段不产生任何“机器人要不要回复”的推断。

## Phase 1：基础范围和硬边界

以下条件直接结束当前插件处理：

- enable_group_chat 或 enable_private_chat 关闭；
- 当前群组不在 enabled_groups，或私聊用户不在 enabled_private_users；
- 用户黑名单或黑名单关键词命中；
- 消息被 enable_command_filter 和 command_prefixes 识别为命令；
- 已被现有去重状态消费。

硬边界的静默不会作为 observation 反复喂给参与判断模型，也不会触发正式回复。

## Phase 2：目标 signal 和消息整理

主流程记录以下事实：

- 当前消息发送者名称和 ID；
- 平台是否检测到 @/戳/结构化回复机器人；
- 是否 @ 或回复其他用户；
- 文本是否命中关键词；
- 当前消息是否包含图片、表情、视频、语音、文件或转发；
- 近期机器人回复以及其后的群聊内容；
- 当前事件是否在语义上承接这轮对话，或只是一个新的公开话题。

这些事实只描述事件，不代替 Persona 决定是否愿意参与。关键词和 @ 是注意力 signal，不能直接设置 reply=yes。DecisionAI 会判断中间的群友消息是否实际接管或改变话题，短暂的无关插话不自动结束续话。

group_reply_scope=addressed 时，未明确指向机器人的群消息在这里结束；被允许进入候选的消息仍要经过后续参与判断。group_reply_scope=ambient 时，普通消息继续进入后续流程。

at_all_message_mode=skip_all、welcome_message_mode=skip_all 等显式管理员 force 分支可以跳过参与判断。这是配置覆盖，不是普通 @ 的默认行为。

## Phase 3：媒体和上下文准备

普通文字经过现有清洗和元数据格式化。图片按 image_read_mode 选择：

- lazy：先让参与判断根据文字、占位符和已有描述判断是否值得回复，通过后才做相关图片识别；
- eager：参与判断阶段直接带入图片信息。

转发、表情、视频、语音和文件沿用各自解析器。媒体失败不能生成一个虚假的消息对象，也不能单独成为回复理由。

## Phase 4：Smart 批次协调

concurrent_mode=smart 时，以到达序号最早的消息作为 anchor，并在有界时间内吸收 follower。每条 follower 继续保留：

- 原发送者和发送者 ID；
- 原文本和媒体；
- 到达顺序；
- 当前批次标记。

anchor 是主要回复对象。follower 只能作为背景，不会覆盖 anchor 的发送者，不会独立制造回复目标，也不会单独启动正式回复。

如果 follower 在 anchor 结束前被消费，它不会重复处理；如果 anchor 被静默，整批消息都标为 observation-only。

## Phase 5：明显低信息快速过滤

ambient 模式可能在模型调用前过滤未明确指向机器人的、确定的纯媒体、低信息反应和只等待他人回答的无正文消息，以减少无意义调用。被 @、点名、关键词或可靠回复信号明确指向的纯媒体仍可进入图片处理和后续参与判断。有效短问题、明确邀请和唯一指代的续问不应被这个阶段误杀。

此阶段只做保守的明显噪声过滤。只要消息仍可能包含具体内容，就交给 DecisionAI，而不是用长度或关键词猜测意愿。

## Phase 6：DecisionAI.evaluate

DecisionAI 使用当前 Persona、当前发送者、当前正文、目标 signal 和必要的 Smart 上下文，返回结构化参与结果：

- target：bot、other、open、unclear；
- information：noise、reaction、substantive；
- continuation：yes、no；
- participation：direct、side、open、none；
- interest：strong、weak、none；
- reason_code：direct_request、shared_interest、personal_experience、emotional_reaction、continuation、none；
- confidence 和有界 topic_key；
- reply：yes 或 no。

判断顺序是先确认消息对象和说话姿态，再由 Persona 结合正文、近期对话、插话是否接管、性格、情绪和聊天氛围做整体判断。模型“能回答”不等于一定要回复，也不再由本地代码强制要求某个 interest 等级。continuation 的语义判断属于 Persona；群聊 continuation=yes 还要通过当前发送者与最近机器人回复对应发送者的结构化事实校验。

## Phase 7：本地硬边界

ParticipationDecision normalizer 在模型之后重新执行不可被 prompt 绕过的规则：

1. 未知枚举、unclear 或 none participation 直接 no。
2. target 与 participation 的说话姿态不一致时直接 no；other 只能采用 side 说话姿态，不能替其他用户作答。
3. open、side 和 direct 的主观是否参与由 Persona 决定；continuation 不构成本地兴趣门槛，但未经发送者事实校验的群聊续话不会进入正式回复；interest、information、reason_code 仍不单独构成本地兴趣门槛。
4. reply=no 不能被后续代码重新解释为可以回答；@ 也不是强制命令。
5. 空 JSON、JSON 解析失败或其他不可信输出：群聊和活动私聊收尾边界保持静默；普通私聊无活动边界时可回退到正式回复链，最终仍由正式 provider 的安全策略决定。

旧 provider 返回精确 yes/no 时保留受限兼容，不授予旧格式的 side 旁观能力。看起来像 JSON 但解析失败的输出不会被当成自然语言 yes。

## Phase 8：开放参与预算

只有已经通过 Phase 7 且 participation 是 open 或 side 的群聊结果进入 ParticipationThrottle：

- 默认同一群最小间隔 45 秒；
- 默认 600 秒内最多 4 次；
- direct 和 private 不消耗预算；
- 每个群独立计数，插件重载清空；
- 预算只限制主动参与，不会提高低兴趣消息的通过率。

预算拒绝的结果与普通模型拒绝一样进入 observation-only 处理，不能成为后续“欠回复”。

## Phase 9：正式回复

通过全部决策后，ReplyHandler：

1. 解析 AstrBot 当前会话最终 Persona；
2. 使用现有格式化上下文、Smart 背景和媒体数据；
3. 追加最小 reply_context_hint，只说明 direct/side/open 姿态和有限 reason；
4. 调用 AstrBot provider，继续复用工具、第三方 Hook、内容过滤和发送流程；
5. 保存用户消息、AI 回复和必要的批次历史。

正式模型不接收 DecisionAI 的完整 JSON、隐藏推理、决策分析标记或关键词命中理由。它只负责说什么，不重新发明是否参与的规则。

## Phase 10：静默消息和缓存

模型明确拒绝、预算拒绝或受保护边界拒绝时：

- 当前消息可以写入 decision_state=observed；
- Smart follower 同样标为 observed；
- 私聊纯文本 anchor 可在 Smart 窗口内提前预判；无 follower 时复用预判，有 follower 时丢弃预判并只使用合并批次的权威判断。
- MessageCacheManager 的 active、regular、window 和图片候选读取都会排除 observed；
- 不生成正式回复；
- 对应的 takeover_group_reply/takeover_private_reply=true 时 stop_event，避免 AstrBot 默认链路再次回答；
- 对应开关为 false 时将控制交还 AstrBot 核心链路。

群聊和活动私聊收尾边界的参与判断异常仍 fail closed；普通私聊无活动边界时在接管模式下 fail-open 到正式回复链，避免 DecisionAI 上游审查或短暂故障直接吞掉私聊。正式 provider 仍会执行自己的安全策略。

## 私聊分支

普通私聊由 enable_private_chat 和 enabled_private_users 控制。private_reply_mode=direct 时白天无收尾状态的普通消息绕过私聊参与判断；本地时间 01:00-07:00 的未明确指向普通私聊会先经过一次概率性深夜复核；实际回复成功后约 45 分钟恢复自然对话，并在后续成功回复时续期。如果 private_concurrent_mode=smart，短时间连续消息仍会先进入 Smart 批次，再生成一条正式回复。private_reply_mode=decide 时使用私聊专用判断，但不套用群聊 open/side 预算。两种模式都会在正式回复生成后检查可识别的收尾短语，并记录睡眠假设或需要后续语义复核的软边界；这不是模型提供的“高置信度”结论。

睡觉类收尾进入有范围的睡眠假设：把“去睡”视为真实的休息或离线边界。普通消息仍会进入私聊判断上下文，但在最早可能醒来之前保持 no；通常的睡眠范围约为 4-10 小时，“睡一会儿/午睡”约为 30 分钟至 3 小时。睡眠范围内会随机安排一次唤醒检查，并在存在更晚的睡眠截止点时追加一次接近范围结束的最终复核；如果期间有待处理消息，Persona 会在这些时点重新经过私聊决策，判断消息是否值得打扰以及是否自然恢复对话，重要或紧急只提高评估优先级，不强制回复；没有待处理消息则不主动发言。范围结束后恢复普通私聊。不想聊、别烦、没空陪你等话语只建立一个软复核提示，不设置固定计时器，也不直接把后续消息判成 no。模型会读取上一条 Persona 原话和当前消息：如果对方继续施压、纠缠或重复打扰可以保持 no；如果对方礼貌收住、道歉、提供有用信息或自然换题，可以 reply=yes；正式回复成功送达后才清除提示，生成或发送失败时继续保留。被跳过的边界消息保留在待处理上下文中。

纯私聊图片和表情按 private_image_mode、private_emoji_mode 处理；图片还必须先通过 enable_image_processing/image_to_text_scope 的前置过滤，未被图片处理器保留时不会到达媒体策略。保留下来的媒体中：ignore 在边界复核前直接丢弃，decide 进入参与判断，always 在没有活动收尾边界时跳过参与判断，有活动睡眠或回避边界时仍按边界复核。takeover_private_reply=false 时，插件的静默结果可能交回 AstrBot 核心。含文字的图文消息仍按普通文本处理。private Smart 合并同一短时批次，不改变发送者和正式人格；direct 普通文本也可参与 Smart 合并，但不调用普通私聊 DecisionAI。普通 direct 纯文本 anchor 会在窗口期间提前生成一份不执行工具、非流式缓冲文本；Smart 提交时仅沿用已完成的草稿并通过正常 LLM 结果保存链路发送；仍在生成的草稿会取消并走正常 Agent，出现 follower 时也作废并对合并消息重生成。literal sleep 静默期缓存而不批处理。

## 故障排查

### 被 @ 了但没有回复

这是预期可能行为。检查日志中的 target、information、interest、reason_code 和 participation；被 @ 只提高注意力，模型仍可能判断无聊、重复、冒犯、已结束或不想回应。若显示 ambient budget，检查 45/600/4 设置。

### 没有 @ 却回复了

确认它是 open/side，且模型明确选择了合适的说话姿态。这是允许的 Persona 自主参与，不是关键词误触发；如果回复过于频繁，降低预算上限或调整 Persona 边界。

### 所有消息都没有回复

检查 DecisionAI provider、timeout、当前 Persona 是否为空、group_reply_scope、enabled_groups 和 takeover 日志。群聊或活动私聊边界的结构化输出解析失败会静默；普通私聊无活动边界时会记录 fail-open 并进入正式回复，正式 provider 仍可能按自身安全策略拒绝。

### 同一条消息反复成为上下文

检查 message_cache_manager 是否过滤 decision_state=observed，以及 Smart anchor/follower 是否正确清理。观察消息不得制造 continuation 或图片候选。
