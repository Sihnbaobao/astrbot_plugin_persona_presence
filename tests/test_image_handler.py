"""Regression tests for quoted-image handling.

The plugin normally runs inside AstrBot.  These tests provide a small set of
message-component doubles so the pure message-chain logic can be tested
without installing the whole AstrBot runtime.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


class _Logger:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _BaseMessageComponent:
    pass


class _Plain(_BaseMessageComponent):
    def __init__(self, text):
        self.text = text


class _Image(_BaseMessageComponent):
    def __init__(self, name):
        self.name = name
        self.file = name
        self.convert_calls = 0

    async def convert_to_file_path(self):
        self.convert_calls += 1
        return self.name


class _Reply(_BaseMessageComponent):
    def __init__(
        self,
        chain=None,
        *,
        sender_nickname=None,
        sender_id=None,
        message_str=None,
        message=None,
    ):
        self.chain = chain
        self.sender_nickname = sender_nickname
        self.sender_id = sender_id
        self.message_str = message_str
        self.message = message


class _Face(_BaseMessageComponent):
    pass


class _At(_BaseMessageComponent):
    pass


class _AtAll(_BaseMessageComponent):
    pass


@pytest.fixture
def image_handler(monkeypatch):
    """Load image_handler with lightweight AstrBot module stubs."""

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api_all = types.ModuleType("astrbot.api.all")
    message_components = types.ModuleType("astrbot.api.message_components")
    core = types.ModuleType("astrbot.core")
    core_message = types.ModuleType("astrbot.core.message")
    core_components = types.ModuleType("astrbot.core.message.components")

    api_all.AstrMessageEvent = object
    api_all.Context = object
    api_all.BaseMessageComponent = _BaseMessageComponent
    api_all.Image = _Image
    api_all.Plain = _Plain
    api_all.logger = _Logger()

    message_components.Face = _Face
    message_components.At = _At
    message_components.AtAll = _AtAll
    message_components.Reply = _Reply

    class _Video(_BaseMessageComponent):
        pass

    class _Record(_BaseMessageComponent):
        pass

    class _File(_BaseMessageComponent):
        pass

    core_components.Video = _Video
    core_components.Record = _Record
    core_components.File = _File

    # image_handler now imports emoji_detector, which needs astrbot.api.logger.
    api.logger = _Logger()

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.all": api_all,
        "astrbot.api.message_components": message_components,
        "astrbot.core": core,
        "astrbot.core.message": core_message,
        "astrbot.core.message.components": core_components,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    package_name = "group_chat_plus_test_package"
    package = types.ModuleType(package_name)
    package.__path__ = [str(Path(__file__).parents[1])]
    utils_package = types.ModuleType(f"{package_name}.utils")
    utils_package.__path__ = [str(Path(__file__).parents[1] / "utils")]
    cache_module = types.ModuleType(f"{package_name}.utils.image_description_cache")
    cache_module.ImageDescriptionCache = object
    formatter_module = types.ModuleType(f"{package_name}.utils.ai_error_formatter")
    formatter_module.format_ai_error = lambda error, label: f"{label}: {error}"

    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, f"{package_name}.utils", utils_package)
    monkeypatch.setitem(
        sys.modules,
        f"{package_name}.utils.image_description_cache",
        cache_module,
    )
    monkeypatch.setitem(
        sys.modules,
        f"{package_name}.utils.ai_error_formatter",
        formatter_module,
    )

    module_name = f"{package_name}.utils.image_handler"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).parents[1] / "utils" / "image_handler.py",
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module, _Plain, _Image, _Reply


def test_analyze_message_finds_images_inside_reply_chain(image_handler):
    handler, Plain, Image, Reply = image_handler
    quoted_image = Image("quoted.png")
    message = [
        Plain("请看看"),
        Reply(
            [Plain("被引用的文字"), quoted_image],
            sender_nickname="Alice",
            sender_id="42",
        ),
    ]

    has_image, has_text, images = handler.ImageHandler._analyze_message(message)

    assert has_image is True
    assert has_text is True
    assert images == [quoted_image]


def test_analyze_message_applies_image_limit_after_recursive_walk(image_handler):
    handler, Plain, Image, Reply = image_handler
    first = Image("first.png")
    quoted = Image("quoted.png")
    last = Image("last.png")
    message = [first, Reply([Plain("引用"), quoted]), last]

    _, _, images = handler.ImageHandler._analyze_message(message, max_images=2)

    assert images == [first, quoted]


def test_reply_nesting_depth_is_bounded(image_handler):
    handler, Plain, Image, Reply = image_handler
    nested = Image("too-deep.png")
    for _ in range(handler._MAX_REPLY_NESTING_DEPTH + 1):
        nested = Reply([nested])

    has_image, has_text, images = handler.ImageHandler._analyze_message([nested])

    assert has_image is False
    assert has_text is True
    assert images == []


def test_render_keeps_image_description_order_across_reply_chain(image_handler):
    handler, Plain, Image, Reply = image_handler
    message = [
        Plain("前"),
        Image("outer.png"),
        Reply(
            [Plain("引用"), Image("quoted.png")],
            sender_nickname="Alice",
            sender_id="42",
        ),
        Image("after.png"),
    ]

    rendered = handler.ImageHandler._render_message_chain(
        message,
        image_descriptions={0: "外图", 1: "引用图", 2: "后图"},
    )

    assert rendered == (
        "前[图片内容: 外图]"
        "[引用 >>> Alice(ID:42): 引用[图片内容: 引用图]]\n"
        "[图片内容: 后图]"
    )


def test_extract_text_only_removes_nested_images_but_keeps_quote_text(image_handler):
    handler, Plain, Image, Reply = image_handler
    message = [
        Plain("当前消息"),
        Reply(
            [Plain("引用消息"), Image("quoted.png")],
            sender_nickname="Alice",
            sender_id="42",
        ),
    ]

    text = handler.ImageHandler._extract_text_only(message)

    assert text == "当前消息[引用 >>> Alice(ID:42): 引用消息]"


def test_format_reply_uses_message_fallback_and_marks_bot_sender(image_handler):
    handler, _, _, Reply = image_handler
    reply = Reply(
        chain=None,
        sender_nickname="水原千鹤",
        sender_id="3683026476",
        message_str="旧版引用文本",
    )

    formatted = handler.ImageHandler._format_reply_component(
        reply,
        self_id="3683026476",
    )

    assert formatted == "[引用 >>> 水原千鹤(你)(ID:3683026476): 旧版引用文本]\n"


def test_deferred_processing_keeps_placeholders_without_vision(image_handler):
    """Deferred mode extracts image paths without calling a provider."""
    handler, Plain, Image, _ = image_handler

    image = Image("image.png")

    class Event:
        message_obj = types.SimpleNamespace(message=[Plain("这张图怎么样"), image])

        def get_message_outline(self):
            return "这张图怎么样[图片]"

    processed = asyncio.run(
        handler.ImageHandler.process_message_images(
            Event(),
            context=object(),
            enable_image_processing=True,
            image_to_text_scope="all",
            image_to_text_provider_id="vision-provider",
            image_to_text_prompt="describe",
            is_at_message=False,
            has_trigger_keyword=False,
            defer_image_processing=True,
        )
    )

    assert processed == (True, "这张图怎么样[图片]", ["image.png"], True)
    assert image.convert_calls == 0


def test_select_relevant_image_urls_returns_provider_indexes(image_handler):
    """The relevance gate maps valid provider indexes back to URLs."""
    handler, _, _, _ = image_handler

    class Provider:
        def __init__(self):
            self.calls = []

        async def text_chat(self, **kwargs):
            self.calls.append(kwargs)
            return types.SimpleNamespace(
                completion_text=f"{chr(96) * 3}json\n[2]\n{chr(96) * 3}"
            )

    provider = Provider()

    class Context:
        def get_using_provider(self):
            return provider

    selected = asyncio.run(
        handler.ImageHandler.select_relevant_image_urls(
            Context(),
            ["first.png", "second.png"],
            "用户问这张图怎么样",
        )
    )

    assert selected == ["second.png"]
    assert provider.calls[0]["image_urls"] == ["first.png", "second.png"]
    assert provider.calls[0]["contexts"] == []


@pytest.mark.parametrize("completion_text", ["not-json", "[99]"])
def test_select_relevant_image_urls_rejects_invalid_selection(
    image_handler, completion_text
):
    """Invalid or out-of-range provider output never selects an image."""
    handler, _, _, _ = image_handler

    class Provider:
        async def text_chat(self, **kwargs):
            return types.SimpleNamespace(completion_text=completion_text)

    class Context:
        def get_using_provider(self):
            return Provider()

    selected = asyncio.run(
        handler.ImageHandler.select_relevant_image_urls(
            Context(), ["image.png"], "普通文字对话"
        )
    )

    assert selected is None


def test_select_relevant_image_urls_accepts_empty_selection(image_handler):
    """A valid empty JSON array means that no candidate is related."""
    handler, _, _, _ = image_handler

    class Provider:
        async def text_chat(self, **kwargs):
            return types.SimpleNamespace(completion_text="[]")

    class Context:
        def get_using_provider(self):
            return Provider()

    selected = asyncio.run(
        handler.ImageHandler.select_relevant_image_urls(
            Context(), ["image.png"], "普通文字对话"
        )
    )

    assert selected == []


def test_describe_image_urls_limits_concurrency(image_handler):
    """Description requests are bounded and share one overall deadline."""
    handler, _, _, _ = image_handler

    class Provider:
        def __init__(self):
            self.active = 0
            self.max_active = 0

        async def text_chat(self, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                return types.SimpleNamespace(
                    completion_text=f"description for {kwargs['image_urls'][0]}"
                )
            finally:
                self.active -= 1

    provider = Provider()

    class Context:
        def get_provider_by_id(self, provider_id):
            return provider

        def get_using_provider(self):
            return provider

    descriptions = asyncio.run(
        handler.ImageHandler.describe_image_urls(
            Context(),
            ["1.png", "2.png", "3.png", "4.png"],
            provider_id="vision-provider",
            prompt="describe",
            timeout=1,
        )
    )

    assert set(descriptions) == {"1.png", "2.png", "3.png", "4.png"}
    assert provider.max_active <= 3


def test_select_relevant_image_urls_fails_closed_on_timeout(image_handler):
    """A relevance timeout must not forward candidate images."""
    handler, _, _, _ = image_handler

    class Provider:
        async def text_chat(self, **kwargs):
            raise asyncio.TimeoutError

    class Context:
        def get_using_provider(self):
            return Provider()

    selected = asyncio.run(
        handler.ImageHandler.select_relevant_image_urls(
            Context(), ["image.png"], "普通文字对话"
        )
    )

    assert selected is None
