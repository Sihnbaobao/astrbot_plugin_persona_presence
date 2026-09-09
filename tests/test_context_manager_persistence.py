"""Regression tests for official AstrBot history persistence."""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path


class _Logger:
    """Logger stub required by the standalone context module."""

    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _Event:
    """Minimal event carrying the official unified message origin."""

    unified_msg_origin = "qq:GroupMessage:42"
    session_id = "session-42"

    def get_platform_name(self):
        return "qq"

    def get_platform_id(self):
        return "qq-1"

    def is_private_chat(self):
        return False

    def get_group_id(self):
        return "42"

    def get_sender_id(self):
        return "user-7"

    def get_sender_name(self):
        return "Alice"

    def get_self_id(self):
        return "bot-1"

    def get_result(self):
        return None


def _load_context_manager(monkeypatch):
    """Load ContextManager with the minimal imports needed by these tests."""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api_all = types.ModuleType("astrbot.api.all")
    api_all.logger = _Logger()
    api_all.AstrBotMessage = type("AstrBotMessage", (), {})
    api_all.AstrMessageEvent = type("AstrMessageEvent", (), {})
    api_all.MessageMember = type("MessageMember", (), {})
    api_all.MessageType = type(
        "MessageType",
        (),
        {"GROUP_MESSAGE": "GroupMessage", "FRIEND_MESSAGE": "FriendMessage"},
    )
    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = type("Plain", (), {})
    package = types.ModuleType("context_manager_test")
    package.__path__ = []
    message_processor = types.ModuleType("context_manager_test.message_processor")
    message_processor.MessageProcessor = type("MessageProcessor", (), {})
    message_cleaner = types.ModuleType("context_manager_test.message_cleaner")
    message_cleaner.MessageCleaner = type("MessageCleaner", (), {})
    message_cleaner.MessageCleaner.clean_message = staticmethod(lambda value: value)

    for name, module in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.all": api_all,
        "astrbot.api.message_components": components,
        "context_manager_test": package,
        "context_manager_test.message_processor": message_processor,
        "context_manager_test.message_cleaner": message_cleaner,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    path = Path(__file__).parents[1] / "utils" / "context_manager.py"
    spec = importlib.util.spec_from_file_location(
        "context_manager_test.context_manager",
        path,
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module.ContextManager


def test_history_read_uses_unified_msg_origin(monkeypatch):
    """Official platform history reads use the same key as AstrBot core writes."""
    context_manager = _load_context_manager(monkeypatch)
    event = _Event()
    calls = []

    class History:
        async def get(self, **kwargs):
            calls.append(kwargs)
            return [{"content": "hello"}]

    context = types.SimpleNamespace(message_history_manager=History())
    message = types.SimpleNamespace(message_str="hello")
    monkeypatch.setattr(
        context_manager,
        "_official_history_to_message",
        staticmethod(lambda **_kwargs: message),
    )

    history = asyncio.run(
        context_manager.get_history_messages_with_fallback(
            event, max_messages=10, context=context
        )
    )

    assert history == [message]
    assert calls[0]["user_id"] == event.unified_msg_origin
    assert calls[0]["user_id"] != event.get_group_id()


def test_history_clear_uses_unified_msg_origin(monkeypatch):
    """Reset deletes only the official platform-history session key."""
    context_manager = _load_context_manager(monkeypatch)
    event = _Event()
    delete_calls = []
    update_calls = []

    class History:
        async def delete(self, **kwargs):
            delete_calls.append(kwargs)

    class Conversations:
        async def get_curr_conversation_id(self, umo):
            assert umo == event.unified_msg_origin
            return "conversation-1"

        async def update_conversation(self, *args):
            update_calls.append(args)

    context = types.SimpleNamespace(
        message_history_manager=History(),
        conversation_manager=Conversations(),
    )

    assert asyncio.run(context_manager.clear_official_history_for_event(context, event))
    assert delete_calls[0]["user_id"] == event.unified_msg_origin
    assert update_calls == [(event.unified_msg_origin, "conversation-1", [])]


def test_user_and_bot_platform_saves_use_unified_msg_origin(monkeypatch):
    """Both platform-history inserts use the official unified origin key."""
    context_manager = _load_context_manager(monkeypatch)
    event = _Event()
    insert_calls = []

    class History:
        async def insert(self, **kwargs):
            insert_calls.append(kwargs)

    context = types.SimpleNamespace(message_history_manager=History())
    assert asyncio.run(context_manager.save_user_message(event, "hello", context))
    assert asyncio.run(context_manager.save_bot_message(event, "hi", context))
    assert [call["user_id"] for call in insert_calls] == [
        event.unified_msg_origin,
        event.unified_msg_origin,
    ]


def test_direct_platform_saves_are_serialized(monkeypatch):
    """Concurrent direct user and bot inserts never overlap."""
    context_manager = _load_context_manager(monkeypatch)
    active = 0
    maximum = 0

    class History:
        async def insert(self, **_kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1

    context = types.SimpleNamespace(message_history_manager=History())

    async def scenario():
        await asyncio.gather(
            context_manager.save_user_message(_Event(), "one", context),
            context_manager.save_bot_message(_Event(), "two", context),
        )

    asyncio.run(scenario())
    assert maximum == 1


def test_complete_official_saves_are_serialized(monkeypatch):
    """Concurrent read-modify-write turns never overlap."""
    context_manager = _load_context_manager(monkeypatch)
    active = 0
    maximum = 0

    async def fake_unlocked(*_args, **_kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return True

    monkeypatch.setattr(
        context_manager,
        "_save_to_official_conversation_with_cache_unlocked",
        staticmethod(fake_unlocked),
    )

    async def scenario():
        await asyncio.gather(
            context_manager.save_to_official_conversation_with_cache(
                _Event(), [], "one", "reply-one", object()
            ),
            context_manager.save_to_official_conversation_with_cache(
                _Event(), [], "two", "reply-two", object()
            ),
        )

    asyncio.run(scenario())
    assert maximum == 1
