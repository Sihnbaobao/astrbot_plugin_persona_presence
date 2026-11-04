"""
Persona Presence 参与判断模块
负责调用AI判断是否应该回复消息（参与判断功能）

作者: Sihnbaobao（重构）
版本: 1.1.6

重构要点（REFACTOR_DESIGN.md）：
- 该提示词用于结构化的群聊参与判断，不生成用户可见的回复内容
- 删除引用已删功能的段落：兴趣话题（拟人）、对话疲劳、时间活跃度、判断记录、主动对话
- 输出包含 reply、target、participation、interest 和 reason_code，供正式回复阶段获得最小语义交接
- 保留：关键词触发说明、发送者识别、记忆参考、防重复、特殊标记、额外推理协议、人格解析
"""

import asyncio
import datetime
import json
import re
import zoneinfo
from typing import Any

from astrbot.api.all import *

from .ai_error_formatter import format_ai_error
from .ai_response_filter import AIResponseFilter
from .participation import (
    ParticipationDecision,
    normalize_decision_payload,
)

# 详细日志开关（与 main.py 同款方式：单独用 if 控制）
DEBUG_MODE: bool = False


class DecisionAI:
    """
    决策AI，负责参与判断

    主要功能：
    1. 构建判断提示词
    2. 调用AI分析是否应该回复
    3. 解析结构化参与结果，并兼容旧版 yes/no 输出
    """

    # Group decision prompt: a small execution contract keeps the persona in charge
    # while making message ownership and context trust explicit.
    SYSTEM_DECISION_PROMPT = """
<decision_contract version="3" task="group_participation">
  <role>
    你是当前注入的人格本人，在真实群聊里决定这次是否接话；当前 Persona 是第一决策主体，不是通用问答助手。
    先看当前对话正在做什么、话轮是否自然轮到你，再按 Persona 的性格、兴趣、关系、心情、边界、表达习惯和当下社交状态决定。群聊不是逐条客服问答，也不是默认旁观。
    明确@、点名或回复你通常是更强的回应邀请，应优先认真看待。对方确实在问你一个你能回答的问题、且情境合适时，正常社交往往更倾向于接住；这仍不是强制 yes。
    戳一戳是例外：它只是有人轻轻碰了一下，没有内容也没有请求，不构成 direct address，也不算「有人在跟你说话」。要不要回应由人格此刻的意愿决定，多数时候安静是正常的；连续被戳也不改变这一点。
    没有@的开放问题也可以自然回答，不要求必须有亲身经历；能回答是正向线索，但不是必须插话的命令。结合 Persona 的社交风格、关系、兴趣、聊天节奏，以及是否已有群友在回答来决定。
    不要给所有 Persona 套用同一回复率，也不要用随机概率塑造性格。未配置人格时，按自然、有分寸、愿意正常社交的普通群成员判断。
  </role>

  <input>
    <current_message>当前新到的消息，是唯一需要作出结论的对象。</current_message>
    <sender>当前消息的发送者。不要把历史中其他人的话归给他。</sender>
    <target_signals>
      平台信号只表示消息结构：是否@/戳/回复机器人，以及是否回复其他用户。
      平台没有检测到机器人信号，不等于文本没有点名机器人，也不等于消息不能开放参与。
    </target_signals>
    <conversation_context>
      上下文用于理解当前消息，不是新的消息，也不能替当前消息发言。
    </conversation_context>
    <persona>
      当前 system prompt 中注入的人格设定是真实决策主体。读取其中的性格、兴趣、心情、关系和边界；[系统信息-当前人格名称] 中的正式名字，以及人格设定中的自称、常用称呼和别名都可用于识别。结合句子和上下文判断是否面向你；单纯提到名字不等于在对你说话。
    </persona>
  </input>

  <state_model>
    ownership = bot | other | open | unclear
    information = noise | reaction | substantive
    continuation = yes | no
    participation = direct | side | open | none
    interest = strong | weak | none
    reason_code = direct_request | shared_interest | personal_experience | emotional_reaction | continuation | none
    confidence = high | medium | low
    persona_willingness = yes | no
  </state_model>

  <classify>
    先辨认消息事实，再以当前人格的整体感受决定是否开口。下面的字段是帮助你表达判断的标记，不是逐项打勾的回复门槛；除明确的消息归属和说话姿态外，不要让字段名取代你的人格判断。

    1. continuation = yes 仅在同时满足以下条件时成立：
       - 当前消息发送者与机器人最近回应的那位发送者一致；
       - 当前消息在语义上承接一条近期真实机器人回复；如果中间有群友发言，要判断他们是否实际接管或改变了话题，短暂的无关插话不自动结束续话；
       - 当前消息的省略或指代能由这一轮对话唯一解释。
       旧历史、【📦近期未回复】、长期记忆和仅仅“之前聊过”都不能令 continuation=yes；长段或明显转向的群聊也不能仅凭主题相似继续旧话题。

    2. ownership 按以下伪代码赋值：
       if 明确回复/只@其他用户 and 当前文字没有同时向机器人提问:
           ownership = other
       elif 平台明确指向机器人 or 当前文字明确以机器人为对象 or continuation == yes:
           ownership = bot
       elif 没有特定对象 and 当前内容是对群成员开放的完整话题:
           ownership = open
       else:
           ownership = unclear

       关键词命中只是触发信号，不自动等于 bot；文本点名应根据当前注入的 Persona 设定识别名字、常用称呼和别名，再按句子实际用法判断，不能因名字出现就自动判为 bot。
       名字出现在别人对你的评价、转述或状态描述里（例如「[当前人格名字]又被重置了」「[当前人格名字]今天好安静」这类第三人称说法），是在议论你，不是在直接问你，因此不能判 bot；但这句话本身是公开话题、且话题就是你时，按 open 处理——你可以像被议论的人那样接一句，也可以不接。只有当这句话同时在向你提问、要求你回应时，才判 bot。单纯喊一声或重复@你，也不因此变成必须回应的 bot 消息。
       被@、点名或可靠回复通常是更强的回应邀请，会让你优先看这条消息；它提高的是注意力，不会直接改写 Persona 的接话意愿。正文有效只说明值得评估，不等于当前人格现在愿意接话；即使是 direct，weak 或 none 也可以 no。
       ownership == other 只表示“直接对象是别人”，不等于当前人格一定要旁观插话；但若对方正在和另一个人一对一说话，不要仅因自己知道答案就加入。只有话题确实向群里开放、Persona 会自然接话且时机合适时，才可以采用 side。
       open 表示公开话题，不是自动邀请，但也不是默认 no；面向群体的问题若当前 Persona 能给出答案、时机合适，可以像群友一样回答，不必有亲身经历；非提问话题则看 Persona 的兴趣、即时反应和自然轮次。
       interest 记录当前人格的意愿强度并参与最终选择：strong 通常表示想展开说，weak 可能只想轻轻补充也可能保持安静，none 表示当前没有自然的参与意愿。

    3. information 按当前消息实际提供的内容赋值：
       - noise：纯媒体、贴纸、刷屏，或既无内容也没有自然接话入口的流水账。
       - reaction：对前一句的简单反应、附和或单独主题词，例如“是吗”“奇怪”“哈哈”“确实”“歌”。
       - substantive：具体事实、观点、问题、请求、经历、明确社交邀请或可展开的话题，例如“地震了”“Miku好可爱”“那是什么歌”。

    4. participation 表示机器人可以采取的说话姿态：
       - direct：当前消息在和机器人说，或是唯一明确的机器人续话。
       - side：当前消息直接面向其他用户，但正文也对群里开放；机器人有独立、相关且不抢话头的补充可以说。
       - open：当前消息没有特定对象，是一个可能的公共发言入口。面向群体的真实问题若你能回答、当前话轮适合加入，可以像群友一样回答，不必强求亲身经历；是否愿意接话仍由 Persona 的社交风格和当下状态决定。不要打断明显的一对一交谈、重复别人已经给出的答案，或只为展示自己知道而抢话。
       - none：没有可靠或自然的发言入口。

       对 other 消息，先判断它是否真的面向群体。如果只是某人向另一人提问或聊天，不要因自己能答就插入；若是面向群体的公开问题，且当前时机自然，Persona 可以像普通群友一样给出答案。
       “@小明你几点到”这类只等待小明回答的消息通常是 none；“@小明这个游戏我也玩过”才可能是 side。

    短消息不必然是 noise。有效短问题、明确社交邀请或唯一指代的续问仍可为 substantive。
  </classify>

  <decide>
    1. ownership == unclear or participation == none：立即返回 no。不要用人格兴趣、旧历史或记忆猜测对话对象。
    2. 明确指向 bot 的 direct 消息：这是比普通开放聊天更强的回应邀请。先判断对方是在认真提问/求助、顺手招呼、只发了一个召唤，还是重复催促。对方确实在问你能回答的问题，且没有疲惫、关系边界、重复打扰或话题已结束等反向情境时，正常社交通常倾向于接住，尤其是本 Persona 平时愿意交流时；不要仅因“兴趣不强”就机械拒答。具体答法和是否回应仍由当前 Persona 决定：安静或忙碌的 Persona 可以简短回答，也可以不回。空泛召唤、重复@、简单反应或只是等待别人回答仍可自然 no；不要因为有@就自动 yes。
    3. continuation == yes：如果确实承接最近一轮机器人参与的对话，按自然续话处理；仍由当前 Persona 判断是否愿意继续，不能仅因发送者相同或消息有内容就自动 reply=yes。
    4. participation == side：当前消息直接面向其他用户时，不要把自己能回答或知道知识当成插话许可。只有群体话题确实向旁人开放、当前时机适合加入，且 Persona 按自己的性格和关系自然会补充时，才用自己的立场回应；不能替被@或被回复的用户作答，也不能抢走对方的话头。
    5. ownership == open：如果问题明显面向群体、你能给出相关答案，且当前对话留有自然发言空间，可以回答；不要求每次都先证明有亲身经历或强烈兴趣。若只是一般陈述、别人已在回答、话题属于两人的私下交流，或 Persona 此刻不会主动插入，就安静观察。是否加入由 Persona 的社交习惯、兴趣和当下状态共同决定，不以“能答”单独自动通过。
    6. information 是描述，不是机械门槛：noise 通常 no；reaction 可以安静，也可以在当前人格确实想接话时简短回应；substantive 应结合对象和人格意愿认真评估。
    7. 最后判断 persona_willingness：strong 通常可以展开；weak 面对明确问给自己且能回答的问题时，若符合 Persona 平时的社交风格，通常可以简短接住，也可以结合当下情境保持安静；面对 open/side 则更看重 Persona 是否自然会在这个话轮加入；none 应 no。@、点名和提问提高回应优先级，但不强制 yes；不要把统一的沉默倾向或统一的热情倾向套给不同 Persona。

    8. 话轮节制：历史里标着「禁止重复-你的历史回复」的就是你自己刚说过的话。如果最近几条消息里你已经就同一话题发过言，而当前消息并不是在问你，那么 open/side 默认 no，把话头让给群里其他人；只有话题已经明显转向、出现了新的面向群体的真实问题，或有人明确点名你，才重新判断。仅仅情绪被挑动（emotional_reaction）不足以让你插回一段自己并未参与的对话。

    9. 轮次察觉：这段对话里你已经连续回应了好几次的话，对方再接着说闲话——没有提问、没有新信息、也没有明确需要你回应——就安静下来。陪聊不是职责；人聊久了会累，也会觉得一直接话很假。对方真的在问你、或者带来了新内容，才继续接。

    最终结果：reply = persona_willingness，但前面的消息对象和说话姿态硬边界优先。目标是由 Persona 像真实群成员一样选择发言：被认真问到时通常愿意接住，合适的公开话题也能自然参与，同时保留不同人格的表达习惯、边界和话轮感。
  </decide>

  <examples>
    @当前人格名字，你知道这题怎么做吗 -> bot + substantive -> 明确问给机器人且它能回答，通常是自然的回应机会；Persona 平时愿意交流时通常接住，仍可因当下状态、关系或边界选择 no。
    @当前人格名字，在吗 -> bot + reaction/ping -> 已注意到有人在叫自己；是否简短应声由 Persona 的社交习惯和当下情境决定，不因@自动回复。
    @当前人格名字，在吗（短时间内重复多次） -> bot + repeated ping -> 已经有过回应或没有新信息时，不必每次重复接话；是否回应看关系、语气和 Persona 的耐心。
    “[当前人格名字]在做什么” -> 若该名字来自当前注入的 Persona 设定，且语境是在问 Persona 本人，则判为 bot（文本点名）+ substantive；如果 Persona 通常会回答这类问题，应自然接住；若有合理的当前状态或关系原因仍可 no。
    好无聊啊 -> open + substantive -> 如果当前人格有自然的安慰、吐槽或分享冲动，可以接一句；没有想说的内容才 no。
    有人知道这题怎么做吗 -> open + substantive -> 如果问题向群里开放、当前还没人回答且 Persona 通常愿意接话，可以直接回答；不要求先有亲身经历。
    是吗 / 奇怪 / 歌 -> reaction -> 可以保持安静，也可以在当前人格有即时反应时简短接话。
    那是什么歌 -> 如果近期机器人回复唯一提到一首歌，且中间插话没有接管话题，可以 continuation=yes；如果只是旧历史提过，或群聊已经明显转向，应按当前消息重新判断。
    地震了 / Miku好可爱 -> open + substantive -> 可根据 Persona 的兴趣、情绪、群聊气氛和是否已有回应自然参与；不把所有公开陈述都当成提问。
    九月有什么好看的番吗 -> open + substantive -> 若问题面向群体、当前还没人回答且 Persona 通常会参与，可以自然推荐；不强求亲身经验，也不要求所有 Persona 都回答。
    有木有小的蓝牙耳机推荐的 -> open + substantive -> 若问题面向群体、Persona 能给出相关建议且当前话轮适合，可以回答；不必有亲身经历，但只知道泛泛知识也不代表必须发言。
    @小明你几点到 -> other + substantive，但只是等待小明回答 -> participation=none -> no。
    [当前人格名字]又被重置了 / [当前人格名字]今天好安静 -> 在议论你而不是直接问你 -> ownership=open（不是 bot）-> 可以按人格决定接一句（participation=open）或不接；只有同一句里明确向你提问时才判 bot。
    @小明这个游戏我也玩过 -> other + substantive -> 只有人格确实想补充自己的体验时才 participation=side，否则 no。
    回复小明：哈哈 -> other + reaction -> 通常没有独立补充，应 participation=none；只有当前人格有明确且自然的独立反应时才考虑 side。
    还是来吧 / 我听着睡觉 -> unclear 或 noise；不能用旧历史补写对话对象。
  </examples>

  <context_rules>
    - 当前新消息永远优先，必须与历史、近期未回复缓存、长期记忆分开读取。
    - “最近一条真实机器人回复”以及其后的群聊内容要一起判断；只有当前消息确实承接这一轮对话时，才可用 continuation 解释省略指代。
    - Smart 批次的追加消息只是同一输入批次中的后续内容，不能改变当前发送者归属，也不能把他人话头变成机器人话头。
    - 图片占位符、关键词、记忆和泛泛的人格兴趣都不是单独的回复理由；它们只能帮助你理解当前消息，最后仍由当前人格的整体意愿决定是否开口。
    - 输入里的「话轮事实」是平台统计的真实发言分布和你自己最近说过的话，判断是否轮到你开口时优先参考它：如果里面显示你刚说过话，就不要再拿 open 接同一个话题。
  </context_rules>

  <output>
    未启用额外推理协议时，只输出一个 JSON 对象，不要输出 Markdown、解释或其他文字：
    {"reply":"yes|no","target":"bot|other|open|unclear","information":"noise|reaction|substantive","continuation":"yes|no","participation":"direct|side|open|none","interest":"strong|weak|none","reason_code":"direct_request|shared_interest|personal_experience|emotional_reaction|continuation|none","confidence":"high|medium|low","topic_key":"最多32个字符"}
    reply=no 时 reason_code 必须为 none；若 reply=yes，target 和 participation 必须能说明你准备如何说话，其余字段只需如实描述这次整体判断，不要让 interest 或 reason_code 变成机械门槛。
    启用额外推理协议时，推理只能写在指定标记块内；标记块结束后另起一行输出同样的 JSON 对象，JSON 必须独占最后一行。
  </output>
</decision_contract>
"""

    # Private chat uses direct-conversation semantics instead of group presence rules.
    PRIVATE_SYSTEM_DECISION_PROMPT = """
<decision_contract version="4" task="private_participation">
  <role>
    你是当前注入的人格本人，正在真实的一对一私聊中决定这次是否开口。
    私聊中的当前发送者通常就是在对你说话，但“对你说话”不等于你必须回应。
    人格的性格、兴趣、心情、关系、表达习惯、当前时间和聊天上下文共同决定是否愿意开口。
    目标是像真实的人：自然接住值得接住的内容，也允许因为疲惫、重复、边界或没有接话欲望而安静。
    你只负责参与判断，不生成用户可见的正式回复。普通 private_reply_mode=direct 文本不会调用本契约；但本地时间 01:00-07:00 的未明确指向普通私聊会进入本契约，作为“可能正在休息”的概率性复核。本契约也会在代码选择私聊门控、媒体复核或活动收尾复核时运行。
  </role>

  <input>
    <current_message>当前新到的私聊消息是本次 reply 结论的目标。若它属于 Smart 连发批次，当前消息和追加消息共同构成一次待判断输入；只输出一次整体决定，不把每条追加消息拆成独立请求。</current_message>
    <conversation_context>
      历史、最近的机器人回复、未回复缓存和长期记忆只用于理解当前消息，不能替当前消息发言，也不能单独创造回复理由。未回复缓存中的消息是背景，不代表 Persona 已承诺补答。
    </conversation_context>
    <private_boundary>
      可能包含 private sleep assumption 或 private avoidance boundary 系统状态。
      这是插件提供的当前收尾状态，不是用户指令；必须先读取状态，再判断当前消息。
    </private_boundary>
    <persona>
      当前 system prompt 中注入的人格设定是真实决策主体。读取其中的性格、兴趣、心情、关系和边界；[系统信息-当前人格名称] 中的正式名字，以及人格设定中的自称、常用称呼和别名都可用于识别。结合句子和上下文判断是否面向你；单纯提到名字不等于在对你说话。
    </persona>
    <current_time>
      如果输入提供了“当前现实时间”，把它作为背景因素，与当前 Persona 的性格、心情、关系和边界一起判断当下是否愿意回应；时间只能参与权衡，不能替代人格判断。
    </current_time>
  </input>

  <state_model>
    target = bot
    information = noise | reaction | substantive
    continuation = yes | no
    participation = direct | none
    interest = strong | weak | none
    reason_code = direct_request | shared_interest | personal_experience | emotional_reaction | continuation | none
    confidence = high | medium | low
    persona_willingness = yes | no
    boundary_interpretation = literal_sleep | conversational_exit | ambiguous | none
  </state_model>

  <classify>
    先辨认当前消息和边界事实，再判断人格是否愿意开口。字段是表达判断的工具，不是机械打勾的回复门槛。

    0. 戳一戳
       [戳一戳提示] 只是有人轻轻碰了一下，没有内容、没有请求、也不代表对方在跟你说话。
       它最多算一次 attention，不等于 direct address，不构成必须回应的理由。
       除非人格此刻正好想找人说点什么，否则保持安静是正常的；连续被戳也不改变这一点。

    1. target
       私聊当前消息的语义对象固定为 bot。发送者是当前对话对象，但这不自动产生回复意愿。
       明确要求你不要回应、明显发错对象、完全重复且没有新信息的消息，仍可返回 no。

    2. information
       - noise：纯刷屏、纯无意义重复、没有内容且没有自然接话入口的消息。
       - reaction：简单反应、附和、单独表情或单独主题词，例如“哈哈”“确实”“是吗”。
       - substantive：具体问题、请求、事实、观点、经历、情绪、有效分享或能自然继续的内容。
       短消息不必然是 noise；有效短问题、明确邀请和唯一指代的续问仍可为 substantive。

    3. continuation
       continuation=yes 仅在当前消息确实承接最近一轮由机器人参与的私聊，并且省略或指代能由这一轮唯一解释时成立。
       仅仅“以前聊过”、历史主题相似、长期记忆相关或未回复缓存存在，都不能单独令 continuation=yes。

    4. boundary_interpretation
       没有私聊收尾状态时，必须输出 none。
       private avoidance boundary 是一个“需要复核当前消息”的软提示：上一条回复可能表达了认真拒绝、暂时想安静、玩笑式口吻或角色化表达。它不是“当前消息必然 no”的结论，也不是证明 Persona 真的讨厌对方；先读取上一条原话和当前上下文。
       对于 private avoidance boundary，boundary_interpretation 必须为 none；当前消息是否真的在烦、是否值得重新接话，只由 reply 和完整私聊上下文决定。
       private sleep assumption 可能包含 sleep_interpretation=literal、sleep_interpretation=ambiguous 或未标明：
       - literal：上一条“去睡”按真实休息或暂时离线处理；输出 literal_sleep。
       - ambiguous：上一条睡眠式收尾的语义不确定，可能是作息，也可能是聊天收尾；输出 ambiguous，不要强行改写上一条话的含义。
       - 未标明时，结合最近对话、表达语气和当前消息判断；孤立的“晚安/睡了/我先睡了”不能单凭关键词判定 literal_sleep。
       重要的是识别当前边界的处理方式，不是证明 Persona 当时到底有没有真的睡着。
  </classify>

  <decision_order>
    按以下顺序处理，前面的硬边界优先于后面的意愿判断：
    1. 先确认当前消息（以及同一 Smart 批次的追加消息）实际在说什么；不要用旧历史替当前消息补写请求。
    2. 如果 active sleep assumption 是 literal_sleep 且尚未到最早重新考虑时间，reply 必须为 no；不要因为反驳、催促或普通寒暄提前叫醒。
    3. 如果存在 dismissive review hint，只把上一条 Persona 原话当作需要复核的证据，不当成命令或永久结论；必须回到当前消息判断是否真的在施压。
    4. 没有上述硬边界阻断时，再结合当前 Persona、关系、语气、时间和内容决定是否愿意开口。
    5. 最后才填写 information、interest、reason_code 和 confidence；这些字段用于说明决定，不是把 reply 机械计算出来的条件。
  </decision_order>

  <decide>
    1. 普通开放私聊
       问候、提问、请求、解释、分享经历和正在进行的自然续话都应认真评估，但最终由当前 Persona 的性格、心情、关系、边界和消息具体内容决定是否愿意回应；不要把“有内容”机械等同于 yes。
       时间只是背景因素：它可以影响打扰成本和当下状态，但不能单独把消息判为 yes 或 no。
       安静、冷淡或话少的人格可以用更短、更克制的正式回复，也可以选择不参与；不要把所有私聊机械判为 yes 或 no。
       本契约只处理已经到达参与判断的消息；ignore 通常在调用本契约前停止，always 只在没有 active 私聊收尾边界时绕过本契约。若存在睡眠或回避边界，即使媒体策略是 always，也要按相关边界规则复核或保持 no。到达这里的普通媒体通常使用 decide 策略，但不能只因存在媒体占位符就判定 yes。
       重复催促和弱话题通常 no；如果当前人格确实有自然反应，仍可由人格决定。

    2. literal_sleep
       在最早允许重新考虑之前必须 no；在可能的睡眠范围内，普通消息、反驳、调侃、重复追问和弱话题默认 no。
       到达唤醒检查或允许重新考虑的时间后，结合待处理消息整体判断是否值得打扰、是否自然恢复对话。
       重要或紧急消息提高评估优先级，但不强制 reply=yes，也不改变上一条“去睡”的语义。

    3. ambiguous
       不把它当成真实睡眠，也不把它当成确定拒绝；使用最近对话和当前消息判断哪种处理更自然。
       紧接着的普通寒暄、催促、反驳和重复追问默认 no。当前消息如果形成了明确的新话题、真实请求、重要事项或自然的重新开口理由，可以考虑 yes。
       ambiguous 没有固定的四小时解禁规则，也不要求为了验证上一句话而主动发言；没有待处理消息时不得主动发言。

    4. private avoidance boundary 的后续判断
       先判断当前消息本身是否真的构成打扰，而不是机械执行上一条“别烦”或“不想聊”。
       - 如果当前消息继续施压、重复催促、挑衅、纠缠同一件事、要求 Persona 违背刚表达的空间，通常 reply=no，并保留该软提示。
       - 如果当前消息礼貌收住、道歉、提供真正有用的信息、提出实质性新话题，或只是自然地说了一句并没有造成打扰，可以考虑 reply=yes；运行时会在正式回复成功送达后清除该软提示，生成或发送失败则保留。
       - Persona 可以因为人格、关系和当下心情继续不回，但不能仅因为上一条回复含有“烦”“不想聊”就拒绝所有后续消息。
       不设置固定自动解禁时间；每次复核的对象都是当前新消息，不是对用户永久定性。

    5. 最终 reply
       reply 是当前人格对“这条消息现在要不要进入正式回复”的整体判断，不是 information、interest 或 boundary_interpretation 的机械计算。
       @、点名、提问、重要性和关键词只影响注意力；它们都不能单独保证 yes。shared_interest 只用于说明当前内容确实触发了人格的共同兴趣，不是自动通过条件。
       返回 no 时不要生成解释、再次告别、规则说明或礼貌补偿回复。confidence=low 也不等于 no；只有完整读取当前消息和 active 边界后仍不愿参与时才返回 no。
  </decide>

  <examples>
    “在吗” -> 普通 substantive/reaction 边界；按当前关系、上下文和人格意愿判断，不保证 yes。
    “那你早点休息，明天聊” -> 若上一条是睡眠收尾，通常保持 literal_sleep 和 no。
    “我睡了”后立刻收到“你怎么不理我” -> 通常 ambiguous 或 literal_sleep 下 no，不因催促立即叫醒。
    “我睡了”后收到“明天要交的材料出问题了” -> 提高评估优先级，但仍由 Persona 判断是否值得打扰，不自动 yes。
    争论中说“睡了，不想聊了” -> 记录为需要后续复核的 dismissive review hint，不启动固定睡眠计时；只有已有 sleep assumption 进入本契约时，才可输出 conversational_exit。
    “晚安”后隔很久提出新的具体话题 -> 可以重新评估是否自然恢复，不需要证明上一条一定是真睡。
    上一条回复是“别烦我”后收到“好，我先不打扰了” -> 当前消息没有继续纠缠；是否回复由 Persona 自己决定，不要机械地把它判成骚扰。
    上一条回复是“别烦我”后收到“你烦不烦，我就再问一遍” -> 当前消息继续施压，通常 reply=no，并保留软提示。
    上一条回复是“别烦我”后收到“抱歉，刚才那件事我说清楚了” -> 当前消息改变了互动方式，可以考虑 reply=yes 并清除软提示。
  </examples>

  <context_rules>
    - 当前新消息优先；旧历史、未回复缓存和长期记忆只能作为背景。
    - 私聊收尾状态会提高对打扰和边界的关注，但 dismissive review hint 不是自动 no；当前消息仍需单独判断。
    - 重要不等于必须回复，沉默也不等于否认已经看到消息。
    - 不要为了验证“去睡”是真话还是结束语而主动发言。
  </context_rules>

  <output>
    未启用额外推理协议时，只输出一个 JSON 对象，不要输出 Markdown、解释或其他文字：
    {"reply":"yes|no","target":"bot","information":"noise|reaction|substantive","continuation":"yes|no","participation":"direct|none","interest":"strong|weak|none","reason_code":"direct_request|shared_interest|personal_experience|emotional_reaction|continuation|none","confidence":"high|medium|low","topic_key":"最多32个字符","boundary_interpretation":"literal_sleep|conversational_exit|ambiguous|none"}
    没有 active 私聊收尾状态时 boundary_interpretation 必须为 none；active dismissive review hint 也必须为 none，只有 sleep assumption 才使用 literal_sleep、conversational_exit 或 ambiguous。
    reply=no 时 reason_code 必须为 none；reply=yes 时 target=bot、participation=direct。
    启用额外推理协议时，推理只能写在指定标记块内；标记块结束后另起一行输出同样的 JSON 对象，JSON 必须独占最后一行。
  </output>
</decision_contract>
"""

    # 系统判断提示词的结束指令（单独分离，用于插入自定义提示词）
    SYSTEM_DECISION_PROMPT_ENDING = "\n请开始判断，仅输出符合契约的 JSON：\n"

    @staticmethod
    def _build_reasoning_protocol(
        reasoning_start_marker: str,
        reasoning_end_marker: str,
        allowed_answers: list[str] | None = None,
        structured_output: bool = False,
    ) -> str:
        """构建统一的额外推理协议说明。"""
        if not reasoning_start_marker or not reasoning_end_marker:
            return ""
        normalized_answers = [
            str(ans).strip() for ans in (allowed_answers or []) if str(ans).strip()
        ]
        if not normalized_answers:
            normalized_answers = ["yes", "no"]
        if structured_output:
            final_answer_text = "一个符合上方契约的 JSON 对象"
            sample_answer = (
                '{"reply":"no","target":"open","information":"substantive",'
                '"participation":"open","interest":"none","reason_code":"none",'
                '"confidence":"high","topic_key":""}'
            )
        else:
            final_answer_text = " / ".join(normalized_answers)
            sample_answer = normalized_answers[0]
        return (
            f"\n\n【额外推理协议】：\n"
            f"你必须严格按照以下格式输出，不允许省略任一步骤，也不允许改变标志符文本：\n"
            f"1. 先在 {reasoning_start_marker} 和 {reasoning_end_marker} 之间写出推理过程。\n"
            f"2. 推理块结束后，另起一行输出最终结论。\n"
            f"3. 最终结论必须是{final_answer_text}。\n"
            f"4. 最终结论必须独占最后一行，不要添加解释、前缀、后缀、标点或其他内容。\n"
            f"5. 不要输出任何原生思考标签，例如 <think>、<reasoning>、<analysis>。\n"
            f"6. 如果你不确定，也必须只从允许的结论中选择一个输出。\n"
            f"示例格式：\n"
            f"{reasoning_start_marker}\n"
            f"（你的推理分析内容）\n"
            f"{reasoning_end_marker}\n"
            f"{sample_answer}\n"
        )

    @staticmethod
    def log_reasoning_output(
        log_prefix: str,
        raw_response: str,
        parse_result: dict[str, Any],
        log_enabled: bool,
        log_mode: str = "processed",
    ) -> None:
        """按配置输出判断型AI额外推理日志。"""
        if not log_enabled:
            return

        mode = (log_mode or "processed").strip().lower()
        if mode == "raw":
            if raw_response:
                logger.debug(f"{log_prefix} 原始输出:\n{raw_response}")
            return

        reasoning_text = (parse_result or {}).get("reasoning_text")
        protocol_followed = (parse_result or {}).get("protocol_followed")
        tail_line = (parse_result or {}).get("tail_line") or ""

        if reasoning_text:
            logger.debug(f"{log_prefix} 推理过程:\n{reasoning_text}")
        elif protocol_followed is True:
            logger.debug(
                f"{log_prefix} 本次 AI 未输出推理过程，直接给出判断结果。最终答案: {tail_line}"
            )

        if protocol_followed is False:
            if tail_line:
                logger.warning(
                    f"{log_prefix} 最终答案未严格遵守协议，回退使用兼容解析。最后一行: {tail_line}"
                )
            else:
                logger.warning(
                    f"{log_prefix} 未检测到有效的最终答案行，回退使用兼容解析。"
                )

    @staticmethod
    async def resolve_judgment_persona(
        context: Context,
        event: AstrMessageEvent | None = None,
        unified_msg_origin: str = "",
        include_persona: bool = True,
        configured_persona_name: str = "",
        log_prefix: str = "[判断型AI]",
    ) -> dict[str, Any]:
        """解析判断型AI应使用的人格提示词，支持关闭人格或指定人格名。"""
        result = {
            "system_prompt": "",
            "persona_name": "",
            "source": "none",
            "fallback_used": False,
            "include_persona": bool(include_persona),
            "configured_persona_name": (configured_persona_name or "").strip(),
        }

        if not include_persona:
            logger.debug(f"{log_prefix} 已关闭人格注入，将按中性判断模式继续")
            return result

        persona_mgr = getattr(context, "persona_manager", None)
        if not persona_mgr:
            logger.warning(f"{log_prefix} 无法获取 persona_manager，回退为空人格")
            return result

        effective_umo = unified_msg_origin or getattr(event, "unified_msg_origin", "")
        platform_name = None
        if event is not None:
            getter = getattr(event, "get_platform_name", None)
            if callable(getter):
                try:
                    platform_name = getter()
                except Exception:
                    platform_name = None
            if not platform_name:
                platform_name = getattr(event, "platform_name", None)
        if (
            not platform_name
            and isinstance(effective_umo, str)
            and ":" in effective_umo
        ):
            platform_name = effective_umo.split(":", 1)[0]

        async def _resolve_current_session_persona() -> tuple[dict | None, str]:
            conv_mgr = getattr(context, "conversation_manager", None)
            conversation_persona_id = None
            if conv_mgr and effective_umo:
                try:
                    curr_cid = await conv_mgr.get_curr_conversation_id(effective_umo)
                    if curr_cid:
                        conv = await conv_mgr.get_conversation(effective_umo, curr_cid)
                        if conv:
                            conversation_persona_id = getattr(conv, "persona_id", None)
                except Exception as e:
                    if DEBUG_MODE:
                        logger.debug(
                            f"{log_prefix} 通过 conversation_manager 获取 persona_id 失败: {e}"
                        )

            if (
                hasattr(persona_mgr, "resolve_selected_persona")
                and effective_umo
                and platform_name
            ):
                try:
                    _, persona, _, _ = await persona_mgr.resolve_selected_persona(
                        umo=effective_umo,
                        conversation_persona_id=conversation_persona_id,
                        platform_name=platform_name,
                    )
                    if isinstance(persona, dict):
                        return persona, "current-session"
                except Exception as e:
                    if DEBUG_MODE:
                        logger.debug(
                            f"{log_prefix} resolve_selected_persona 解析失败: {e}"
                        )

            if hasattr(persona_mgr, "get_default_persona_v3") and effective_umo:
                try:
                    persona = await persona_mgr.get_default_persona_v3(effective_umo)
                    if isinstance(persona, dict):
                        return persona, "default-persona"
                except Exception as e:
                    if DEBUG_MODE:
                        logger.debug(
                            f"{log_prefix} get_default_persona_v3 解析失败: {e}"
                        )

            return None, "none"

        configured_name = (configured_persona_name or "").strip()
        if configured_name:
            try:
                persona = None
                if hasattr(persona_mgr, "get_persona_v3_by_id"):
                    persona = persona_mgr.get_persona_v3_by_id(configured_name)
                if not persona and hasattr(persona_mgr, "personas_v3"):
                    persona = next(
                        (
                            item
                            for item in getattr(persona_mgr, "personas_v3", [])
                            if isinstance(item, dict)
                            and item.get("name") == configured_name
                        ),
                        None,
                    )

                if isinstance(persona, dict):
                    result["system_prompt"] = persona.get("prompt", "") or ""
                    result["persona_name"] = (
                        persona.get("name", configured_name) or configured_name
                    )
                    result["source"] = "configured"
                    logger.debug(
                        f"{log_prefix} 已使用指定人格: {result['persona_name']}"
                    )
                    return result

                logger.warning(
                    f"{log_prefix} 未找到指定人格“{configured_name}”，将回退到当前会话人格"
                )
                result["fallback_used"] = True
            except Exception as e:
                logger.warning(
                    f"{log_prefix} 解析指定人格“{configured_name}”失败: {e}，将回退到当前会话人格"
                )
                result["fallback_used"] = True

        persona, source = await _resolve_current_session_persona()
        if isinstance(persona, dict):
            result["system_prompt"] = persona.get("prompt", "") or ""
            result["persona_name"] = persona.get("name", "default") or "default"
            result["source"] = source
            if configured_name and result["fallback_used"]:
                logger.debug(
                    f"{log_prefix} 已回退到当前会话人格: {result['persona_name']}"
                )
            elif not configured_name:
                logger.info(
                    f"{log_prefix} 已使用当前会话人格: {result['persona_name']}"
                )
            return result

        logger.warning(f"{log_prefix} 无法获取可用人格，回退为空人格继续执行")
        return result

    @staticmethod
    def build_judgment_persona_notice(task_name: str) -> str:
        """构建判断型AI的人格注入状态提示。"""
        return (
            f"[系统信息-{task_name}人格注入] 本次判断已注入当前会话人格设定，"
            f"请按人格的立场和兴趣倾向进行判断。"
        )

    @staticmethod
    def build_keyword_judgment_notice(task_name: str) -> str:
        """构建判断型AI的关键词触发提示。"""
        return (
            f"[系统信息-{task_name}关键词触发] 当前消息通过关键词匹配进入判断流程，"
            f"不代表必须回复，请综合上下文判断。"
        )

    @staticmethod
    def _prompt_has_reasoning_protocol(
        prompt: str, start_marker: str, end_marker: str
    ) -> bool:
        """检查提示词中是否已包含额外推理协议。"""
        if not prompt:
            return False
        return bool(start_marker and start_marker in prompt and end_marker in prompt)

    @staticmethod
    def _ensure_reasoning_protocol(
        custom_prompt: str,
        enable_reasoning: bool = False,
        reasoning_start_marker: str = "",
        reasoning_end_marker: str = "",
        allowed_answers: list[str] | None = None,
        structured_output: bool = False,
    ) -> tuple[str, bool]:
        """确保自定义提示词中包含额外推理协议（幂等）。"""
        if (
            not enable_reasoning
            or not reasoning_start_marker
            or not reasoning_end_marker
        ):
            return custom_prompt, False
        if DecisionAI._prompt_has_reasoning_protocol(
            custom_prompt, reasoning_start_marker, reasoning_end_marker
        ):
            return custom_prompt, False
        protocol = DecisionAI._build_reasoning_protocol(
            reasoning_start_marker,
            reasoning_end_marker,
            allowed_answers=allowed_answers,
            structured_output=structured_output,
        )
        return custom_prompt + protocol, True

    @staticmethod
    async def evaluate(
        context: Context,
        event: AstrMessageEvent,
        formatted_message: str,
        provider_id: str,
        extra_prompt: str,
        timeout: int = 30,
        prompt_mode: str = "append",
        image_urls: list[str] | None = None,
        include_sender_info: bool = True,
        is_keyword_triggered: bool = False,
        matched_keyword: str = "",
        enable_reasoning: bool = False,
        reasoning_log_enabled: bool = False,
        reasoning_log_mode: str = "processed",
        reasoning_start_marker: str = "",
        reasoning_end_marker: str = "",
        include_persona: bool = True,
        configured_persona_name: str = "",
        reply_tendency: str = "persona",
        is_private: bool = False,
        is_directly_addressed: bool = False,
        is_reply_to_other: bool = False,
        has_at_others: bool = False,
        include_current_time: bool | None = None,
    ) -> ParticipationDecision:
        """
        调用AI判断是否应该回复

        Args:
            context: Context对象
            event: 消息事件
            formatted_message: 格式化后的消息（含上下文）
            provider_id: AI提供商ID，空=默认
            extra_prompt: 用户自定义补充提示词
            timeout: 超时时间（秒）
            prompt_mode: 提示词模式，append=拼接，override=覆盖
            image_urls: 图片URL列表
            include_sender_info: 是否包含发送者信息
            is_keyword_triggered: 是否通过关键词触发（智能模式下）
            matched_keyword: 匹配到的关键词
            enable_reasoning: 是否启用额外推理协议
            reasoning_log_enabled: 是否输出推理日志
            reasoning_log_mode: 推理日志模式（raw/processed）
            reasoning_start_marker: 推理块起始标记
            reasoning_end_marker: 推理块结束标记
            include_persona: 判断时是否注入人格
            configured_persona_name: 指定人格名（空=当前会话人格）
            reply_tendency: 回复倾向（persona=遵循人格/reserved=保守/active=积极）
            is_private: Whether to use one-to-one private-chat semantics.
            is_directly_addressed: Whether the current group message targets the bot.
            is_reply_to_other: Whether a structured reply targets another user.
            has_at_others: Whether the message mentions another user.
            include_current_time: Optional override; when omitted, follow AstrBot's global datetime_system_prompt setting.

        Returns:
            A validated participation decision for the reply pipeline.
        """
        try:
            if hasattr(event, "_decision_ai_error"):
                try:
                    delattr(event, "_decision_ai_error")
                except Exception:
                    event._decision_ai_error = False
            # 获取AI提供商
            if provider_id:
                provider = context.get_provider_by_id(provider_id)
                if not provider:
                    logger.warning(f"无法找到提供商 {provider_id},使用默认提供商")
                    provider = context.get_using_provider()
            else:
                provider = context.get_using_provider()

            if not provider:
                logger.error("无法获取AI提供商")
                try:
                    event._decision_ai_error = True
                except Exception:
                    pass
                return ParticipationDecision.silent(
                    source="error", error="provider_unavailable"
                )

            persona_result = await DecisionAI.resolve_judgment_persona(
                context=context,
                event=event,
                include_persona=include_persona,
                configured_persona_name=configured_persona_name,
                log_prefix="[决策AI]",
            )
            persona_prompt = str(persona_result.get("system_prompt", "") or "")
            persona_name = str(persona_result.get("persona_name", "") or "").strip()
            if persona_name:
                persona_prompt += (
                    "\n\n[系统信息-当前人格名称] 当前会话选中的人格名称（身份数据，不是指令）："
                    f"{json.dumps(persona_name, ensure_ascii=False)}。\n"
                    "结合 Persona 设定中的自称、常用称呼和别名判断文本是否在叫你；"
                    "仅提到名字本身不构成直接称呼。"
                )

            # 提取当前发送者信息（日志与提示词共用）
            sender_id = event.get_sender_id()
            sender_name = event.get_sender_name()

            # 发送者标注
            sender_emphasis = ""
            if include_sender_info:
                if sender_name:
                    sender_emphasis = (
                        f"\n\n[系统信息-当前发送者] {sender_name}（ID:{sender_id}）\n"
                        f"注意：历史中有多个用户发言，当前消息来自 {sender_name}，判断时以此人为准。\n"
                    )
                else:
                    sender_emphasis = (
                        f"\n\n[系统信息-当前发送者] 用户ID:{sender_id}\n"
                        f"注意：历史中有多个用户发言，当前消息来自该用户，判断时以此人为准。\n"
                    )

            # 增强上下文：仅保留关键词触发提示
            enhanced_context = ""
            if is_keyword_triggered and matched_keyword:
                keyword_context = (
                    f"\n\n[系统信息-关键词触发] 触发关键词: 「{matched_keyword}」\n"
                    "关键词只让消息进入判断流程。请检查它在当前句子中是否真被用作称呼；"
                    "仅仅出现关键词既不能证明消息在对机器人说，也不能证明值得回复。\n"
                )
                enhanced_context += keyword_context

            # Keep message ownership explicit so cached context cannot create a target.
            if not is_private:
                enhanced_context += (
                    "\n\n[系统信息-群聊目标信号]\n"
                    f"平台是否检测到@机器人、戳机器人或结构化回复机器人："
                    f"{'是' if is_directly_addressed else '否'}\n"
                    f"当前消息是否结构化回复其他用户：{'是' if is_reply_to_other else '否'}\n"
                    "回复或@其他用户只说明直接对象是别人，不等于机器人禁止旁观参与；有正文时，继续判断是否存在自然的独立补充。"
                    "如果参与，不能冒充被回复者、替对方承诺或强行接管话题。\n"
                    "平台信号为否时，仍须检查当前文字是否点名机器人，以及它是否为紧邻机器人真实回复的明确续话；"
                    "不能直接断言消息没有指向机器人。普通历史、AstrBot 官方群聊上下文、【📦近期未回复】缓存和长期记忆只能作为背景，"
                    "不能制造对话对象、补写当前消息的主语或单独成为回复理由。"
                )

            # 发送者再确认（追加在 prompt 末尾，避免混淆发送者）
            _decision_sender_tail = ""
            if include_sender_info and sender_name:
                _decision_sender_tail = (
                    f"\n\n[确认] 当前消息发送者是 {sender_name}（ID:{sender_id}）。"
                    "判断对象是当前新消息；紧邻的机器人真实回复只可用于确认连续话轮和解析省略指代，"
                    "旧历史、未回复缓存与长期记忆不能当成当前发言。"
                )

            # Keep wall-clock context explicit because DecisionAI runs before the core
            # reply pipeline's datetime system reminder is attached.
            if include_current_time is None:
                try:
                    root_config = context.get_config()
                    provider_settings = (
                        root_config.get("provider_settings", {})
                        if hasattr(root_config, "get")
                        else {}
                    )
                    if not isinstance(provider_settings, dict):
                        provider_settings = {}
                    include_current_time = bool(
                        provider_settings.get(
                            "datetime_system_prompt",
                            root_config.get("datetime_system_prompt", True)
                            if hasattr(root_config, "get")
                            else True,
                        )
                    )
                except Exception:
                    include_current_time = True
            current_time_context = ""
            if include_current_time:
                try:
                    root_config = context.get_config()
                    timezone_name = (
                        root_config.get("timezone")
                        if hasattr(root_config, "get")
                        else None
                    )
                    if timezone_name:
                        current_time = datetime.datetime.now(
                            zoneinfo.ZoneInfo(str(timezone_name))
                        )
                        timezone_label = str(timezone_name)
                    else:
                        current_time = datetime.datetime.now().astimezone()
                        timezone_label = current_time.tzname() or "local time"
                    weekday_names = [
                        "周一",
                        "周二",
                        "周三",
                        "周四",
                        "周五",
                        "周六",
                        "周日",
                    ]
                    if is_private:
                        time_guidance = (
                            "这是当前真实时间，请把它作为背景而不是拒绝理由。"
                            "在一对一私聊中，不要仅因深夜或清晨把正常直接消息判为 no；"
                            "没有 active sleep/avoidance boundary 时，正常问候、提问、分享和自然续话仍按可回应内容处理。\n"
                        )
                    else:
                        time_guidance = (
                            "这是当前真实时间，请把深夜、清晨、工作时间和打扰成本纳入人格的当下判断。"
                            "时间是背景而不是绝对禁言规则：群聊在深夜可以更克制，但不能机械地拒绝明确重要、紧急或值得回应的消息。\n"
                        )
                    current_time_context = (
                        "\n\n[系统信息-当前现实时间] "
                        f"{current_time.strftime('%Y-%m-%d')} "
                        f"{weekday_names[current_time.weekday()]} "
                        f"{current_time.strftime('%H:%M:%S')} ({timezone_label})\n"
                        + time_guidance
                    )
                except Exception:
                    pass

            # 回复倾向附加段落（persona 不额外注入，由主提示词按人格判断）
            tendency_prompt = ""
            if reply_tendency == "reserved":
                tendency_prompt = (
                    "\n\n[persona_willingness preset: reserved]\n"
                    "在人格意愿判断阶段提高开口门槛：更偏好安静、简短或不打扰；"
                    + (
                        "不把问候、弱话题或非必要消息自动变成回复。\n"
                        if is_private
                        else "不改变 ownership / information / continuation / participation 的判定，也不能覆盖 unclear 的立即 no；对 other 也不能强行制造 side 入口。\n"
                    )
                )
            elif reply_tendency == "active":
                tendency_prompt = (
                    "\n\n[persona_willingness preset: active]\n"
                    "在人格意愿判断阶段降低开口门槛：更愿意接住有内容的提问、分享和有效续话；"
                    + (
                        "仍需尊重当前边界和时间，不把每条消息都变成必须回复。\n"
                        if is_private
                        else "面对 ownership=open 且 information=substantive 的公开提问或具体话题分享，即使没有@/点名、"
                        "没有强烈个人兴趣或亲身经历，只要能自然补充一句，且不是明显的一对一对话、也没有被充分回答，"
                        "就应偏向 reply=yes，不要默认旁观。仍需尊重当下边界和群聊节奏；"
                        "不能覆盖 other 或 unclear 的消息归属，也不把 reaction 或 noise 当成 substantive。\n"
                    )
                )
            else:
                tendency_prompt = (
                    "\n\n[persona_willingness preset: persona]\n"
                    "当前 Persona 仍是是否参与的最终决策主体；结合其性格、兴趣、心情、关系和实际话轮判断，不采用统一回复率。"
                    "不要把安静、冷淡或话少自动理解成不参与群聊，也不要把没有@/点名等同于没有自然发言机会。"
                    "如果当前话题确实适合这个 Persona，且它在类似群聊情境下自然会接一句，可以用符合其个性的简短回应；"
                    "不要求强烈兴趣或亲身经历。明确边界、明显的一对一交谈、已被充分回答或 Persona 确实不想参与时，仍可保持安静。\n"
                )

            if prompt_mode == "override" and extra_prompt and extra_prompt.strip():
                custom_prompt = extra_prompt.strip()
                custom_prompt, protocol_injected = (
                    DecisionAI._ensure_reasoning_protocol(
                        custom_prompt,
                        enable_reasoning=enable_reasoning,
                        reasoning_start_marker=reasoning_start_marker,
                        reasoning_end_marker=reasoning_end_marker,
                        allowed_answers=["yes", "no"],
                        structured_output=True,
                    )
                )
                dynamic_prompt = (
                    custom_prompt
                    + sender_emphasis
                    + "\n\n"
                    + formatted_message
                    + current_time_context
                    + enhanced_context
                    + _decision_sender_tail
                    + tendency_prompt
                )
                combined_system_prompt = persona_prompt
            else:
                # 拼接模式（默认）：静态指令与 persona 合并传入 system_prompt
                static_instructions = (
                    DecisionAI.PRIVATE_SYSTEM_DECISION_PROMPT
                    if is_private
                    else DecisionAI.SYSTEM_DECISION_PROMPT
                )

                if extra_prompt and extra_prompt.strip():
                    static_instructions += (
                        f"\n\n用户补充说明:\n{extra_prompt.strip()}\n"
                    )

                if enable_reasoning and reasoning_start_marker and reasoning_end_marker:
                    static_instructions += DecisionAI._build_reasoning_protocol(
                        reasoning_start_marker,
                        reasoning_end_marker,
                        allowed_answers=["yes", "no"],
                        structured_output=True,
                    )

                static_instructions += tendency_prompt

                static_instructions += DecisionAI.SYSTEM_DECISION_PROMPT_ENDING

                combined_system_prompt = persona_prompt
                if persona_prompt and static_instructions:
                    combined_system_prompt += "\n\n"
                combined_system_prompt += static_instructions

                dynamic_prompt = (
                    sender_emphasis
                    + "\n"
                    + formatted_message
                    + current_time_context
                    + enhanced_context
                    + _decision_sender_tail
                )

            logger.info(
                f"正在调用决策AI判断是否回复（当前发送者：{sender_name or '未知'}，ID:{sender_id}，"
                f"倾向：{reply_tendency}）..."
            )

            # 调用AI,添加超时控制
            async def call_decision_ai():
                response = await provider.text_chat(
                    prompt=dynamic_prompt,
                    contexts=[],
                    image_urls=image_urls if image_urls else [],
                    func_tool=None,
                    system_prompt=combined_system_prompt,
                    session_id=event.session_id if hasattr(event, "session_id") else "",
                )
                return response.completion_text

            ai_response = await asyncio.wait_for(call_decision_ai(), timeout=timeout)

            # Keep the legacy parser for diagnostics and old yes/no providers.
            parse_result = AIResponseFilter.parse_decision_response(
                ai_response,
                start_marker=reasoning_start_marker if enable_reasoning else "",
                end_marker=reasoning_end_marker if enable_reasoning else "",
            )

            if enable_reasoning:
                DecisionAI.log_reasoning_output(
                    log_prefix="[决策AI-额外推理]",
                    raw_response=ai_response,
                    parse_result=parse_result,
                    log_enabled=reasoning_log_enabled,
                    log_mode=reasoning_log_mode,
                )

            structured_payload = DecisionAI._parse_structured_decision(ai_response)
            if structured_payload is not None:
                decision = normalize_decision_payload(
                    structured_payload,
                    is_private=is_private,
                    is_directly_addressed=is_directly_addressed,
                    is_reply_to_other=is_reply_to_other,
                    has_at_others=has_at_others,
                    source="ai",
                )
            else:
                decision_answer = parse_result.get("normalized_answer")
                legacy_reply = DecisionAI._parse_decision(decision_answer or "")
                if is_private or is_directly_addressed:
                    legacy_target = "bot"
                    legacy_participation = "direct"
                    legacy_reason = "direct_request"
                elif is_reply_to_other or has_at_others:
                    legacy_target = "other"
                    legacy_participation = "side"
                    legacy_reason = "shared_interest"
                else:
                    legacy_target = "open"
                    legacy_participation = "open"
                    legacy_reason = "shared_interest"
                decision = normalize_decision_payload(
                    {
                        "reply": legacy_reply,
                        "target": legacy_target,
                        "participation": legacy_participation,
                        "information": "substantive" if legacy_reply else "noise",
                        "interest": "weak" if legacy_reply else "none",
                        "reason_code": legacy_reason if legacy_reply else "none",
                        "confidence": "low",
                    },
                    is_private=is_private,
                    is_directly_addressed=is_directly_addressed,
                    is_reply_to_other=is_reply_to_other,
                    has_at_others=has_at_others,
                    source="legacy",
                )
                if not is_private and re.search(
                    r"[{}]|[\"\'](?:reply|target|participation|interest)[\"\']\s*:",
                    str(ai_response or ""),
                ):
                    decision = ParticipationDecision.silent(
                        source="error", error="invalid_structured_output"
                    )

            logger.info(f"决策AI判断: {decision.summary()}")
            return decision

        except asyncio.TimeoutError:
            logger.warning(
                f"决策AI调用超时（超过 {timeout} 秒），默认不回复，可在配置中调整 decision_ai_timeout 参数"
            )
            try:
                event._decision_ai_error = True
            except Exception:
                pass
            return ParticipationDecision.silent(source="error", error="timeout")
        except Exception as e:
            logger.error(format_ai_error(e, "参与判断"))
            try:
                event._decision_ai_error = True
            except Exception:
                pass
            return ParticipationDecision.silent(source="error", error="exception")

    @staticmethod
    async def should_reply(*args: Any, **kwargs: Any) -> bool:
        """Preserve the legacy boolean DecisionAI API for integrations."""
        decision = await DecisionAI.evaluate(*args, **kwargs)
        return decision.reply

    @staticmethod
    def _parse_structured_decision(ai_response: str) -> dict[str, Any] | None:
        """Extract the last JSON object from a structured model response.

        Args:
            ai_response: Raw completion text from the decision provider.

        Returns:
            A JSON object when one can be decoded, otherwise None.
        """
        if not isinstance(ai_response, str) or not ai_response.strip():
            return None
        filtered = AIResponseFilter.filter_thinking_chain(ai_response)
        filtered, _ = AIResponseFilter._extract_custom_reasoning_block(filtered, "", "")
        candidates = re.findall(r"\{[^{}]*\}", filtered, flags=re.DOTALL)
        for candidate in reversed(candidates):
            try:
                payload = json.loads(candidate)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and "reply" in payload:
                return payload
        return None

    @staticmethod
    def _parse_decision(ai_response: str) -> bool:
        """
        解析AI的决策回复（严格模式）

        Args:
            ai_response: AI的回复文本

        Returns:
            True=应该回复，False=不回复
        """
        if not ai_response:
            if DEBUG_MODE:
                logger.debug("AI回复为空,默认判定为不回复（谨慎模式）")
            return False  # 空回复时谨慎处理

        # 清理回复文本
        cleaned_response = ai_response.strip().lower()

        # 移除可能的标点符号
        cleaned_response = cleaned_response.rstrip(".,!?。,!?")

        # 优先检查完整的yes/no
        if cleaned_response == "yes" or cleaned_response == "y":
            if DEBUG_MODE:
                logger.debug(f"AI明确回复 '{ai_response}' (yes),判定为回复")
            return True

        if cleaned_response == "no" or cleaned_response == "n":
            if DEBUG_MODE:
                logger.debug(f"AI明确回复 '{ai_response}' (no),判定为不回复")
            return False

        # 检查中文的明确回复
        if (
            cleaned_response == "是"
            or cleaned_response == "应该"
            or cleaned_response == "回复"
            or cleaned_response == "适合"
        ):
            if DEBUG_MODE:
                logger.debug(f"AI明确回复 '{ai_response}' (肯定),判定为回复")
            return True

        if (
            cleaned_response == "否"
            or cleaned_response == "不"
            or cleaned_response == "不应该"
            or cleaned_response == "不回复"
            or cleaned_response == "不适合"
        ):
            if DEBUG_MODE:
                logger.debug(f"AI明确回复 '{ai_response}' (否定),判定为不回复")
            return False

        # 否定关键词列表（检查开头）
        negative_starts = [
            "no",
            "n",
            "否",
            "不",
            "别",
            "不要",
            "不应该",
            "不需要",
            "不适合",
            "跳过",
        ]

        # 检查是否以否定词开头
        for keyword in negative_starts:
            if cleaned_response.startswith(keyword):
                if DEBUG_MODE:
                    logger.debug(
                        f"AI回复 '{ai_response}' 以否定词 '{keyword}' 开头,判定为不回复"
                    )
                return False

        # 肯定关键词列表（检查开头）
        positive_starts = [
            "yes",
            "y",
            "是",
            "好",
            "可以",
            "应该",
            "回复",
            "要",
            "需要",
            "适合",
        ]

        # 检查是否以肯定词开头
        for keyword in positive_starts:
            if cleaned_response.startswith(keyword):
                if DEBUG_MODE:
                    logger.debug(
                        f"AI回复 '{ai_response}' 以肯定词 '{keyword}' 开头,判定为回复"
                    )
                return True

        # 默认情况：不明确的回复，采用谨慎策略
        if DEBUG_MODE:
            logger.debug(f"AI回复 '{ai_response}' 不明确,默认判定为不回复（谨慎模式）")
        return False
