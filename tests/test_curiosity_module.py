"""Regression tests for the curiosity feature pack."""

import asyncio
import importlib.util
import sys
import types
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


class _Response:
    def __init__(self, text):
        self.completion_text = text


class _Context:
    def __init__(self, text="结论：洛天依是虚拟歌手"):
        self.text = text
        self.calls = 0

    async def llm_generate(self, **_kwargs):
        self.calls += 1
        return _Response(self.text)

    async def get_using_provider_async(self):
        return types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(id="test-provider")
        )


class _Event:
    def __init__(self, text):
        self._text = text

    def get_message_str(self):
        return self._text


def _load_modules(monkeypatch):
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", api)

    package_name = "curiosity_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    utils_package = types.ModuleType(package_name + ".utils")
    utils_package.__path__ = [str(UTILS_DIR)]
    modules_package = types.ModuleType(package_name + ".utils.modules")
    modules_package.__path__ = [str(MODULES_DIR)]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, package_name + ".utils", utils_package)
    monkeypatch.setitem(sys.modules, package_name + ".utils.modules", modules_package)

    loaded = {}
    for name in ("base", "notebook", "curiosity"):
        full = f"{package_name}.utils.modules.{name}"
        spec = importlib.util.spec_from_file_location(full, MODULES_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, full, module)
        setattr(modules_package, name, module)
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded["notebook"], loaded["curiosity"]


def _settings(curiosity, **overrides):
    values = dict(
        daily_cap=3,
        quiet_hours="",
        results=3,
        max_chars=120,
        provider_id="test",
        tavily_key="fake",
    )
    values.update(overrides)
    return types.SimpleNamespace(**values)


def test_question_marks_are_detected(monkeypatch):
    _notebook, curiosity = _load_modules(monkeypatch)

    assert curiosity.CuriosityModule._detect("有人知道洛天依是什么吗") == ["洛天依"]
    assert curiosity.CuriosityModule._detect("今天吃了饭") == []


def test_filler_words_are_stripped(monkeypatch):
    _notebook, curiosity = _load_modules(monkeypatch)

    assert curiosity.CuriosityModule._detect("请问蓝原是什么") == ["蓝原"]
    assert curiosity.CuriosityModule._detect("有谁知道提瓦特是什么") == ["提瓦特"]
    assert curiosity.CuriosityModule._detect("这个「提瓦特」啥意思") == ["提瓦特"]


def test_reflection_queues_what_the_persona_wants(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context('{"gap": "提瓦特"}'),
        book,
        _settings(curiosity),
    )

    term = asyncio.run(pack.reflect(["有谁知道提瓦特是什么"]))

    assert term == "提瓦特"
    assert [entry.text for entry in book.pending_curiosities()] == ["提瓦特"]


def test_reflection_stays_silent_when_not_curious(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context('{"gap": ""}'),
        book,
        _settings(curiosity),
    )

    term = asyncio.run(pack.reflect(["有谁知道朱珠是什么"]))

    assert term == ""
    assert book.pending_curiosities() == []


def test_entries_can_be_renamed_and_removed(monkeypatch, tmp_path):
    notebook_mod, _curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    book.add("朱珠是女演员", kind="knows")

    assert book.rename("朱珠是女演员", "朱珠：群里在说他像猪猪")
    assert [entry.text for entry in book.entries] == ["朱珠：群里在说他像猪猪"]

    assert book.remove("朱珠：群里在说他像猪猪")
    assert book.entries == []
    assert book.remove("不存在的") is False


def test_messages_are_only_buffered_not_queued(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(_Context(), book, _settings(curiosity))

    asyncio.run(pack.on_message(_Event("有谁知道提瓦特是什么"), False))

    assert book.pending_curiosities() == []
    assert len(pack._buffer) == 1


def test_private_chat_is_ignored(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(_Context(), book, _settings(curiosity))

    asyncio.run(pack.on_message(_Event("提瓦特是什么"), True))

    assert book.pending_curiosities() == []


def test_research_turns_curiosity_into_knowledge(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    book.add_curiosity("提瓦特")
    pack = curiosity.CuriosityModule(
        _Context("结论：一个游戏世界"), book, _settings(curiosity)
    )

    async def fake_search(term):
        return "某百科：提瓦特是游戏中的世界"

    pack._search = fake_search
    summary = asyncio.run(pack.research(force=True))

    assert summary
    assert book.entries[0].kind == "knows"
    assert book.entries[0].text.startswith("提瓦特：")


def test_failed_search_keeps_the_curiosity(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    book.add_curiosity("提瓦特")
    pack = curiosity.CuriosityModule(_Context(), book, _settings(curiosity))

    async def empty_search(term):
        return ""

    pack._search = empty_search
    assert asyncio.run(pack.research(force=True)) == ""
    assert book.entries[0].kind == "curious"


def test_daily_cap_is_respected(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    book.add_curiosity("提瓦特")
    pack = curiosity.CuriosityModule(
        _Context(), book, _settings(curiosity, daily_cap=1)
    )

    async def fake_search(term):
        return "资料"

    pack._search = fake_search
    asyncio.run(pack.research())
    book.add_curiosity("洛天依")

    assert asyncio.run(pack.research()) == ""


def test_quiet_hours_block_research(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    book.add_curiosity("提瓦特")
    pack = curiosity.CuriosityModule(
        _Context(), book, _settings(curiosity, quiet_hours="00:00-23:59")
    )

    async def fake_search(term):
        return "资料"

    pack._search = fake_search

    assert asyncio.run(pack.research()) == ""
