## 2.6.2

- 身体契约新增 comfort：吃得好、胃里舒服时心情会好一点，吃辣吃油胃胀时心情会差一点；天气也通过 valence 影响底色。

## 2.6.1

- 语义评估升级：新增“反讽”意图（字面夸奖、语气挖苦时不再当夸奖），并让模型给出不好意思/愧疚/得意/被冷落的强度，直接写入社会情绪。

## 2.6.0

- 新增社会情绪：不好意思、愧疚、得意、被冷落，各自有半衰期，会写进回复提示的 [社会] 块；被夸会得意，被调侃会不好意思，被说“别理你”会觉得被冷落。
- 新增情绪调节：生气/低落时有一部分会被压住（held），不会立刻全写在脸上；反复被同一个人惹会反刍（rumination），愤怒衰减变慢、语气变冷。
- 评估保留原有的语义评估（LLM），新加的社会情绪走规则，两者叠加。

## 2.5.1

- 慢心情层：全局心情的衰减放慢到 8 倍（半衰期约 6-16 小时），一句话的余味能留到第二天；经前会给愤怒/悲伤设一个底线，PMS 过去后自然回落。

## 2.5.0

- 性格底色不再按哈希瞎编：优先从人格设定里的用词提取（活泼/冷淡/敏感等），同时后台让模型按人设判定一次六种基础情绪的底线，按 prompt 哈希缓存到 temperament.json；改了人设会重新判定。
- 关键词只是首轮兜底，模型判定成功后覆盖。

## 2.4.0

- 情绪有性格底色了：按人格稳定生成一组基础情绪（有人天生乐一点、有人更容易恼），不再所有人从 0 开始。
- 回复风格由情绪决定，而不是由决策 AI 决定：生气时句子短、语气冲；低落时话少、回得慢；经前耐心薄。新增 [状态] 提示块。
- 决策 AI 只做 yes/no：去掉“回短一点”这类它管不到的指令，只保留“更倾向不回/少接话”。

## 2.3.1

- 决策 AI 也开始读身体：经前（PMS）会倾向回短一点或这次先不回，经期倾向少接话；只在群聊/需要判断时生效，私聊直接回复的逻辑不变。
- 身体上下文新增 PMS、经期、性欲、湿润度，判断时更接近“她今天是什么状态”。

## 2.3.0

- 情绪开始受身体周期影响：经前烦躁、敏感、易委屈和经期低落会折算成愤怒/悲伤/不安的底色；同样的事件，经前更容易被点着，也更容易委屈。
- 不同人格感受不同：按 emotion_sensitivity 缩放，已经生气时身体的烦躁会加倍；不是所有角色在经期都一个样。

## 2.2.2

- 不再因为某条消息决定“先去自慰”：移除 body_action=release 的指令、请求文件和恢复回复的分支。自慰由身体插件按自己的欲望和节奏触发；人格只负责在身体说她忙时安静。
- 身体上下文里只保留性欲/湿润度和一句“不用谁提醒，也不用解释”，不再引导人格在聊天里选择 release。

## 📝 更新日志

## 2.3.10

- Treat a punctuation-only afterthought (such as “…”) as silence. It used to pass the “沉默” check and send an empty ellipsis bubble.

## 2.3.9

- Simplify the afterthought prompt. Remove the parenthetical self-reference and the stacked anti-self-dialogue rules; state only that the line is the persona’s own last message and that a follow-up continues it.

## 2.3.8

- Make the afterthought a continuation of the persona’s own turn, not a reply to its own main message. The prompt now labels the main text as the persona’s own sent line and forbids answering it or self-dialogue.

## 2.3.7

- Refresh the relationship “last change” cause on every conflict, praise or apology. It previously used `cause or new_text`, so after the first significant event the displayed cause stayed frozen even while counters and timestamps moved.

## 2.3.6

- Give the afterthought the same background block as the main reply: notebook/jargon, curiosity knowledge, emotion/relationship and life context. Remove the extra “don’t pretend you forgot” instructions; the context itself now carries the facts.

## 2.3.5

- Give the afterthought call the same long-term memory retrieval as the main reply. Recent curiosity facts alone could not cover older conversations; the follow-up now retrieves the memory plugin result and is told not to pretend it forgot known facts.

## 2.3.4

- Do not trigger proactive speech on a media-only turn. A sticker or picture at the tail of the buffer is not a conversation hook, even if earlier text exists.

## 2.3.3

- Stop teaching the model to write `queue_meme` in the prompt text; tools are offered structurally. Strip any remaining `&&` tool markup generically and clear residual markers, instead of enumerating each variant.

## 2.3.2

- Strip inline `&& queue_meme:` / `&& meme:` markup from outgoing text. Give the afterthought call recent curiosity facts and forbid asking about a date or value it already knows; if unsure, it stays silent.

## 2.3.1

- Remember which context caused a change in the shared person attitude. Each relation keeps its latest cause per group/private context, the reply prompt mentions where it happened, and the control page labels the cause with its context.

## 2.3.0

- Unify long-term attitudes toward a person across group and private chats. The relation row is shared by user id, while short-term mood remains per conversation and group attitude remains per group. The control page shows one card with “群聊 + 私聊” when both contexts exist.

## 2.2.6

- Show a recent private-chat mood even when it has decayed to calm. The control page no longer looks as if private emotion does not exist just because the current state is below the active-mood threshold.

## 2.2.5

- Remove the extra “don’t force the topic back” instruction from the afterthought prompt. Allowing a natural topic shift already covers it; spelling it out only adds noise.

## 2.2.4

- Remove the “don’t jump to an unrelated topic” restriction from the afterthought prompt. A natural topic shift is allowed; the line only needs to add something new rather than repeat the main reply.

## 2.2.3

- Give the afterthought call the same reply-boundary hint as the main reply, and make it stricter: only a new fact, correction or genuinely unfinished half-sentence is allowed; repeating the stance or jumping topics must be silent.

## 2.2.2

- Reject follow-up lines that only paraphrase the main reply. The duplicate check now ignores leading fillers and uses fuzzy similarity, so “B服不碰” followed by “B服懒得碰” is dropped instead of sent as a second bubble.

## 2.2.1

- Move proactive reply/@ markers out of the message text and into structured JSON fields. A user typing “（回复别人）” in their own sentence can no longer be mistaken for platform metadata.

## 2.2.0

- Mark proactive buffer lines that structurally reply to someone or @ someone. The proactive prompt now treats those as another conversation chain, not as a fresh hook for the whole group.

## 2.1.9

- Give proactive speech the same turn-awareness. Buffered lines now carry their chat-record clock, and the proactive prompt is told that short reactions are usually answers to someone else, not invitations to the whole group.

## 2.1.8

- Carry the previous textual speaker and their chat-record time into the reply prompt too, not only the decision AI. For ambiguous group messages the reply model now sees the same candidate and age before it starts writing.

## 2.1.7

- Put the previous textual speaker’s chat-record clock time into the decision context (for example `2026-11-21 18:48:47`), alongside the elapsed gap. This is the time shown in the transcript, not just “a few seconds ago”.

## 2.1.6

- Give the previous textual speaker an age (“约 8 秒前”, “约 4 分钟前”). A nearby message is a stronger candidate; an old or topic-shifted one is weaker. Time is context, not proof of the addressee.

## 2.1.5

- Do not treat the immediately preceding event as the addressee. Pokes, images and stickers are skipped; the last textual speaker is only a candidate, not a default owner of “你/你的”.

## 2.1.4

- Pass the previous non-bot speaker into the decision and reply context. When a group message has no @, no reply and no bot name, an ambiguous “你/你的” is read against the immediately preceding speaker instead of defaulting to the bot.

## 2.1.3

- Treat “你的X” as an address to the owner of X, not to the bot, unless the platform or the bot’s name says otherwise. Add a concrete group-chat example and a reply-side pointer-confirmation hint.

## 2.1.2

- Add a pronoun-ownership hint for open group messages that use “你” without addressing the bot. The model is told not to assume every “你” means itself; it may answer as an observer when the target is unclear.

## 2.1.1

- Strip trailing bare word tags such as `&&angry` (and `&&angry &&`) from outgoing replies. These are model-side labels, not part of the message.

## 2.1.0

- **Fix private direct regression**: a high body-desire/wetness state no longer lets the group-style decision AI veto ordinary private messages. In `private direct` mode, if the persona did not explicitly choose `body_action=release` and is not in an away window, the reply is restored. Only an explicit release choice or an active away window suppresses the private reply.

## 2.0.99

- **Body-aware private decisions**: read the body plugin's `vitals.json` before a private reply. High desire or wetness no longer auto-replies through `private direct`; the decision AI gets a short `[身体]` background line and can still choose to reply.
- **Explicit body choice**: the structured decision adds `body_action = none | release`. Only when the persona explicitly chooses to skip the reply and deal with the urge does it write `body_request.json`; the body plugin applies the release and sets its away window. High desire is a fact, not a command.
- **No hard silence default**: the body plugin's standalone `ENABLE_AWAY` remains off; persona_presence owns the reply/no-reply decision.

## 2.0.98

- Strip trailing `&&<uuid>` and `&&call_...` tool-call markers from outgoing text. Some providers append these internal ids after a reply; they are not part of the message.

## 2.0.97

- Make image curation selective. Favorites now start at 0.80, screenshots and promotional pages are capped and never favorited, and “might be useful as a meme” no longer counts as “I want to keep this.” The existing archive was re-curated under the same rules.

## 2.0.96

- Separate “last interaction” from “last attitude change” in the relationship cards. The cause only changes on significant events; the card now shows the actual last-talk time and dates the cause, so an old but meaningful event is no longer mistaken for the latest conversation.

## 2.0.95

- Keep the activity bridge and the per-persona emotion state together in the release line: music, Bilibili and Qzone activity feed the persona-wide background mood, while chat emotion remains scoped per person and per conversation.

## 2.0.94

- Fix the emotion module failing to initialize after the per-persona chat-state migration: the image-archive name backfill now walks the nested persona → user structure instead of treating it as a flat user map.

## 2.0.93

- Add the companion-activity bridge. New music listening, Bilibili watch/share records and QQ-space actions are tailed from the other plugins’ files and folded into the persona-wide background mood. The bridge records a cursor on first sight, so old history is not replayed; a control-page section shows the background mood and its latest cause.

## 2.0.95

- 人格名不再跨人格残留：切换人格后，上一个名字不会继续参与夸奖识别；人格正文没有写名字时退回只认“你”。

## 2.0.94

- 情绪按会话人格分开存储：同一会话切换人格不会再串情绪；旧版 `emotion.json` 自动迁移到 legacy 桶，不丢数据。
- 新增人格级“背景心情”：该人格所有会话的事件以 0.5 系数累积、三倍半衰期衰减；当前用户没有事件心情时用 `[心情]` 补上。
- 去掉写死的“璃月”：人格名从人格正文读取，读不到时不再默认成璃月，群聊里只有被点名或 @ 才算对她说。
- 发布 `mood.json` 给身体插件：低落/生气压低食欲与欲望，恐惧/生气轻微拉长卵泡期。

## 2.0.93

- 情绪资料片读取身体插件的 `vitals.json`，把饥饿、睡眠、疼痛、清洁折进六种基础情绪：紧张抬高恼/不安，负向底色抬高低落，身体负担压低开心，脏抬高嫌。
- 情绪块前新增一行 `[身体]`；没有事件原因时，身体描述也可以成为情绪的原因。
- 新增 `emotion_body_link` 开关（默认开）；身体插件不存在或文件缺失时静默跳过。

## 2.0.92

- Stop showing the neutral relationship midpoint as “亲近 50”. Warmth is a −1..+1 scale, so 0 is neutral; the card now shows +0.00 / 中性 (or 疏远) with a −1 · 0 · +1 scale line.

## 2.0.91

- Make praise relationship-aware. From someone with high resentment or a bottomed-out relationship, a compliment is treated as coaxing or mockery: no joy gain, a little anger/disgust. Mild resentment reduces the gain; warmth increases it. The same modifiers apply to semantic “夸奖” results.

## 2.0.90

- Replace the “what is your first reaction” interview with a short inner-voice pattern: a few concrete examples of someone speaking to her, then her one-line thought and the tag. The model now records a “thought” and uses it as the cause, rather than producing an emotional report.

## 2.0.89

- Rewrite the emotion appraisal prompt as an inner reaction (“你是{name}……你看到对方的话，第一反应是什么”) instead of asking a neutral classifier to judge how a third person feels. The persona and relationship context are included naturally, and the test harness now keeps the appraisal task in the same event loop.

## 2.0.88

- Make semantic emotion appraisal happen in the persona's own frame instead of as a neutral intent classifier. The appraiser now receives the persona prompt and the current relationship, and judges how the message lands on her.

## 2.0.87

- Re-appraise group praise that might be teasing an AI. The semantic prompt now distinguishes sincere praise from “调侃” (testing/playing with the character); a teasing “夸奖” reverses the joy gain and adds a little irritation instead of being taken literally.

## 2.0.86

- Require praise to be aimed at the persona (“你/璃月” plus the compliment, or “喜欢你/璃月”). “好看的百合” and “喜欢被骚扰” no longer count as praise. Also stop a later negative event from overwriting the cause of a still-dominant earlier joy; the UI now says “最近一次” rather than implying direct causation.

## 2.0.85

- Remove the broad “never comment on an @someone-else message” boundary. The real issue was that any @ was being passed to DecisionAI as if the bot had been addressed. Now only @bot or a reply to the bot counts as direct address; messages aimed at another user remain their conversation, but the persona can still choose a natural side comment.

## 2.0.84

- Do not join a group conversation that explicitly @s someone else. Side comments are still allowed when the bot is also mentioned, mentioned by name through a trigger, @all is used, or the message replies to the bot.

## 2.0.83

- Do not let the lazy image-question override fire when the current message mentions someone else and does not mention the bot. A message like “@小略3 你不是要看图吗” is not a request for the bot to inspect an image.

## 2.0.82

- Normalize the emotion radar to the current mood composition, so the filled shape reflects shares rather than tiny absolute intensities. The separate intensity figure still shows the dominant emotion's absolute strength.

## 2.0.81

- Backfill missing display names for older emotion records from the local image archive, so user ids become readable names on the dashboard.

## 2.0.80

- Add percent units to radar axis labels and keep the labels just outside the filled area.

## 2.0.79

- Make the emotion radar an actual filled area, show axis labels with percentages, and add a short explanation of what the axes mean.

## 2.0.78

- Improve emotion dashboard readability: rename the relationship metric to “不满”, label cause text as “最近一次”, soften the hero tint, and enlarge radar axis labels (hidden on tiny cards where bars carry the labels).

## 2.0.77

- Refine emotion visualization: explicit KPI labels, emotion composition bars, stronger hero details, person names instead of group ids as card titles, per-person negative/positive event counts, and per-group conflict counts.

## 2.0.76

- Apply the dominant emotion colour to the emotion hero background, so the visual accent follows the current mood.

## 2.0.75

- Redesign the emotion view: dominant-mood hero with a six-axis radar, compact emotion chips and intensity, per-mood radar cards, relationship rings, and group withdrawal gauges.

## 2.0.74

- Add an emotion visualization view to the plugin control page: current six-primary moods, long-lived attitudes toward users, and group comfort/withdrawal, with bars and cause text.

## 2.0.73

- Show the emotion DLC in the plugin control page: its card in the DLC grid, overview count, group summary, and config tab. Bump the static app cache so the page picks it up after reload.

## 2.0.72

- Add optional semantic emotion appraisal for ambiguous direct messages. It runs asynchronously, at most once per sender per two minutes, only when keyword rules are inconclusive; obvious keywords skip the model. Timeout or failure falls back to local rules. The appraiser judges intent and intensity, not the reply text.

## 2.0.71

- Reduce emotion false positives: “装个软件” no longer counts as an insult, “不喜欢” no longer counts as praise, and one turn gets one dominant appraisal instead of stacking every keyword. Fix newly-created relationship records not being persisted.

## 2.0.70

- Add long-lived relationship and group attitudes to the emotion DLC. Repeated direct insults can bottom out a relationship; apologies no longer restore it automatically. Group conflict raises withdrawal and suppresses proactive speech, without auto-leaving the group.

## 2.0.69

- Observe emotion on every incoming turn, including private chat and direct @ messages, not only unaddressed group messages.

## 2.0.68

- Add the emotion DLC. It keeps a six-primary emotional vector per sender per conversation, appraises only direct messages, decays over time, and injects a short `[此刻]` inner-state note with the cause. The note carries no instruction about how to perform the emotion.

## 2.0.67

- Strip inline `&& meme:... &&` (and unclosed variants) from outgoing replies; this custom markup was never a real tool call and must not reach the chat.

## 2.0.66

- Raise the delayed-quote threshold from three newer messages to four.

## 2.0.65

- Raise the overtaken-quote threshold to three newer messages, and stop proactive replies from quoting the immediately previous text message. This reduces constant quote decorations while keeping quotes for genuinely displaced replies.

## 2.0.64

- Quote the original anchor when a reply is delayed because newer group messages arrived, not only when it exceeds the slow-reply threshold. Follow-up bubbles remain unquoted.

## 2.0.63

- Clarify proactive quoting when a reply mixes an older text line with the latest image: quote the older text, not only the image. Quoting the image remains reserved for replies that are purely about the image itself.

## 2.0.62

- Extend the pronoun ownership rule: if the same sender just @-mentioned or quoted someone else, a following “你…” is normally still aimed at that person, not the bot, unless this message explicitly @s/replies to the bot or uses its name.

## 2.0.61

- Remove spaces before an ellipsis when the preceding character is Chinese or Chinese punctuation, so “想干什么 ...有点烦了” becomes “想干什么...有点烦了”.

## 2.0.60

- The music memory block now states facts only (song, time, recipient, count) and drops the meta instruction about remembering naturally.

## 2.0.59

- Music topics now recall the music plugin's recommendation memory (who, when, how many times) before replying, so a song recommended a few days ago can be remembered instead of repeated as new.

## 2.0.58

- Track song titles mentioned in the last seven days per chat and add them to the repeat guard, so a song recommended a few days ago is not pushed again as if it were new.

## 2.0.57

- The repeat guard now lists song titles from the last few replies and asks the persona not to push the same song again unless it is actually necessary.

## 2.0.56

- Proactive context now records a shared music card as `[音乐卡片：歌名 - 歌手]` instead of a generic media placeholder, so a later image reaction can see that a song was shared just before it.

## 2.0.55

- Proactive speech now quotes the latest image/emote itself when the trigger is media, instead of falling back to the last text message id. The prompt also asks the model to anchor the newest media and use `[图片]` as the excerpt.

## 2.0.54

- Tightened DecisionAI ownership rules for the pronoun “你/你的”: without an @ or reply to the bot, when the previous turn is another user talking to someone else, it should not become `target=bot` just because the word “你” appears.

## 2.0.53

- Strip leaked tool tags such as `<queue_meme>...</queue_meme>` and unclosed `<queue_meme>...` from outgoing text, so a model that writes tool markup inline cannot send it to the chat.

## 2.0.52

- Collapse blank lines inside a reply text component into a single space before sending, so a text-plus-image reply does not arrive as “...所以...

你才发现”.

## 2.0.51

- Exclude laughter and single-character repetition from the duplicate-collapse guard, so “哈哈哈哈……” is not folded into a shorter laugh. Only repeated blocks with at least three distinct characters are collapsed.

## 2.0.50

- Collapse an immediately repeated phrase in the outgoing reply, and drop a queued follow-up when its normalized text is already contained in the main reply. This removes the duplicate main bubble and the duplicate follow-up sent after it.

## 2.0.49

- Proactive quotes now require `quote_excerpt`: the model copies a short exact snippet from the chosen line. The code matches that text against the recent lines and uses the matching message id, so a wrong anchor number can no longer attach an unrelated quote.

## 2.0.48

- Validate proactive quote anchors against the generated line. If the model picks an older message that shares no meaningful token, the quote falls back to the current message instead of attaching an unrelated quote.

## 2.0.47

- Slang validation now gives the model 800 tokens per step instead of 260/400, and logs each candidate verdict (is_jargon, confidence, scope) or the point where parsing failed. This makes it visible whether recent candidates are being rejected as ordinary words or the model is returning unparseable JSON.

## 2.0.46

- Curiosity summaries now use a larger output budget (1200, then 2000) and retry when the model returns under 80 characters, instead of accepting a one-line fragment as a finished note.

## 2.0.45

- Curiosity summaries now ask for a complete sentence within the length budget and clip at the last sentence boundary instead of cutting mid-sentence. The default limit remains 400 characters.

## 2.0.44

- Normalise ellipses on every text component in `on_decorating_result`, including replies that bypass `on_llm_response` through the agent tool loop. Mixed `……` and `...` in one reply no longer leaks through.

## 2.0.43

- Meme annotations inside an afterthought are now parsed anywhere in the text, not only when they occupy the whole message. “...反着念也还是个傻子...（表情包：无语）” becomes a real meme image plus the remaining caption.

## 2.0.42

- Afterthought meme parsing now accepts an annotation followed by a caption, e.g. “（表情包：无语）
...去听歌了”. It sends the real meme image plus the caption instead of leaking the annotation as text.

## 2.0.41

- Fixed marketplace review findings: all logging now imports `logger` from `astrbot.api`; the per-message scene note is appended as a temporary `TextPart` in `extra_user_content_parts` instead of mutating `system_prompt`, preserving prefix-cache hits.

## 2.0.40

- Proactive speech no longer interjects when DecisionAI marked the current message as targeting another user (`target=other/unclear`). The previous gate only checked that the base declined, so a message @-ing a third party could still trigger an ambient roast.

## 2.0.39

- Proactive turns can now call `web_lookup` through a narrow tool loop. The tool description stays curiosity-only; code caps it at two searches per proactive turn and keeps the latest image input. Without a Tavily key, proactive falls back to the normal text-only call.

## 2.0.38

- Reframed `web_lookup` as curiosity rather than an answer service: the description now says she may look things up when she personally wants to know, and may skip it. Added a 180-second per-chat cooldown so a single chat cannot turn into repeated searches.

## 2.0.37

- Added an explicit no-repeat rule to the afterthought prompt: do not repeat, rewrite, or summarize the main reply. The string guard remains, but the model should now choose silence instead of producing a paraphrase.

## 2.0.36

- Reject afterthoughts that merely repeat the main reply. The guard now compares punctuation-stripped text and skips when one is contained in the other, so a main “...知道... E站客户端...你问这个干嘛” no longer gets a second “...知道...问这干嘛...”.

## 2.0.35

- Limited proactive vision input to one image, and only when the latest group message is itself media. This avoids pairing an old image with a newer text message and letting the model conflate two unrelated things.

## 2.0.34

- Proactive generation now passes up to two recent group images directly to the chat model as `image_urls` instead of only recording `[图片或表情]`. If the provider rejects image input, it retries text-only once instead of failing the proactive turn.

## 2.0.33

- Proactive messages now carry an explicit anchor. The prompt numbers the last eight group messages, the model outputs the index it is reacting to (or 0 for no target), and the base maps it to that message's id for the Reply quote. The old “last text” heuristic remains only as a compatibility fallback.

## 2.0.32

- Proactive lines now quote the last text message when the newest message is media (image/sticker), so a reaction to a sentence several messages back no longer looks attached to the latest picture.

## 2.0.31

- Actually applied the slow-reply quote in `on_decorating_result`. The 2.0.30 release only carried the version/changelog edit; the runtime path still lacked the quote code, so 140–195 second replies were still sent without it.

## 2.0.30

- Slow-reply quotation now runs in `on_decorating_result`, which is the path used by agent/tool-loop replies. Previously the quote was added only in the non-tool generator path, so 140–195 second group replies were sent without quoting the source message.

## 2.0.29

- Split multi-component text replies into separate bubbles. When a main result contains more than one non-empty Plain segment, the first stays in the main message and the rest are queued as delayed follow-ups, instead of appearing as one message with internal line breaks.

## 2.0.28

- Afterthought is skipped when the main reply already contains an image/meme, so a meme reply no longer gets a second, unrelated meme bubble. The prompt also now says most replies are already finished; only a genuinely unfinished half-line should be added.

## 2.0.27

- Removed the 60-character main-reply gate and the 180-second afterthought cooldown. Afterthought is now offered after every eligible main reply, and the persona decides whether a second line exists; only structural guards remain (main text, no already-queued followups, newer user message during generation).

## 2.0.26

- Removed the afterthought hard length cap entirely. Length is left to the persona; the plugin no longer silently drops a second bubble because it exceeded an arbitrary character count.

## 2.0.25

- Raised the afterthought hard cap from 30 to 100 characters (configurable up to 300) because verbose personas were having every second bubble dropped. The cap is a runaway guard, not a style rule; prompting still leaves length to the persona.

## 2.0.24

- Trimmed the afterthought task text to the useful parts. Removed the staged “phone in hand” line and the redundant length/no-question/no-explanation rules; the persona and the “only supplement/correct/roast this” line already carry that.

## 2.0.23

- Afterthought now reuses the exact context prompt from the main reply (last 6000 chars) instead of rebuilding a short last-8 history. It keeps the persona and appends the just-sent line plus the afterthought instruction, with history fallback if the main context is unavailable.

## 2.0.22

- Gave the afterthought pass the same recent conversation the main reply saw: last eight speaker-labelled messages plus the just-sent main line. Kept the full persona; the instruction now follows that context instead of replacing it.

## 2.0.21

- Restored the full persona block in the afterthought prompt and removed the extra “don’t start a new topic / don’t mention unseen works” lines. The preceding main reply and user message are enough; the afterthought should sound like the same person, not a stripped-down continuation.

## 2.0.20

- Afterthought meme text like “（表情包：冷笑猫）” is now converted into a real meme image through the semantic bridge instead of being sent literally as a parenthetical message. If the bridge is off or no image matches, the afterthought is dropped rather than leaking the annotation.

## 2.0.19

- Constrained the afterthought pass to the just-sent exchange. It no longer receives the full persona block or permission to “跑题”, which caused a music question to be followed by an unrelated “…蓝色星原…快了”. It now only supplements, corrects, or comments on the line it just sent.

## 2.0.18

- Added a real afterthought pass: after a short main reply, the plugin waits briefly, asks the persona once whether a short follow-up naturally follows, and sends it as a separate bubble. It is a second model call, not queue_followup. It stays silent when nothing comes, is discarded if a newer user message arrives, and has a per-chat cooldown.

## 2.0.17

- Group multi-bubble now actually works. `followup_scope=group/both` was previously ignored for groups because the enable flag was tied to the private generation mode; group turns now get the follow-up hint/tool, queued second bubbles, and natural newline/sentence splitting independently. Live scope set to `both`.

## 2.0.16

- Allowed a natural exception in the group decision prompt: a concrete public topic with a real hook can be answered without being @-mentioned, if the persona genuinely wants to react. This is a judgment rule, not a probability override; `participation=none`, one-to-one chats, noise, and repeated topics still stay silent.

## 2.0.15

- Reworked built-in tool descriptions into capability statements instead of imperative instructions. `web_lookup`, `queue_meme`, and the image archive tools now describe what exists (“作品、角色…的资料检索”, “你图库里的一张图…”) rather than telling the persona to search/send/queue. This reduces the assistant-like pull of the always-injected tool schema.

## 2.0.14

- 回复链路接入音乐插件的工具（listen_music / listen_song / like_song / save_to_playlist），人格可以在聊天中自己决定去听歌，而不是等指令。

## 2.0.13

- Fixed the proactive meme instruction: “只发一张表情” contradicted “可以带一句很短的话” and gave the model no reason to choose a meme over text. It now says “不想写字、或者只有情绪的时候，用一张表情冒泡”, with an optional short caption.

## 2.0.12

- Reworded `queue_followup` as a neutral capability: “在当前回复后追加一句补充，等主回复发出后再发；用不用由你判断。” This removes the action-like “短话” wording while keeping the send timing explicit.

## 2.0.11

- Removed the stray space after ellipses before Chinese text. Model output like “...还行吧... 脚画得不错” now becomes “...还行吧...脚画得不错”, even when the spacer was introduced on a single line rather than by newline collapse.

## 2.0.10

- Queued follow-ups are now also scheduled from `on_decorating_result`, which is the hook that actually runs for agent tool-loop replies. The previous scheduling point was only reached by the non-tool generation path, so `queue_followup` could log “已排队” and still never deliver a second bubble.

## 2.0.9

- Collapse short separated duplicates such as “...行吧...行吧” into one phrase. The existing whole-sentence guard intentionally skipped short emphasis, but dotted repeats are usually a generation stutter.

## 2.0.8

- Added a second follow-up backup keyed by chat id, plus send-time diagnostics. If the processing-id key or event extra is lost during the tool loop, the queued second bubble can still be recovered and scheduled.

## 2.0.7

- Fixed queued follow-up delivery: the follow-up task is now scheduled before the reply generator yields, and the queued texts are backed up by processing id in case the event extra is cleared. Previously `queue_followup` logged “已排队” but the second bubble was never sent.

## 2.0.6

- Clarified the multi-bubble hint: “另起一行写，那会作为下一条短消息发出去” instead of “换行”, so the model understands the line break is a message boundary. Removed the meta “不要为了凑气泡硬拆” phrasing.

## 2.0.5

- Rewrote the tangent hint as a permission rather than a checklist: “想补一句就换行接着写；跑题、自言自语、突然想起别的事都可以。不想补就停。” Removed the example phrases that nudged the model into manufacturing a second bubble.

## 2.0.4

- Private multi-bubble now treats newlines as natural bubble boundaries as well as sentence punctuation, and allows up to two follow-up bubbles (three total). The rhythm hint explicitly permits a short tangent/status line when it arises naturally, while still forbidding forced splitting.

## 2.0.3

- Raised the slow-reply quotation threshold from 30 to 45 seconds for the live configuration, which better matches normal fast replies while still catching 429/retry delays.

## 2.0.2

- Added optional slow-reply quotation (`quote_slow_reply`, default off; live on). When a reply takes at least `quote_slow_reply_threshold_seconds` (default 30), it is sent with a Reply component pointing at the message that triggered it, so a late reply reads as a reply rather than an out-of-nowhere interjection.

## 2.0.1

- Applied the configured ellipsis style to direct feature proposal sends and follow-up bubbles. Those paths bypass `on_llm_response`, so proactive/react and queued second bubbles previously kept `……` or `......` even when `reply_ellipsis_style=ascii`.

## 2.0.0

- **Proactive voice changed from self-reporting to group reaction**: the proactive pack now sees the last few group messages and chooses `react` (short roast/reaction), `meme` (semantic meme bubble, optionally with a short caption), or `silent`. It no longer defaults to narrating its own imaginary day.
- Added meme proposals to the base delivery path: `react` sends text, `meme` selects through the same semantic bridge and sends an image (with optional caption), and `silent` sends nothing.
- Replaced the old interest/experience prompt blocks with persona + recent group context; repeated-topic and grounding rules remain.

## 1.9.9

- Meme selection now keeps the last six used image basenames per conversation and skips them when another valid semantic candidate exists. This reduces the feeling that the same few memes are always chosen, without falling back to random category picks.

## 1.9.8

- Rewrote the avatar tool description as a plain capability statement instead of an action prompt (“看看…并得到…”), so the persona decides whether looking at an avatar is appropriate rather than being nudged into it whenever the topic appears.

## 1.9.7

- Avatar tool failure text is now honest: if the vision call returns nothing, it says it did not get a clear avatar image instead of claiming it saw one.

## 1.9.6

- `see_avatar` now accepts `self` (or “你/自己”) and explicitly allows the bot's own QQ id, so the persona can look at its own avatar without needing the user to mention it.

## 1.9.5

- Added a `see_avatar` tool. It fetches the QQ avatar of the current speaker or an @-mentioned user, describes it through the vision provider, and caches the description for six hours. Arbitrary QQ ids outside the current conversation are rejected.

## 1.9.4

- Added an outgoing punctuation style option (`reply_ellipsis_style`): keep the model's pause punctuation, normalise it to three ASCII dots, or normalise it to a Chinese ellipsis. This only affects delivered text, not the prompt or saved context. Live config is set to `ascii`.

## 1.9.3

- Near-duplicate reply collapse: punctuation and ellipsis variants are normalized before checking whether the text is the same phrase twice. This catches cases like “……躺着……等蓝原……躺着…等蓝原” that the exact-half check missed.

## 1.9.2

- Rewrote the private/group scene note as a terse factual aside (“（私聊。对面只有对方一个人。）” / “（群聊。刚才说话的是「某某」，周围还有人。）”) instead of imperative assistant-style instructions.

## 1.9.1

- Moved the private/group scene label from the final user-message suffix into the LLM system prompt. The persona now receives the conversation mode as part of the system context, so it is much harder for the generic “online chat” persona wording to make a private chat feel like a group.

## 1.9.0

- **Private image carryover**: when a private conversation has image-only messages followed by a separate text message, the recent private images are attached to the text turn. This fixes the “没图” reply when images were sent separately rather than merged.
- **Proactive quality**: proactive generation now sees recent proposed/skipped lines as well as sent lines, so near-duplicate attempts are rejected before delivery; the prompt also forbids inventing scenes and tells the persona to avoid reusing the same interest or sentence pattern. Grounding is limited to the supplied real state, recent knowledge and known interests.

## 1.8.9

- Private multi-bubble replies no longer depend only on the model calling `queue_followup`. When a text reply already contains multiple natural sentence units, the first sentence is sent as the main message and the rest are delivered as follow-up bubbles after the configured delay. Single-sentence replies, ellipsis-heavy lines and image-bearing replies stay as one message.

## 1.8.8

- Fixed proactive-speech lull measurement. The current message was recorded before the gate check, so `now - last_seen` was always near zero and the 90-second lull could never pass. The group state now keeps the quiet gap that preceded the latest message and the gate uses that value.

## 1.8.7

- Fixed the configurable slang threshold wiring: `notebook_jargon_min_count` is now actually read from config (default 4) and used by the candidate filter, and the field is present in the config schema.

## 1.8.6

- Made the slang frequency threshold configurable as `notebook_jargon_min_count`, default 4. The self-learning plugin uses 5, but that plugin keeps continuous per-group statistics; our 30-message batch would miss too much at 5, while 3 let ordinary chat words through.

## 1.8.5

- Group-slang learning now uses a statistical pre-filter before the LLM: character n-grams must repeat at least three times, and ordinary vocabulary in jieba's dictionary is excluded. This follows the self-learning plugin's frequency/standard-word filtering idea.
- The three-way inference now returns an explicit `is_jargon` verdict. The learner adds a term only when context and word-only meanings actually diverge, so ordinary nouns repeated in chat no longer become slang.
- The extraction prompt explicitly excludes group roleplay, temporary titles, nicknames and jokes, which should not be learned as stable group vocabulary.

## 1.8.4

- Curiosity reflection now classifies candidates as `web` or `group`. Group roleplay, titles, nicknames, in-jokes and ongoing lore are explicitly excluded from web lookup, so the persona no longer researches things that only exist in the current group's conversation.
- Cleaned the two existing bogus group-lore knowledge entries so they no longer get injected as facts.

## 1.8.3

- Curiosity reflection now receives the current group's known slang and is told not to treat those terms as general web-searchable knowledge. If a reflected topic exactly matches a stored group-slang headword, it is skipped instead of being sent to Tavily.

## 1.8.2

- Rewrote the image-library memory block as first-person declarative memory (“我保存过的图片 / 这些是我自己收进图库的”) instead of imperative guidance. It now reads like something the persona remembers, not an assistant instruction.

## 1.8.1

- Added an explicit per-turn scene block. Private chats are labelled as one-to-one and told not to treat the user as a group; group chats are labelled as multi-person with the current speaker named. This prevents the persona's generic “online chat” wording from blurring the two contexts.

## 1.8.0

- **Image-library self-memory**: when a turn includes images or mentions the image library, the persona now receives a short block with her own saved images, her scores, tags and descriptions. It explicitly frames them as her own collection rather than images forced on her by the user.
- The block also separates “interest when saving” from “willingness to be spammed now”, so she can own the collection without pretending every repeat is welcome.
- Image tools now describe results as the persona's own saved images, and the page labels scores as “我的评分”.

## 1.7.9

- The image-card delete button is neutral by default and uses the accent colour on hover; red is reserved for the second-click confirmation state, making that state easier to notice.

## 1.7.8

- The image-card delete button now gets a red hover highlight, matching the visual feedback of the sendable toggle instead of staying visually inert on hover.

## 1.7.7

- Restored the wider sendable control while keeping the text delete button: the sendable toggle takes the available footer width and delete keeps a fixed natural width, so both actions stay visible and balanced.

## 1.7.6

- Image-card actions are now compact: the sendable toggle only takes its content width, and delete is a normal text button again instead of a square icon. Actions stay balanced at the two ends of the card footer.

## 1.7.5

- Refined image-card actions: the sendable control is now a compact status toggle with a dot, and delete is a square icon button. The action row sits under a divider instead of visually competing with the card metadata, and delete confirmation changes the icon colour rather than replacing the label.

## 1.7.4

- Image cards now separate the score/size metadata from the action row, so the sendable button no longer covers file information; actions wrap instead of turning into vertical text.
- Added image-library filters: all, sendable, unmarked, favorite, and tags such as 表情包 / R18 / 截图.
- GIFs stay stored as GIFs. Cards still show a static thumbnail with a GIF badge; opening the lightbox loads the animated original.

## 1.7.3

- **Image library pagination**: 24 images per page (three rows at the usual desktop width), and the image pager is centered instead of pushed to the far edges. Card actions wrap cleanly with the sendable button.
- **Sensitive-image allowlist**: sensitive tags are still blocked by default. When `archive_sensitive_send_enabled` is on, only conversations whose group id or sender id appears in `archive_sensitive_allowlist` may receive them; an empty list allows nobody. Sensitive images can now be marked sendable, but delivery still checks the allowlist.

## 1.7.2

- **Repeated-reply collapse**: if the model emits the exact same whole sentence twice, the output stage now keeps one copy. The check requires a repeated unit of at least six characters so short emphasis like “哈哈” is untouched.

## 1.7.1

- **Image-library send permission**: archived images are no longer sendable by default. A new per-image `sendable` flag, exposed as a toggle in the image library page, is required before `send_saved_image` or `queue_saved_image` can use it.
- Added generic safety tags (`表情包`, `截图`, `聊天记录`, `二维码/证件`, `真人照片`, `R18/性暗示`, `血腥/暴力`) and a blocked-tag filter. Sensitive images cannot be marked sendable.
- Added `queue_saved_image(image_id)` so a permitted archive image can be merged with the final reply or sent alone when the final text is empty.

## 1.7.0

- **Optional meme bridge**: `queue_meme(query)` selects one specific image through meme_manager's semantic index and queues it for the current reply. The base merges it into the final message chain, so text+image stay together and an empty/punctuation-only reply becomes a pure meme. Off by default; private scope by default; falls back to no image when meme_manager or its index is unavailable.
- While the bridge is enabled, the old direct `send_meme`/`search_memes` tools are removed from the request so the model doesn't bypass the merge path.

## 1.6.6

- **Meme closing guard in the base plugin**: when `meme_manager` reports a direct `send_meme` send, a final reply made only of punctuation (for example `...` or `……`) is dropped. The fix lives in Persona Presence rather than the third-party meme plugin, so updating meme_manager does not erase it.

## 1.6.5

- Raised the private adaptive hard cap from 3500ms to 4000ms. The quiet threshold stays at 1200ms, so normal bursts still reply after 1.2s of silence; only sustained multi-message bursts can extend to 4s.

## 1.6.4

- **Adaptive private Smart window**: private chat now waits for 1200ms of silence after the last message, resetting on each new arrival, with a 3500ms hard cap. Group chat stays on the fixed 3s window. This replaces the fixed 2500ms private wait and keeps quick bursts merged without always paying the full delay.

## 1.6.3

- **Private Smart window shortened**: default `private_batch_wait_ms` lowered from 4500 to 2500. With `followup` mode the speculative draft is disabled, so the old window was exposed as pure latency; 2.5s still catches most same-thought bursts. `precomputed` users can raise it back toward 3000-4500.

## 1.6.2

- **Private generation mode**: replaced the separate follow-up toggle with one `private_generation_mode` choice. `precomputed` keeps the faster speculative draft and disables multi-bubble; `followup` disables the draft, uses the tool-enabled Agent path, and allows one queued supplement. The two can no longer be enabled at the same time.
- Fixed a jargon-log formatting error when candidates were tuples.

## 1.6.1

- **Follow-up activation fix**: private speculative drafts could bypass the tool-enabled Agent path, so `queue_followup` was rarely available. Speculative reply generation is now skipped while follow-ups are enabled, and the reply prompt gets a short rhythm hint. Added `[多气泡]` queue/send logs for testing.

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