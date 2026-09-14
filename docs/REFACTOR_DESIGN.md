# Persona Presence 重构设计

> 本文是当前实现的行为契约和维护入口。代码细节可以变化，但这里定义的群聊社交边界、数据流和失败策略不能被无意改回旧行为。

## 1. 目标

Persona Presence 的职责是帮助 AstrBot 在群聊中像一个真实群成员一样选择发言时机，同时保留 AstrBot 的人格、历史、媒体和正式回复链路。

目标不是让机器人尽可能多地回答，而是让每次开口都像人格自己的选择：

- 大多数群消息只被看见，不产生回复。
- @、点名、戳一戳、关键词和结构化回复会提高注意力，但都不是回复保证。
- 没有 @ 的公开话题，如果当前 Persona 自然想参与，可以按自己的力度补充；不再要求固定的强兴趣等级。
- “我知道答案”“我能帮忙”或“模型可以回答”不等于人格想发言。
- 明确无聊、重复、冒犯、打扰、已经结束、无信息或只等待别人回答的消息通常保持安静。

本次重构只改变参与决策和必要的上下文边界，不改变 AstrBot 正式回复模型的人格来源。

## 2. 不可违反的行为契约

### 2.1 三类参与姿态

每条候选群消息都要先确定说话姿态：

| participation | 含义 | 允许条件 |
| --- | --- | --- |
| direct | 消息在和机器人说，或是唯一明确的机器人续话 | 仍需人格愿意；被 @ 也可以拒绝 |
| side | 消息直接面向其他用户，但正文是公共话题 | 只能补充人格自己的相关内容，不能替对方作答或接管话题 |
| open | 没有特定对象的公共发言入口 | 有具体个人补充或自然反应时可以参与；是否展开或只说一句由当前 Persona 决定 |
| none | 没有可靠的自然发言入口 | 立即静默 |

side 不能替被 @ 的用户作答、替对方承诺、抢走对方的对话，也不能把一句泛泛知识答案包装成个人参与。

### 2.2 兴趣强度

- strong：当前内容明显击中 Persona，Persona 现在就想展开说自己的具体经历、观点或情绪。
- weak：Persona 没有强烈冲动，但可能只想轻轻补充一句。strong、weak 和 none 都是意愿描述，不再由本地代码机械决定能不能发言。
- none：不感兴趣、讨厌、疲惫、重复、冒犯、打扰、话题已结束或没有自然切入点。

open、side 和 direct 都由 Persona 的整体意愿决定；本地硬策略只校验消息对象、说话姿态、输出格式和不替他人作答的边界。直接地址仍不是强制命令。

### 2.3 信息级别

- noise：纯媒体、贴纸、刷屏或没有内容的流水账。
- reaction：简单附和、单独主题词或对上一句的短反应。
- substantive：具体事实、观点、问题、请求、经历、社交邀请或可以展开的内容。

短消息不必然是 noise；有效短问题、明确邀请和唯一指代的续问仍可进入判断。反过来，长句也不自动值得回复。

### 2.4 连续话轮的客观边界

continuation 不是“历史中曾经聊过同一主题”的同义词。DecisionAI 会结合机器人近期回复、当前消息和中间的群聊内容判断：短暂的无关插话不自动结束续话，后续聊天已经实质接管或明显转向时也不能只凭主题相似继续旧话题。时间间隔本身不是硬截止条件。

## 3. 运行时数据流

1. 事件入口执行平台重复过滤、黑名单、指令、欢迎消息、@全体和 @他人策略等硬边界。
2. 提取当前发送者、原文、平台目标信号、回复目标、媒体和戳一戳信息。平台 signal 是事实输入，不替模型决定社交意愿。
3. 处理图片、转发、表情和媒体描述；lazy 图片只在确实需要时继续识别。
4. Smart 模式由最早到达的消息担任 anchor，后续 follower 保留自己的发送者和顺序，并作为同一批上下文交给正式回复；private_reply_mode=direct 的白天普通文本不进入私聊 DecisionAI；本地时间 01:00-07:00 的未明确指向普通私聊先经过一次概率性深夜复核，但仍可进入 Smart。decide 纯文本 anchor 可在窗口内提前预判；direct 纯文本 anchor 可在窗口内提前生成不执行工具、非流式缓冲文本；模型若请求工具则放弃草稿，改走完整 Agent。Smart 提交时仅复用已完成的对应结果；仍在生成的 direct 草稿会取消并走正常 Agent，窗口内出现 follower 时作废并对合并批次重判或重生成。
5. DecisionAI 只对仍可处理且需要门控的消息做参与判断。普通 direct 私聊文本直接进入 ReplyHandler；private decide、媒体 decide、活动私聊边界和群聊候选才走 DecisionAI。direct Smart 的纯文本预生成不使用 DecisionAI；预生成阶段不执行工具，模型若返回工具调用则回退到完整 Agent。
6. 结构化结果经过本地 normalizer 的硬规则校验。模型不能通过未知枚举、模糊目标或无效意愿绕过边界。
7. open/side 的通过结果进入按群维护的 in-memory participation throttle；direct/private 不消耗这个预算。
8. 通过后才调用 ReplyHandler 生成正式回复；普通 direct 私聊没有 DecisionAI 结果也可直接调用 ReplyHandler。正式回复使用 AstrBot 当前会话最终 Persona，只收到最小的参与 handoff，不收到决策模型的推理过程。
9. 未通过的当前消息和 Smart follower 可以保留为 observation-only 缓存，但 observation 不会成为下一次的“未回复问题”、不会制造 continuation、也不会提供图片候选。
10. 正式回复经过现有内容过滤、重复检测、保存和发送流程；该部分仍由 AstrBot/插件既有链路负责。

### 3.1 group_reply_scope

- ambient：普通群消息可以进入统一参与判断；未明确指向机器人的明显纯媒体、低信息反应和只等待其他人的无正文消息可在模型前快速过滤。被 @、点名、关键词或可靠回复信号明确指向的纯媒体仍可继续图片处理和参与判断。明确指向机器人的有效消息按正常回应机会评估；开放话题有具体个人补充或自然反应时也可以参与。
- addressed：未明确指向机器人的群消息在决策前静默。明确指向包括平台 @/戳/回复信号、文本中可靠的机器人称呼和触发关键词；这些信号只让消息进入候选，仍不保证模型返回 yes。


takeover_group_reply=true 时，插件会阻止被判定为静默或被插件前置过滤的消息落入 AstrBot 默认兜底；DecisionAI 或插件处理出错也静默，避免服务故障反而导致全量回复。关闭接管时，这些结果交还核心链路，这是显式兼容选择。

at_all_message_mode=skip_all、欢迎消息 skip_all 等现有显式强制配置是管理者覆盖，不代表普通 @ 行为。除这些明确的 force 分支外，@、关键词和点名都不能绕过参与决策。

## 4. DecisionAI 契约

DecisionAI 的 system prompt 是判断协议，不是正式回复提示。群聊版本要求一个 JSON 对象，字段为：

- reply：yes 或 no。
- target：bot、other、open 或 unclear。
- information：noise、reaction 或 substantive。
- continuation：yes 或 no。
- participation：direct、side、open 或 none。
- interest：strong、weak 或 none。
- reason_code：direct_request、shared_interest、personal_experience、emotional_reaction、continuation 或 none。
- confidence：high、medium 或 low。
- topic_key：长度受限的诊断标签，不用于生成回复目标。

normalize_decision_payload 是最终边界：

- target、participation、information、interest、reason_code、confidence 必须属于已知枚举。
- 未知枚举、unclear 或 none participation 必须静默。
- continuation 的主题关系只是模型分类，不作为本地兴趣门槛；群聊 yes 仍需通过当前发送者关系的结构化事实校验，是否被短插话打断继续由 Persona 结合上下文判断。
- target=other 时只能采用 side 说话姿态；是否确有独立公共补充由 Persona 判断，代码不再替它做主观内容裁决。
- target、participation 或输出结构不可靠时必须静默；subjective interest、information 和 reason_code 本身不再由本地代码强制拦截。
- reply=no 不能被后续代码重新解释为“可以回答”。

旧 provider 仍可能只返回 yes/no。纯旧格式继续被兼容为一个受限的 direct/open 决策；新代码不得把这个兼容层扩展成新的旁路。看起来像 JSON 但无法解析的响应按失败处理，不用宽松的 yes/no 前缀猜测。

### 4.1 发送者与上下文信任

当前消息发送者永远来自 event 元数据。历史消息、长期记忆、未回复缓存和 Smart follower 只能帮助理解，不能替当前消息指定对象，也不能把别人的话归给当前发送者。

平台没有检测到 @ 只表示平台 signal 缺失，不表示文本一定没有点名机器人；模型可以根据当前正文和群聊上下文判断文本目标，但不能仅凭较早历史的主题相似臆造当前目标。

## 5. 正式回复 handoff

DecisionAI 只把以下最小信息交给 ReplyHandler：允许的 participation 姿态和一个固定 reason_code 对应的短提示。handoff 用于提醒正式模型保持正确对象边界，不是新的行为人格。

正式模型不会收到：

- DecisionAI 的完整 JSON 诊断；
- 隐藏推理或 chain-of-thought；
- “模型应该怎样分析”的过程指令；
- 关键词命中本身作为回复理由。

正式模型会收到当前会话 Persona、格式化的消息上下文、现有 Smart/媒体必要提示，以及必要时的最小参与 handoff。side 和 open 的 handoff 明确要求只说自己的相关内容，不要替其他用户作答。

## 6. 缓存与 Smart 规则

观察缓存的用途是保留事件痕迹和避免消息完全消失，不是堆积“机器人欠下的回答”。active 缓存读取接口会排除 decision_state=observed；正式群聊回复可以在 TTL 内以低优先级背景读取最近观察消息：

- 不进入 active regular context；
- 不进入 window continuation context；
- 不触发下一条消息的自动续话；
- 不参与 lazy 图片候选合并；
- 不会因为作为背景被读取而自动触发正式回复。

正式回复读取的观察背景带有“此前未参与的消息”标记，只有当前消息已经通过 DecisionAI 时才加入，避免观察缓存反过来改变参与判断。

Smart anchor 被判定静默时，anchor 和 follower 都转为 observation-only。Smart anchor 通过时，follower 作为背景保留原发送者、顺序和媒体信息，不改变主要回复对象。

## 7. 开放参与节流

节流只作用于没有明确指向机器人的 open 和 side 通过结果：

- ambient_reply_min_interval_seconds 默认 45 秒；
- ambient_reply_window_seconds 默认 600 秒；
- ambient_reply_max_per_window 默认 4 次；
- direct 和 private 回复不消耗该预算；
- 每个群独立计数，插件重载时清空内存状态；
- 任意值设为 0 可关闭对应限制。

节流是第二道社交保护，不替代 DecisionAI 的兴趣判断。它不能用来提高 open 消息的通过率，也不能让 direct 消息变成 open 消息。

## 8. 失败策略

| 场景 | 行为 |
| --- | --- |
| provider 超时或异常 | 返回 error/silent；接管群聊时静默，非接管时交回核心链路 |
| JSON 不完整或枚举未知 | 本地校验失败，静默 |
| 旧版纯 yes/no | 受限兼容；不扩展到结构化旁观参与 |
| 正式回复生成失败 | 复用现有 AstrBot 错误、保存和事件清理流程 |
| Smart follower 被更早 anchor 吸收 | 不独立生成回复，按现有 Smart 状态清理 |
| 观察缓存过期 | 丢弃，不制造新任务 |

失败和静默都必须避免把未决策消息伪装成机器人回复或把它变成下一轮的直接目标。

## 9. 保留的既有能力

本次参与重构不应删除以下能力：

- AstrBot 官方历史、会话人格解析和 reset 指令；
- lazy/eager 图片处理、图片描述缓存、转发解析和媒体清理；
- 黑名单、指令过滤、重复过滤、内容过滤和戳一戳；
- Smart anchor/follower 到达顺序与批次上下文；
- livingmemory 可选注入；
- ReplyHandler 的 provider request、工具集和第三方 Hook 兼容边界；
- 插件页配置接口与既有配置迁移。

ProbabilityManager 仍承担会话 key、生命周期和兼容 reset/status 接口，但旧的 initial/after-reply 概率值不再是群聊回复闸门。统一参与判断是当前群聊的唯一回复选择层。

## 10. 配置与调参

推荐起点：

- takeover_group_reply=true；
- group_reply_scope=ambient；
- decision_ai_include_persona=true；
- decision_ai_reply_tendency=persona；
- keyword_smart_mode 保留旧配置即可，关键词不会绕过判断；
- 节流使用 45/600/4 默认值。

如果仍然太吵，先提高 DecisionAI Persona 的边界或降低节流上限；不要通过增加关键词把所有消息送进必回路径。如果太安静，先确认 DecisionAI 是否拿到了当前 Persona、当前发送者和真实正文，再调整 active 倾向或缩短节流间隔；不要删除消息对象、说话姿态和 open/side 频率保护。

## 11. 验收清单

行为测试至少覆盖：

- 直接相关 @ 消息可以通过；直接无聊、重复、讨厌或已经结束的 @ 消息必须可以拒绝；
- 无 @ 的强 Persona 相关公开话题可以通过；普通可回答问题保持安静；
- @ 或回复其他用户的物流/只等待对方消息保持安静；有强个人经历的公共补充可以通过；
- noise、reaction、ambiguous 和未知枚举静默；
- malformed JSON 静默，旧 yes/no 仍兼容；
- handoff 不泄露推理；观察缓存不回流 active context；
- open/side 节流验证 interval、window、cap，direct bypass；
- Smart anchor/follower 不改变主要对象；
- provider timeout/error 在接管模式下 fail closed。

代码验证：

    /home/ubuntu/AstrBot/.venv/bin/ruff format .
    /home/ubuntu/AstrBot/.venv/bin/ruff check .
    /home/ubuntu/AstrBot/.venv/bin/python -m compileall -q .
    /home/ubuntu/AstrBot/.venv/bin/pytest -q
    git diff --check

维护者在修改参与规则时，应同时更新 utils/participation.py、DecisionAI 协议、主流程缓存语义、配置参考和本文件，并补一个针对行为变化的回归测试。
