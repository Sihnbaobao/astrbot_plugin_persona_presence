"""Regression tests for deterministic group addressing."""

import ast
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
UTILS_DIR = REPO_ROOT / "utils"


class _Logger:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _Plain:
    def __init__(self, text):
        self.text = text


class _At:
    def __init__(self, qq):
        self.qq = qq


class _Reply:
    def __init__(self, sender_id, message_str=""):
        self.sender_id = sender_id
        self.message_str = message_str


class _Event:
    def __init__(self, message_chain, message_str=""):
        self.message_obj = types.SimpleNamespace(message=message_chain)
        self._message_str = message_str
        self.unified_msg_origin = "bot_name:GroupMessage:10001"

    def get_self_id(self):
        return "bot_self"

    def get_sender_id(self):
        return "user_1"

    def get_sender_name(self):
        return "测试用户"

    def get_message_str(self):
        return self._message_str

    def get_message_outline(self):
        return self._message_str


def _install_stubs(monkeypatch):
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api_all = types.ModuleType("astrbot.api.all")
    core = types.ModuleType("astrbot.core")
    core_message = types.ModuleType("astrbot.core.message")
    core_components = types.ModuleType("astrbot.core.message.components")

    logger = _Logger()
    api.logger = logger
    api_all.logger = logger
    api_all.AstrMessageEvent = _Event
    core_components.At = _At
    core_components.Plain = _Plain
    core_components.Reply = _Reply

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.all": api_all,
        "astrbot.core": core,
        "astrbot.core.message": core_message,
        "astrbot.core.message.components": core_components,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def _load_module(monkeypatch, module_name):
    _install_stubs(monkeypatch)
    package_name = "group_addressing_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    utils_package = types.ModuleType(f"{package_name}.utils")
    utils_package.__path__ = [str(UTILS_DIR)]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, f"{package_name}.utils", utils_package)

    full_name = f"{package_name}.utils.{module_name}"
    spec = importlib.util.spec_from_file_location(
        full_name, UTILS_DIR / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, full_name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def addressing_modules(monkeypatch):
    return (
        _load_module(monkeypatch, "message_processor"),
        _load_module(monkeypatch, "keyword_checker"),
    )


def test_decision_address_metadata_is_passed_through():
    tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
    decision_method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_check_ai_decision"
    )
    parameter_names = {argument.arg for argument in decision_method.args.args}
    assert {"is_directly_addressed", "is_reply_to_other"} <= parameter_names

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_check_ai_decision"
    ]
    calls_with_address_metadata = [
        call
        for call in calls
        if {keyword.arg for keyword in call.keywords}
        >= {"is_directly_addressed", "is_reply_to_other"}
    ]
    assert calls_with_address_metadata
    assert len(calls_with_address_metadata) == len(calls)


def test_forced_smart_flag_is_initialized_before_payload_branches():
    source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert (
        "early_message_id = processing_id\n"
        "        _is_forced = is_at_message or has_trigger_keyword"
    ) in source
    assert "if not use_smart_batch:\n                break" in source


def test_private_turns_skip_legacy_per_chat_wait():
    source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert "private_chat_parallel = bool(is_private)" in source
    assert "1 if private_chat_parallel else self.concurrent_wait_max_loops" in source
    assert "if still_processing and not private_chat_parallel" in source


def test_direct_private_turns_can_use_smart_batching():
    source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("        use_smart_batch = (")
    end = source.index("        # 步骤2: 检查消息触发器", start)
    smart_block = source[start:end]
    assert 'self.private_concurrent_mode == "smart"' in smart_block
    assert 'self.private_reply_mode != "direct"' not in smart_block
    assert "private_boundary.seconds_until_reopen() > 0" in smart_block


def test_direct_private_formal_draft_starts_before_smart_commit():
    main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    reply_source = (UTILS_DIR / "reply_handler.py").read_text(encoding="utf-8")

    assert "speculative_private_reply = bool(" in main_source
    assert "Started buffered formal reply before the batch window" in main_source
    assert "precomputed_reply_text = await speculative_reply_task" in main_source
    assert "elif not speculative_reply_task.done():" in main_source
    assert "Draft still running at the Smart commit" in main_source
    assert "precomputed_reply_text=precomputed_reply_text" in main_source
    assert "ResultContentType.LLM_RESULT" in main_source
    assert "await _discard_speculative_reply()" in main_source
    assert "async def generate_speculative_reply(" in reply_source
    assert "allow_tools=True" in reply_source
    assert "func_tool=request.func_tool" in reply_source
    assert "Tool call requested" in reply_source


def test_direct_private_late_night_routes_plain_text_through_decision_ai():
    source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

    assert "_PRIVATE_LATE_NIGHT_START_HOUR = 1" in source
    assert "_PRIVATE_LATE_NIGHT_END_HOUR = 7" in source
    assert "_PRIVATE_LATE_NIGHT_ACTIVE_SECONDS = 45 * 60" in source
    assert "_private_late_night_active_until" in source
    assert "Existing awake session;" in source
    assert "private_late_night_review = bool(" in source
    assert "Sending ordinary private text through DecisionAI" in source
    assert "or private_late_night_review" in source
    assert "and not private_late_night_review" in source
    assert "probabilistic rest assumption" in source


def test_quoted_reply_cannot_fake_at_or_keyword(addressing_modules):
    processor, checker = addressing_modules
    event = _Event(
        [_Reply("other", "@bot_self 璃月"), _Plain("算了")],
        "[引用消息] @bot_self 璃月 算了",
    )

    assert not processor.MessageProcessor.is_at_message(event)
    assert not checker.KeywordChecker.check_trigger_keywords(event, ["璃月"])
    assert processor.MessageProcessor.get_reply_target_id(event) == "other"
    assert not processor.MessageProcessor.is_reply_to_bot(event)


def test_current_message_address_signals_are_preserved(addressing_modules):
    processor, checker = addressing_modules

    at_event = _Event([_At("bot_self"), _Plain("你好")], "@bot_self 你好")
    keyword_event = _Event([_Reply("other"), _Plain("璃月，听得到吗")], "璃月")
    bot_reply_event = _Event(
        [_Reply("bot_self", "上一条回复"), _Plain("继续")],
        "[引用消息] 继续",
    )
    other_reply_event = _Event(
        [_Reply("other", "上一条消息"), _Plain("继续")],
        "[引用消息] 继续",
    )

    assert processor.MessageProcessor.is_at_message(at_event)
    assert checker.KeywordChecker.check_trigger_keywords(keyword_event, ["璃月"])
    assert processor.MessageProcessor.get_reply_target_id(bot_reply_event) == "bot_self"
    assert processor.MessageProcessor.is_reply_to_bot(bot_reply_event)
    assert processor.MessageProcessor.get_reply_target_id(other_reply_event) == "other"
    assert not processor.MessageProcessor.is_reply_to_bot(other_reply_event)


def test_keyword_metadata_does_not_invite_a_reply(addressing_modules):
    processor, _checker = addressing_modules
    event = _Event([_Plain("璃月")], "璃月")

    current = processor.MessageProcessor.add_metadata_to_message(
        event,
        "璃月",
        False,
        True,
        trigger_type="keyword",
    )
    cached = processor.MessageProcessor.add_metadata_from_cache(
        "璃月",
        "user_1",
        "测试用户",
        None,
        False,
        True,
        trigger_type="keyword",
    )

    for rendered in (current, cached):
        assert "命中了配置关键词" in rendered
        assert "不代表消息在对你说" in rendered
        assert "自然回应" not in rendered
