## 📝 更新日志

## 1.1.0

- **Private late-night behavior**: direct private chat now uses a probabilistic DecisionAI review from 01:00 to 07:00, with a short awake-session window after a successful reply.
- **Private Smart replies**: direct formal drafts start early when safe, invalidate on followers, fall back on tool calls, and cancel stale slow drafts at Smart commit.
- **Official lifecycle alignment**: history, duplicate filtering, reset isolation, sleep boundaries, and concurrent saves follow AstrBot's normal result path.
- **Configuration and diagnostics**: expose private DecisionAI prompt controls and add ordering, sleep-boundary, tool-call, and late-night regression coverage.

## 1.0.0

- **Rename**: the plugin is now Persona Presence / 人格自主参与, with technical ID astrbot_plugin_persona_presence.
- **Participation model**: @, keywords and direct address increase attention but do not force a reply; the current Persona owns the subjective choice to speak, including on ordinary messages without @.
- **Safety boundaries**: structured participation decisions, Persona-owned continuation classification, observation-only cache entries, and an open/side reply budget limit stale-context and unsolicited replies without treating every interjection as a topic break.
- **Migration**: move the old plugin data/config paths to the new ID before enabling this plugin; the repository is now https://github.com/Sihnbaobao/astrbot_plugin_persona_presence.

## 0.0.12

- **Plain image handling**: `image_to_text_scope` now defaults to `all` and is exposed in the config schema; when `enable_image_processing` is enabled and a group image is not removed by the unaddressed ambient-media prefilter, plain images within that scope can reach the multimodal reply model instead of being dropped as undecidable messages. Addressed/keyword/reply candidates can therefore proceed, while image processing disabled or prefiltered pure images still stop before this scope is applied.
- **Market-face safety**: QQ market-face stickers are labeled in text context, and their dead CDN preview URLs are filtered out of vision requests instead of failing the payload download.
- **Echo prefix**: Mechanical phrase-plus-particle openers that echo the user's message are now removed by reply post-processing rather than prompt wording.

## 0.0.11

- **Repeated-message context**: Smart now identifies exact repeated private text and tells the reply model to follow the active persona's attitude toward repetition and verbosity.
- **Persona fidelity**: Repetition is no longer presented only as a neutral batch, so personas that dislike nagging can respond naturally without forcing annoyance for every character.

## 0.0.10

- **Private forced batching**: Private Smart now merges consecutive keyword/@ messages from the same sender; group Smart keeps forced-message boundaries.
- **Diagnostics**: Private arrival logs now include the platform message ID, arrival sequence, and compact message text for duplicate-delivery diagnosis.

## 0.0.9

- **Private Smart ordering**: Follower polling now follows the configured private batch window instead of using a fixed three-second limit.
- **Duplicate-request fix**: A follower that times out at the anchor boundary is rechecked before it can start an independent reply.
- **Input tracing**: Added a concise private Smart log showing the exact formal input and message count sent to the reply path.

## 0.0.8

- **Forced private messages**: Trigger keywords and @-forced messages that enter a decision-gated private Smart path still honor the configured batching window; ordinary `private_reply_mode=direct` text bypasses private participation and Smart, while forced status only affects reply decision behavior.
- **Regression fix**: Prevented the first keyword message from starting an independent LLM request before later messages in the same private burst can join it.

## 0.0.7

- **Private Smart window**: Increased the default private burst window from 3000ms to 4500ms to cover messages arriving near the previous boundary.
- **Latency boundary**: Kept the window bounded; this does not turn private Smart into indefinite unanswered-message collection.

## 0.0.6

- **Private Smart window**: Increased the default private burst window to 3000ms as a latency and mergeability compromise.
- **Behavior clarification**: Messages separated by several seconds remain separate turns; Smart does not wait for an unanswered conversation indefinitely.

## 0.0.5

- **Private Smart batching**: When private Smart is active, short bursts from the same private user are combined into one logical turn and produce one comprehensive reply instead of replying to each message separately; ordinary direct-mode text remains immediate.
- **Latency**: Removed the legacy ten-second per-chat wait from private Smart processing; messages outside the short burst window proceed without waiting for an earlier model request to finish.
- **Batch window**: Increased the default private burst window to 1200ms and clarified that longer gaps are separate turns.

## 0.0.4

- **Private chat support**: Added an opt-in private-chat switch, user whitelist, takeover control, and private Smart batching.
- **Media routing**: Pure private images and stickers now have independent ignore / decide / always policies.
- **Sticker deduplication**: Repeated identical stickers are collapsed within a short window and inside one private batch.
- **Persona switching**: Formal replies now resolve the active conversation persona instead of always using the configured default persona.
- **Private reply strategy**: Added direct private replies by default, a private-specific decide mode, takeover-controlled handling for AI/processing errors (only `takeover_private_reply=false` falls back to the core pipeline), and optional plain-text newline collapsing.

## 0.0.3 (2026-08-18)

- **消息库统一官方**：历史只使用 AstrBot 官方库。
- **reset 彻底清**：@bot reset 联动清空 platform_message_history + conversations + 插件内存缓存，不再残留旧记忆。
- **移除传统概率模式**：配置与代码一并删除（概率筛选/参与判断概率/回复后boost/相关属性日志与注释）。
- **修官方保存**：官方库写入改走标准 API update_conversation（此前靠猜方法名静默失败、官方库不更新）。
- **发送前剥离工具协议残留**（/parameter /invoke /tool_calls 及闭合标签），修回复后带代码正文问题。
- 空回复日志守卫；判断日志去重；@/关键词一视同仁（继承 0.0.2）。

## 0.0.2 (2026-08-18)

- **@/触发关键词与普通消息一视同仁**：被@、点名、触发关键词（含bot名字）都交给参与判断AI按人格判断，不再因@/关键词而必回；keyword_smart_mode 默认开启。
- **接管群聊回复**：新增 takeover_group_reply（默认开），stop_event 挡住 AstrBot 主对话的兜底响应——判 no 时不再被主 LLM 兜底回复（@ 必回真因）。
- **修 bug**：孤儿 @staticmethod 误令 _is_enabled 变静态方法；_safe_sender_display 迁移丢装饰器；孤儿 after_message_sent 装饰器令非协程 hook 触发 AssertionError；判断日志去重。
- 行为调整：@ 提示词改为与普通消息一视同仁（不再倾向回）。
- 结构：main.py 由 6059 行精简至约 3800 行（戳一戳/@识别/指令/回复保存拆为 PokeMixin/MentionMixin/CommandMixin/SaveMixin）。

## 0.0.1 (2026-08-18) — 重生版

彻底重构并重置版本号：
- **人格主导参与判断**：判断以用户 LLM 人格为立场，仅保留防乱回护栏；随机概率默认关闭（AI 全权判断），`enable_random_probability_filter` 可开。
- **配置页精简重做**：12组合并为7组、80→50项；分组 tab 浏览、短说明直显/长说明ⓘ折叠；移除消息处理流水线（重复且失真）；删除原作者运行时指纹/免责横幅（_session_guard）与空消息 INFO 噪声。
- **删除冗余功能代码**：移除转发解析、入群欢迎解析、输出/保存内容过滤及其工具类（约1400行）。
- 作者改为 Sihnbaobao；日志降噪（内部流程转 DEBUG）。

### V2.6.1-lite (2026-08-18)

**🧹 日志降噪 + 配置页精简**

- **日志降噪**：决策链路、缓存、保存明细等内部流程日志从 INFO 降为 DEBUG（正常不刷屏；调 AstrBot 日志级别到 DEBUG 可看细节），关键错误/启动信息保留。
- **配置页精简**：插件页由 80 项精简到 44 项、14 组到 12 组——转发解析、输出/保存内容过滤整组从配置页移除；图片/记忆/戳一戳/过滤/Smart 并发等组的"花样"高级项（模式微调、追踪、等待参数等）不再在页面展示。**被移除项仅不再可调，默认行为照常生效，功能不丢**；Smart 并发保留模式开关，启用后仍自动合并批量消息。
- 修正 main 启动日志与实际版本一致。
- **✅ 验证**：重启 AstrBot 插件正常加载（V2.6.1-lite）。

### V2.6.0-lite (2026-08-18)

**🧠 参与判断决策人格化重构 + 主流程整理**

- **人格第一立场**：重写 `SYSTEM_DECISION_PROMPT`，参与判断以用户 LLM 人格为主角（按人格的性格/兴趣/心情判断要不要回），把原来盖住人格的中性社交规则清单替换为"@/点名/提问/求助必回、别乱回他人对话/水话/被拒绝/复读"等防乱回护栏。判断只决定"要不要回复"，不生成回复内容（回复 AI 保持人格一致，不受影响）。
- **随机概率默认关闭（AI 全权主导）**：新增 `enable_random_probability_filter`（默认 `false`）——普通消息不再随机抽签，直接交给参与判断 AI 用人格判断；概率参数保留，打开开关即可恢复旧随机风格。
- **主流程整理**：把 5 处重复的 `ContextManager.format_context_for_ai` 调用抽为 `_format_ai_context` helper；并发等待标记由易残留的动态属性改为局部变量，行为不变。
- **✅ 验证**：语法校验通过；重启 AstrBot 插件正常加载（`Plugin astrbot_plugin_chat_plus_lite (V2.6.0-lite) by Sihnbaobao`），无报错。

### V2.5.0-lite (2026-08-15)

**📋 插件页配置 100% 覆盖（schema 驱动，与 AstrBot 配置页完全同步）**

- **目标**：插件页可配置项 = 插件全部可配置项，不再有"插件页少配置"的问题
- **机制**：插件页不再手写配置清单，改为**由 _conf_schema.json 动态驱动**——分组、字段、说明、选项、默认值全部从 schema 读取，AstrBot 配置页有任何变更，插件页自动同步，永不脱节
- **15 张分组卡片**：14 个 schema 分组 + 新增「🔧 高级参数」分组（收纳 21 个 main.py 有读取但从未在配置页展示的隐藏参数，如 思考过程标记、@全体概率加成、欢迎消息模式等），共 **99 个可配置项**（78 schema + 21 隐藏）
- **流水线胶囊**：5 个环节点击展开对应分组的全部字段（如「指令&关键词」展开关键词+消息过滤两组全部配置）
- **控件自动推导**：bool→开关、int/float→数字框、string+options→下拉、text→多行框、list→每行一项；提供商选择类字段显示当前值并提示到 AstrBot 配置页选择
- **保存**：白名单动态化（schema 键 ∪ 隐藏键），其余保持不变
- **✅ 验证**：13 个回归测试通过；schema 驱动模拟测试通过（控件类型推导/列表收集/emoji 标题提取）；node --check / py_compile 通过

### V2.4.2-lite (2026-08-15) (2026-08-15)

**🎨 插件页配置面板字段布局彻底对齐**