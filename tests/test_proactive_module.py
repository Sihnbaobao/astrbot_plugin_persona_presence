"""Regression tests for the proactive feature pack."""

import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

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


class _Response:
    def __init__(self, text):
        self.completion_text = text


class _Context:
    def __init__(self, text=""):
        self.text = text
        self.sent = []
        self.calls = 0

    async def llm_generate(self, **_kwargs):
        self.calls += 1
        return _Response(self.text)

    async def get_using_provider_async(self):
        return types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(id="test-provider")
        )

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True


class _Event:
    def __init__(self, group="100", text="聊点别的", sender="某人"):
        self._group = group
        self._text = text
        self._sender = sender
        self.unified_msg_origin = f"plat:GroupMessage:{group}"

    def get_group_id(self):
        return self._group

    def get_message_str(self):
        return self._text

    def get_sender_name(self):
        return self._sender

    def is_private_chat(self):
        return False


def _load_module(monkeypatch):
    api_all = types.ModuleType("astrbot.api.all")
    api_all.logger = _Logger()
    api_all.AstrMessageEvent = object
    api_all.Plain = lambda text: text
    api_all.MessageChain = lambda items: items
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = api_all.logger
    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", api_mod)
    monkeypatch.setitem(sys.modules, "astrbot.api.all", api_all)

    package_name = "proactive_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    modules_package = types.ModuleType(package_name + ".utils.modules")
    modules_package.__path__ = [str(UTILS_DIR / "modules")]
    utils_package = types.ModuleType(package_name + ".utils")
    utils_package.__path__ = [str(UTILS_DIR)]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, package_name + ".utils", utils_package)
    monkeypatch.setitem(sys.modules, package_name + ".utils.modules", modules_package)

    for name in ("base", "proactive"):
        full = f"{package_name}.utils.modules.{name}"
        spec = importlib.util.spec_from_file_location(
            full, UTILS_DIR / "modules" / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, full, module)
        setattr(modules_package, name, module)
        spec.loader.exec_module(module)
    return sys.modules[package_name + ".utils.modules.proactive"]


def _settings(module, **overrides):
    values = dict(
        groups={"100"},
        cooldown_minutes=10,
        min_gap_seconds=0,
        daily_cap=20,
        quiet_hours="01:00-07:00",
        min_messages=1,
        base_probability=1.0,
        max_chars=30,
        provider_id="test",
    )
    values.update(overrides)
    return module.ProactiveSettings(**values)


def test_message_that_wants_to_speak_is_sent(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句…")
    pack = module.ProactiveModule(context, _settings(module), data_dir=tmp_path)

    asyncio.run(pack.on_message(_Event(), False))

    assert context.sent, "expected a proactive message"
    assert context.sent[0][1] == ["接一句…"]


def test_silence_is_respected(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("沉默")
    pack = module.ProactiveModule(context, _settings(module), data_dir=tmp_path)

    asyncio.run(pack.on_message(_Event(), False))

    assert context.sent == []


def test_private_chat_is_ignored(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句")
    pack = module.ProactiveModule(context, _settings(module), data_dir=tmp_path)

    asyncio.run(pack.on_message(_Event(), True))

    assert context.sent == []
    assert context.calls == 0


def test_unlisted_group_is_ignored(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句")
    pack = module.ProactiveModule(context, _settings(module), data_dir=tmp_path)

    asyncio.run(pack.on_message(_Event(group="999"), False))

    assert context.sent == []


def test_cooldown_blocks_second_line(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句")
    pack = module.ProactiveModule(context, _settings(module), data_dir=tmp_path)

    asyncio.run(pack.on_message(_Event(text="第一句"), False))
    asyncio.run(pack.on_message(_Event(text="第二句"), False))

    assert len(context.sent) == 1


def test_state_survives_restart(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句")
    pack = module.ProactiveModule(context, _settings(module), data_dir=tmp_path)
    asyncio.run(pack.on_message(_Event(), False))
    asyncio.run(pack.close())

    context2 = _Context("接一句")
    pack2 = module.ProactiveModule(context2, _settings(module), data_dir=tmp_path)
    asyncio.run(pack2.on_message(_Event(text="重启后"), False))

    assert context2.sent == []


def test_quiet_hours_detection(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    pack = module.ProactiveModule(
        _Context(), _settings(module, quiet_hours="00:00-23:59"), data_dir=tmp_path
    )

    assert pack._in_quiet_hours() is True


def test_interests_and_experience_reach_the_prompt(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    captured = {}

    class _Recorder(_Context):
        async def llm_generate(self, **kwargs):
            captured["prompt"] = kwargs.get("prompt", "")
            return await super().llm_generate(**kwargs)

    pack = module.ProactiveModule(
        _Recorder("沉默"),
        _settings(module),
        interest_provider=lambda: ["在意：妹妹泠", "喜欢：推理小说"],
        state_provider=lambda: ["最近：5 小时前看了《奶龙》"],
        data_dir=tmp_path,
    )

    asyncio.run(pack.on_message(_Event(), False))

    prompt = captured.get("prompt", "")
    assert "妹妹泠" in prompt
    assert "推理小说" in prompt
    assert "奶龙" in prompt
