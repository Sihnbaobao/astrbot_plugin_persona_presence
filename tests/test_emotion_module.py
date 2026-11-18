"""Regression tests for the emotion feature pack."""

import asyncio
import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
UTILS_DIR = REPO_ROOT / "utils"
MODULES_DIR = UTILS_DIR / "modules"


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
    def __init__(self, text, uid="u1", private=True, at=True):
        self._text = text
        self._uid = uid
        self.unified_msg_origin = "test:private:u1" if private else "test:group:g1"
        self.is_at_or_wake_command = at

    def get_message_str(self):
        return self._text

    def get_sender_id(self):
        return self._uid


def _ctx(event):
    base = _load().base
    return base.FeatureContext(
        event=event,
        umo=event.unified_msg_origin,
        group_id="" if "private" in event.unified_msg_origin else "g1",
        is_private="private" in event.unified_msg_origin,
        chat_key=event.unified_msg_origin,
        persona="你的名字是星野璃月",
        provider_id="test",
        now=datetime.now(),
    )


def _load():
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    sys.modules["astrbot.api"] = api
    package_name = "emotion_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    utils_package = types.ModuleType(package_name + ".utils")
    utils_package.__path__ = [str(UTILS_DIR)]
    modules_package = types.ModuleType(package_name + ".utils.modules")
    modules_package.__path__ = [str(MODULES_DIR)]
    sys.modules[package_name] = package
    sys.modules[package_name + ".utils"] = utils_package
    sys.modules[package_name + ".utils.modules"] = modules_package
    loaded = {}
    for name in ("base", "emotion"):
        full = f"{package_name}.utils.modules.{name}"
        spec = importlib.util.spec_from_file_location(full, MODULES_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        setattr(modules_package, name, module)
        spec.loader.exec_module(module)
        loaded[name] = module
    return types.SimpleNamespace(**loaded)


def test_insult_creates_anger_state(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    asyncio.run(pack.on_event(_ctx(_Event("你真是傻，闭嘴"))))
    block = pack.block_for(_Event("你真是傻，闭嘴"), True)
    assert "[此刻]" in block
    assert "恼" in block
    assert "闭嘴" in block


def test_group_message_not_addressed_does_not_change(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    asyncio.run(pack.on_event(_ctx(_Event("今天天气不错", private=False, at=False))))
    assert pack.chats == {}


def test_state_is_separate_per_sender(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    asyncio.run(pack.on_event(_ctx(_Event("你真烦", uid="a", private=True))))
    assert pack.block_for(_Event("", uid="b"), True) == ""
