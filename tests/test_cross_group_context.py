"""Regression tests for the cross-group sight layer."""

import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
UTILS_DIR = REPO_ROOT / "utils"

GROUP_A = "星野璃月:GroupMessage:182517325"
GROUP_B = "星野璃月:GroupMessage:487350614"
GROUP_C = "星野璃月:GroupMessage:1109877695"
PRIVATE = "星野璃月:FriendMessage:1756758977"
NAMES = {
    GROUP_A: "ACG动漫音乐交流",
    GROUP_B: "bot插件测试",
    GROUP_C: "蓝色星原奇波养殖基地",
}


class _Logger:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _Event:
    def __init__(self, umo, text, sender="张三", ts=None):
        self.unified_msg_origin = umo
        self._text = text
        self._sender = sender
        self.message_obj = types.SimpleNamespace(timestamp=ts or time.time())

    def is_private_chat(self):
        return ":FriendMessage:" in self.unified_msg_origin

    def get_message_str(self):
        return self._text

    def get_sender_name(self):
        return self._sender

    def get_sender_id(self):
        return "10001"


class _Alias:
    def __init__(self, umo, name):
        self.umo = umo
        self.auto_name = name
        self.user_alias = None


class _Db:
    def __init__(self, names):
        self._names = names

    async def get_umo_aliases(self, umos):
        return [_Alias(umo, self._names[umo]) for umo in umos if umo in self._names]


class _Context:
    def __init__(self, names=None):
        self._db = _Db(names or {})
        self.message_history_manager = None

    def get_db(self):
        return self._db


def _install_stubs(monkeypatch):
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api_all = types.ModuleType("astrbot.api.all")
    core = types.ModuleType("astrbot.core")
    core_star = types.ModuleType("astrbot.core.star")
    core_star_context = types.ModuleType("astrbot.core.star.context")

    logger = _Logger()
    api.logger = logger
    api_all.logger = logger
    api_all.AstrMessageEvent = _Event
    core_star_context.Context = object

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.all": api_all,
        "astrbot.core": core,
        "astrbot.core.star": core_star,
        "astrbot.core.star.context": core_star_context,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def _load_module(monkeypatch):
    _install_stubs(monkeypatch)
    package_name = "cross_group_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    utils_package = types.ModuleType(package_name + ".utils")
    utils_package.__path__ = [str(UTILS_DIR)]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, package_name + ".utils", utils_package)

    full_name = package_name + ".utils.cross_group_context"
    spec = importlib.util.spec_from_file_location(
        full_name, UTILS_DIR / "cross_group_context.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, full_name, module)
    spec.loader.exec_module(module)
    return module


def _sight(module, **kwargs):
    # Trigger tests pin reference-only mode; the baseline mode has its own
    # cases below.
    options = {"enabled": True, "buffer_size": 20, "always_on": False}
    options.update(kwargs)
    return module.CrossGroupSight(_Context(NAMES), **options)


def test_module_loads(monkeypatch):
    module = _load_module(monkeypatch)
    assert hasattr(module, "CrossGroupSight")


def test_unrelated_message_quotes_nothing(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module)
    asyncio.run(sight.observe(_Event(GROUP_A, "今晚吃什么")))
    assert asyncio.run(sight.collect(_Event(GROUP_B, "今天好累"))) == []


def test_group_name_mention_quotes_that_group(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module)
    asyncio.run(sight.observe(_Event(GROUP_A, "周末去爬山吗")))

    sightings = asyncio.run(
        sight.collect(_Event(GROUP_B, "ACG动漫音乐交流 那边说的爬山是什么"))
    )

    assert [item.name for item in sightings] == ["ACG动漫音乐交流"]
    block = sight.render(sightings)
    assert "不属于本群" in block
    assert "周末去爬山吗" in block


def test_generic_reference_prefers_most_recent_group(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module)
    now = time.time()
    asyncio.run(sight.observe(_Event(GROUP_A, "这只是旧消息", ts=now - 600)))
    asyncio.run(sight.observe(_Event(GROUP_C, "这是新消息", ts=now - 10)))

    sightings = asyncio.run(sight.collect(_Event(GROUP_B, "另一个群刚才在说啥")))

    assert [item.name for item in sightings] == [
        "蓝色星原奇波养殖基地",
        "ACG动漫音乐交流",
    ]


def test_generic_reference_is_capped(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module, max_groups=1)
    now = time.time()
    asyncio.run(sight.observe(_Event(GROUP_A, "旧消息", ts=now - 60)))
    asyncio.run(sight.observe(_Event(GROUP_C, "新消息", ts=now - 5)))

    sightings = asyncio.run(sight.collect(_Event(GROUP_B, "别的群在聊什么")))

    assert len(sightings) == 1


def test_denylisted_group_is_never_quoted(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module, denylist=[GROUP_A])
    asyncio.run(sight.observe(_Event(GROUP_A, "敏感内容")))

    assert (
        asyncio.run(sight.collect(_Event(GROUP_B, "ACG动漫音乐交流 那边说了啥"))) == []
    )


def test_current_group_and_private_chat_are_excluded(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module)
    asyncio.run(sight.observe(_Event(GROUP_A, "群里的事")))

    assert asyncio.run(sight.collect(_Event(GROUP_A, "另一个群"))) == []
    assert asyncio.run(sight.collect(_Event(PRIVATE, "另一个群"))) == []


def test_anaphor_with_known_speaker_quotes_group(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module)
    asyncio.run(sight.observe(_Event(GROUP_A, "我周五到", sender="李四")))

    assert asyncio.run(sight.collect(_Event(GROUP_B, "刚才李四说的那个挺好")))
    assert asyncio.run(sight.collect(_Event(GROUP_B, "刚才说的那个挺好"))) == []


def test_expired_sightings_are_dropped(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module, window_seconds=60)
    asyncio.run(sight.observe(_Event(GROUP_A, "很久以前", ts=time.time() - 600)))

    assert asyncio.run(sight.collect(_Event(GROUP_B, "另一个群刚才在说啥"))) == []


def test_disabled_layer_stays_silent(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module, enabled=False)
    asyncio.run(sight.observe(_Event(GROUP_A, "群里的事")))

    assert asyncio.run(sight.collect(_Event(GROUP_B, "另一个群刚才在说啥"))) == []


def test_history_content_text_extraction(monkeypatch):
    module = _load_module(monkeypatch)
    raw = '[{"type": "text", "data": {"text": "你好"}}, {"type": "image"}]'

    assert module.CrossGroupSight._history_text(raw) == "你好 [图片]"
    assert module.CrossGroupSight._history_text("") == ""


def test_baseline_digest_needs_no_reference(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module, always_on=True, max_groups=2, baseline_per_group=2)
    now = time.time()
    asyncio.run(sight.observe(_Event(GROUP_A, "第一条", ts=now - 30)))
    asyncio.run(sight.observe(_Event(GROUP_A, "第二条", ts=now - 20)))
    asyncio.run(sight.observe(_Event(GROUP_A, "第三条", ts=now - 10)))

    sightings = asyncio.run(sight.collect(_Event(GROUP_B, "今天好累")))

    assert [item.name for item in sightings] == ["ACG动漫音乐交流"]
    assert [m["text"] for m in sightings[0].messages] == ["第二条", "第三条"]


def test_baseline_respects_group_cap(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module, always_on=True, max_groups=1)
    now = time.time()
    asyncio.run(sight.observe(_Event(GROUP_A, "旧", ts=now - 60)))
    asyncio.run(sight.observe(_Event(GROUP_C, "新", ts=now - 5)))

    sightings = asyncio.run(sight.collect(_Event(GROUP_B, "今天好累")))

    assert [item.name for item in sightings] == ["蓝色星原奇波养殖基地"]


def test_referenced_group_gets_full_budget(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module, always_on=True, max_per_group=4, baseline_per_group=1)
    now = time.time()
    for index in range(4):
        asyncio.run(sight.observe(_Event(GROUP_C, f"奇波{index}", ts=now - 40 + index)))
    asyncio.run(sight.observe(_Event(GROUP_A, "语聊", ts=now - 50)))

    sightings = asyncio.run(sight.collect(_Event(GROUP_B, "蓝原群那边聊了啥")))
    by_name = {item.name: item for item in sightings}

    assert len(by_name["蓝色星原奇波养殖基地"].messages) == 4
    assert len(by_name["ACG动漫音乐交流"].messages) == 1


def test_abbreviation_resolves_group(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module)
    asyncio.run(sight.observe(_Event(GROUP_C, "奇波的事")))

    for text in ("蓝原群那边说啥了", "星原那群呢", "蓝色星原奇波养殖基地"):
        sightings = asyncio.run(sight.collect(_Event(GROUP_B, text)))
        assert [item.name for item in sightings] == ["蓝色星原奇波养殖基地"], text


def test_generic_fragment_does_not_focus_group(monkeypatch):
    module = _load_module(monkeypatch)
    sight = _sight(module)
    asyncio.run(sight.observe(_Event(GROUP_B, "占位", sender="甲")))

    assert asyncio.run(sight.collect(_Event(GROUP_A, "插件又崩了"))) == []


def test_sightings_survive_restart(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    store = tmp_path / "sight.json"
    now = time.time()

    first = _sight(module, always_on=True, baseline_per_group=2, store_path=str(store))
    asyncio.run(first.observe(_Event(GROUP_C, "重启前说的话", ts=now - 10)))
    first.flush()
    asyncio.run(first._ensure_names((GROUP_C,)))
    first.flush()

    second = _sight(module, always_on=True, baseline_per_group=2, store_path=str(store))
    sightings = asyncio.run(second.collect(_Event(GROUP_B, "今天好累")))

    assert [item.name for item in sightings] == ["蓝色星原奇波养殖基地"]
    assert [m["text"] for m in sightings[0].messages] == ["重启前说的话"]
