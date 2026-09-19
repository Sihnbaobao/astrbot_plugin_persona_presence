"""Regression tests for the Persona Presence participation rewrite.

Core guarantees under test:
1. ReplyHandler no longer ships SYSTEM_REPLY_PROMPT (the ~100 line behavior
   instruction block that caused persona drift in group chats).
2. generate_reply produces a ProviderRequest whose system_prompt is exactly the
   persona and whose prompt is context plus only narrowly scoped reply-boundary
   annotations when another user is involved.
3. DecisionAI.SYSTEM_DECISION_PROMPT defines structured interest-based
   participation and contains no references to removed features.
4. MessageCacheManager's expiry filter is pure and correct.
5. KeywordChecker trigger/blacklist matching still works.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path


def _run(coro):
    """Run an async coroutine (sync test helper)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return asyncio.run(coro)
    return loop.run_until_complete(coro)


REPO_ROOT = Path(__file__).parents[1]
UTILS_DIR = REPO_ROOT / "utils"


class _Logger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass


class _ProviderRequest:
    """Minimal double for astrbot.core.provider.entities.ProviderRequest."""

    def __init__(self, **kwargs):
        self.prompt = kwargs.get("prompt", "")
        self.system_prompt = kwargs.get("system_prompt", "")
        self.session_id = kwargs.get("session_id", "")
        self.image_urls = kwargs.get("image_urls", [])
        self.audio_urls = kwargs.get("audio_urls", [])
        self.contexts = kwargs.get("contexts", [])
        self.func_tool = kwargs.get("tool_set", None)
        self.conversation = kwargs.get("conversation")


class _Event:
    """Minimal double for AstrMessageEvent used by generate_reply."""

    def __init__(self, message_str="", sender_id="42", sender_name="小明"):
        self._message_str = message_str
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.session_id = "sess-1"
        self.unified_msg_origin = "aiocqhttp:GroupMessage:10001"
        self._extras = {}

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_message_str(self):
        return self._message_str

    def get_self_id(self):
        return "bot_self"

    def get_platform_name(self):
        return "aiocqhttp"

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def request_llm(self, **kwargs):
        return _ProviderRequest(**kwargs)


class _PersonaManager:
    def __init__(self, prompt):
        self._prompt = prompt

    async def get_default_persona_v3(self, umo):
        return {
            "prompt": self._prompt,
            "name": "测试人格",
            "_begin_dialogs_processed": [],
        }

    async def resolve_selected_persona(self, **_kwargs):
        return (
            "active",
            {
                "prompt": "当前会话人格",
                "name": "当前人格",
                "_begin_dialogs_processed": [],
            },
            None,
            False,
        )


class _ToolManager:
    def get_full_tool_set(self):
        return None


class _ConversationManager:
    def __init__(self, conversation):
        self._conversation = conversation

    async def get_curr_conversation_id(self, _umo):
        return "conversation-1" if self._conversation is not None else None

    async def get_conversation(self, _umo, _conversation_id):
        return self._conversation


class _Context:
    def __init__(self, persona_prompt, conversation=None):
        self.persona_manager = _PersonaManager(persona_prompt)
        self.conversation_manager = _ConversationManager(conversation)
        self._tools = _ToolManager()

    def get_llm_tool_manager(self):
        return self._tools

    def get_config(self, umo=None):
        return {"provider_settings": {}}


def _install_astrbot_stubs(monkeypatch):
    """Install the same lightweight astrbot module stubs used by test_image_handler."""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api_all = types.ModuleType("astrbot.api.all")
    api_event = types.ModuleType("astrbot.api.event")
    api_platform = types.ModuleType("astrbot.api.platform")
    core = types.ModuleType("astrbot.core")
    core_message = types.ModuleType("astrbot.core.message")
    core_components = types.ModuleType("astrbot.core.message.components")
    core_components.Plain = object
    core_provider = types.ModuleType("astrbot.core.provider")
    entities = types.ModuleType("astrbot.core.provider.entities")

    api_all.logger = _Logger()
    api.logger = _Logger()
    api_all.Context = object
    api_all.AstrMessageEvent = _Event
    api_event.AstrMessageEvent = _Event
    api_platform.AstrBotMessage = object
    api_platform.MessageMember = object
    api_platform.MessageType = object
    entities.ProviderRequest = _ProviderRequest

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.all": api_all,
        "astrbot.api.event": api_event,
        "astrbot.api.platform": api_platform,
        "astrbot.core": core,
        "astrbot.core.message": core_message,
        "astrbot.core.message.components": core_components,
        "astrbot.core.provider": core_provider,
        "astrbot.core.provider.entities": entities,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def _load_module(monkeypatch, rel_path, module_name, stubs=None):
    """Load a utils module under a fake package with optional extra stubs."""
    _install_astrbot_stubs(monkeypatch)

    package_name = "persona_presence_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    utils_package = types.ModuleType(f"{package_name}.utils")
    utils_package.__path__ = [str(UTILS_DIR)]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, f"{package_name}.utils", utils_package)

    # 让 utils 包能被相对导入（stub 兄弟模块）
    for sub, obj in (stubs or {}).items():
        stub_module = types.ModuleType(f"{package_name}.utils.{sub}")
        for attr, value in obj.items():
            setattr(stub_module, attr, value)
        monkeypatch.setitem(sys.modules, f"{package_name}.utils.{sub}", stub_module)

    full_name = f"{package_name}.utils.{module_name}"
    spec = importlib.util.spec_from_file_location(
        full_name, UTILS_DIR / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, full_name, module)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# ReplyHandler
# ---------------------------------------------------------------------------


def test_reply_handler_has_no_system_reply_prompt(monkeypatch):
    handler = _load_module(monkeypatch, "reply_handler.py", "reply_handler")
    assert not hasattr(handler.ReplyHandler, "SYSTEM_REPLY_PROMPT")


def test_reply_handler_removes_current_message_echo_prefix(monkeypatch):
    handler = _load_module(monkeypatch, "reply_handler.py", "reply_handler")

    assert (
        handler.ReplyHandler.remove_echo_prefix(
            "...剧场版啊...《企鹅公路》和《海兽之子》都挺冷门的...",
            "你还知道啥冷门的好看的吗？特别是剧场版的",
        )
        == "...《企鹅公路》和《海兽之子》都挺冷门的..."
    )
    assert (
        handler.ReplyHandler.remove_echo_prefix(
            "...悠哉日常大王吧...喵帕斯那个...节奏很慢...",
            "想要更轻松一点的，题材稍微偏小众",
        )
        == "...悠哉日常大王吧...喵帕斯那个...节奏很慢..."
    )


def test_reply_handler_keeps_unrelated_particle_openers(monkeypatch):
    handler = _load_module(monkeypatch, "reply_handler.py", "reply_handler")
    reply = "...摇曳露营吧...还有孤独摇滚..."

    assert handler.ReplyHandler.remove_echo_prefix(reply, "都有点老了") == reply


def test_generate_reply_system_prompt_is_exactly_persona(monkeypatch):
    handler = _load_module(monkeypatch, "reply_handler.py", "reply_handler")
    persona = "你是温柔的猫娘，说话带喵。"
    event = _Event(message_str="在吗")
    context = _Context(persona_prompt=persona)

    req = _run(
        handler.ReplyHandler.generate_reply(
            event,
            context,
            formatted_message="[10:00] 小明(42): 在吗",
            extra_prompt="",
            prompt_mode="append",
        )
    )

    # system_prompt 只含人格，不叠加任何插件指令
    assert req.system_prompt == "当前会话人格"
    # req.prompt 是短消息占位（供向量检索类插件召回），完整上下文在 extra 中
    assert req.prompt == "在吗"
    # 完整 prompt（存于 extra，由 on_llm_request 恢复）只含上下文 + 发送者标注 + 最小结尾
    full_prompt = event.get_extra(handler.PLUGIN_CUSTOM_PROMPT, "")
    assert "在吗" in full_prompt
    assert "小明" in full_prompt
    assert "42" in full_prompt
    assert "[系统信息-当前发送者]" in full_prompt
    assert "[系统信息-当前对话对象]" not in full_prompt
    for banned in ("严禁元叙述", "系统行为指令", "回复身份", "严禁重复", "请开始回复"):
        assert banned not in full_prompt
    assert "请直接输出你的回复" in full_prompt


def test_generate_reply_includes_other_user_boundary_hint(monkeypatch):
    handler = _load_module(monkeypatch, "reply_handler.py", "reply_handler")
    event = _Event(message_str="@小明这个游戏我也玩过")
    context = _Context(persona_prompt="人格A")
    hint = (
        "[系统提示-群聊旁观边界] 当前消息直接指向其他群友；"
        "本次若回复，请用你自己的口吻、立场和经历补充相关内容。"
    )

    req = _run(
        handler.ReplyHandler.generate_reply(
            event,
            context,
            formatted_message="当前新消息：@小明这个游戏我也玩过",
            extra_prompt="",
            prompt_mode="append",
            reply_context_hint=hint,
        )
    )

    full_prompt = event.get_extra(handler.PLUGIN_CUSTOM_PROMPT, "")
    assert hint in full_prompt
    assert "机器人自己的身份" not in full_prompt
    assert "你自己的口吻、立场和经历" in full_prompt
    assert req.system_prompt == "当前会话人格"


def test_generate_reply_resolves_current_conversation_persona(monkeypatch):
    """The reply request uses the persona selected for the active conversation."""
    handler = _load_module(monkeypatch, "reply_handler.py", "reply_handler")
    event = _Event(message_str="切换后测试")
    context = _Context(persona_prompt="旧默认人格", conversation=object())

    req = _run(
        handler.ReplyHandler.generate_reply(
            event,
            context,
            formatted_message="当前会话上下文",
            extra_prompt="",
            prompt_mode="append",
        )
    )

    assert req.system_prompt == "当前会话人格"
    assert req.conversation is None


def test_generate_reply_sets_marker_extras(monkeypatch):
    handler = _load_module(monkeypatch, "reply_handler.py", "reply_handler")
    event = _Event(message_str="测试消息")
    context = _Context(persona_prompt="人格A")

    _run(
        handler.ReplyHandler.generate_reply(
            event,
            context,
            formatted_message="上下文内容",
            extra_prompt="",
            prompt_mode="append",
        )
    )

    assert event.get_extra(handler.PLUGIN_REQUEST_MARKER, False) is True
    assert event.get_extra(handler.PLUGIN_CUSTOM_SYSTEM_PROMPT, "") == "当前会话人格"
    assert "上下文内容" in event.get_extra(handler.PLUGIN_CUSTOM_PROMPT, "")
    # 短消息占位：空消息时提供 [空消息] 占位符
    empty_event = _Event(message_str="")
    req = _run(
        handler.ReplyHandler.generate_reply(
            empty_event,
            context,
            formatted_message="ctx",
            extra_prompt="",
            prompt_mode="append",
        )
    )
    assert req.prompt == "[空消息]"
    assert empty_event.get_extra(handler.PLUGIN_CURRENT_MESSAGE) == "[空消息]"


# ---------------------------------------------------------------------------
# DecisionAI
# ---------------------------------------------------------------------------


def test_decision_prompt_has_no_removed_feature_references(monkeypatch):
    decision = _load_module(
        monkeypatch,
        "decision_ai.py",
        "decision_ai",
        stubs={
            "ai_response_filter": {"AIResponseFilter": object},
            "ai_error_formatter": {
                "format_ai_error": lambda error, label: f"{label}: {error}"
            },
        },
    )
    prompt = decision.DecisionAI.SYSTEM_DECISION_PROMPT
    for removed in (
        "对话疲劳",
        "判断记录",
        "兴趣话题",
        "时间与活跃度",
        "主动对话",
        "拟人",
    ):
        assert removed not in prompt
    assert '<decision_contract version="3" task="group_participation">' in prompt
    assert "ownership = bot | other | open | unclear" in prompt
    assert "information = noise | reaction | substantive" in prompt
    assert "continuation = yes | no" in prompt
    assert "participation = direct | side | open | none" in prompt
    assert "persona_willingness = yes | no" in prompt
    assert "open 表示公开话题，不是自动邀请" in prompt
    assert "open 表示公开话题，不是自动邀请，但也不是默认 no" in prompt
    assert "最终结果：reply = persona_willingness" in prompt
    assert "九月有什么好看的番吗" in prompt
    assert "ownership == other 只表示“直接对象是别人”" in prompt
    assert "不替被@或被回复的用户作答" in prompt
    assert "而不是把“谨慎”执行成几乎永远不说话" in prompt
    assert "平台没有检测到机器人信号" in prompt
    assert "不等于消息不能开放参与" in prompt
    assert "关键词命中只是触发信号" in prompt
    assert "那是什么歌" in prompt
    assert "最近一条真实机器人回复" in prompt
    assert "短暂的无关插话不自动结束续话" in prompt
    assert "【📦近期未回复】" in prompt
    assert "地震了 / Miku好可爱" in prompt
    assert "@小明这个游戏我也玩过" in prompt
    assert "回复小明：哈哈" in prompt
    assert "还是来吧 / 我听着睡觉" in prompt
    assert "图片占位符、关键词、记忆和泛泛的人格兴趣都不是单独的回复理由" in prompt
    assert "只输出一个 JSON 对象" in prompt
    assert "interest" in prompt
    assert "reason_code" in prompt

    private_prompt = decision.DecisionAI.PRIVATE_SYSTEM_DECISION_PROMPT
    assert (
        '<decision_contract version="4" task="private_participation">' in private_prompt
    )
    assert "<state_model>" in private_prompt
    assert (
        "boundary_interpretation = literal_sleep | conversational_exit | ambiguous | none"
        in private_prompt
    )
    assert (
        "孤立的“晚安/睡了/我先睡了”不能单凭关键词判定 literal_sleep" in private_prompt
    )
    assert "重要或紧急消息提高评估优先级，但不强制 reply=yes" in private_prompt
    assert "private sleep assumption" in private_prompt
    assert "private avoidance boundary" in private_prompt
    assert "不是“当前消息必然 no”的结论" in private_prompt
    assert "<decision_order>" in private_prompt
    assert "只输出一次整体决定" in private_prompt
    assert "普通 private_reply_mode=direct 文本不会调用本契约" in private_prompt
    assert "ignore 通常在调用本契约前停止" in private_prompt
    assert "always 只在没有 active 私聊收尾边界时绕过本契约" in private_prompt
    assert "当前消息本身是否真的构成打扰" in private_prompt
    assert "别烦璃月" in private_prompt
    assert "当前消息本身是否真的构成打扰" in private_prompt
    assert "继续施压、重复催促" in private_prompt
    assert "抱歉，刚才那件事我说清楚了" in private_prompt
    assert "没有固定的四小时解禁规则" in private_prompt
    assert "当前现实时间" in private_prompt
    assert "与当前 Persona 的性格、心情、关系和边界一起判断" in private_prompt
    assert "时间只是背景因素" in private_prompt
    assert "不要把“有内容”机械等同于 yes" in private_prompt
    assert "boundary_interpretation" in private_prompt

    source = (UTILS_DIR / "decision_ai.py").read_text(encoding="utf-8")
    assert "[系统信息-群聊目标信号]" in source
    assert "不能直接断言消息没有指向机器人" in source
    assert "紧邻的机器人真实回复只可用于确认连续话轮" in source
    assert "回复或@其他用户只说明直接对象是别人" in source
    assert "不能冒充被回复者、替对方承诺或强行接管话题" in source
    assert "[persona_willingness preset: persona]" in source
    assert "当前消息是否明确指向机器人" not in source
    assert "请只基于当前消息判断是否回复" not in source
    assert "普通闲聊、寒暄、纯陈述一律不回复" not in source
    assert "不确定时倾向于回复（yes）" not in source
    assert "被@、点名或可靠回复只说明消息对象可能是当前人格" in source
    assert "正文有效只表示值得评估，不等于当前人格现在愿意接话" in source
    assert "即使是 direct，weak 或 none 也可以 no" in source
    assert "明确指向 bot 的 direct 消息" in source
    assert "@、点名、提问和关键词只能提高注意力，不能强制 yes" in source
    assert "ownership == open 时默认 no" not in source
    assert "open 不是自动邀请，但也不是默认 no" in source
    assert "continuation_context_available" not in source

    main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    command_source = (UTILS_DIR / "command_processor.py").read_text(encoding="utf-8")
    assert '@filter.command("gcp_reset")' not in command_source
    assert '@filter.command("gcp_reset_here")' not in command_source
    assert '@filter.command("gcp_clear_image_cache")' in command_source
    for preset in ("reserved", "active", "persona"):
        assert f"[persona_willingness preset: {preset}]" in main_source
    assert "仅回复直接@、明确提问、求助" not in main_source
    assert "reply_context_hint=reply_context_hint" in main_source
    assert 'self.private_emoji_mode != "ignore"' in main_source
    assert "if is_private and cm:" in main_source
    assert "last_bot_reply_sender_id" in main_source
    assert "current_sender_id == last_bot_reply_sender_id" in main_source
    assert "_last_bot_reply_context" in main_source
    assert "Suppressed unverified group continuation" in main_source
    assert "speculative_private_decision" in main_source
    assert "Started speculative decision before the batch window" in main_source
    assert "Followers arrived; invalidating speculative decision" in main_source
    assert "No follower arrived; reused speculative decision" in main_source
    assert "message_id not in self._agent_done_flags" in main_source
    assert "机器人自己的身份" not in main_source
    assert (
        "is_at_all_message\n                    )\n                    and not compact_current_text"
        in main_source
    )

    assert "一对一私聊" in decision.DecisionAI.PRIVATE_SYSTEM_DECISION_PROMPT
    assert "安静、冷淡或话少" in decision.DecisionAI.PRIVATE_SYSTEM_DECISION_PROMPT


# ---------------------------------------------------------------------------
# Structured participation policy
# ---------------------------------------------------------------------------


def _load_participation(monkeypatch):
    return _load_module(monkeypatch, "participation.py", "participation")


def _decision_payload(**overrides):
    payload = {
        "reply": "yes",
        "target": "open",
        "information": "substantive",
        "continuation": "no",
        "participation": "open",
        "interest": "strong",
        "reason_code": "shared_interest",
        "confidence": "high",
        "topic_key": "anime",
    }
    payload.update(overrides)
    return payload


def test_participation_policy_balances_direct_and_ambient_messages(monkeypatch):
    participation = _load_participation(monkeypatch)
    normalize = participation.normalize_decision_payload

    open_interest = normalize(_decision_payload())
    assert open_interest.reply is True
    assert open_interest.target == "open"
    assert open_interest.participation == "open"

    model_declined = normalize(
        _decision_payload(
            reply="no",
            interest="weak",
            reason_code="none",
        )
    )
    assert model_declined.reply is False

    open_modest_personal_hook = normalize(
        _decision_payload(interest="weak", reason_code="shared_interest")
    )
    assert open_modest_personal_hook.reply is True

    open_model_choice = normalize(
        _decision_payload(interest="weak", reason_code="none")
    )
    assert open_model_choice.reply is True

    direct_boring = normalize(
        _decision_payload(
            reply="no",
            target="bot",
            participation="direct",
            interest="none",
            reason_code="none",
        ),
        is_directly_addressed=True,
    )
    assert direct_boring.reply is False

    direct_relevant = normalize(
        _decision_payload(
            target="bot",
            participation="direct",
            interest="strong",
            reason_code="shared_interest",
        ),
        is_directly_addressed=True,
    )
    assert direct_relevant.reply is True
    assert direct_relevant.handoff_hint.startswith("[系统提示-本次参与依据]")
    assert "共同兴趣" in direct_relevant.handoff_hint


def test_participation_policy_requires_independent_side_comment(monkeypatch):
    participation = _load_participation(monkeypatch)
    normalize = participation.normalize_decision_payload

    logistics = normalize(
        _decision_payload(
            target="other",
            participation="none",
            interest="strong",
            reason_code="shared_interest",
        ),
        has_at_others=True,
    )
    assert logistics.reply is False

    independent_comment = normalize(
        _decision_payload(
            target="other",
            participation="side",
            interest="strong",
            reason_code="personal_experience",
        ),
        has_at_others=True,
    )
    assert independent_comment.reply is True
    assert "不要替其他用户" in independent_comment.handoff_hint

    weak_side_comment = normalize(
        _decision_payload(
            target="other",
            participation="side",
            interest="weak",
            reason_code="shared_interest",
        ),
        has_at_others=True,
    )
    assert weak_side_comment.reply is True


def test_continuation_payload_stays_model_owned_before_runtime_boundary(monkeypatch):
    participation = _load_participation(monkeypatch)
    normalize = participation.normalize_decision_payload
    decision = normalize(
        _decision_payload(
            target="bot",
            continuation="yes",
            participation="direct",
            reason_code="continuation",
        )
    )

    assert decision.reply is True
    assert decision.continuation == "yes"
    assert decision.reason_code == "continuation"


def test_participation_parser_accepts_json_and_rejects_unknown_enums(monkeypatch):
    response_filter = _load_module(
        monkeypatch,
        "ai_response_filter.py",
        "ai_response_filter",
    )
    decision = _load_module(
        monkeypatch,
        "decision_ai.py",
        "decision_ai",
        stubs={
            "ai_response_filter": {
                "AIResponseFilter": response_filter.AIResponseFilter
            },
            "ai_error_formatter": {
                "format_ai_error": lambda error, label: f"{label}: {error}"
            },
        },
    )
    raw = (
        "\n"
        '{"reply":"yes","target":"open",'
        '"information":"substantive","participation":"open",'
        '"interest":"strong","reason_code":"shared_interest",'
        '"confidence":"high","topic_key":"番剧"}'
    )
    payload = decision.DecisionAI._parse_structured_decision(raw)
    assert payload is not None
    assert payload["reply"] == "yes"

    participation = _load_participation(monkeypatch)
    invalid = participation.normalize_decision_payload(
        payload | {"interest": "very_strong"}
    )
    assert invalid.reply is False

    private = participation.normalize_decision_payload(
        payload
        | {
            "target": "bot",
            "participation": "direct",
            "boundary_interpretation": "ambiguous",
        },
        is_private=True,
    )
    assert private.reply is True
    assert private.boundary_interpretation == "ambiguous"


def test_malformed_group_json_fails_closed_in_evaluate(monkeypatch):
    response_filter = _load_module(
        monkeypatch,
        "ai_response_filter.py",
        "ai_response_filter",
    )
    decision = _load_module(
        monkeypatch,
        "decision_ai.py",
        "decision_ai",
        stubs={
            "ai_response_filter": {
                "AIResponseFilter": response_filter.AIResponseFilter
            },
            "ai_error_formatter": {
                "format_ai_error": lambda error, label: f"{label}: {error}"
            },
        },
    )

    async def resolve_judgment_persona(**kwargs):
        return {"system_prompt": ""}

    decision.DecisionAI.resolve_judgment_persona = staticmethod(
        resolve_judgment_persona
    )

    class ProviderResponse:
        completion_text = '{"reply":"yes","target":"open"'

    class Provider:
        def __init__(self):
            self.last_prompt = ""
            self.last_system_prompt = ""

        async def text_chat(self, **kwargs):
            self.last_prompt = kwargs.get("prompt", "")
            self.last_system_prompt = kwargs.get("system_prompt", "")
            return ProviderResponse()

    class Context:
        def __init__(self):
            self.provider = Provider()

        def get_using_provider(self):
            return self.provider

        def get_config(self):
            return {"timezone": "Asia/Shanghai"}

    class Event:
        session_id = "session"

        def get_sender_id(self):
            return "user"

        def get_sender_name(self):
            return "User"

    context = Context()
    result = _run(
        decision.DecisionAI.evaluate(
            context,
            Event(),
            "当前消息",
            "",
            "",
            is_private=False,
        )
    )
    assert "[系统信息-当前现实时间]" in context.provider.last_prompt
    assert "Asia/Shanghai" in context.provider.last_prompt
    assert result.reply is False
    assert result.source == "error"
    assert result.error == "invalid_structured_output"

    _run(
        decision.DecisionAI.evaluate(
            context,
            Event(),
            "私聊消息",
            "",
            "只在私聊中保持简短",
            prompt_mode="append",
            reply_tendency="reserved",
            is_private=True,
            include_current_time=False,
        )
    )
    private_prompt = context.provider.last_system_prompt + context.provider.last_prompt
    assert "一对一私聊中" in private_prompt
    assert "只在私聊中保持简短" in private_prompt
    assert "不把问候、弱话题或非必要消息自动变成回复" in private_prompt

    _run(
        decision.DecisionAI.evaluate(
            context,
            Event(),
            "私聊消息",
            "",
            "仅输出 yes 或 no",
            prompt_mode="override",
            is_private=True,
            include_current_time=False,
        )
    )
    override_prompt = context.provider.last_system_prompt + context.provider.last_prompt
    assert "仅输出 yes 或 no" in override_prompt
    assert "一对一私聊中" not in override_prompt


def test_global_datetime_setting_can_disable_decision_time(monkeypatch):
    response_filter = _load_module(
        monkeypatch,
        "ai_response_filter.py",
        "ai_response_filter",
    )
    decision = _load_module(
        monkeypatch,
        "decision_ai.py",
        "decision_ai",
        stubs={
            "ai_response_filter": {
                "AIResponseFilter": response_filter.AIResponseFilter
            },
            "ai_error_formatter": {
                "format_ai_error": lambda error, label: f"{label}: {error}"
            },
        },
    )

    class ProviderResponse:
        completion_text = (
            '{"reply":"no","target":"open","information":"noise",'
            '"participation":"none","interest":"none",'
            '"reason_code":"none","confidence":"high","topic_key":""}'
        )

    class Provider:
        def __init__(self):
            self.last_prompt = ""
            self.last_system_prompt = ""

        async def text_chat(self, **kwargs):
            self.last_prompt = kwargs.get("prompt", "")
            self.last_system_prompt = kwargs.get("system_prompt", "")
            return ProviderResponse()

    class Context:
        def __init__(self):
            self.provider = Provider()

        def get_using_provider(self):
            return self.provider

        def get_config(self):
            return {
                "timezone": "Asia/Shanghai",
                "provider_settings": {"datetime_system_prompt": False},
            }

    class Event:
        session_id = "session"

        def get_sender_id(self):
            return "user"

        def get_sender_name(self):
            return "User"

    context = Context()
    result = _run(
        decision.DecisionAI.evaluate(
            context,
            Event(),
            "当前消息",
            "",
            "",
            is_private=True,
        )
    )
    assert result.reply is False
    assert "[系统信息-当前现实时间]" not in context.provider.last_prompt


def test_participation_throttle_limits_only_unsolicited_group_replies(monkeypatch):
    participation = _load_participation(monkeypatch)
    throttle = participation.ParticipationThrottle(
        min_interval_seconds=30,
        window_seconds=100,
        max_replies_per_window=2,
    )
    open_decision = participation.ParticipationDecision(
        reply=True,
        target="open",
        participation="open",
        information="substantive",
        interest="strong",
        reason_code="shared_interest",
    )
    assert throttle.allow_and_record("group", open_decision, now=0) == (True, "")
    assert throttle.allow_and_record("group", open_decision, now=10) == (
        False,
        "ambient_min_interval",
    )
    assert throttle.allow_and_record("group", open_decision, now=31) == (True, "")
    assert throttle.allow_and_record("group", open_decision, now=62) == (
        False,
        "ambient_window_cap",
    )

    direct = open_decision.with_reply(
        True,
        target="bot",
        participation="direct",
        reason_code="direct_request",
    )
    assert throttle.allow_and_record("group", direct, now=10) == (True, "")


def test_rejected_cache_entries_do_not_become_active_context(monkeypatch):
    import time as _time

    manager_module = _load_cache_manager(monkeypatch)
    manager = manager_module.MessageCacheManager()
    now = _time.time()
    manager.pending_messages_cache["group"] = [
        {"content": "ignored", "timestamp": now, "decision_state": "observed"},
        {"content": "active", "timestamp": now},
    ]
    assert [item["content"] for item in manager.get_cached_messages("group")] == [
        "active"
    ]


def test_decision_ai_reads_official_group_context():
    source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

    assert "_get_official_group_context_records" in source
    assert 'get_registered_star("astrbot")' in source
    assert "_group_context_record_id" in source
    assert "[AstrBot 官方群聊上下文]" in source
    assert "不要因为这些消息本身而回复" in source


def test_lazy_image_urls_survive_cache_normalization(monkeypatch):
    """Deferred image URLs remain available when a message is later accepted."""
    import time as _time

    manager_module = _load_cache_manager(monkeypatch)
    manager = manager_module.MessageCacheManager()
    now = _time.time()

    assert (
        manager.add_to_cache(
            "qq:group:42",
            {
                "content": "[图片]",
                "image_urls": ["https://cdn.example/image.png"],
                "timestamp": now,
            },
        )
        == 1
    )
    assert manager.pending_messages_cache["qq:group:42"][0]["image_urls"] == [
        "https://cdn.example/image.png"
    ]
    assert (
        manager.add_to_cache(
            "qq:group:42", {"content": "[图片]", "image_urls": [], "timestamp": now}
        )
        == 0
    )


# ---------------------------------------------------------------------------
# MessageCacheManager expiry filter
# ---------------------------------------------------------------------------


def _load_cache_manager(monkeypatch):
    return _load_module(
        monkeypatch,
        "message_cache_manager.py",
        "message_cache_manager",
        stubs={
            "message_processor": {"MessageProcessor": object},
            "message_cleaner": {"MessageCleaner": object},
            "context_manager": {"ContextManager": object},
        },
    )


def test_expiry_filter_removes_old_messages(monkeypatch):
    manager = _load_cache_manager(monkeypatch)
    import time as _time

    now = _time.time()
    messages = [
        {"content": "旧消息", "timestamp": now - 9999},
        {"content": "新消息", "timestamp": now},
    ]
    filtered = manager._filter_expired_cached_messages(
        messages, cache_ttl_seconds=600, max_cache_count=10
    )
    assert len(filtered) == 1
    assert filtered[0]["content"] == "新消息"


def test_expiry_filter_respects_max_count(monkeypatch):
    manager = _load_cache_manager(monkeypatch)
    import time as _time

    now = _time.time()
    messages = [{"content": f"m{i}", "timestamp": now - 100 + i} for i in range(5)]
    filtered = manager._filter_expired_cached_messages(
        messages, cache_ttl_seconds=600, max_cache_count=2
    )
    assert len(filtered) == 2
    # 保留最新的两条（按 timestamp 排序）
    assert filtered[-1]["content"] == "m4"


# ---------------------------------------------------------------------------
# KeywordChecker
# ---------------------------------------------------------------------------


def test_keyword_checker_triggers_and_blacklist(monkeypatch):
    checker = _load_module(
        monkeypatch,
        "keyword_checker.py",
        "keyword_checker",
    )

    class _MsgEvent:
        def __init__(self, text):
            self._text = text
            self._components = []

        def get_message_str(self):
            return self._text

        def get_message_outline(self):
            return self._text

        def get_messages(self):
            return self._components

    hit_event = _MsgEvent("有人提到机器人关键词啦")
    is_hit, matched = checker.KeywordChecker.check_trigger_keywords_with_match(
        hit_event, ["机器人"]
    )
    assert is_hit is True
    assert matched == "机器人"

    miss_event = _MsgEvent("普通内容")
    is_hit, matched = checker.KeywordChecker.check_trigger_keywords_with_match(
        miss_event, ["机器人"]
    )
    assert is_hit is False

    banned = checker.KeywordChecker.check_blacklist_keywords(miss_event, ["违禁词"])
    assert banned is False
    banned_event = _MsgEvent("这条消息包含违禁词")
    banned = checker.KeywordChecker.check_blacklist_keywords(banned_event, ["违禁词"])
    assert banned is True
