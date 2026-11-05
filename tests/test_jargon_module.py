"""Regression tests for slang prompt construction."""

import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
UTILS_DIR = REPO_ROOT / "utils"


def _load_module(monkeypatch):
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", api_mod)

    package_name = "jargon_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    utils_package = types.ModuleType(package_name + ".utils")
    utils_package.__path__ = [str(UTILS_DIR)]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, package_name + ".utils", utils_package)

    full_name = package_name + ".utils.jargon"
    spec = importlib.util.spec_from_file_location(full_name, UTILS_DIR / "jargon.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, full_name, module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_web_evidence_reaches_both_prompts(monkeypatch):
    module = _load_module(monkeypatch)
    context_prompt, _only, compare_template = module.build_meaning_prompts(
        "大肥鱼",
        "大肥鱼可以称帝了",
        ["大肥鱼可以称帝了"],
        web="大肥鱼 = DeepSeek 蓝鲸 logo 的网友昵称",
    )

    assert "DeepSeek 蓝鲸" in context_prompt
    rendered = compare_template.format(term="大肥鱼", a="A", b="B", web="网页资料")
    assert "网页资料" in rendered


def test_without_web_uses_an_explicit_placeholder(monkeypatch):
    module = _load_module(monkeypatch)
    context_prompt, _only, _compare = module.build_meaning_prompts(
        "大肥鱼", "大肥鱼可以称帝了", ["大肥鱼可以称帝了"]
    )

    assert "没有搜到可用资料" in context_prompt


def test_parse_json_block_prefers_object_over_nested_array(monkeypatch):
    module = _load_module(monkeypatch)
    raw = '{"description":"蓝发少女","tags":["二次元","女仆"],"score":0.35}'
    parsed = module.parse_json_block(raw)
    assert isinstance(parsed, dict)
    assert parsed["tags"] == ["二次元", "女仆"]
