# Persona Presence 架构指南

> 本文描述当前 Persona Presence 实现。它取代了旧版关于注意力、主动对话、概率回复、群聊等待窗口和“@必回”的说明。

## 1. 一句话概括

插件负责把一条群聊或私聊事件整理成“是否值得由当前 Persona 开口”的决定，再把通过结果交给 AstrBot 正式回复链路。群聊通常经过参与判断；普通 direct 私聊绕过参与判断，private Smart 下的纯文本还可以先生成一份不执行工具、非流式缓冲文本，decide、私聊媒体和活动收尾边界才进入私聊专用门控，并由睡眠/回避状态和唤醒调度继续约束。插件不替换 Persona，也不把决策模型的完整分析过程塞进正式回复。

## 2. 组件边界

| 组件 | 责任 | 不负责 |
| --- | --- | --- |
| main.py / PersonaPresence | 事件生命周期、硬过滤、媒体整理、Smart 协调、决策与发送编排 | 代替 Persona 写回复正文 |
| utils/decision_ai.py | 构造参与判断请求、读取发送者和目标信号、解析模型输出 | 决定最终回复措辞 |
| utils/participation.py | 校验结构化决策、执行参与硬规则、生成最小 handoff、限制 open/side 频率 | 调用模型或保存聊天历史 |
| utils/reply_handler.py | 解析当前 Persona、组织正式上下文、创建 AstrBot provider request | 决定消息是否值得参与 |
| utils/message_cache_manager.py | 管理待处理缓存和 observation 过滤 | 把所有缓存当成待回答问题 |
| SmartConcurrentManager | 按到达顺序选 anchor、吸收 follower、维护批次生命周期 | 改写 follower 的发送者或主要对象 |
| ContextManager / SaveMixin | 格式化上下文、官方历史保存和缓存转正 | 生成参与意愿 |
| ProbabilityManager | 保留会话 key、reset/status 和旧接口兼容 | 当前群聊回复概率闸门 |

## 3. 消息处理流程

platform event
    |
    +-- duplicate / blacklist / command / welcome / @ filters
    |
    +-- target signals: bot, other, open, unclear
    |
    +-- media, forward, sticker and image handling
    |
    +-- Smart anchor/follower coordination
    |
    +-- DecisionAI.evaluate()
    |       |
    |       +-- structured JSON or legacy yes/no
    |       +-- normalize_decision_payload()
    |       +-- ParticipationDecision
    |
    +-- open/side throttle
    |
    +-- ReplyHandler.generate_reply()
    |       |
    |       +-- current session Persona
    |       +-- formatted context
    |       +-- minimal participation handoff
    |
    +-- AstrBot provider / output filters / history save

事件进入模型前的过滤只处理确定的系统边界和明显无信息内容。它不能把“消息可回答”变成“消息值得回复”。

### 3.1 ambient 和 addressed

ambient 是默认模式。普通群消息可以进入 DecisionAI，但纯媒体、低信息反应、只等待其他用户回答的无正文消息可以提前静默。明确指向机器人的有效消息按正常回应机会评估；开放话题在当前 Persona 有具体个人补充或自然反应时也可以参与，既可以展开，也可以只低打扰地补充一句。

addressed 只让明确指向机器人或被配置为触发信号的消息进入候选。这里的明确指向包括平台 @/戳/回复信号、当前正文可靠点名和关键词。候选仍会经过 DecisionAI；@ 和关键词不是 bypass。

明确配置 at_all_message_mode=skip_all 或欢迎消息 skip_all 的 force 分支是管理员覆盖。它们不能被解释为普通 @ 的默认语义。

## 4. 目标与参与判断

DecisionAI 将当前消息分成两个互补维度。

### 4.1 事实分类

- target：bot、other、open、unclear。
- information：noise、reaction、substantive。
- continuation：由 DecisionAI 结合近期机器人回复和中间群聊内容判断；短暂的无关插话不自动结束续话，较早历史仅仅主题相似也不足以成为续话依据。
- participation：direct、side、open、none。

历史中的旧消息、未回复标记、长期记忆和 Smart follower 只能帮助解释当前文本，不能单独成为当前消息的发送者或目标。是否续话的语义判断由模型结合当前消息完成；代码只校验 continuation=yes 所声称的发送者关系，不把消息是否严格相邻作为本地硬规则。

### 4.2 Persona 意愿

- strong：Persona 对具体内容有明显兴趣、情绪、经历或观点，并且现在就想展开说自己的相关内容。
- weak：Persona 没有强烈冲动，但可能只想轻轻补充一句；它与 strong 都是意愿描述，不是本地回复门槛。
- none：无聊、重复、冒犯、打扰、疲惫、已经说完或没有自然入口。

硬策略如下：

| 组合 | 结果 |
| --- | --- |
| 未知枚举、unclear 或 none participation | no |
| target 与 participation 说话姿态不一致 | no |
| target=other 但 participation 不是 side | no |
| continuation | 描述上下文关系；群聊 yes 还需通过发送者事实校验，不替 Persona 做主题判断 |
| direct + Persona 明确不愿意回复 | no |
| side + 人格有自己的相关补充 | 可 yes |
| open + Persona 自然想参与 | 可 yes |

“可 yes”表示允许进入正式回复，不表示模型必须回复。模型仍需结合人格心情、关系、氛围和重复程度作最后意愿判断。

## 5. DecisionAI 数据契约

群聊 DecisionAI 要求返回单个 JSON 对象：

{
  "reply": "yes|no",
  "target": "bot|other|open|unclear",
  "information": "noise|reaction|substantive",
  "continuation": "yes|no",
  "participation": "direct|side|open|none",
  "interest": "strong|weak|none",
  "reason_code": "direct_request|shared_interest|personal_experience|emotional_reaction|continuation|none",
  "confidence": "high|medium|low",
  "topic_key": "short diagnostic label"
}

utils/participation.py 是不可信模型输出和业务流程之间的边界。它会收敛布尔值、裁剪 topic_key、检查枚举，并执行消息对象和说话姿态规则。continuation 的主题关系仍由 Persona 判断；main.py 只对群聊 continuation=yes 做结构化发送者事实校验，不把 interest 等主观字段变成本地兴趣门槛。

旧 provider 的精确 yes/no 仍兼容。它只能形成受限的旧式 direct/open 决定，不得用来制造 side 参与。看起来像 JSON 但解析失败的内容必须按失败静默，不能用宽松的自然语言前缀猜结果。

DecisionAI 的 timeout、provider exception、空输出和结构化解析失败会返回 source=error 的静默决策，并在 event 上保留内部错误标记。群聊接管开启时 main.py fail closed；接管关闭时交还 AstrBot 核心处理。

## 6. 正式回复链路

ReplyHandler.generate_reply 的 system_prompt 来源仍是 AstrBot 当前会话最终 Persona，优先级为会话强制人格、会话选择人格、默认人格。正式回复不会复用 DecisionAI 的 system prompt。

允许参与时，main.py 传给 ReplyHandler 的 reply_context_hint 只包含：

- direct、side 或 open 的最小姿态；
- 一个有限 reason_code 的自然语言提示；
- side/open 时不要替其他用户作答的边界。

不传递：

- 完整 JSON 诊断；
- 隐藏推理、chain-of-thought 或推理标记块；
- 模型“应该如何分析”的过程；
- 关键词或 @ 作为必答理由。

DecisionAI 的 reasoning 开关只影响决策请求的解析和可选日志。正式回复模型永远不应看到决策推理块。

## 7. Smart 批处理

Smart 使用消息到达序号选择 anchor，而不是依赖某个异步任务先运行。anchor 的当前消息是主要回复对象；follower 保留发送者、发送者 ID、到达顺序、媒体和内容。direct 私聊也可以使用 Smart；它绕过普通私聊 DecisionAI，但仍把短时间连续消息合并为一条正式回复。decide 纯文本私聊 anchor 可以在短窗口内并行进行参与预判；direct 纯文本 anchor 可以并行生成不执行工具、非流式缓冲文本；模型若请求工具则放弃草稿，改走完整 Agent。Smart 提交时分别复用已完成的对应结果；仍在生成的 direct 草稿会取消并走正常 Agent，出现 follower 时作废预判或草稿，并只使用合并批次的权威结果。

anchor 和 follower 都会进入 DecisionAI 的上下文，因此 DecisionAI 可以知道批次背景，但不应把 follower 的话归给 anchor 发送者。正式回复可以参考 follower，但默认只围绕 anchor 生成一条回复。

如果 anchor 被判定为静默：

- anchor 缓存为 decision_state=observed；
- follower 也转为 decision_state=observed；
- 不生成正式回复；
- 不让这些消息成为下次的未回复任务。

如果 follower 被更早的 anchor 吸收，它不会独立调用正式回复。吸收失败或超时仍复用现有 Smart 清理和并发保护。

## 8. Observation-only 缓存

消息被模型看到但不回复，不等于机器人欠下一个回答。main.py 保存这类消息时添加 decision_state=observed。

MessageCacheManager 的 active 读取接口会排除 observed：

- get_cached_messages；
- get_regular_cached_messages；
- get_window_buffered_messages；
- lazy 图片候选合并。

这阻止三种旧式回流：下一条消息被误判为 continuation、历史缓存制造“当前对象”、上一条被拒绝图片再次触发视觉处理。观察记录仍可以在当前缓存 TTL 内存在，便于诊断和正常生命周期清理。群聊参与判断读取 AstrBot 官方 GroupChatContext 的当前缓冲；正式回复仍由 AstrBot 的官方 LLM hook 注入同一缓冲。

## 9. 节流设计

ParticipationThrottle 是本地、按群的 in-memory 保护，只处理 DecisionAI 已经允许的 open/side 回复尝试。

默认值：

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| ambient_reply_min_interval_seconds | 45 | 同一群两次 open/side 尝试的最小间隔 |
| ambient_reply_window_seconds | 600 | 统计窗口 |
| ambient_reply_max_per_window | 4 | 窗口内最多主动参与次数 |

direct 和 private 不消耗预算。预算在正式生成前记录，用于防止多个并发请求同时通过；插件重载后清空。把单项设为 0 可以关闭该项限制，但不会关闭消息对象、说话姿态和格式校验。

## 10. 其他保留边界

- 黑名单和指令过滤仍在 DecisionAI 前执行。
- 图片描述缓存、lazy/eager 模式和私聊纯媒体策略保持原有路径。
- 官方历史仍是保存和会话 reset 的权威链路；群聊上下文只使用带真实发送者和时间元数据的历史来源，通用 role 历史只保留给私聊。
- continuation 由 Persona 判断，但群聊的 continuation=yes 还必须通过当前发送者与最近机器人回复对应发送者的结构化事实校验；无法验证的非直接消息不会进入正式回复。
- livingmemory 只作为可选上下文注入，不可以替当前消息指定目标。
- 正式回复的重复过滤、内容过滤、工具和第三方 Hook 兼容逻辑继续由现有 ReplyHandler/main.py 管理。
- 关键词配置保留用于注意力入口和兼容旧配置；keyword_smart_mode 不再代表“关闭后必回”。
- ProbabilityManager 的旧状态接口保留用于命令和生命周期兼容，但 initial_probability 等旧值不再决定当前消息是否进入回复。

## 11. 维护规则

修改参与策略时必须同步检查：

1. DecisionAI 的 system contract 与 parser。
2. participation.py 的决策边界和 main.py 的历史事实边界。
3. main.py 的 force 分支、throttle 和 observation 缓存。
4. ReplyHandler handoff 是否仍然最小且无推理泄露。
5. Smart anchor/follower 的主对象不变量。
6. README、CONFIG_REFERENCE.md 和 REFACTOR_DESIGN.md 的行为描述。
7. direct、open、side、other、noise、malformed JSON 和 provider failure 的回归测试。

推荐验证命令：

    /home/ubuntu/AstrBot/.venv/bin/ruff format .
    /home/ubuntu/AstrBot/.venv/bin/ruff check .
    /home/ubuntu/AstrBot/.venv/bin/python -m compileall -q .
    /home/ubuntu/AstrBot/.venv/bin/pytest -q
    git diff --check
