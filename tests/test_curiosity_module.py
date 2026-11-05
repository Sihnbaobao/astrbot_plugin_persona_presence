"""Regression tests for the curiosity feature pack."""

import asyncio
import importlib.util
import sys
import time
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
    for name in ("base", "notebook", "curiosity_store", "curiosity"):
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
    assert curiosity.CuriosityModule._detect("红果短剧到底是什么") == ["红果短剧"]
    assert curiosity.CuriosityModule._detect("你从来都不喜欢我是什么") == []


def test_reflection_queues_what_the_persona_wants(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context('{"topic": "提瓦特", "query": "提瓦特是什么", "why": "想知道"}'),
        book,
        _settings(curiosity),
        store=curiosity.CuriosityStore(tmp_path),
    )

    term = asyncio.run(pack.reflect(["有谁知道提瓦特是什么"]))

    assert term == "提瓦特"
    pending = pack.store.pending()
    assert [entry.text for entry in pending] == ["提瓦特"]
    assert pending[0].query == "提瓦特是什么"


def test_reflection_stays_silent_when_not_curious(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context('{"topic": ""}'),
        book,
        _settings(curiosity),
        store=curiosity.CuriosityStore(tmp_path),
    )

    term = asyncio.run(pack.reflect(["有谁知道朱珠是什么"]))

    assert term == ""
    assert pack.store.pending() == []


def test_entries_can_be_renamed_and_removed(monkeypatch, tmp_path):
    notebook_mod, _curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    book.add("朱珠是女演员", kind="like")

    assert book.rename("朱珠是女演员", "朱珠：群里在说他像猪猪")
    assert [entry.text for entry in book.entries] == ["朱珠：群里在说他像猪猪"]

    assert book.remove("朱珠：群里在说他像猪猪")
    assert book.entries == []
    assert book.remove("不存在的") is False


def test_similar_topics_are_deduplicated(monkeypatch, tmp_path):
    _notebook, curiosity = _load_modules(monkeypatch)
    store = curiosity.CuriosityStore(tmp_path)

    assert store.add("蓝色星原", source="interest")
    assert not store.add("蓝色星原开服", source="interest")


def test_group_questions_do_not_auto_queue_curiosity(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context(),
        book,
        _settings(curiosity, new_topics_per_day=1, pending_soft_limit=100),
        store=curiosity.CuriosityStore(tmp_path),
    )

    asyncio.run(pack.on_message(_Event("提瓦特是什么"), False))
    asyncio.run(pack.on_message(_Event("洛天依是什么"), False))

    assert pack.store.pending() == []


def test_backlog_soft_limit_blocks_ambient_not_questions(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context(),
        book,
        _settings(curiosity, pending_soft_limit=1, new_topics_per_day=8),
        store=curiosity.CuriosityStore(tmp_path),
    )
    pack.store.add("已有话题", source="interest")

    assert pack._can_admit("interest") is False
    assert pack._can_admit("question") is True


def test_explicit_questions_are_not_auto_queued(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context(),
        book,
        _settings(curiosity),
        store=curiosity.CuriosityStore(tmp_path),
    )

    asyncio.run(pack.on_message(_Event("有谁知道提瓦特是什么"), False))

    assert pack.store.pending() == []
    assert len(pack._buffers[""]) == 1


def test_private_chat_is_ignored(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context(),
        book,
        _settings(curiosity),
        store=curiosity.CuriosityStore(tmp_path),
    )

    asyncio.run(pack.on_message(_Event("提瓦特是什么"), True))

    assert pack.store.pending() == []


def test_research_turns_curiosity_into_knowledge(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context("结论：一个游戏世界"),
        book,
        _settings(curiosity),
        store=curiosity.CuriosityStore(tmp_path),
    )
    pack.store.add("提瓦特")

    async def fake_search(term):
        return "某百科：提瓦特是游戏中的世界"

    pack._search = fake_search
    summary = asyncio.run(pack.research(force=True))

    assert summary
    known = pack.store.knowledge(1)
    assert known and known[0].kind == "knows"
    assert known[0].summary


def test_failed_search_keeps_the_curiosity(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context(),
        book,
        _settings(curiosity),
        store=curiosity.CuriosityStore(tmp_path),
    )
    pack.store.add("提瓦特")

    async def empty_search(term):
        return ""

    pack._search = empty_search
    assert asyncio.run(pack.research(force=True)) == ""
    assert pack.store.pending()[0].kind == "curious"


def test_daily_cap_is_respected(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context(),
        book,
        _settings(curiosity, daily_cap=1),
        store=curiosity.CuriosityStore(tmp_path),
    )
    pack.store.add("提瓦特")

    async def fake_search(term):
        return "资料"

    pack._search = fake_search
    asyncio.run(pack.research())
    pack.store.add("洛天依")

    assert asyncio.run(pack.research()) == ""


def test_quiet_hours_block_research(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context(),
        book,
        _settings(curiosity, quiet_hours="00:00-23:59"),
        store=curiosity.CuriosityStore(tmp_path),
    )
    pack.store.add("提瓦特")

    async def fake_search(term):
        return "资料"

    pack._search = fake_search

    assert asyncio.run(pack.research()) == ""


def test_explicit_question_skips_the_classifier(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    context = _Context("结论：一个游戏世界")
    pack = curiosity.CuriosityModule(
        context,
        book,
        _settings(curiosity),
        store=curiosity.CuriosityStore(tmp_path),
    )
    pack.store.add("提瓦特", source="question", query="提瓦特是什么")

    async def fake_search(term):
        return "某百科：提瓦特是游戏中的世界"

    pack._search = fake_search
    assert asyncio.run(pack.research(force=True))
    assert context.calls == 1


def test_research_without_tavily_key_does_not_call_the_model(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    context = _Context()
    pack = curiosity.CuriosityModule(
        context,
        book,
        _settings(curiosity, tavily_key=""),
        store=curiosity.CuriosityStore(tmp_path),
    )
    pack.store.add("提瓦特")

    assert asyncio.run(pack.research()) == ""
    assert context.calls == 0


def test_repeated_empty_search_drops_the_curiosity(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context(),
        book,
        _settings(curiosity),
        store=curiosity.CuriosityStore(tmp_path),
    )
    pack.store.add("提瓦特")

    async def empty_search(term):
        return ""

    pack._search = empty_search

    assert asyncio.run(pack.research(force=True)) == ""
    assert asyncio.run(pack.research(force=True)) == ""
    assert pack.store.pending() == []


def test_curiosity_store_persists_knowledge(monkeypatch, tmp_path):
    _notebook, curiosity = _load_modules(monkeypatch)
    store = curiosity.CuriosityStore(tmp_path)
    store.add("提瓦特", query="提瓦特是什么", why="想知道")
    assert store.resolve("提瓦特", "游戏中的世界")

    reloaded = curiosity.CuriosityStore(tmp_path)

    assert reloaded.pending() == []
    assert [(entry.text, entry.summary) for entry in reloaded.knowledge()] == [
        ("提瓦特", "游戏中的世界")
    ]


def test_curiosity_store_prunes_and_updates(monkeypatch, tmp_path):
    _notebook, curiosity = _load_modules(monkeypatch)
    store = curiosity.CuriosityStore(tmp_path, pending_ttl_days=1, knowledge_ttl_days=1)
    store.add("旧待查", query="旧搜索")
    store.entries[0].created_at = time.time() - 2 * 86400
    assert store.prune() == 1
    assert store.pending() == []

    store.add("旧知识", query="旧搜索")
    store.resolve("旧知识", "旧摘要")
    store.entries[0].resolved_at = time.time() - 2 * 86400
    assert store.prune() == 1
    assert store.knowledge() == []

    store.add("话题", query="旧搜索")
    assert store.update("话题", query="新搜索", why="新理由")
    assert store.pending()[0].query == "新搜索"
    assert store.pending()[0].why == "新理由"


def test_knowledge_uses_staged_forgetting(monkeypatch, tmp_path):
    _notebook, curiosity = _load_modules(monkeypatch)
    now = time.time()
    store = curiosity.CuriosityStore(
        tmp_path,
        pending_ttl_days=7,
        knowledge_ttl_days=0,
        forgetting_cycle_days=1,
        forgetting_grace_days=0,
    )
    store.add("提瓦特")
    store.resolve("提瓦特", "游戏中的世界")
    entry = store.knowledge()[0]
    entry.resolved_at = now - 86400 - 3600

    store.prune(now)
    assert store.knowledge()[0].forget_stage == 1
    store.prune(now + 86400)
    assert store.knowledge()[0].forget_stage == 2
    store.prune(now + 2 * 86400)
    assert store.knowledge() == []


def test_touch_resets_forgetting(monkeypatch, tmp_path):
    _notebook, curiosity = _load_modules(monkeypatch)
    store = curiosity.CuriosityStore(tmp_path)
    store.add("提瓦特")
    store.resolve("提瓦特", "游戏中的世界")
    entry = store.knowledge()[0]
    entry.forget_stage = 2

    assert store.touch("提瓦特")
    assert store.knowledge()[0].forget_stage == 0


def test_useless_summary_is_detected(monkeypatch):
    _notebook, curiosity = _load_modules(monkeypatch)

    assert curiosity.CuriosityModule._useless_summary("资料不足，没查到有用信息")
    assert not curiosity.CuriosityModule._useless_summary("提瓦特是游戏中的世界")


def test_curiosity_knowledge_stays_out_of_memory_network(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    book.add("喜欢推理小说", kind="like")
    store = curiosity.CuriosityStore(tmp_path)
    store.add("提瓦特", query="提瓦特是什么")
    store.resolve("提瓦特", "游戏中的世界")

    graph = book.graph()

    assert graph["total"] == 1
    assert "knows" not in graph["counts"]
    assert [entry.text for entry in store.knowledge()] == ["提瓦特"]


def test_curiosity_knowledge_is_available_as_background(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    pack = curiosity.CuriosityModule(
        _Context(),
        book,
        _settings(curiosity),
        store=curiosity.CuriosityStore(tmp_path),
    )
    pack.store.add("提瓦特", query="提瓦特是什么")
    pack.store.resolve("提瓦特", "游戏中的世界")

    block = pack.knowledge_block("提瓦特是什么")

    assert "提瓦特" in block
    assert "游戏中的世界" in block


def test_reflection_uses_context_persona(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    captured = {}

    class _Recorder(_Context):
        async def llm_generate(self, **kwargs):
            captured.setdefault("prompts", []).append(kwargs.get("prompt", ""))
            return await super().llm_generate(**kwargs)

    pack = curiosity.CuriosityModule(
        _Recorder('{"topic": "提瓦特", "query": "提瓦特是什么", "why": "想知道"}'),
        book,
        _settings(curiosity),
    )
    ctx = curiosity.FeatureContext(
        event=_Event("提瓦特是什么"),
        umo="test-umo",
        group_id="100",
        is_private=False,
        chat_key="test-chat",
        persona="测试人格",
        provider_id="ctx-provider",
        now=datetime(2026, 1, 1, 12, 0),
    )

    assert asyncio.run(pack.reflect(["提瓦特是什么"], ctx=ctx)) == "提瓦特"
    assert any("测试人格" in prompt for prompt in captured["prompts"])


def test_research_uses_context_provider(monkeypatch, tmp_path):
    notebook_mod, curiosity = _load_modules(monkeypatch)
    book = notebook_mod.Notebook(tmp_path, enabled=True)
    captured = {}

    class _Recorder(_Context):
        async def llm_generate(self, **kwargs):
            captured["provider"] = kwargs.get("chat_provider_id")
            captured.setdefault("prompts", []).append(kwargs.get("prompt", ""))
            return await super().llm_generate(**kwargs)

    pack = curiosity.CuriosityModule(
        _Recorder('{"kind": "lookup"}'),
        book,
        _settings(curiosity, provider_id=""),
        store=curiosity.CuriosityStore(tmp_path),
    )
    pack.store.add("提瓦特")

    async def fake_search(term):
        return "某百科：提瓦特是游戏中的世界"

    pack._search = fake_search
    ctx = curiosity.FeatureContext(
        event=_Event("提瓦特是什么"),
        umo="test-umo",
        group_id="100",
        is_private=False,
        chat_key="test-chat",
        persona="测试人格",
        provider_id="ctx-provider",
        now=datetime(2026, 1, 1, 12, 0),
    )

    assert asyncio.run(pack.research(force=True, ctx=ctx))
    assert captured["provider"] == "ctx-provider"
