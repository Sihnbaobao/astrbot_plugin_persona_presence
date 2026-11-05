"""Regression tests for the real-activity state lines."""

import importlib.util
import json
import sys
import time
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


def _load_module(monkeypatch):
    api_all = types.ModuleType("astrbot.api.all")
    api_all.logger = _Logger()
    api_all.AstrMessageEvent = object
    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", types.ModuleType("astrbot.api"))
    monkeypatch.setitem(sys.modules, "astrbot.api.all", api_all)

    package_name = "life_sight_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    utils_package = types.ModuleType(package_name + ".utils")
    utils_package.__path__ = [str(UTILS_DIR)]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, package_name + ".utils", utils_package)

    full_name = package_name + ".utils.life_sight"
    spec = importlib.util.spec_from_file_location(
        full_name, UTILS_DIR / "life_sight.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, full_name, module)
    spec.loader.exec_module(module)
    return module


def _stamp(minutes_ago):
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M")


def _write(path, records):
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


def test_recent_watch_is_described(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    log = tmp_path / "watch_log.json"
    _write(
        log,
        [
            {
                "time": _stamp(42),
                "title": "每天跑3公里，100天后我的身体有什么变化？",
                "score": 6.5,
                "actions": ["👍点赞"],
            }
        ],
    )

    lines = module.LifeSight(str(log), enabled=True).state_lines()

    assert any("看了《" in line for line in lines)
    assert any("每天跑3公里" in line for line in lines)
    assert any("6.5分" in line for line in lines)
    assert any("点了赞" in line for line in lines)


def test_no_record_says_nothing_happened(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    lines = module.LifeSight(str(tmp_path / "nope.json"), enabled=True).state_lines()

    assert lines == [] or lines[0].startswith("· ")


def test_failed_evaluation_is_skipped(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    log = tmp_path / "watch_log.json"
    _write(
        log,
        [
            {"time": _stamp(120), "title": "正常看完的", "score": 7},
            {
                "time": _stamp(10),
                "title": "没看懂的",
                "score": 0,
                "evaluation_status": "failed",
            },
        ],
    )

    lines = module.LifeSight(str(log), enabled=True).state_lines()

    assert any("正常看完的" in line for line in lines)
    assert not any("没看懂的" in line for line in lines)


def test_remaining_plan_is_listed(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    log = tmp_path / "watch_log.json"
    _write(log, [])
    today = datetime.now().strftime("%Y-%m-%d")
    (tmp_path / "autonomous_plan_today.json").write_text(
        json.dumps(
            {
                "date": today,
                "proactive_times": ["23:55"],
                "dynamic_watch_times": [],
                "special_follow_times": [],
                "bangumi_times": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    lines = module.LifeSight(str(log), enabled=True).state_lines()

    assert any("待会要做的" in line for line in lines)
    assert any("23:55" in line for line in lines)


def test_broken_file_is_silent(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    log = tmp_path / "watch_log.json"
    log.write_text("{not json", encoding="utf-8")

    lines = module.LifeSight(str(log), enabled=True).state_lines()

    assert lines == [] or lines[0].startswith("· ")


def test_disabled_layer_is_silent(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    log = tmp_path / "watch_log.json"
    _write(log, [{"time": _stamp(5), "title": "新视频", "score": 8}])

    assert module.LifeSight(str(log), enabled=False).state_lines() == []


def test_relative_labels(monkeypatch):
    module = _load_module(monkeypatch)
    relative = module.LifeSight._relative

    assert relative(_stamp(5)).endswith("分钟前")
    assert relative(_stamp(180)) == "3 小时前"


def test_activity_question_detection(monkeypatch):
    module = _load_module(monkeypatch)
    asked = module.LifeSight.looks_like_activity_question

    assert asked("在干嘛")
    assert asked("最近在忙什么")
    assert asked("你在看什么视频")
    assert not asked("你喜欢什么")
    assert not asked("吃饭了吗")
    assert not asked("奶龙是什么")


def test_content_question_detection(monkeypatch):
    module = _load_module(monkeypatch)
    asked = module.LifeSight.looks_like_content_question

    assert asked("你之前看了什么视频吗")
    assert asked("你在看什么")
    assert asked("最近看了哪些番")
    assert not asked("在干嘛")
    assert not asked("最近在忙什么")


def test_general_state_hides_the_video_title(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    log = tmp_path / "watch_log.json"
    _write(log, [{"time": _stamp(20), "title": "被无惨咬，还是被DIO咬？", "score": 7}])
    sight = module.LifeSight(str(log), enabled=True)

    general = sight.state_lines(include_title=False, fresh_only=True)
    assert any("刚刚" in line for line in general)
    assert not any("被无惨咬" in line for line in general)

    content = sight.state_lines(include_title=True)
    assert any("被无惨咬" in line for line in content)


def test_stale_watch_is_hidden_from_general_questions(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    log = tmp_path / "watch_log.json"
    _write(log, [{"time": _stamp(180), "title": "三小时前的视频", "score": 7}])
    sight = module.LifeSight(str(log), enabled=True)

    assert not any(
        "三小时前的视频" in line
        for line in sight.state_lines(include_title=False, fresh_only=True)
    )
    assert any(
        "三小时前的视频" in line for line in sight.state_lines(include_title=True)
    )
