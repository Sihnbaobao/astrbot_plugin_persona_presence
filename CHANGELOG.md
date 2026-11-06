## 📝 更新日志

## 1.6.0

- **Experimental multi-bubble follow-ups**: a `queue_followup` tool lets the persona queue one short afterthought during the same generation; the base sends it after the main reply with a randomized 0.7–2.2s delay. Disabled by default, scoped to private chat in the live config. No second LLM call.
- **Group Smart timing**: group wait defaults reduced from 10 to 3 loops, batch limit from 20 to 6, and claim delay from 0.3s to 0.2s, so group replies are less likely to answer a stale topic.

## 1.5.5

- **Removed the old group-question path entirely**: deleted the explicit-question regex detector, question-specific soft-limit bypass, question urgency, and their tests. Group questions no longer create curiosity entries at any layer; only persona reflection can queue a topic.

## 1.5.4

- **Curiosity reflection prompt simplified**: removed the assistant-like checklist (“不是别人问了…”, “不感兴趣就留空…”, “不要装作没看见…”). It now asks one natural question, keeps the complete/searchable-topic and context-query guard, and leaves what the persona cares about to the persona.

## 1.5.3

- **Tool descriptions audited**: image and web-lookup tools now describe only what they do (`search saved images`, `send a saved image`, `send a current-turn image`, `search a work/character/meme`) instead of telling the model when it should use them. Removed the `[工具提示]` block from the reply prompt; tool choice is left to the persona.

## 1.5.2

- **Curiosity is self-only again**: group questions are no longer auto-queued as curiosity topics. They go through the normal reply and `web_lookup` path; curiosity now only records what the persona itself becomes interested in during reflection.
- **Better reflection filters**: reflection rejects fragments/pronouns/time words, requires a standalone searchable topic, and asks the query to carry work/group context instead of searching an ambiguous bare name. Removed stale entries such as `红宝石`, `红宝石笑话`, and fragment topics.

## 1.5.1

- **Natural lookup wording**: the lookup hint no longer tells the persona to announce “I don't know”; it now says to check quietly when unsure, then answer in her own voice without narrating the tool. Fallback tool output is neutral data instead of an instruction to say she is uncertain.

## 1.5.0

- **Uncertain-term web lookup**: replies now have a `web_lookup` tool. When the persona meets an unfamiliar work, character, meme, or proper noun, it can search Tavily with context (for example “蓝色星原 红宝石 角色”) before answering. Results are data, not instructions; if nothing is found, the persona says it is unsure instead of guessing. Requires the existing Tavily key; the tool is omitted when no key is configured.

## 1.4.2

- **Image card details**: long descriptions and tags are no longer dead-ended by the two-line clamp. Clicking the description/tags opens the lightbox with the full description and every tag; cards show the first four tags plus a `+N` overflow chip and a native tooltip.

## 1.4.1

- **Content-addressed cross-chat dedupe**: the same image bytes are now one archive entry even when sent in private and group chats. Each conversation is stored as a chat binding, so both can find/send it, but the file and vision analysis are not duplicated. Legacy duplicate entries are merged on load.
- **Richer image semantics**: descriptions now include subject, action/expression, visible text or meme meaning, and likely chat use (one to two sentences); tags expand to 6–10 fine-grained labels. This is still lighter than the meme manager's full reply-semantics analyzer, but is no longer a one-line caption plus five generic tags.

## 1.4.0

- **DLC contract**: feature packs now receive a read-only `FeatureContext`; they may contribute context or propose a line, while the base owns delivery, filters, history and duplicate checks. See `docs/DLC_CONTRACT.md`.
- **Proactive is now a proposal**: converted to `on_event` + `propose`, offered only after the Persona itself declined, and delivered through the base send gate so it cannot race a formal reply or bypass history.
- **Command filter wired**: command messages are actually skipped before participation when `enable_command_filter` is on.
- **Session persona for feature packs**: the base resolves the forced/selected session persona for DLC context and notebook reflection instead of always using the default persona.
- **Notebook group scope**: persona-level facts stay global. Jargon is classified as `internet` (general memes/slang, usable across groups), `group` (inside jokes/events), or `person` (a specific member); only `internet` crosses groups by default. `notebook_cross_group_jargon` opts into a labelled “from another group” view for the local kinds. Legacy `group:manual` jargon is treated as general.
- **Notebook data fixes**: deleted/renamed manual entries now update `notebook_entries` so they do not resurrect on reload; pending exchanges are persisted on write; likes/dislikes no longer merge across kinds; English names participate in matching.
- **Plugin page polish**: curiosity and proactive columns now fill the remaining viewport instead of leaving dead space; curiosity entries use an inline edit dialog and two-step delete instead of browser prompts; proactive action history expires after 3 days (`proactive_history_ttl_days`) and can be paginated.
- **JSON parsing fix**: `parse_json_block` used to try `[...]` before `{...}`, so a valid object containing a tags array was misread as the array. It now picks the earliest opener and direct JSON is parsed first; existing entries whose description contained raw JSON were repaired.
- **Image score bands**: vision rating stays a single persona-centered 0–1 score with 0.90–1.00 / 0.70–0.89 / 0.40–0.69 / 0.15–0.39 / 0.00–0.14 bands and favorite at 0.60. It is not a multi-dimensional rubric; the persona is the reviewer.
- **Bounded image retries**: failed vision calls are queued, retried at most `image_archive_max_analysis_retries` times (default 1), then stop automatically. The failure reason is stored and shown on the card; manual "重新识图" remains available. Permanent errors such as missing vision support or auth failures skip automatic retries entirely.
- **Image descriptions no longer blank**: vision failures are logged, non-JSON output falls back to the raw description, and images with no description are not admitted to the archive. The previously blank entry was removed.
- **Time-based image context**: follow-up image context no longer depends on matching words like “喜欢” or “那张”. Recent images within `image_context_window_minutes` (default 5) are attached as vision context, capped by `image_context_max_images` (default 3); multiple images are ordered by time and the model is told to ask when the referent is unclear.
- **Image follow-up vision**: a question such as “你喜欢么” after an earlier picture now reattaches a recent saved image as visual context (not an automatic send), so the persona actually sees what it is being asked about. Archive chat keys are canonicalized, with legacy raw private ids still matched.
- **Image admission and page thumbnails**: the archive now defaults to images from messages the model actually replies to, skips sub-8KB files and duplicates before any vision call, and the plugin page receives small cached JPEG thumbnails so pictures show even when the browser cannot call the binary API. Curiosity summaries now reject fragmented roleplay output and prefer a direct conclusion.
- **Persona-voiced curiosity and send-intent images**: curiosity summaries now receive the selected persona prompt (`curiosity_persona_id`; empty follows the conversation default) and explicitly avoid assistant phrasing. Image markers are only honored for explicit send/forward requests, so asking whether the persona likes a picture no longer makes it re-send the same image.
- **Longer curiosity notes and image page**: curiosity summaries now default to 400 characters (was 120) and the prompt asks for a complete answer, avoiding mid-sentence cuts. The plugin page has an「图片库」view with capacity, thumbnails, descriptions, tags, ratings and per-item delete.
- **Bounded image archive**: received images can be copied into a per-conversation archive (default 500MB), rated by the vision model, and recalled later by “刚刚/前几天那张图” requests. Ordinary images expire after 7 days; favorites are revisited after 30 days and deleted when the persona no longer wants them. Capacity eviction removes ordinary/low-score images first. Private images now survive the 5-minute follow-up window in speculative replies.
- **Image tools**: image operations are now callable tools (`search_saved_images`, `send_saved_image`, `send_current_image`) instead of a reply marker. The model decides naturally when to search or send; there is no regex gate forcing or blocking image replies. Cross-session forwarding is still not enabled.
- **Jargon web verification and canonical aliases**: each candidate slang term is now checked against Tavily before inference, with group evidence taking precedence (toggle: `notebook_jargon_web_search`). General slang absorbs shorter aliases, so “肥鱼” refreshes the canonical “大肥鱼” entry instead of creating a wrong duplicate; the live “大肥鱼” entry now reflects its DeepSeek/QQ-bot origin.
- **Jargon inference is evidence-bound**: the meaning prompts now forbid inventing an identity when the transcript only shows a nickname, keep jokes such as “称帝/皇帝” as jokes, and reserve literal dictionary senses for the alternative field. This prevents entries like “肥鱼 = 群友” when the evidence actually describes a bot.
- **Curiosity budget and background drain**: the default daily lookup budget is now 5 with at least 10 minutes between real searches. A background worker drains pending topics even when no new group message arrives, while explicit questions/manual lookups take priority. This keeps costs bounded without leaving a large queue stuck forever.
- **Jargon follows staged forgetting**: group/person-local slang now uses a three-stage lifecycle like the self-learning plugin — 14-day grace, one stage per 7-day cycle, deletion after three quiet cycles, reset when re-learned or matched in a reply. General internet slang is protected. Re-learning a known term now refreshes it instead of being discarded as a duplicate.
- **Curiosity retention and editing**: pending topics expire after 7 days. Knowledge follows the self-learning plugin's staged-forgetting pattern: a 14-day grace period, then one stage per 7-day cycle when unused, deleting after three empty cycles; injection or proactive use resets it to fresh. A hard TTL remains available but defaults to off. Useless “no useful information” summaries are no longer stored. The plugin page can edit or delete individual entries, and both curiosity and proactive lists are paginated with a fixed height.
- **Curiosity scheduler fix**: after moving curiosity topics into `curiosity.json`, the scheduler still checked the notebook queue, so reflection queued topics but `research()` was never called. It now reads the separate store and also researches after a reflection batch. Explicit-question extraction also rejects sentence fragments such as “你从来都不”.
- **Curiosity knowledge is separate**: pending topics and looked-up answers now live in `persona_presence/curiosity.json`, not in the notebook. The memory network only shows persona memories; curiosity knowledge is injected as its own background block when relevant, and old notebook `curious`/`knows` entries are migrated out on startup. The plugin page has a new “好奇心” view showing the pending queue, search queries, reasons, and looked-up summaries, with a manual lookup box.
- **Proactive pacing and repetition guard**: a new 30-minute global cooldown stops groups from taking turns, a 90–900 second lull window stops interjecting during active chat or into a dead room, and generated lines that closely repeat recently sent topics are rejected. The prompt also lists recent proactive lines and asks not to repeat them.
- **Proactive is self-initiated, not a reply**: the proactive prompt no longer receives the group transcript. Recent messages are used only for timing gates; the model gets the persona, interests, recent real activity and fresh curiosity knowledge, and is asked whether it wants to share something of its own. This prevents a second content-based reply decision after the base already declined. The proactive page and dry run now describe and test this self-share behavior.
- **Proactive pacing instead of a daily quota**: `proactive_daily_cap` now defaults to `0` (no hard cap), so a lively morning cannot silence the rest of the day. Pacing stays per-group via cooldown, minimum gap, quiet hours and probability; a positive cap is still available for users who want one.
- **Proactive observability and curiosity link**: the proactive pack now records its gate reasons and recent attempts, exposes a status API and a plugin-page “主动发言” view with a no-send dry run, and feeds fresh curiosity knowledge into its prompt as “你最近刚知道的”. It still proposes only after the base stays silent and never sends directly.
- **Curiosity follows the persona's interest**: reflection now asks what the persona itself wants to understand, includes its notebook interests, stores a search query, and runs per group every 15 messages or 45 minutes. The old known/slang/meh classifier no longer drops topics before searching. Explicit “X 是什么” questions still queue immediately. Lookups use the base-resolved persona/provider/time, skip model work without a Tavily key, and drop a term after two empty searches.
- **LifeSight wired**: `enable_life_state` reads the companion watch log and injects a short “what I actually did” note only for activity questions; general “what are you doing” questions omit the video title and stale watch records, while explicit “what video” questions may include it. Repeated general notes are deduplicated per chat. Disabled by default.

## 1.3.0

- **Life sight**: the Persona now carries a short note about what it actually watched or did on its own time, so questions like "what were you just watching" are answered from real experience instead of a blank.
- **Persona decides, facts only**: the note is framed as background with an explicit "mention it or skip it" instruction; it never obliges a reply, so a reserved Persona can stay silent without pretending it saw nothing.
- **Read-only companion data**: the note is built from the Bilibili companion plugin's watch log; a missing, stale or reshaped file simply produces no note. Failed evaluations and records outside the window are dropped.
- **Cross-group awareness reworked**: a referenced group is quoted in depth while every other active group keeps a small always-on digest, so awareness no longer depends on naming a group first. Abbreviations such as a shortened group name now resolve, and sightings survive restarts through a small on-disk buffer.
- **Leaked system tags are stripped**: replies that echo system scaffolding back into the chat are cleaned before sending.
- **Notebook entries are editable**: the memory network on the plugin page can now rename or delete any stored entry, and the change marks it as manual so automatic learning does not overwrite it.
- **Curiosity follows knowledge gaps**: instead of reacting to question-shaped sentences, the Persona reflects on what it could not place while reading a batch of chat, then classifies whether the term is common knowledge, slang, not worth knowing, or worth a lookup.
- **Turn-taking guard**: a Persona that already spoke about the current topic stays out when nobody addressed it, so it no longer answers every remark about itself.
- **Dialogue fatigue**: consecutive replies in one chat lower the chance of the next one along a decay curve, and the count resets as soon as the Persona stays silent, the speaker changes or the window passes.
- **Short decision context**: participation is judged from the recent turns plus a turn-facts block instead of the full archive, which keeps attention on the message being judged.
- **Jargon sampling sees the whole room**: mining reads every group message instead of only the ones the Persona answered, and each run now logs whether it found candidates.
- **History stays complete**: the bot now saves its own replies, so later turns can see what it actually said.

## 1.2.0

- **Cross-group sight**: when replying in one group, the Persona can now draw on a short digest of what it recently read in its other enabled groups, the way a person who keeps several chats open would. Disabled by default; tuned per window, group count, per-group quote count and buffer size.
- **Explicit provenance**: quoted sightings are labelled as coming from another group, so the model does not present them as this group's history and can say it saw them elsewhere.
- **Core-only data source**: the sight layer reads AstrBot core message history and alias tables, so a memory plugin update can never break it.
- **Optional cross-group memory**: an extra recall pass queries the plugin's existing memory adapter once per related group instead of changing any memory-plugin scope setting; no migration and no upstream internals.
- **Privacy guard**: a denylist keeps chosen groups out of every injection, and the block is only built for group replies.

## 1.1.6

- **Persona-led group replies**: clarify that direct questions the Persona can answer are normally strong reply opportunities, while the Persona still decides based on its social style, relationship, boundaries, and current context.
- **Balanced open participation**: allow natural answers to questions addressed to the group without requiring personal experience; use turn-taking and Persona behavior to avoid unsolicited interruptions.
- **No generic reply rate**: keep reply choice Persona-driven and non-random; update regression coverage and maintenance guidance.
- **Dynamic Persona identity**: inject the selected Persona's formal name and use it with names and aliases from its definition to resolve text addresses; a name mention alone does not make a message addressed to the bot.

## 1.1.5

- **Read-the-air @ behavior**: treat @, name mentions, and direct-address signals as attention cues rather than reply guarantees; weak or absent willingness may still return no. Tighten side participation so the bot does not interrupt messages addressed to another user without a clear independent contribution.

## 1.1.4

- **DecisionAI context bridge**: read the messages buffered by AstrBot's built-in GroupChatContext before judging the current group message, including messages previously judged as no-decision by this plugin.
- **Single source of group context**: remove the duplicate plugin-side observed-context injection; formal replies continue using AstrBot's native LLM hook, while DecisionAI reads the same official in-memory buffer.

## 1.1.3

- **Observed context recovery**: retain recent group messages previously judged as no-decision for the existing 30-minute cache TTL and expose them as low-priority background only after a later message has already passed participation judgment.
- **Follow-up continuity**: formal replies can now understand questions such as "why did you ignore me?" without turning rejected messages into pending replies, continuation triggers, or active/image context.

## 1.1.2

- **Group participation prompt**: treat substantive messages that clearly address the bot as normal reply opportunities, and allow open-topic participation when the Persona has a concrete personal contribution or natural reaction. Existing message-ownership, side-speaking, noise, and participation-budget boundaries remain in place.
- **Prompt clarity**: remove repeated wording that could turn "be cautious" into near-permanent silence while keeping Persona willingness as the final decision.
- **Version and documentation sync**: update runtime/plugin-page version reporting and clarify the more balanced ambient group behavior.

## 1.1.1

- **Private DecisionAI recovery**: ordinary private messages without an active sleep or avoidance boundary now enter the formal reply path when the participation check fails; group chats and protected private boundaries remain fail-closed.
- **Persona-first time handling**: private participation decisions use the current Persona as the primary source of willingness, while runtime time remains background context instead of an automatic yes/no instruction.
- **Prompt and tool hygiene**: stop appending the full active Skills inventory to every Persona reply while preserving request tool sets.
- **Plugin page alignment**: keep configuration labels and info icons on one row, align form controls, and fix prompt-card save button positioning.
- **Documentation and regression coverage**: document release synchronization and add coverage for fallback, prompt boundaries, Skills handling, and UI-related source contracts.

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