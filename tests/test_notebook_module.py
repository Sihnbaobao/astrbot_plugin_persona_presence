"""Regression tests for the notebook feature pack."""

import asyncio
import importlib.util
import json
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
