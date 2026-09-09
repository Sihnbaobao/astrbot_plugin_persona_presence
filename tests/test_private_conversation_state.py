"""Regression tests for private conversation states."""

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parents[1] / "utils" / "private_conversation_state.py"
_SPEC = importlib.util.spec_from_file_location(
    "private_conversation_state_test", _MODULE_PATH
)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
PrivateConversationState = _MODULE.PrivateConversationState


def test_sleep_reply_uses_a_range_and_exposes_messages_to_the_gate():
    """A sleep sign-off has a range and keeps incoming messages visible to review."""
    state = PrivateConversationState()
    boundary = state.record_reply(
        "qq_private_42", "璃月很困，明天还要早起，先睡了", now=100
    )

    assert boundary is not None
    assert boundary.kind == "sleep"
    assert boundary.sleep_mode == "literal"
    assert boundary.reopen_after == 100 + 4 * 60 * 60
    assert boundary.expire_after == 100 + 10 * 60 * 60
    assert boundary.reopen_after <= boundary.wake_at <= boundary.expire_after
    assert boundary.seconds_until_wake(now=boundary.wake_at) == 0
    assert state.get("qq_private_42", now=101) is boundary
    prompt = state.build_decision_context("qq_private_42", now=101)
    assert "private sleep assumption" in prompt
    assert "Incoming private messages are visible" in prompt
    assert "sleep_interpretation=literal" in prompt
    assert "real rest or offline boundary" in prompt
    assert "Importance raises priority but does not force a reply" in prompt
    assert state.get("qq_private_42", now=100 + 8 * 60 * 60) is boundary
    assert state.get("qq_private_42", now=100 + 10 * 60 * 60) is None


def test_ambiguous_sleep_signoff_uses_a_soft_boundary():
    """A bare sleep sign-off stays ambiguous instead of imposing a hard lock."""
    state = PrivateConversationState()
    boundary = state.record_reply("qq_private_42", "晚安，我先睡了", now=100)

    assert boundary is not None
    assert boundary.kind == "sleep"
    assert boundary.sleep_mode == "ambiguous"
    assert boundary.reopen_after == 100
    assert boundary.expire_after == 100 + 10 * 60 * 60
    assert 100 + 4 * 60 * 60 <= boundary.wake_at <= boundary.expire_after
    prompt = state.build_decision_context("qq_private_42", now=101)
    assert "sleep_interpretation=ambiguous" in prompt
    assert "soft boundary with no fixed earliest lock" in prompt


def test_explicit_sleep_claim_is_literal_even_with_a_persona_name():
    """A clear persona sleep claim creates the real overnight boundary."""
    state = PrivateConversationState()
    boundary = state.record_reply(
        "qq_private_42", "凌晨三点了，璃月要睡了", now=100
    )

    assert boundary is not None
    assert boundary.sleep_mode == "literal"
    assert boundary.reopen_after == 100 + 4 * 60 * 60
    assert boundary.expire_after == 100 + 10 * 60 * 60


def test_ai_interpretation_can_convert_ambiguous_sleep_to_avoidance():
    """A later private decision can classify a sleep sign-off as a chat exit."""
    state = PrivateConversationState()
    state.record_reply("qq_private_42", "我睡了", now=100)

    boundary = state.apply_sleep_interpretation(
        "qq_private_42", "conversational_exit", now=200
    )

    assert boundary is not None
    assert boundary.kind == "dismissive"
    assert boundary.expire_after is None
    assert boundary.wake_at is None


def test_short_sleep_reply_uses_a_shorter_range():
    """An explicit short nap does not use the overnight sleep range."""
    state = PrivateConversationState()
    boundary = state.record_reply("qq_private_42", "我去睡一会儿", now=100)

    assert boundary is not None
    assert boundary.reopen_after == 100 + 30 * 60
    assert boundary.expire_after == 100 + 3 * 60 * 60


def test_playful_dismissive_language_becomes_a_review_hint_only():
    """Playful dismissal language is reviewed from the next message context."""
    state = PrivateConversationState()

    boundary = state.record_reply(
        "qq_private_42", "不记得了，反正比你晚，别烦璃月", now=200
    )

    assert boundary is not None
    assert boundary.kind == "dismissive"
    prompt = state.build_decision_context("qq_private_42", now=201)
    assert "soft review hint" in prompt
    assert "别烦璃月" in prompt
    assert "not proof" in prompt


def test_nonterminal_dismissive_words_are_left_to_semantic_review():
    """The lexical signal schedules review but does not decide the outcome."""
    state = PrivateConversationState()

    boundary = state.record_reply(
        "qq_private_42", "我不是不想聊，只是现在还有别的事", now=200
    )

    assert boundary is not None
    assert boundary.kind == "dismissive"


def test_dismissive_reply_stays_visible_without_a_fixed_timeout():
    """A dismissal defaults to silence but can be reconsidered for every message."""
    state = PrivateConversationState()
    boundary = state.record_reply("qq_private_42", "不想理你了，别来烦我", now=200)

    assert boundary is not None
    assert boundary.kind == "dismissive"
    assert boundary.reopen_after == 200
    assert boundary.expire_after is None
    second_boundary = state.record_reply("qq_private_43", "没空陪你", now=200)
    assert second_boundary is not None
    assert second_boundary.kind == "dismissive"
    assert state.get("qq_private_42", now=200 + 30 * 24 * 60 * 60) is boundary
    prompt = state.build_decision_context("qq_private_42", now=200 + 30 * 24 * 60 * 60)
    assert "private boundary review hint" in prompt
    assert "only message that needs a reply decision" in prompt
    assert "not actually bothersome" in prompt
    assert "no automatic cooldown timer" in prompt
    assert "不想理你了，别来烦我" in prompt


def test_pending_messages_survive_the_sleep_range_for_later_context():
    """Messages received while asleep remain available after the state expires."""
    state = PrivateConversationState()
    state.record_reply("qq_private_42", "晚安，璃月去睡了", now=100)
    state.record_pending_message(
        "qq_private_42",
        {
            "message_id": "m-1",
            "content": "醒了记得看这个重要的事情",
            "sender_name": "小明",
        },
    )

    assert "重要的事情" in state.build_pending_context("qq_private_42")
    state.clear_pending_messages("qq_private_42")
    assert state.build_pending_context("qq_private_42") == ""


def test_ordinary_reply_and_user_sleep_instruction_do_not_close_chat():
    """Ordinary conversation and telling the user to sleep stay open."""
    state = PrivateConversationState()

    assert state.record_reply("qq_private_42", "这个问题还挺有意思", now=300) is None
    assert state.record_reply("qq_private_42", "你先去睡吧，我还不困", now=301) is None


def test_boundary_can_be_cleared_for_a_real_reopening():
    """A deliberate reopening clears only the selected private chat."""
    state = PrivateConversationState()
    state.record_reply("qq_private_42", "晚安，璃月去睡了", now=400)
    state.record_reply("qq_private_43", "不聊了", now=400)

    state.clear("qq_private_42")

    assert state.get("qq_private_42", now=401) is None
    assert state.get("qq_private_43", now=401) is not None
