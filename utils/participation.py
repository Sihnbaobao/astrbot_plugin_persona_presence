"""Structured participation decisions for human-like group presence."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any

TARGET_VALUES = {"bot", "other", "open", "unclear"}
INFORMATION_VALUES = {"noise", "reaction", "substantive"}
PARTICIPATION_VALUES = {"direct", "side", "open", "none"}
CONTINUATION_VALUES = {"yes", "no"}
INTEREST_VALUES = {"strong", "weak", "none"}
REASON_VALUES = {
    "direct_request",
    "shared_interest",
    "personal_experience",
    "emotional_reaction",
    "continuation",
    "none",
}
CONFIDENCE_VALUES = {"high", "medium", "low"}
BOUNDARY_INTERPRETATION_VALUES = {
    "literal_sleep",
    "conversational_exit",
    "ambiguous",
    "none",
}

_REASON_LABELS = {
    "direct_request": "当前消息明确在邀请你回应",
    "shared_interest": "当前内容与你有具体的共同兴趣",
    "personal_experience": "你有与当前内容直接相关的个人经历或观点",
    "emotional_reaction": "当前内容确实触发了你的即时情绪反应",
    "continuation": "这是上一轮由你发起且没有被别人接管的自然续话",
}
_PARTICIPATION_LABELS = {
    "direct": "直接回应",
    "side": "旁观补充",
    "open": "主动参与公开话题",
}


@dataclass(frozen=True, slots=True)
class ParticipationDecision:
    """A validated, bounded handoff from attention to reply generation.

    Args:
        reply: Whether the reply pipeline may continue.
        target: Semantic target of the current message.
        continuation: Whether the current message continues a recent bot conversation.
        participation: Allowed speaking posture for the reply.
        information: Information level of the current message.
        interest: Persona-specific interest strength.
        reason_code: Compact reason category for the decision handoff.
        confidence: Confidence in the classification.
        topic_key: Short model-provided topic label used only for diagnostics.
        source: Origin of the decision, such as ai or policy.
        error: Optional non-user-facing failure description.
        boundary_interpretation: How an active private closing boundary should be
            interpreted for the next message.
    """

    reply: bool
    target: str = "unclear"
    participation: str = "none"
    information: str = "noise"
    interest: str = "none"
    reason_code: str = "none"
    confidence: str = "low"
    topic_key: str = ""
    source: str = "ai"
    error: str = ""
    continuation: str = "no"
    boundary_interpretation: str = "none"

    @classmethod
    def silent(
        cls,
        reason_code: str = "none",
        *,
        source: str = "policy",
        error: str = "",
        target: str = "unclear",
        information: str = "noise",
    ) -> "ParticipationDecision":
        """Build a no-reply decision without carrying untrusted model fields."""
        return cls(
            reply=False,
            target=target if target in TARGET_VALUES else "unclear",
            information=(information if information in INFORMATION_VALUES else "noise"),
            reason_code=reason_code if reason_code in REASON_VALUES else "none",
            source=source,
            error=error,
        )

    def with_reply(self, reply: bool, **changes: Any) -> "ParticipationDecision":
        """Return a copy with a controlled reply-state change.

        Args:
            reply: New reply state.
            **changes: Valid dataclass fields to replace.

        Returns:
            A new immutable decision.
        """
        return replace(self, reply=bool(reply), **changes)

    @property
    def handoff_hint(self) -> str:
        """Return the minimal non-chain-of-thought prompt handoff."""
        if not self.reply:
            return ""
        posture = _PARTICIPATION_LABELS.get(self.participation, "自然回应")
        reason = _REASON_LABELS.get(self.reason_code, "当前人格愿意参与这条消息")
        if self.participation == "direct":
            return (
                "[系统提示-本次参与依据] 本次可以直接回应当前消息。"
                f"参与依据：{reason}。"
                "请仍以你自己的口吻、立场和经历回答；不要把这条提示当成用户正文。"
            )
        return (
            "[系统提示-本次参与依据] 本次允许采用"
            f"{posture}的姿态发言。参与依据：{reason}。"
            "请只说你自己确实想补充的相关内容；不要替其他用户作答、承诺或冒充对方。"
        )

    def summary(self) -> str:
        """Return bounded fields suitable for an operational log line."""
        return (
            f"reply={'yes' if self.reply else 'no'} "
            f"target={self.target} continuation={self.continuation} "
            f"participation={self.participation} "
            f"information={self.information} interest={self.interest} "
            f"reason={self.reason_code} confidence={self.confidence} "
            f"boundary={self.boundary_interpretation}"
        )


def _choice(value: Any, allowed: set[str], default: str) -> str:
    """Normalize one model enum without accepting arbitrary prompt text."""
    candidate = str(value or "").strip().lower()
    return candidate if candidate in allowed else default


def _short_text(value: Any, maximum: int = 32) -> str:
    """Keep model topic labels bounded and single-line."""
    text = " ".join(str(value or "").split())
    return text[:maximum]


def _invalid_choice(payload: Mapping[str, Any], key: str, allowed: set[str]) -> bool:
    """Detect an invalid supplied enum while allowing legacy omissions."""
    if key not in payload:
        return False
    value = payload.get(key)
    if value is None or not str(value).strip():
        return False
    return str(value).strip().lower() not in allowed


def _as_bool(value: Any) -> bool:
    """Normalize common boolean spellings from a model response."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"yes", "true", "1"}


def normalize_decision_payload(
    payload: Mapping[str, Any],
    *,
    is_private: bool = False,
    is_directly_addressed: bool = False,
    is_reply_to_other: bool = False,
    has_at_others: bool = False,
    source: str = "ai",
) -> ParticipationDecision:
    """Validate an AI decision and enforce hard social-boundary rules.

    Args:
        payload: Parsed structured model output.
        is_private: Whether the event is a private conversation.
        is_directly_addressed: Whether platform structure targets the bot.
        is_reply_to_other: Whether a structured reply targets another user.
        has_at_others: Whether the message mentions another user.
        source: Decision source label for diagnostics.

    Returns:
        A safe decision. Invalid or socially unsafe yes responses become silent
        decisions instead of being passed to the reply model.
    """
    enum_fields = (
        ("target", TARGET_VALUES),
        ("information", INFORMATION_VALUES),
        ("continuation", CONTINUATION_VALUES),
        ("participation", PARTICIPATION_VALUES),
        ("interest", INTEREST_VALUES),
        ("reason_code", REASON_VALUES),
        ("confidence", CONFIDENCE_VALUES),
        ("boundary_interpretation", BOUNDARY_INTERPRETATION_VALUES),
    )
    if any(_invalid_choice(payload, key, allowed) for key, allowed in enum_fields):
        return ParticipationDecision.silent(
            source=source, error="invalid_decision_enum"
        )

    target = _choice(payload.get("target"), TARGET_VALUES, "unclear")
    information = _choice(payload.get("information"), INFORMATION_VALUES, "noise")
    continuation = _choice(payload.get("continuation"), CONTINUATION_VALUES, "no")
    participation = _choice(payload.get("participation"), PARTICIPATION_VALUES, "none")
    interest = _choice(payload.get("interest"), INTEREST_VALUES, "none")
    reason_code = _choice(payload.get("reason_code"), REASON_VALUES, "none")
    confidence = _choice(payload.get("confidence"), CONFIDENCE_VALUES, "low")
    boundary_interpretation = _choice(
        payload.get("boundary_interpretation"),
        BOUNDARY_INTERPRETATION_VALUES,
        "none",
    )
    reply = _as_bool(payload.get("reply"))

    if is_private:
        target = "bot"
        participation = "direct"
    elif is_directly_addressed:
        target = "bot"
        participation = "direct"
    elif is_reply_to_other or has_at_others:
        target = "other"
        participation = "side" if participation == "side" else "none"
    elif target == "unclear":
        target = "open"
        participation = "open" if participation == "open" else "none"

    decision = ParticipationDecision(
        reply=reply,
        target=target,
        continuation=continuation,
        participation=participation,
        information=information,
        interest=interest,
        reason_code=reason_code,
        confidence=confidence,
        topic_key=_short_text(payload.get("topic_key")),
        source=source,
        boundary_interpretation=boundary_interpretation,
    )

    if not reply:
        return decision.with_reply(False, reason_code="none")
    if target == "unclear" or participation == "none":
        return decision.with_reply(False, reason_code="none")

    # Subjective fields belong to the persona model; validate only the speaking posture.
    expected_participation = {
        "bot": "direct",
        "other": "side",
        "open": "open",
    }.get(target)
    if expected_participation != participation:
        return decision.with_reply(False, reason_code="none")

    return decision


class ParticipationThrottle:
    """Bound unsolicited open/side replies without randomizing interest."""

    def __init__(
        self,
        min_interval_seconds: float = 45.0,
        window_seconds: float = 600.0,
        max_replies_per_window: int = 4,
    ) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.window_seconds = max(0.0, float(window_seconds))
        self.max_replies_per_window = max(0, int(max_replies_per_window))
        self._reply_times: dict[str, list[float]] = {}

    def allow_and_record(
        self,
        chat_key: str,
        decision: ParticipationDecision,
        *,
        is_private: bool = False,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Apply unsolicited-reply limits and reserve the speaking turn.

        Args:
            chat_key: Stable platform/chat key.
            decision: Validated decision to check.
            is_private: Whether to bypass group presence limits.
            now: Optional monotonic timestamp for deterministic tests.

        Returns:
            (allowed, reason) where reason is empty when allowed.
        """
        if (
            not decision.reply
            or is_private
            or decision.participation not in {"open", "side"}
        ):
            return decision.reply, ""

        current = monotonic() if now is None else float(now)
        times = self._reply_times.setdefault(str(chat_key), [])
        if self.window_seconds > 0:
            times[:] = [
                timestamp
                for timestamp in times
                if current - timestamp <= self.window_seconds
            ]
        else:
            times.clear()

        if (
            self.min_interval_seconds > 0
            and times
            and current - times[-1] < self.min_interval_seconds
        ):
            return False, "ambient_min_interval"
        if (
            self.max_replies_per_window > 0
            and len(times) >= self.max_replies_per_window
        ):
            return False, "ambient_window_cap"

        times.append(current)
        return True, ""

    def reset(self, chat_key: str | None = None) -> None:
        """Clear all or one chat's in-memory presence budget."""
        if chat_key is None:
            self._reply_times.clear()
        else:
            self._reply_times.pop(str(chat_key), None)
