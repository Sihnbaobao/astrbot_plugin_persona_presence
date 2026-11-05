"""Tests for the bounded image archive."""

import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
MODULES_DIR = REPO_ROOT / "utils" / "modules"
DAY = 86400


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

    package_name = "image_archive_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    modules_pkg = types.ModuleType(package_name + ".utils.modules")
    modules_pkg.__path__ = [str(MODULES_DIR)]
    utils_pkg = types.ModuleType(package_name + ".utils")
    utils_pkg.__path__ = [str(REPO_ROOT / "utils")]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, package_name + ".utils", utils_pkg)
    monkeypatch.setitem(sys.modules, package_name + ".utils.modules", modules_pkg)

    full_name = package_name + ".utils.modules.image_archive"
    spec = importlib.util.spec_from_file_location(
        full_name, MODULES_DIR / "image_archive.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, full_name, module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _image(tmp_path, name, size=64, fill=b"x"):
    path = tmp_path / name
    path.write_bytes(fill * size)
    return path


def test_ingest_search_and_dedupe_are_chat_scoped(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    archive = module.ImageArchive(tmp_path, enabled=True)
    source = _image(tmp_path, "a.png")

    entry = archive.ingest(
        source,
        chat_id="100",
        is_group=True,
        description="一只橘猫趴在键盘上",
        tags=["猫", "键盘"],
        score=0.8,
    )
    assert entry is not None
    assert (
        archive.ingest(
            source,
            chat_id="100",
            is_group=True,
            description="重复",
            score=0.1,
        ).id
        == entry.id
    )
    assert len(archive.search("100", "猫")) == 1
    assert archive.search("200", "猫") == []


def test_ordinary_images_expire(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    archive = module.ImageArchive(tmp_path, enabled=True, ordinary_days=7)
    archive.ingest(
        _image(tmp_path, "b.png"),
        chat_id="100",
        is_group=False,
        score=0.1,
        now=1000,
    )

    assert archive.prune(1000 + 8 * DAY) == 1
    assert archive.stats()["count"] == 0


def test_favorite_is_reviewed_and_deleted_when_unwanted(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    archive = module.ImageArchive(tmp_path, enabled=True, review_days=30)
    entry = archive.ingest(
        _image(tmp_path, "c.png"),
        chat_id="100",
        is_group=False,
        score=0.9,
        now=1000,
    )

    assert entry.favorite is True
    assert archive.due_for_review(now=1000 + 31 * DAY)
    assert archive.review(entry.id, keep=False, now=1000 + 31 * DAY)
    assert archive.stats()["count"] == 0
    assert not (archive.root / entry.file).exists()


def test_capacity_evicts_ordinary_before_favorite(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    archive = module.ImageArchive(tmp_path, enabled=True)
    archive.max_bytes = 3000
    archive.ingest(
        _image(tmp_path, "old.png", 2000),
        chat_id="100",
        is_group=False,
        score=0.1,
        now=1000,
    )
    favorite = archive.ingest(
        _image(tmp_path, "keep.png", 2000, fill=b"y"),
        chat_id="100",
        is_group=False,
        score=0.9,
        now=1001,
    )

    assert archive.stats()["count"] == 1
    assert favorite.favorite is True
    assert archive.stats()["bytes"] <= 3000


def test_pending_analysis_can_be_retried(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    archive = module.ImageArchive(tmp_path, enabled=True)
    entry = archive.ingest(
        _image(tmp_path, "pending.png"),
        chat_id="100",
        is_group=False,
        description="",
    )

    assert entry.analysis_pending is True
    assert archive.pending_analysis(retry_seconds=0)
    assert archive.mark_analysis_failed(entry.id, now=1000) == 1
    assert archive.update_analysis(
        entry.id,
        description="一只橘猫",
        tags=["猫"],
        score=0.8,
        favorite=True,
    )
    assert entry.analysis_pending is False
    assert entry.description == "一只橘猫"
    assert entry.favorite is True


def test_failed_analysis_stops_after_retry_budget(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    archive = module.ImageArchive(tmp_path, enabled=True)
    entry = archive.ingest(
        _image(tmp_path, "stuck.png"),
        chat_id="100",
        is_group=False,
        description="",
    )

    assert archive.pending_analysis(retry_seconds=0, max_attempts=1)
    assert archive.mark_analysis_failed(entry.id, reason="timeout") == 1
    assert archive.pending_analysis(retry_seconds=0, max_attempts=1) == []
    assert entry.analysis_error == "timeout"


def test_same_image_in_another_chat_reuses_entry(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    archive = module.ImageArchive(tmp_path, enabled=True)
    source = _image(tmp_path, "shared.png", size=128)

    first = archive.ingest(
        source,
        chat_id="100",
        is_group=False,
        description="一张共享的图",
        tags=["共享"],
        score=0.7,
    )
    second = archive.ingest(
        source,
        chat_id="200",
        is_group=True,
        description="不该重复分析",
        tags=["重复"],
        score=0.1,
    )

    assert second.id == first.id
    assert len(archive.entries) == 1
    assert archive.search("100", "共享") == [first]
    assert archive.search("200", "共享") == [first]
    assert archive.matches_entry(first, "200")
