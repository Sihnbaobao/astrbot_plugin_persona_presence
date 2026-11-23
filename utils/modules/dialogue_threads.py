"""A lightweight per-group dialogue-thread tracker.

This is deliberately heuristic, not a topic model. It keeps recent turns,
assigns them to a thread using reply/@ links, short-reaction inheritance and
lexical overlap, and exposes a short addressee hint for the decision and
reply prompts.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field

_MAX_TURNS = 24
_NEW_THREAD_GAP = 180.0
_MAX_TURNS_IN_THREAD = 8


def _normal_text(text: str) -> str:
    """Strip punctuation and spaces for lexical comparison."""
    return re.sub(r"[\s\W_]+", "", str(text or ""))


def _bigrams(text: str) -> set[str]:
    """Return Chinese character bigrams for a cheap topic-overlap check."""
    clean = _normal_text(text)
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


@dataclass
class _Turn:
    """One observed group turn."""

    sender_id: str
    sender_name: str
    text: str
    at: float
    thread_id: str
    reply_to_other: bool = False
    at_other: bool = False
    is_bot: bool = False


@dataclass
class _Thread:
    """A short-lived conversation thread."""

    thread_id: str
    turns: deque = field(default_factory=lambda: deque(maxlen=_MAX_TURNS_IN_THREAD))


class DialogueThreads:
    """Keep bounded turn/thread history per chat."""

    def __init__(self) -> None:
        self._chats: dict[str, deque[_Turn]] = {}
        self._threads: dict[str, dict[str, _Thread]] = {}
        self._serial = 0

    def observe(
        self,
        umo: str,
        sender_id: str,
        sender_name: str,
        text: str,
        *,
        at: float | None = None,
        reply_to_other: bool = False,
        at_other: bool = False,
        is_bot: bool = False,
    ) -> str:
        """Record one turn and return its thread id.

        Args:
            umo: Conversation origin.
            sender_id: Sender identifier.
            sender_name: Sender display name.
            text: Message text.
            at: Event timestamp, or now when omitted.
            reply_to_other: Whether the turn structurally replies to someone else.
            at_other: Whether the turn mentions someone else.
            is_bot: Whether the sender is the persona itself.

        Returns:
            The thread id this turn was assigned to.
        """
        umo = str(umo)
        turns = self._chats.setdefault(umo, deque(maxlen=_MAX_TURNS))
        threads = self._threads.setdefault(umo, {})
        now = float(at if at is not None else time.time())
        previous = turns[-1] if turns else None
        clean = str(text or "").strip()
        thread_id = ""
        if previous is not None and now - previous.at <= _NEW_THREAD_GAP:
            if reply_to_other or at_other:
                thread_id = previous.thread_id
            elif len(clean) <= 8:
                thread_id = previous.thread_id
            else:
                previous_thread = threads.get(previous.thread_id)
                known = " ".join(
                    turn.text for turn in (previous_thread.turns if previous_thread else ())
                )
                overlap = _bigrams(known) & _bigrams(clean)
                if overlap:
                    thread_id = previous.thread_id
        if not thread_id:
            self._serial += 1
            thread_id = f"t{self._serial}"
        thread = threads.get(thread_id)
        if thread is None:
            thread = _Thread(thread_id=thread_id)
            threads[thread_id] = thread
        turn = _Turn(
            sender_id=str(sender_id or ""),
            sender_name=str(sender_name or ""),
            text=clean[:300],
            at=now,
            thread_id=thread_id,
            reply_to_other=bool(reply_to_other),
            at_other=bool(at_other),
            is_bot=bool(is_bot),
        )
        turns.append(turn)
        thread.turns.append(turn)
        return thread_id

    def _current_thread(self, umo: str, sender_id: str, text: str) -> _Thread | None:
        """Find the thread of the current turn, or the latest thread."""
        turns = list(self._chats.get(str(umo), ()))
        if not turns:
            return None
        clean = str(text or "")[:300]
        for turn in reversed(turns):
            if turn.sender_id == str(sender_id) and turn.text == clean:
                return self._threads.get(str(umo), {}).get(turn.thread_id)
        return self._threads.get(str(umo), {}).get(turns[-1].thread_id)

    def hint(
        self,
        umo: str,
        sender_id: str,
        text: str,
        *,
        is_directly_addressed: bool = False,
        persona_name: str = "",
    ) -> str:
        """Return a short addressee hint for an unaddressed group turn.

        Args:
            umo: Conversation origin.
            sender_id: Current sender identifier.
            text: Current message text.
            is_directly_addressed: Whether the platform explicitly targets the bot.
            persona_name: Persona name, which counts as explicit address.

        Returns:
            A compact context line, or an empty string when the turn is clear.
        """
        if is_directly_addressed:
            return ""
        if persona_name and persona_name in str(text or ""):
            return ""
        thread = self._current_thread(umo, sender_id, text)
        if thread is None:
            return ""
        turns = [turn for turn in thread.turns if not turn.is_bot]
        if any(turn.is_bot for turn in thread.turns):
            return ""
        # On a re-decision the current turn may already be in the thread.
        if (
            turns
            and turns[-1].sender_id == str(sender_id)
            and turns[-1].text == str(text or "")[:300]
        ):
            turns = turns[:-1]
        if not turns:
            return ""
        lines: list[str] = []
        for turn in turns[-3:]:
            speaker = turn.sender_name or "群友"
            lines.append(f"{speaker}：{turn.text[:20]}")
        if not lines:
            return ""
        return "[线程] " + "；".join(lines) + "。没点你，顺着这段聊。"

    def is_open_thread(self, umo: str, sender_id: str, text: str = "") -> bool:
        """Tell whether the current thread looks open to the group.

        Args:
            umo: Conversation origin.
            sender_id: Current sender identifier.
            text: Current message text when available.

        Returns:
            True when the thread has at least three human participants.
        """
        thread = self._current_thread(umo, sender_id, text)
        if thread is None:
            return False
        names = {
            turn.sender_id
            for turn in thread.turns
            if not turn.is_bot and turn.sender_id
        }
        return len(names) >= 3
