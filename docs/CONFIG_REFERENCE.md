# Persona Presence 配置参考

本文说明当前 Persona Presence（人格自主参与）的配置契约。配置页由 _conf_schema.json 驱动；本文重点解释会影响群聊参与边界、人格意愿、缓存和失败策略的选项。

- 插件 ID：astrbot_plugin_persona_presence
- 展示名：人格自主参与 / Persona Presence
- 仓库：https://github.com/Sihnbaobao/astrbot_plugin_persona_presence
- 当前版本：1.1.2

## 从旧版本迁移

这是一次插件 ID 迁移，不是同目录内的小版本更新。停掉 AstrBot 后：

1. 将 data/plugins/astrbot_plugin_chat_plus_lite 改名为 data/plugins/astrbot_plugin_persona_presence，或重新安装新仓库。
2. 将 data/config/astrbot_plugin_chat_plus_lite_config.json 改名为 data/config/astrbot_plugin_persona_presence_config.json。
3. 如果存在 data/plugin_data/astrbot_plugin_chat_plus_lite，也将它改名为 data/plugin_data/astrbot_plugin_persona_presence。
4. 删除或禁用旧插件目录，避免两个插件同时处理同一条消息。
5. 启动后确认插件页显示“人格自主参与”，再检查群聊和私聊开关。

## 核心行为

### 群聊边界

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| enable_group_chat | bool | true | 群聊总开关。 |
| enabled_groups | list | [] | 留空处理所有群；填写群号后只处理指定群。 |
| takeover_group_reply | bool | true | 插件已接管群消息时，静默、参与判断失败或处理失败是否阻止 AstrBot 默认兜底；true 保持静默，false 将结果交回 AstrBot 核心链路。 |
| group_reply_scope | string | ambient | ambient 让普通群消息进入参与判断；明确指向机器人的有效消息按正常回应机会评估，开放话题有具体个人补充时也可以参与。addressed 只让 @、戳、回复机器人、可靠文本点名或关键词消息进入候选。两种模式下 @ 和关键词都不保证回复。 |

群聊的基本规则：

- direct：消息面向机器人，但人格仍可因为无聊、重复、冒犯、打扰或话题结束而拒绝。
- side：消息面向其他人时，只有人格自己的独立、相关补充才允许参与，不能替对方回答或接管话题。
- open：无明确对象的公共话题不是默认 no；如果 Persona 有具体的个人经历、观点、情绪反应或自然补充，可以展开，也可以只低打扰地插一句。
- noise、reaction、unclear 和只等待其他人回答的消息通常静默。

### 私聊

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| enable_private_chat | bool | false | 是否启用私聊增强。 |
| enabled_private_users | list | [] | 留空处理所有私聊；填写用户 ID 后只处理指定用户。 |
| private_reply_mode | string | direct | direct 白天普通私聊绕过私聊参与判断，直接进入正式回复流程；本地时间 01:00-07:00 的未明确指向普通私聊会进入一次概率性深夜复核，时间只作背景，由当前 Persona 结合性格、心情、关系、边界和消息内容权衡，不单独决定 yes/no；实际回复成功后约 45 分钟视为已醒并恢复自然对话。decide 让普通私聊也逐条经过私聊参与判断，可能增加延迟并返回 no。两种模式都会尊重收尾边界。 |
| takeover_private_reply | bool | true | 私聊静默、边界拒绝或正式处理失败时是否阻止 AstrBot 默认回复；普通私聊参与判断超时/异常且无活动边界时会 fail-open 进入正式回复，false 将其他未接管结果交回核心链路。 |
| provider_settings.datetime_system_prompt | bool | true | AstrBot 全局现实世界时间感知。私聊 DecisionAI 跟随此设置；私聊中时间只作背景，由当前 Persona 结合性格、心情、关系、边界和消息内容权衡；深夜/清晨本身不单独构成 yes 或 no。 |
| private_decision_ai_extra_prompt | text | 空 | 私聊专用的参与判断补充提示词；留空时沿用通用 decision_ai_extra_prompt，不会注入正式回复链路。 |
| private_decision_ai_prompt_mode | string | append | append 保留内置私聊规则并追加自定义内容；override 完全替换内置私聊提示词，必须自行保留结构化 JSON 输出契约。 |
| private_decision_ai_reply_tendency | string | persona | 只影响私聊参与判断：persona 按当前人格，reserved 更克制，active 更愿意接住有内容的私聊；不会改写正式回复人格。 |

私聊不受群聊 open/side 预算限制。私聊协议使用结构化 boundary_interpretation：literal_sleep 按真实睡眠处理，conversational_exit 只用于已有睡眠假设被判断为聊天收尾，ambiguous 结合前后文继续判断。纯图片和纯表情先按本地媒体策略处理：图片还必须先通过 enable_image_processing/image_to_text_scope 的前置过滤；保留下来的媒体中，ignore 直接丢弃且不进入边界复核，always 在没有活动收尾边界时跳过参与判断，有活动边界时仍按边界复核，decide 交给 DecisionAI；含文字的图文消息仍按普通文本处理。插件页的“提示词预览”会同时显示群聊参与判断、私聊参与判断和回复生成三份提示词；私聊卡片可以直接编辑 private_decision_ai_extra_prompt。私聊卡片显示“沿用通用提示词”时，实际仍使用通用补充提示词，只有填写私聊专用内容后才会覆盖该回退。睡觉类收尾使用有范围的睡眠假设（普通睡眠约 4-10 小时，短睡约 30 分钟至 3 小时），并在范围内随机安排一次唤醒检查；如果存在更晚截止点，还会追加接近范围结束的最终复核。睡眠期间有消息时，Persona 会在这些时点重新判断是否主动回复，没有消息时不会凭空发言。拒绝类话语不设置固定自动解禁时间，也不直接把后续消息判成 no；它只触发一次私聊语义复核。模型会读取上一条 Persona 原话和当前消息，判断对方是否真的在施压、纠缠或打扰；如果当前消息礼貌、有用、道歉或自然换题，可以 reply=yes；只有正式回复成功送达后才清除软提示，生成或发送失败时仍继续保留，reply=no 也继续保留。私聊时间感知不再单独配置，直接跟随 AstrBot 全局 provider_settings.datetime_system_prompt 和 timezone。消息上下文的 include_timestamp 只控制每条历史消息是否显示发送时间，不会开启或关闭全局现实时间注入；反之亦然。

## Persona 参与判断

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| decision_ai_provider_id | string | 空 | 参与判断使用的 provider；留空跟随会话默认模型。 |
| decision_ai_include_persona | bool | true | 将当前 Persona 注入参与判断，让人格的兴趣、关系、心情和边界成为决策主体。 |
| decision_ai_persona_name | string | 空 | 留空跟随当前会话 Persona；填写后固定使用指定人格判断。 |
| decision_ai_extra_prompt | text | 空 | 追加参与判断要求。不得写成“命中就必回”来绕过本地硬边界。 |
| decision_ai_timeout | int | 30 | 群聊参与判断超时会静默；普通私聊在无活动睡眠/回避边界且 takeover_private_reply=true 时会回退到正式回复链。 |
| decision_ai_reply_tendency | string | persona | persona 完全依据人格；reserved 更克制；active 更愿意参与有内容的公共话题。三种倾向都不能绕过消息对象、说话姿态和主动参与预算。 |

群聊参与判断输出一个结构化 JSON，包含 reply、target、information、continuation、participation、interest、reason_code、confidence 和 topic_key。代码会校验枚举并再次执行消息对象和说话姿态边界；continuation 的语义由 DecisionAI 根据近期对话和中间群聊内容判断，群聊 continuation=yes 还必须通过当前发送者关系的结构化事实校验。旧 provider 返回精确 yes/no 仍可兼容，但不能借此获得 side 旁观权限。

### 结构化参与字段

| 字段 | 可选值 | 作用 |
| --- | --- | --- |
| reply | yes / no | 模型是否建议进入正式回复。 |
| target | bot / other / open / unclear | 当前消息的语义对象。 |
| information | noise / reaction / substantive | 当前消息的信息量。 |
| continuation | yes / no | DecisionAI 对当前消息是否承接近期机器人对话的判断；会结合中间群聊内容，不把短暂无关插话自动视为话题结束。 |
| participation | direct / side / open / none | 允许采用的说话姿态。 |
| interest | strong / weak / none | Persona 对当前内容的具体参与意愿。 |
| reason_code | direct_request / shared_interest / personal_experience / emotional_reaction / continuation / none | 交给正式回复的最小理由类别。 |
| confidence | high / medium / low | 分类置信度，仅用于决策语义和诊断。 |
| topic_key | 短文本 | 有界诊断标签，不用于生成回复目标。 |

strong 和 weak 只描述这次人格意愿的力度：strong 可以展开，weak 可能只想轻轻说一句，none 则表示不想参与。它们用于帮助 DecisionAI 表达整体判断，不再被 participation.py 作为主观强度门槛；真正能回答但人格不想说时仍应输出 reply=no。

### 可选推理协议

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| enable_decision_ai_reasoning | bool | false | 允许参与判断模型使用指定标记输出分析块；最终仍需输出 JSON。分析块只用于决策解析，不会传给正式回复模型。 |
| decision_ai_reasoning_log | bool | false | 是否把解析后的分析内容写入日志。 |
| decision_ai_reasoning_log_mode | string | processed | processed 记录处理后的内容；raw 记录原始响应。 |
| judgment_reasoning_start_marker | string | [[GCP_REASONING_START]] | 分析块起始标记。 |
| judgment_reasoning_end_marker | string | [[GCP_REASONING_END]] | 分析块结束标记。 |

解析失败、JSON 不完整或未知枚举都按静默处理。不要依赖隐藏推理来实现业务规则；结构化决策边界在 participation.py 中保持可测试，依赖 event/history 事实的边界在 main.py 中保持可测试。

## 开放发言预算

这三个选项只限制没有明确指向机器人的 open/side 回复，不限制 direct，也不限制私聊：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| ambient_reply_min_interval_seconds | float | 45.0 | 同一群两次开放参与尝试的最小间隔；0 关闭间隔限制。 |
| ambient_reply_window_seconds | float | 600.0 | 统计窗口；0 不保留窗口记录。 |
| ambient_reply_max_per_window | int | 4 | 单个群在窗口内最多开放参与次数；0 关闭上限。 |

预算按群保存在内存中，插件重载后清空。预算在正式生成前预留，用于防止并发消息同时通过；它不是随机回复概率，也不能让低兴趣消息通过。

## 触发与过滤

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| trigger_keywords | list | [] | 命中后提高注意力并进入统一参与判断，不直接保证回复。 |
| keyword_smart_mode | bool | true | 兼容旧配置。现在无论开关状态都不会让关键词绕过参与判断。 |
| blacklist_keywords | list | [] | 命中后直接忽略消息。 |
| enable_user_blacklist | bool | false | 是否启用用户黑名单。 |
| blacklist_user_ids | list | [] | 被忽略的用户 ID。 |
| enable_command_filter | bool | true | 是否忽略命令消息。 |
| command_prefixes | list | [/,!,#] | 命令前缀列表。 |
| enable_duplicate_filter | bool | true | 避免重复事件和重复表达。 |
| enable_emoji_filter | bool | false | 识别 QQ 表情并按媒体策略处理。 |

关键词、@、戳和结构化回复都是注意力或目标事实，不是人格意愿。要改变开口倾向，应调整 Persona 或 decision_ai_reply_tendency，不要把关键词列表当作回复白名单。

## 上下文、回复与历史

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| reply_ai_extra_prompt | text | 空 | 只追加到正式回复生成，不参与决策。 |
| include_timestamp | bool | true | 在上下文中标注消息时间。 |
| include_sender_info | bool | true | 在群聊上下文中标注发送者和 ID。 |
| max_context_messages | int | -1 | 历史上限；-1 不限，0 不获取。 |
| collapse_reply_newlines | bool | false | 是否合并普通纯文本回复中的主动换行。 |

正式回复使用 AstrBot 当前会话 Persona。参与判断的 JSON 和分析过程不会进入正式回复 prompt；只会传递 direct/side/open 姿态和有限 reason_code 的短 handoff。Persona Presence 不会把全部 active Skills 清单无条件追加到每条人格回复的 system prompt，但会继续合并当前请求的工具集。

被拒绝的消息可以写入 observation-only 缓存。它们不会作为 active 未回复上下文、续话依据或 lazy 图片候选。

## Smart 并发

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| concurrent_mode | string | legacy | 群聊 legacy 逐条处理；smart 按到达顺序合并短时批次。普通 direct 私聊不使用群聊并发配置。 |
| private_concurrent_mode | string | smart | 私聊批次独立控制；direct 和 decide 都可合并短连发，direct 仍绕过普通私聊 DecisionAI。 |
| private_batch_wait_ms | int | 4500 | 私聊 Smart 短连发合并窗口；decide 纯文本会并行预判，无 follower 时复用结果，有 follower 时作废预判并合并重判；direct 纯文本会并行生成不执行工具、非流式缓冲文本；Smart 提交时仅复用已完成草稿，仍在生成的草稿会取消并走正常 Agent，有 follower 时作废并对合并消息重生成；媒体和边界路径不使用此预生成。 |
| private_batch_max_size | int | 10 | 私聊 Smart 单批最大消息数。 |
| enable_smart_batch_reply_hint | bool | true | Smart 批次进入正式回复时，是否添加“追加消息仅作背景/合并理解”的最小提示。 |
| smart_concurrent_merge_wait | float | 30.0 | Smart 批次合并的时间上限。 |
| smart_concurrent_max_batch_size | int | 20 | Smart 批次最大消息数。 |
| smart_concurrent_claim_delay | float | 0.3 | anchor 快照前收拢几乎同时到达消息的延迟。 |

anchor 始终是主要回复对象。follower 不会覆盖发送者、不独立制造目标；anchor 静默时，整批转为 observation-only。

## 图片、记忆与戳一戳

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| enable_image_processing | bool | false | 是否处理图片。 |
| image_read_mode | string | lazy | lazy 在参与判断或正式回复接受前暂不读取图片，之后再筛选/读取相关图片；eager 在前置处理阶段读取。direct 私聊没有参与判断时，lazy 仍会在正式回复前处理相关图片。 |
| image_to_text_scope | string | all | 图片转文字范围。 |
| image_to_text_provider_id | string | 空 | 图片转文字 provider；留空按现有回复模型路径处理。 |
| max_images_per_message | int | 10 | 单条消息最多处理的图片数。 |
| private_image_mode | string | decide | 仅对图片处理器保留的纯图片生效；enable_image_processing=false 或 image_to_text_scope 不匹配时会在此配置之前被过滤。保留后：ignore 直接丢弃且不进入边界复核，decide 进入参与判断，always 在没有活动收尾边界时直接进入正式回复；有活动边界时 always 仍按边界复核。takeover_private_reply=false 时，ignore 或前置过滤结果可能交回核心链路。 |
| private_emoji_mode | string | ignore | 私聊纯表情：ignore 直接丢弃且不进入边界复核，decide 进入参与判断，always 在没有活动收尾边界时直接进入正式回复；有活动边界时 always 仍按边界复核。takeover_private_reply=false 时，ignore 或前置过滤结果可能交回核心链路。 |
| private_collapse_duplicate_emoji | bool | true | 是否在短时间或同一 Smart 批次内折叠重复表情；关闭后仍可按 private_emoji_mode 识别和处理表情。 |
| private_duplicate_emoji_window_ms | int | 1500 | 重复表情折叠窗口。 |
| enable_memory_injection | bool | false | 是否从 livingmemory 获取辅助上下文。 |
| memory_plugin_mode | string | auto | 记忆插件检测模式。 |
| livingmemory_top_k | int | 5 | 召回记忆条数。 |
| poke_message_mode | string | bot_only | ignore、bot_only 或 all。 |
| enable_poke_after_reply | bool | false | 回复后是否尝试戳用户。 |
| poke_after_reply_probability | float | 0.15 | 回复后戳一戳的概率。 |

媒体和记忆只能补充理解，不能替当前消息指定发送者或对象。纯图片的 always 只对通过图片前置处理的媒体生效，并且只在没有活动边界时绕过当前参与判断；普通文本和活动收尾边界仍遵循参与边界，ignore 则在边界复核前直接丢弃。

## 旧配置说明

以下历史配置仍可能出现在旧配置文件或迁移代码中，但不再是当前群聊回复闸门：

- initial_probability、after_reply_probability、probability_duration；
- 概率上下限和旧式回复后 boost；
- attention、fatigue、mood、frequency、density、proactive 等已经删除的运行时状态；
- 旧的 keyword_smart_mode“关闭即必回”语义。

ProbabilityManager 保留 reset/status 和会话 key 兼容接口。修改这些旧值不会让普通消息绕过统一参与判断。

## 排查顺序

1. 确认只启用了 astrbot_plugin_persona_presence，没有旧 ID 插件并行运行。
2. 检查 enable_group_chat、enabled_groups、takeover_group_reply 和 group_reply_scope。
3. 查看参与判断是否拿到了当前 Persona、当前发送者和真实正文。
4. 用事件日志确认 target、participation、interest 和静默原因，不要只看是否命中关键词。
5. 如果 open/side 通过后仍不发言，检查 45/600/4 预算是否生效。
6. 如果模型异常，接管模式下预期是静默；先修 provider 或 timeout，不要关闭 fail-closed 边界。

相关文档：

- REFACTOR_DESIGN.md：行为契约、失败策略和维护规则。
- ARCHITECTURE.md：组件边界和运行时数据流。
- MESSAGE_WORKFLOW.md：按事件阶段展开的流程说明。
- _conf_schema.json：插件页真实字段定义。
