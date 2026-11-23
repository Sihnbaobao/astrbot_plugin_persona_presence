"""Tests for the lightweight group turn tracker."""

import importlib.util
import sys
from pathlib import Path

MODULE = Path(__file__).parents[1] / "utils" / "modules" / "dialogue_threads.py"


def _load():
    spec = importlib.util.spec_from_file_location("dialogue_threads_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_two_person_side_chat_is_flagged():
    module = _load()
    threads = module.DialogueThreads()
    threads.observe("g", "a", "阿甲", "这个本子好看")
    threads.observe("g", "b", "阿乙", "你那个在哪买的")
    hint = threads.hint("g", "b", "你说的这个能发我吗")
    assert "阿甲" in hint
    assert "没点你" in hint


def test_bot_turn_clears_side_hint():
    module = _load()
    threads = module.DialogueThreads()
    threads.observe("g", "bot", "璃月", "好看", is_bot=True)
    assert threads.hint("g", "a", "你说的这个") == ""


def test_explicit_name_has_no_hint():
    module = _load()
    threads = module.DialogueThreads()
    threads.observe("g", "a", "阿甲", "随便")
    assert (
        threads.hint("g", "b", "璃月你觉得呢", persona_name="璃月")
        == ""
    )


def test_thread_openness_needs_three_people():
    module = _load()
    threads = module.DialogueThreads()
    threads.observe("g", "a", "阿甲", "话费怎么扣这么快")
    threads.observe("g", "b", "阿乙", "我也想知道")
    assert threads.is_open_thread("g", "b", "我也想知道") is False
    threads.observe("g", "c", "阿丙", "运营商的问题")
    assert threads.is_open_thread("g", "c", "运营商的问题") is True


def test_topic_summary_sticks_to_thread():
    module = _load()
    threads = module.DialogueThreads()
    threads.observe("g", "a", "阿甲", "为什么不找女朋友")
    threads.observe("g", "b", "阿乙", "因为是男同")
    thread_id, context, previous = threads.topic_context("g", "b", "因为是男同")
    assert thread_id
    assert "阿甲" in context
    assert previous == ""
    threads.set_topic_summary("g", "b", "因为是男同", "在聊阿甲不找女朋友的事")
    _, _, updated = threads.topic_context("g", "b", "因为是男同")
    assert updated == "在聊阿甲不找女朋友的事"
