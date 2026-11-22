"""Regression tests for the leaked system tag sanitizer."""

import importlib.util
import sys
import types
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


def _load_module(monkeypatch):
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", api)

    package_name = "response_filter_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    utils_package = types.ModuleType(package_name + ".utils")
    utils_package.__path__ = [str(UTILS_DIR)]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, package_name + ".utils", utils_package)

    full_name = package_name + ".utils.ai_response_filter"
    spec = importlib.util.spec_from_file_location(
        full_name, UTILS_DIR / "ai_response_filter.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, full_name, module)
    spec.loader.exec_module(module)
    return module


def _strip(monkeypatch, text):
    module = _load_module(monkeypatch)
    return module.AIResponseFilter.strip_leaked_system_tags(text)


def test_closed_system_block_is_removed(monkeypatch):
    text = "...<system>直接敷衍即可。不超过20字。</system>"
    assert _strip(monkeypatch, text) == "..."


def test_system_reminder_block_is_removed(monkeypatch):
    text = "晚安<system_reminder>user: 张三\ngroup: 123</system_reminder>"
    assert _strip(monkeypatch, text) == "晚安"


def test_system_internal_block_is_removed(monkeypatch):
    text = '好<system_internal do_not_output="true">别输出</system_internal>'
    assert _strip(monkeypatch, text) == "好"


def test_dangling_system_block_is_removed(monkeypatch):
    text = "...<system>直接敷衍即可。不超过20字。"
    assert _strip(monkeypatch, text) == "..."


def test_dangling_closing_tag_is_removed(monkeypatch):
    text = "...没看...</system>"
    assert _strip(monkeypatch, text) == "...没看..."


def test_plain_reply_is_untouched(monkeypatch):
    text = "...没看...无聊"
    assert _strip(monkeypatch, text) == text


def test_non_system_tags_are_untouched(monkeypatch):
    text = "看这个 <b>重点</b>"
    assert _strip(monkeypatch, text) == text


def test_empty_input_is_safe(monkeypatch):
    assert _strip(monkeypatch, "") == ""


def test_uuid_tool_marker_is_stripped(monkeypatch):
    module = _load_module(monkeypatch)
    text = "没死...就是懒得理你 &&8c39f0a1-44d3-4e01-b5f2-9b5b9c1a4e6f"
    assert module.AIResponseFilter.sanitize_reply_text(text) == "没死...就是懒得理你"


def test_trailing_word_mood_tag_is_stripped(monkeypatch):
    module = _load_module(monkeypatch)
    text = "玩玩...别玩到自己都信了... &&angry"
    assert (
        module.AIResponseFilter.sanitize_reply_text(text)
        == "玩玩...别玩到自己都信了..."
    )


def test_inline_queue_meme_markup_is_stripped(monkeypatch):
    module = _load_module(monkeypatch)
    text = "...加个合法就想蒙过去... && queue_meme: 阴阳怪气地看不起对方"
    cleaned = module.AIResponseFilter.sanitize_reply_text(text)
    assert "queue_meme" not in cleaned
    assert "阴阳怪气" not in cleaned
    assert cleaned == "...加个合法就想蒙过去..."


def test_generic_ampersand_markup_is_stripped(monkeypatch):
    module = _load_module(monkeypatch)
    for text in (
        "你好 && queue_meme: 无语",
        "你好 &&queue_meme: 无语",
        "你好 && meme: 无语 && 再见",
    ):
        cleaned = module.AIResponseFilter.sanitize_reply_text(text)
        assert "&&" not in cleaned
        assert "queue_meme" not in cleaned
        assert "无语" not in cleaned
