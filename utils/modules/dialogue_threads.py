"""A small, non-semantic view of who has been talking to whom.

The tracker does not try to solve topic modeling. It keeps the last few
turns per group and exposes one useful fact: when the current line has no
structural addressee, who were the people immediately talking? This is
enough to keep “你/你说的这个” from defaulting to the bot in a side chat.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

_MAX_TURNS = 24


@dataclass
class _Turn:
    """One observed group turn."""

    sender_id: str
    sender_name: str
    text: str
    reply_to_other: bool = False
    at_other: bool = False
    is_bot: bool = False


class DialogueThreads:
    """Keep a bounded per-chat turn history for addressee hints."""

    def __init__(self) -> None:
        self._chats: dict[str, deque[_Turn]] = {}

    def observe(
        self,
        umo: str,
        sender_id: str,
        sender_name: str,
        text: str,
        *,
        reply_to_other: bool = False,
        at_other: bool = False,
        is_bot: bool = False,
    ) -> None:
        """Record one turn.

        Args:
            umo: Conversation origin.
            sender_id: Sender identifier.
            sender_name: Sender display name.
            text: Message text.
            reply_to_other: Whether the turn structurally replies to someone else.
            at_other: Whether the turn mentions someone else.
            is_bot: Whether the sender is the persona itself.
        """
        turns = self._chats.setdefault(str(umo), deque(maxlen=_MAX_TURNS))
        turns.append(
            _Turn(
                sender_id=str(sender_id or ""),
                sender_name=str(sender_name or ""),
                text=str(text or "")[:300],
                reply_to_other=bool(reply_to_other),
                at_other=bool(at_other),
                is_bot=bool(is_bot),
            )
        )

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
        turns = list(self._chats.get(str(umo), ()))
        # The tracker may have already seen this turn during a second decision.
        if turns and turns[-1].sender_id == str(sender_id) and turns[-1].text == str(text or "")[:300]:
            turns = turns[:-1]
        if not turns:
            return ""
        recent = turns[-4:]
        if any(turn.is_bot for turn in recent[-2:]):
            return ""
        names: list[str] = []
        for turn in recent:
            if turn.is_bot or not turn.sender_name:
                continue
            if turn.sender_name not in names:
                names.append(turn.sender_name)
        if not names:
            return ""
        if len(names) == 1:
            return (
                "[话轮线索] 刚才是 "
                + names[0]
                + " 在说话。当前这句没有明确点你，先按对方的话理解，"
                "不要默认“你/你说的这个”是在说 bot。"
            )
        return (
            "[话轮线索] 刚才主要是 "
            + "、".join(names[:3])
            + " 在接话。当前这句没有明确点你，先看它是不是在接他们之间的对话，"
            "不要默认“你/你说的这个”是在说 bot。"
        )
