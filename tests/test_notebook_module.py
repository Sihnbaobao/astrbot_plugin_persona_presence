"""Regression tests for the notebook feature pack."""

import importlib.util
import json
import sys
import time
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
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = _Logger()
    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", api_mod)

    package_name = "notebook_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    utils_package = types.ModuleType(package_name + ".utils")
    utils_package.__path__ = [str(UTILS_DIR)]
    modules_package = types.ModuleType(package_name + ".utils.modules")
    modules_package.__path__ = [str(UTILS_DIR / "modules")]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, package_name + ".utils", utils_package)
    monkeypatch.setitem(sys.modules, package_name + ".utils.modules", modules_package)

    full_name = package_name + ".utils.modules.notebook"
    spec = importlib.util.spec_from_file_location(
        full_name, UTILS_DIR / "modules" / "notebook.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, full_name, module)
    spec.loader.exec_module(module)
    return module


def test_entry_matches_related_message(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.add("在补推理小说", kind="doing")

    block = book.block_for("你最近在看什么推理小说")

    assert "在补推理小说" in block


def test_unrelated_message_gets_nothing(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.add("在补推理小说", kind="doing")

    assert book.block_for("今天天气不错") == ""


def test_disabled_notebook_matches_nothing(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=False)
    book.add("在补推理小说", kind="doing")

    assert book.match("推理小说") == []


def test_confident_candidate_is_learned(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)

    created = book.merge_candidate("讨厌被反复追问", kind="dislike", confidence=0.9)

    assert created is True
    assert book.entries[0].source == "learned"


def test_low_confidence_candidate_is_dropped(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)

    created = book.merge_candidate("可能喜欢下雨", kind="like", confidence=0.2)

    assert created is False
    assert book.entries == []


def test_repeated_candidate_gains_evidence(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.merge_candidate("讨厌被反复追问", kind="dislike", confidence=0.9)
    first_weight = book.entries[0].weight

    book.merge_candidate("讨厌被反复追问", kind="dislike", confidence=0.9)

    assert book.entries[0].evidence == 2
    assert book.entries[0].weight > first_weight


def test_reflection_reply_is_absorbed(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.observe("你平时干什么", "在补小说...别吵")
    payload = json.dumps(
        {"entries": [{"text": "在补推理小说", "kind": "doing", "confidence": 0.8}]},
        ensure_ascii=False,
    )

    new_count, _strengthened = book.absorb_reflection(payload)

    assert new_count == 1
    assert book.pending == []
    assert book.entries[0].text == "在补推理小说"


def test_book_survives_restart(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.add("在意妹妹泠", kind="person")

    reloaded = module.Notebook(tmp_path, enabled=True)

    assert reloaded.entries[0].text == "在意妹妹泠"
    assert reloaded.entries[0].kind == "person"


def test_garbage_reflection_is_ignored(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)

    new_count, strengthened = book.absorb_reflection("完全不是 JSON")

    assert (new_count, strengthened) == (0, 0)


def test_group_jargon_is_scoped_and_labelled_when_cross_group(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.add_jargon("香草", "群里的内部说法", group="100", cross_group=False)

    assert book.match("香草", "200") == []
    assert book.match("香草", "100")
    assert book.block_for("香草", "200") == ""

    cross = book.block_for("香草", "200", include_other_groups=True)
    assert "来自其他群" in cross


def test_general_slang_crosses_groups_without_provenance(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.add_jargon("哈基美", "互联网通用调侃", group="100", cross_group=True)

    assert book.match("哈基美", "200")
    block = book.block_for("哈基美", "200")
    assert "哈基美" in block
    assert "来自其他群" not in block


def test_legacy_jargon_scope_is_inferred(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    payload = {
        "entries": [
            {
                "text": "晚安小桃 = 称呼群成员小桃",
                "kind": "jargon",
                "group_id": "100",
                "source": "group:100",
            },
            {
                "text": "哈基美 = 互联网调侃",
                "kind": "jargon",
                "group_id": "100",
                "source": "group:100",
            },
        ]
    }
    (tmp_path / "notebook.json").write_text(json.dumps(payload), encoding="utf-8")

    book = module.Notebook(tmp_path, enabled=True)

    assert book.entries[0].cross_group is False
    assert book.entries[1].cross_group is True


def test_merge_does_not_cross_kinds(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.merge_candidate("喜欢推理小说", kind="like", confidence=0.9)

    created = book.merge_candidate("讨厌推理小说", kind="dislike", confidence=0.9)

    assert created is True
    assert len(book.entries) == 2
    kinds = {entry.kind for entry in book.entries}
    assert kinds == {"like", "dislike"}


def test_english_entries_match(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.add("VRChat", kind="doing")

    assert book.match("我最近在玩vrchat")


def test_manual_jargon_is_general(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.add_jargon("测试黑话", "只用于验证", group="manual")

    assert book.match("测试黑话", "100")
    assert book.match("测试黑话", "200")
    assert "来自其他群" not in book.block_for("测试黑话", "200")


def test_pending_survives_restart(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.observe("你在干嘛", "在补推理小说")

    reloaded = module.Notebook(tmp_path, enabled=True)

    assert len(reloaded.pending) == 1
    assert reloaded.pending[0]["user"] == "你在干嘛"


def test_relearning_jargon_refreshes_it(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.add_jargon("测试黑话", "只用于验证", group="100", cross_group=False)
    entry = book.entries[0]

    assert (
        book.add_jargon("测试黑话", "只用于验证", group="100", cross_group=False)
        is False
    )

    assert entry.evidence == 2
    assert entry.use_count == 1
    assert entry.forget_stage == 0


def test_matching_jargon_resets_forgetting(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.add_jargon("测试黑话", "只用于验证", group="100", cross_group=False)
    entry = book.entries[0]
    entry.forget_stage = 2

    assert book.match("测试黑话", "100")
    assert entry.forget_stage == 0
    assert entry.use_count == 1


def test_group_jargon_forgets_but_general_is_protected(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(
        tmp_path,
        enabled=True,
        jargon_cycle_days=1,
        jargon_grace_days=0,
    )
    book.add_jargon("本群限定", "只在本群用", group="100", cross_group=False)
    book.add_jargon("通用梗", "到处都在用", group="100", cross_group=True)
    old = time.time() - 10 * 86400
    local = next(e for e in book.entries if e.text.startswith("本群限定"))
    general = next(e for e in book.entries if e.text.startswith("通用梗"))
    for entry in (local, general):
        entry.created_at = old
        entry.last_seen = old
        entry.forget_updated_at = old

    for _ in range(3):
        book.apply_decay()
        local.forget_updated_at = old

    assert not any(e.text.startswith("本群限定") for e in book.entries)
    assert any(e.text.startswith("通用梗") for e in book.entries)
    assert general.forget_stage == 0


def test_short_slang_alias_does_not_create_second_entry(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    book = module.Notebook(tmp_path, enabled=True)
    book.add_jargon("大肥鱼", "DeepSeek 的网友昵称", group="", cross_group=True)

    assert book.add_jargon("肥鱼", "错误简称", group="100", cross_group=False) is False

    jargon = [entry for entry in book.entries if entry.kind == "jargon"]
    assert len(jargon) == 1
    assert jargon[0].text.startswith("大肥鱼 = ")
