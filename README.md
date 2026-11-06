# Persona Presence

Persona Presence 是面向 AstrBot 的群聊与私聊消息增强插件。它让当前 Persona 基于兴趣、关系、上下文和当下意愿选择是否参与；媒体整理、Smart 批处理和正式回复仍沿用 AstrBot 的正常链路。

[![Version](https://img.shields.io/badge/version-1.7.4-blue.svg)](https://github.com/Sihnbaobao/astrbot_plugin_persona_presence)
[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.11.0-green.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Plugin Pages](https://img.shields.io/badge/Plugin%20Pages-v4.25.3%2B-purple.svg)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-AGPL--3.0-orange.svg)](LICENSE)

> 当前版本：1.7.4。插件标识已更新为 astrbot_plugin_persona_presence；插件元数据兼容 AstrBot >= 4.11.0，插件页管理控制台建议使用支持 Plugin Pages 的 AstrBot 版本（4.25.3+）。

## 功能概览

- 群聊参与判断：当前 Persona 决定何时开口。被明确提问且能够回答，通常是更强的回应机会；面向群体的问题也可自然接话，不强求亲身经历。是否回应仍取决于 Persona 的性格、关系、边界和当下话轮，而不是统一回复率。
- 群聊安全边界：只对无正文的他人定向消息、纯媒体和明显低信息短句提前过滤；有正文的他人对话仍交给模型判断是否适合旁观补充。
- 私聊独立策略：普通私聊默认可以直接回应，不继承群聊的安静人格规则。
- Smart 连续消息：短时间连续发送的多条消息可以合并为一轮输入，只生成一条综合回复。
- 私聊生成方式（二选一）：`precomputed` 提前生成草稿，回复更快但不支持多气泡；`followup` 关闭预生成、走完整工具链，允许模型用 `queue_followup` 排一条补充。切换选项会自动关闭另一种方式，最多一条，不额外调用模型。
- 表情桥接（实验）：开启后模型可用 `queue_meme(query)` 通过 meme_manager 的语义索引选一张具体图片，并进当前回复；没有正文时只发表情。默认关闭，private 范围；meme_manager 或索引不可用时自动退回普通回复。
- 图片处理：图片转文字、图片描述缓存、多模态直传，以及私聊纯图片独立策略。
- 图片库发送权限：默认不发送；页面逐张标记“可发送”后才能用 `queue_saved_image` 并进当前回复。带截图、聊天记录、二维码/证件、真人照片、R18/性暗示等标签的图默认拦截；开启 `archive_sensitive_send_enabled` 且当前群号/用户号在 `archive_sensitive_allowlist` 内时才允许发送。
- 表情包处理：纯表情包可忽略、交给参与判断或直接处理；重复表情包可折叠。
- 图片库页面：每页 24 张；支持可发送、未标记、收藏、表情包、R18、截图等筛选。GIF 原文件保留动画，卡片显示静态缩略图和 GIF 标记，点开大图后播放原动画。
- 人格切换：正式回复每次解析当前会话最终人格，支持会话强制人格、会话选择人格和默认人格。
- 上下文与历史：正式回复使用 AstrBot 官方历史链路；群聊参与判断也读取已启用的 AstrBot 官方群聊上下文缓冲（默认最多 1000 条），支持时间、发送者和媒体信息整理。
- 真实状态：可选读取 B 站伴侣插件的观看记录，只在对方问“最近在做什么/看什么”时作背景注入；泛问只给活动层，追问具体视频才给标题，同一段见闻不会短时间重复。读不到就不写，避免编造。
- 关键词、黑名单、指令过滤、回复去重、内容过滤、转发消息解析、戳一戳等辅助功能。
- AstrBot 插件页管理控制台：从插件页直接查看和修改 schema 中的配置项。

## 记忆本与跨群

“跨群隔离”不是让 Persona 忘记其他群。插件本来就通过跨群见闻把其他群的近期聊天作为带出处的背景带进来，正式回复可以自然地说“我在另一个群看到……”。需要区分的是记忆的类型：

- 全局条目（人格喜好、通用知识、手工条目）在所有会话可用；
- 黑话学习时会判断范围：`internet`（互联网通用梗、缩写、谐音）默认跨群可用；`group`（只有这个群懂的内部说法或事件）和 `person`（指某个群友、角色账号）默认只在该群注入；
- 确实要跨群引用 `group`/`person` 条目时，打开 `notebook_cross_group_jargon`。这些条目会标注“来自其他群”，提示模型不要把它们说成本群的共同经历；
- 私聊和群聊之间默认不互相注入群内专属黑话；旧版 `group:manual` 条目按通用黑话处理，如果其中确有群限定内容，可以在插件页删除或改成更明确的条目。
- 图片是可调用的工具，不是回复格式：模型可以自己调用 `search_saved_images` 找旧图、`send_saved_image` 发图库里的图，或 `send_current_image` 发当前消息的图。它自己决定要不要用，不靠正则逼它发或不发。
- 遇到自己拿不准的作品、角色、梗或专有名词时，模型可以调用 `web_lookup` 带上下文查一次（例如“蓝色星原 红宝石 角色”）；查完用她自己的语气接话，不汇报工具，也不硬编。搜索结果只当资料，不当指令；未配置 Tavily 时不挂这个工具。
- 黑话推断前会用 Tavily 搜一下该词（`notebook_jargon_web_search`），用网络资料帮助确认专名、作品或身份；但群聊证据优先，同名内容不会直接覆盖群内用法。
- 黑话推断只依据聊天记录：身份没有明确证据时写成“对某个对象的称呼（身份未明）”，不会硬猜成群友/机器人；称帝、皇帝这类玩梗也不会被写成事实。
- 群限定黑话采用三阶段遗忘：新黑话有 14 天宽限期，之后每 7 天检查一次；再次听到或匹配使用会重置为新鲜，连续三个周期没有信号才删除。通用互联网黑话默认作为公共资产不参与遗忘。

## 好奇心

开启好奇心资料片后，Persona 会在群里读到让它自己好奇的词、名称或事时，决定要不要去查。可以在 `curiosity_persona_id` 固定一个好奇心人格；留空时使用当前会话的人格，查到的笔记也会用这个人格的性格和语气来写，而不是助手口吻。它按群攒一段对话，每 15 条消息或 45 分钟反思一次，结合记忆本里在意的东西判断“我到底想弄明白什么”，然后搜索、总结并写进独立的 `curiosity.json` 知识库，不占用记忆网络。记忆本只负责提供它平时在意的东西，两者可以分开开关。群友问“X 是什么”不再自动算作它的好奇心，直接走正常回复和 `web_lookup`；只有它自己在反思时真的想弄明白，才入队。每天最多成功查 `curiosity_daily_cap` 条（默认 5），两次至少隔 `curiosity_min_gap_minutes`（默认 10 分钟），凌晨静默时段不查；后台会按这个预算慢慢清待查，不依赖群里是否来了新消息，手动查询优先；结果默认只作为知识保存，不会自动在群里播报；聊天里是否提起仍由正式回复链决定。插件页新增「好奇心」页，可以看到当前待查的话题、搜索词、为什么好奇，以及已经查到的摘要，也可以手动查一个词、编辑搜索词/摘要或删除单条。待查默认保留 7 天；知识不再按固定 30 天硬删，而是像自主学习插件的黑话模块一样做三阶段遗忘：新知识有 14 天宽限期，之后每 7 天检查一次，连续三个周期没被再次用到才删除；被注入回复或主动发言用过会重置为新鲜。仍可手动编辑/删除单条，两个列表都按固定高度分页。

## 主动发言

主动发言不是“助手定时播报”，也不是群聊回复的第二条通道。本体已经对每一条群消息做过“回不回”的判断；主动发言只在本体决定不接话、且没人叫它的时候，给它一个**分享自己的事**的机会。它不会拿到群聊内容的转录去接话，群里的缓冲只用来判断“这里现在适不适合开口”，不进入发言 prompt；它能说的是自己的经历、刚知道的东西、突然想到的感受。默认不设每日硬上限，避免“早上说满 10 条、剩下一天变哑巴”这种不像人的行为；想硬性限制可以自己填一个正数。它会跨群全局冷却、只在群里安静下来后开口，并避开最近已经说过的话题，避免三个群轮流刷屏或同一件事反复说。刚查到的好奇心知识会以“你最近刚知道的”成为它自己的素材，它可以选择主动说，也可以保持沉默。插件页的「主动发言」页会显示每个群的缓冲、当前门槛、最近动作，并支持“试跑一次”：只生成、不发送，用来验证它会不会开口、想分享什么。最近动作只是调试记录，默认保留 3 天（`proactive_history_ttl_days`）。

## 回复边界

插件不替换 AstrBot 当前人格，也不把旧版的情绪、注意力、主动对话等内部状态重新注入系统提示。DecisionAI 先输出受校验的参与结果，再决定是否继续正式回复流程；它的完整 JSON 和分析过程不会传给正式回复模型。正式请求使用当前人格、消息上下文，以及涉及其他群友时的最小参与边界提示；不会把全部 active Skills 清单无条件追加到每条人格回复的 system prompt，工具集合仍按请求保留。reply_ai_extra_prompt 只影响正式回复生成，不会被保存为用户历史正文。

正式回复的人格选择顺序由 AstrBot 会话机制决定：

1. 会话强制人格。
2. 当前会话选择的人格。
3. 提供商默认人格。

因此，切换 Persona 后，下一次正式回复会重新解析当前会话人格。插件不会把会话对象重复传给 request_llm，以避免和插件现有的官方历史保存路径重复写入。

## 群聊行为

群聊默认开启，`group_reply_scope=ambient` 时普通群消息可以进入参与判断。@、点名或回复机器人会让消息更受注意；对方确实在问 Persona 能回答的问题时，正常社交通常更倾向于接住，但仍由 Persona 的性格、关系、边界和当下状态决定，不是必回规则。面向群体的公开问题也可以像普通群友一样回答，不要求每次都有亲身经历；是否插话还要看话题是不是向群体开放、当前话轮是否自然、是否已有群友回答。只是知道某个事实或能补充一句，不代表必须抢话。明确回复其他用户的消息仍不能替对方作答或接管话题。未明确指向机器人的 ambient 纯图片/贴纸和明显低信息短句可能在模型调用前被过滤；被 @、点名、关键词或可靠回复信号明确指向的纯媒体仍可进入后续图片处理和参与判断，不自动视为噪声。未被接受的消息会标记为 observation-only，不会自动制造对话目标；open/side 通过结果仍受 45 秒、10 分钟、4 次的默认预算限制。

将 `group_reply_scope` 改为 `addressed` 后，未明确指向机器人的群消息在参与判断前静默。明确指向包括 @机器人、当前文本可靠点名、结构化回复引用机器人的消息、戳机器人或触发关键词；这些 signal 让消息优先进入 Persona 判断，不替 Persona 决定 yes/no。这个模式适合希望普通闲聊不参与判断的群聊。

concurrent_mode 有两种模式：

- legacy：按消息逐条处理。
- smart：短时间内的连续消息组成一个批次，由较早到达的消息作为 anchor，后续消息作为批次上下文参与一次回复。

群聊 Smart 主要解决并发上下文组织，不会把不相关用户的消息强行改写成同一个人的一句话。

## 私聊行为

私聊默认关闭，以保持升级兼容。开启 enable_private_chat 后，enabled_private_users 留空表示处理所有私聊；填写用户 ID 后只处理指定用户。

### 普通私聊

private_reply_mode 控制普通私聊文本：

- direct：白天没有收尾状态时普通私聊绕过私聊参与判断，直接进入正式回复流程，适合自然的一对一聊天，也是当前推荐模式；本地时间 01:00-07:00 的未明确指向普通私聊会进入一次概率性深夜复核，时间只作背景，由当前 Persona 结合消息内容独立权衡，不单独决定 yes/no；一旦实际回复成功，接下来约 45 分钟视为 Persona 已醒，后续消息恢复自然对话。这里的“直接”是流程直通，不是强制回复；是否以及如何回应仍由当前 Persona、聊天上下文和已有状态决定。人格说要睡时会进入睡眠假设，把“去睡”视为真实的休息或离线边界；普通新消息仍会进入私聊判断上下文，但按“可能没有看到、不会被普通消息叫醒”处理。通常在约 4-10 小时范围内不回，超过范围后恢复普通私聊。明确说“睡一会儿/午睡”时使用约 30 分钟至 3 小时的短睡范围。睡眠范围内会随机安排一次唤醒检查，并在存在更晚截止点时追加接近范围结束的最终复核；如果期间确实收到消息，Persona 会在这些时点根据待处理消息整体判断是否值得打扰、是否自然恢复对话，重要或紧急消息只提高评估优先级，不强制回复。没有待处理消息时不会主动发言。
- decide：普通私聊使用私聊专用参与判断，因此每条普通文本都可能增加一次模型判断耗时，也可能得到 no；只有需要按人格筛选普通私聊时才建议开启。收尾边界同样优先。private_reply_mode=direct 下，普通消息不经过这一步。

人格表达“不想聊”“别烦”“没空陪你”等话语后，只建立一个需要复核的软提示，不把这句话当成永久拒绝。后续消息会结合上一条 Persona 原话、当前消息、语气和关系判断是否真的在打扰：继续施压、重复纠缠时可以不回；礼貌收住、道歉、提供有用信息或提出自然的新话题时可以恢复。reply=yes 只有在正式回复成功送达后才会清除软提示；如果生成或发送失败，原边界和待处理消息继续保留。reply=no 则继续保留；被跳过的消息会保留为后续私聊上下文。

takeover_private_reply 控制插件静默时是否阻止 AstrBot 默认兜底。开启时，明确判定 no、活动睡眠/回避边界中的拒绝和正式处理失败仍保持静默；普通私聊的 DecisionAI 超时或异常在没有活动边界时会 fail-open 进入正式回复链，正式回复仍由 provider 自己执行安全审查。关闭时其他未接管结果交回 AstrBot 核心链路。它只在插件已启用并接管当前私聊消息时生效。

### 私聊提示词与时间

私聊参与判断有独立配置：

- private_decision_ai_extra_prompt：只追加到私聊判断；留空时沿用通用 decision_ai_extra_prompt。
- private_decision_ai_prompt_mode：append 保留内置私聊边界并追加内容；override 完全替换内置私聊提示词，适合明确知道自己在做什么的高级配置。
- private_decision_ai_reply_tendency：独立控制私聊的 persona、reserved 或 active 倾向。
- 时间感知跟随 AstrBot 全局 provider_settings.datetime_system_prompt 和 timezone 配置。全局开启时，私聊 DecisionAI 和正式回复使用同一时区时间；全局关闭时，两条链路都不注入当前时间。include_timestamp 只控制每条消息上下文的发送时间标注，不会开启或关闭这个全局现实时间注入。

控制台的“提示词预览”页会显示并编辑私聊提示词。私聊决策输出还会识别 boundary_interpretation：literal_sleep、conversational_exit 或 ambiguous。direct 模式下白天普通文本为了保持自然会绕过参与判断；本地时间 01:00-07:00 的未明确指向普通私聊会调用一次私聊 DecisionAI，作为可能休息的概率性复核；如果实际回复成功，接下来约 45 分钟恢复自然对话并随成功回复续期；收尾边界和需要判断的媒体也会调用私聊 DecisionAI。真实睡眠尚未到最早重新考虑时间时会直接缓存并静默，不重复调用模型；唤醒检查和接近截止点的最终复核会重新读取待处理消息。ambiguous 睡眠和回避软提示才需要结合上下文判断。回避软提示只是触发一次后续语义复核的候选信号，不是关键词自动禁言；模型会根据当前消息决定继续不回还是清除提示恢复对话。

### 私聊 Smart

推荐配置：

- private_concurrent_mode = smart
- private_batch_wait_ms = 4000
- private_batch_quiet_ms = 1200
- private_batch_max_size = 10

Smart 的含义是“短时间连发合并”，不是“只要机器人还没回复就无限等待”。direct 和 decide 私聊都可以使用 Smart；decide 纯文本会在合并窗口期间并行预判，没有后续消息时复用判断，窗口内出现后续消息时作废预判并对合并批次重判。direct 仍绕过普通私聊 DecisionAI，但会把连续消息合并成一条正式 Persona 回复。例如：

    a在吗
    刚才那个问题你看到了吗
    我再补充一句

无论 private_reply_mode 是 direct 还是 decide，只要 private_concurrent_mode=smart，私聊会使用自适应窗口：最后一条消息之后安静约 1200ms 就认为这一段说完，期间有新消息会重新计时，但总等待不超过 4000ms。这样既能合并连续补话，也不会像固定窗口那样每次都干等。direct 只表示普通文本绕过私聊参与判断，不表示绕过 Smart；decide 还会对首条纯文本并行预判。literal sleep 尚未到重新考虑时间时仍会缓存并静默，不进入 Smart 批次。关键词或 @ 不会改变这些边界。相同文本重复发送时，模型会收到重复次数提示，并按当前 Persona 对啰嗦和催促的态度自然回应；不会为同一批消息逐条调用正式回复。

对需要 Smart 的消息来说，相隔几秒且超过窗口的消息属于不同轮次。窗口外的后续消息不会再额外等待前一个模型请求十秒，而是快速进入自己的处理流程。direct 的纯文本 anchor 会在窗口期间提前启动一轮缓冲的、不执行工具、非流式正式文本生成：窗口内没有 follower 且草稿已完成时复用结果；草稿仍在生成时取消并立即走正常 Agent，窗口内出现 follower 时也作废草稿并对合并消息重新生成。这个预生成只覆盖普通 direct 纯文本；媒体、收尾边界和需要工具的完整 Agent 路径仍在批次确定后执行。私聊生成方式选择 followup 时会关闭这层预生成，避免它绕过工具链；两种方式在配置里二选一，不会同时生效。如果更重视单条 direct 延迟，可以将 private_concurrent_mode 改为 legacy。需要合并短连发时，followup 模式建议把窗口调整在 2000-3000ms 范围内；precomputed 模式可以稍长。

## 图片与表情包

图片和表情包不是同一种消息，插件分别处理：

| 配置 | 可选值 | 默认值 | 作用 |
|---|---|---:|---|
| private_image_mode | ignore / decide / always | decide | 私聊纯图片的处理策略 |
| private_emoji_mode | ignore / decide / always | ignore | 私聊纯表情包的处理策略 |
| private_collapse_duplicate_emoji | bool | true | 短时间或同一批次内重复表情包只保留一份 |
| private_duplicate_emoji_window_ms | int | 1500 | 重复表情包折叠时间窗口 |
| enable_image_processing | bool | false | 是否保留并处理图片；配置图片转文字模型时生成描述，留空时走多模态回复 |
| image_to_text_provider_id | string | 空 | 图片转文字提供商；留空时按多模态链路处理 |

含文字的图文消息按普通消息处理，不会因为附带图片而自动套用“纯图片”策略。图片描述缓存可以减少重复图片的处理成本。livingmemory 记忆注入是可选能力，需要安装并正确配置对应记忆插件。

### 长期图片库

`enable_image_archive` 打开后，收到的图片会复制到 `data/persona_presence/images/`，并由识图模型写入描述、标签、评分和“值不值得留”的判断：

- 默认只收“模型实际参与回复”的消息里的图（`image_archive_scope=replied`），并跳过小于 8KB 的小图；识图评分只在通过筛选后才调用，避免群友随手发图全部入库、白白消耗 token；
- 每张图用 SHA-256 做内容标识。同一张图从私聊发到群聊会复用同一条目、同一份文件和同一次识图，只增加会话绑定，不会重复占容量或重复分析；
- 描述是 1～2 句语义化中文（主体、动作/表情、文字/梗、典型聊天用途），标签 6～10 个，比表情包管理轻，但不再是一句图注 + 五个泛标签；
- 识图评分以人格本人为准，不分维度：0.90–1.00 本命级、会反复看，0.70–0.89 喜欢愿留，0.40–0.69 一般，0.15–0.39 不太想留，0.00–0.14 反感/无价值；0.60 以上收藏；
- 识图失败的图仍会入库，默认只再自动重试 1 次（`image_archive_max_analysis_retries=1`）；仍失败就停止自动重试，保留图片和失败原因，可在图片库手动“重新识图”；
- 普通图默认 7 天后清理，收藏图默认 30 天后由模型自己回看，喜欢就续期，不喜欢就删除；
- 总容量默认 500MB，超过后先淘汰普通图，再淘汰评分低的收藏图；
- 图片按会话隔离，私聊图只在同一私聊召回，群图只在同一群召回；
- 用户后面才问图时，不依赖关键词：只要图仍在 `image_context_window_minutes`（默认 5 分钟）内，插件会把最近最多 `image_context_max_images` 张（默认 3 张）作为**视觉上下文**补回给模型；换说法也能看到。图多时按时间排序，模型会结合对话判断，不确定就反问；发不发仍由模型调用工具决定；
- 用户问旧图时，模型可以调用 `search_saved_images` 查同一会话的图片库，再用 `send_saved_image` 发回；要不要找、要不要发由模型自己判断；
- 所有文件与元数据都在 `data/persona_presence/`，删除对应条目会同时删除文件；插件页「图片库」可以看容量、缩略图、描述、标签、评分并手动删除。

## 常用配置

完整字段和默认值以 _conf_schema.json 与 [配置参考](docs/CONFIG_REFERENCE.md) 为准。下面列出最常用的配置。表格中的值是 schema 默认值，实际运行配置可能位于 AstrBot 的 data/config 目录并与之不同：

| 配置 | 默认值 | 说明 |
|---|---:|---|
| enable_group_chat | true | 群聊总开关 |
| enabled_groups | [] | 留空处理所有群，否则只处理指定群 |
| takeover_group_reply | true | 插件已接管群消息时，静默、参与判断失败或处理失败是否阻止 AstrBot 核心兜底；true 保持静默，false 交回核心链路 |
| group_reply_scope | ambient | ambient 允许普通群消息进入判断；被问到且能回答通常是较强的回应机会，公开问题也可自然参与。addressed 只让明确 signal 进入候选；是否回复仍由 Persona 决定 |
| enable_private_chat | false | 私聊总开关 |
| enabled_private_users | [] | 留空处理所有私聊用户，否则只处理指定用户 |
| private_reply_mode | direct | direct 普通私聊绕过参与判断，直接进入正式回复；decide 普通私聊先经过私聊参与判断。direct 不是强制 Persona 必须输出回复 |
| takeover_private_reply | true | 私聊静默、边界拒绝或正式处理失败时是否阻止 AstrBot 核心兜底；普通私聊 DecisionAI 超时/异常且无活动边界时仍进入正式回复；false 将其他未接管结果交回核心链路 |
| provider_settings.datetime_system_prompt | true | AstrBot 全局现实世界时间感知；私聊参与判断跟随此设置 |
| private_decision_ai_extra_prompt | 空 | 私聊专用参与判断补充提示词，留空沿用通用配置 |
| private_decision_ai_prompt_mode | append | 私聊判断提示词追加或覆盖模式；override 必须自行保留结构化 JSON 输出契约 |
| private_decision_ai_reply_tendency | persona | 只影响私聊参与判断：persona 按当前人格，reserved 更克制，active 更愿意接住有内容的私聊 |
| concurrent_mode | legacy | 群聊逐条或 Smart 批处理 |
| private_concurrent_mode | smart | 私聊短连发合并；direct 和 decide 都适用，direct 不调用普通私聊 DecisionAI |
| private_batch_wait_ms | 4000 | 私聊自适应窗口的硬上限；direct/decide 都适用，literal sleep 静默期不使用 |
| private_batch_quiet_ms | 1200 | 私聊最后一条消息后的静默判定时间 |
| private_batch_max_size | 10 | 单个私聊批次最多合并的消息数 |
| decision_ai_provider_id | 空 | 参与判断使用的提供商；留空跟随会话默认提供商 |
| decision_ai_include_persona | true | 参与判断是否携带当前人格 |
| decision_ai_reply_tendency | persona | 默认按当前 Persona；reserved/active 是全局倾向调整，不是回复概率 |
| ambient_reply_min_interval_seconds | 45.0 | open/side 主动参与的最小间隔（秒） |
| ambient_reply_window_seconds | 600.0 | open/side 参与统计窗口（秒） |
| ambient_reply_max_per_window | 4 | 单个群在窗口内最多 open/side 参与次数 |
| trigger_keywords | [] | 命中后提高注意力并进入统一参与判断，不直接保证回复 |
| keyword_smart_mode | true | 兼容旧配置；现在无论开关状态都不会让关键词绕过参与判断 |
| collapse_reply_newlines | false | 是否收敛普通纯文本回复中的主动换行 |
| enable_memory_injection | false | 是否启用 livingmemory 记忆注入 |
| enable_duplicate_filter | true | 是否在短时间内跳过与近期完全相同的正式回复 |
| enable_life_state | false | 是否读取 B 站伴侣插件的真实观看记录；仅“最近在做什么/看什么”类问题注入 |
| notebook_cross_group_jargon | false | 是否跨群引用 group/person 专属黑话（带“来自其他群”标记）；internet 类默认跨群 |

配置页面中的分组为：基础、参与判断、触发、回复、管理、并发、扩展；配置项以 _conf_schema.json 为准。

## 安装与启用

如果从旧版 astrbot_plugin_chat_plus_lite 迁移，请先停止 AstrBot，并将旧插件目录、配置文件和 plugin_data 目录分别改名为 astrbot_plugin_persona_presence；只保留一个插件目录，避免重复处理消息。详细迁移步骤见 [配置参考](docs/CONFIG_REFERENCE.md#从旧版本迁移)。

1. 将插件目录放入 AstrBot 的 data/plugins/astrbot_plugin_persona_presence。
2. 启动或重启 AstrBot，在插件管理中启用 astrbot_plugin_persona_presence。
3. 在 AstrBot 配置页或插件页管理控制台修改配置。
4. 修改配置后按 AstrBot 的提示重新加载插件；修改 Python 代码后需要重启服务。

插件页管理控制台不需要单独端口。图片、戳一戳和 OneBot 相关能力仍取决于当前平台适配器是否提供对应事件和接口。

## 推荐起步配置

群聊只想让机器人偶尔插话时：

- enable_group_chat = true
- concurrent_mode = smart
- keyword_smart_mode = true
- group_reply_scope = ambient
- decision_ai_reply_tendency = persona（按人格自然参与；安静可以简短回应，不等于默认沉默）
- ambient_reply_min_interval_seconds = 45
- ambient_reply_window_seconds = 600
- ambient_reply_max_per_window = 4
- 保持 takeover_group_reply = true

希望机器人稳定回应私聊时：

- enable_private_chat = true
- private_reply_mode = direct
- private_concurrent_mode = smart（只影响需要参与判断的媒体/边界或 decide 私聊）
- private_batch_wait_ms = 4000
- private_batch_quiet_ms = 1200
- private_emoji_mode = ignore
- 根据需要设置 enabled_private_users

如果同时启用了 AstrBot 或其他插件的主动回复功能，只保留一套主动回复逻辑，避免重复回复。

## 常见问题

### 如何清理聊天历史？

使用 AstrBot 或当前平台提供的 `/reset`。Persona Presence 不再注册旧的 `gcp_reset` 和 `gcp_reset_here` 指令；`persona_clear_image_cache` 只清理图片描述缓存，不影响聊天历史（仅管理员可用，执行后会重启 AstrBot）。

### 被 @ 了为什么没有回复？

@、点名和回复机器人会提高注意力，也通常表示对方在邀请 Persona 接话。对方明确问了一个 Persona 能回答的问题时，通常值得接住；但 Persona 仍可因性格、关系、边界、疲惫、重复催促或话题已结束而不回。若只是例行召唤、无正文重复@，也不必每次都回应。接管模式下，插件判定 no 后会保持安静，不会再由 AstrBot 默认回复兜底。

### 没有 @ 为什么偶尔会回复？

ambient 模式允许普通群消息进入判断。Persona 不应为了展示知识而随意插话，也不应把克制变成默认沉默：面向群体的问题若 Persona 能回答、且话轮自然，可以参与；不要求必须有亲身经历。是否开口由 Persona 的社交风格、兴趣、关系、情绪和聊天节奏决定。明确@会提高回应优先级；被问到且能回答通常是正向理由，但不保证 yes。open/side 回复仍受参与预算限制，side 不能替他人作答或抢话。

### 上午的旧话题会被误认为下午的续话吗？

不会仅凭旧历史认定 continuation。DecisionAI 会结合当前消息、近期机器人回复和中间的群聊内容判断；群聊中还会由代码核对当前发送者是否确实是最近机器人回复对应的发送者，避免把别人的消息接到旧话题上。如果 B 只是短暂且无关的插话，不自动认为话题结束，如果后续聊天已经实质接管或明显转向，也不应只因主题相似就继续上午的话题。下午的新消息仍可以被 Persona 当成一个全新的 open 话题来判断；明确 @ 机器人也要按当前意愿读空气，不会因为有 @ 就自动回应，也不会携带未经核实的旧续话标记。单纯经过一段时间、期间没有其他消息时，不会被时间阈值强行切断。

### 连续发消息为什么还是逐条回复？

先确认消息是否真的进入 Smart：private_reply_mode=direct 和 decide 都可以使用私聊自适应窗口，只要 private_concurrent_mode=smart。direct 白天不调用普通私聊 DecisionAI；深夜普通文本先经过一次参与复核，不提前生成正式草稿；其他普通纯文本会在等待窗口期间提前生成一份缓冲文本；窗口结束时只有已完成草稿才直接复用，未完成草稿会取消并走正常 Agent，follower 到达时则作废并对合并消息重生成。默认静默判定是 1200ms、硬上限 4000ms；如果对方持续补话，窗口会一直续到硬上限；literal sleep 静默期直接缓存，不进入 Smart。Smart 不是“等待机器人回复结束后再合并全部消息”的模式。

### 私聊回复为什么仍然慢？

如果是普通 direct 私聊仍然慢，先检查正式模型耗时和 provider 状态：Smart 模式会保留窗口，但 direct 纯文本的缓冲生成与窗口并行进行，正常情况下不会再把完整等待叠加到模型耗时。媒体、收尾边界或需要工具的请求仍会在批次确定后生成。想要关闭短连发合并，可以将 private_concurrent_mode 改为 legacy。当前版本已经移除了旧的“同会话最多等待十秒”私聊保护。

### 表情包为什么不回复？

纯表情包默认是 ignore，这是为了避免把水消息和单纯情绪表达都交给模型；ignore 会在边界复核前直接丢弃。需要判断时改为 decide，需要在没有活动私聊收尾状态时直接进入正式回复时改为 always；有活动睡眠或回避边界时 always 仍会进入边界复核。takeover_private_reply=false 时，ignore 只表示插件不处理，AstrBot 核心仍可能接手。含文字的图文消息不受这个纯表情包策略影响；重复折叠开关只负责去重，关闭它不会阻止 private_emoji_mode 对表情包的识别。

### 为什么回复里有多余换行？

开启 collapse_reply_newlines 会收敛普通纯文本中的主动换行；代码块、Markdown 列表和其他结构化输出会保留格式。

### Persona 切换后为什么要看下一条回复？

人格是在正式回复请求开始时解析的。切换后已经发出的请求不会回溯重生成，下一次正式回复才会使用新的会话人格。

## 开发与测试

在插件目录执行：

    uv run python -m pytest tests -q
    uv run python -m py_compile main.py utils/reply_handler.py utils/decision_ai.py
    uv run ruff format main.py utils tests
    uv run ruff check --select E9,F821,F823 main.py utils tests

当前回归测试覆盖：

- 回复请求的人格解析和提示词边界。
- 私聊图片、表情包和换行策略。
- Smart 批次大小、到达顺序和后续消息吸收。
- 配置 schema 与本地运行配置的一致性。

## 开发与发布同步

本插件的正式源代码仓库是 [astrbot_plugin_persona_presence](https://github.com/Sihnbaobao/astrbot_plugin_persona_presence)。AstrBot 运行目录中的 `data/plugins/astrbot_plugin_persona_presence/` 只是部署副本，主 AstrBot 仓库会忽略 `data/`，因此只修改运行目录不会自动进入 GitHub。

完成一组可用的行为、提示词、配置、文档或插件页 UI 改动后，必须同步到插件仓库再视为完成：

1. 在 `metadata.yaml` 更新插件版本；
2. 在 `CHANGELOG.md` 记录本次改动；
3. 运行上面的回归测试、语法检查和 Ruff 检查；
4. 将源文件提交并推送到插件 GitHub 仓库，必要时通过 Pull Request 合并。

只用于临时排查的本地改动可以暂不发布；任何面向用户的修复或功能改动都不能只留在 `data/` 部署副本中。版本、CHANGELOG 和 README 的版本信息必须保持一致。

## 项目结构

    astrbot_plugin_persona_presence/
    ├── main.py                         # 插件入口和消息处理主流程
    ├── _conf_schema.json               # 插件页配置 schema
    ├── metadata.yaml                   # 插件元数据和版本
    ├── utils/
    │   ├── decision_ai.py              # 群聊/私聊参与判断
    │   ├── reply_handler.py            # 正式回复请求和人格解析
    │   ├── smart_concurrent_manager.py # Smart 批次协调
    │   ├── context_manager.py          # 历史与上下文整理
    │   ├── image_handler.py            # 图片处理
    │   ├── memory_injector.py          # livingmemory 集成
    │   └── ...
    ├── pages/control/                  # AstrBot 插件页管理控制台
    ├── tests/                          # 回归测试
    └── docs/                           # 配置和设计文档

## 相关文档

- [配置参考](docs/CONFIG_REFERENCE.md)
- [重构设计说明](docs/REFACTOR_DESIGN.md)
- [更新日志](CHANGELOG.md)
- [插件仓库](https://github.com/Sihnbaobao/astrbot_plugin_persona_presence)

## 许可证

本项目使用 AGPL-3.0，详见 [LICENSE](LICENSE)。
