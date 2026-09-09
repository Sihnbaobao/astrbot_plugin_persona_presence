"""Regression tests for asynchronous image-description caching."""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path


class _Logger:
    """Logger stub required by the standalone cache module."""

    def debug(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


def _load_cache(monkeypatch):
    """Load the cache module without importing the full AstrBot runtime."""
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    astrbot = types.ModuleType("astrbot")
    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.api", api)

    path = Path(__file__).parents[1] / "utils" / "image_description_cache.py"
    spec = importlib.util.spec_from_file_location("image_description_cache_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module.ImageDescriptionCache


def test_get_or_create_is_single_flight(monkeypatch, tmp_path):
    """Concurrent requests for one image share one async generation."""
    cache_class = _load_cache(monkeypatch)
    cache = cache_class(str(tmp_path), enabled=True)
    calls = 0

    async def scenario():
        nonlocal calls

        async def factory():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return "a generated description"

        results = await asyncio.gather(
            *(cache.get_or_create("image-1", factory) for _ in range(8))
        )
        return results

    results = asyncio.run(scenario())

    assert results == ["a generated description"] * 8
    assert calls == 1
    assert cache.lookup("image-1") == "a generated description"
    assert cache.entry_count == 1


def test_cache_loads_bom_and_keeps_newest_entries(monkeypatch, tmp_path):
    """A BOM-prefixed JSONL file loads and cleanup keeps recent entries."""
    cache_dir = tmp_path / "image_cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "descriptions.jsonl"
    cache_file.write_text(
        "\ufeff" + json.dumps({"u": "old", "d": "old description"}) + "\n",
        encoding="utf-8",
    )

    cache_class = _load_cache(monkeypatch)
    cache = cache_class(str(tmp_path), max_entries=10, enabled=True)
    for index in range(11):
        cache.save(f"image-{index}", f"description-{index}")

    assert cache.lookup("image-10") == "description-10"
    assert cache.lookup("old") is None
    assert cache.entry_count <= 10


def test_clear_async_removes_persisted_entries(monkeypatch, tmp_path):
    """The async clear operation updates memory and disk together."""
    cache_class = _load_cache(monkeypatch)
    cache = cache_class(str(tmp_path), enabled=True)
    cache.save("image-1", "description")

    assert asyncio.run(cache.clear_async()) is True
    assert cache.lookup("image-1") is None
    assert cache.entry_count == 0
    assert not (tmp_path / "image_cache" / "descriptions.jsonl").exists()
