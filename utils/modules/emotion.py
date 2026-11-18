"""Emotion DLC: a small, decaying emotional state for the persona.

The state is a vector of six base emotions, not one label. Pair and triple
combinations produce a short inner-state note for the reply prompt. Events are
appraised only when the message is actually directed at the persona; ordinary
group chatter does not move the state.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .base import FeatureContext, ModuleManifest, PluginModule
from .emotion_relations import RelationLedger

_BASE_EMOTIONS = ("joy", "anger", "sadness", "fear", "disgust", "surprise")
_HALF_LIFE_MINUTES = {
    "joy": 60.0,
    "anger": 45.0,
    "sadness": 120.0,
    "fear": 30.0,
    "disgust": 90.0,
    "surprise": 15.0,
}
_SINGLE_LABELS = {
    "joy": "有点开心",
    "anger": "有点恼",
    "sadness": "有点低落",
    "fear": "有点不安",
    "disgust": "有点嫌",
    "surprise": "有点意外",
}
_PAIR_LABELS = {
    ("joy", "anger"): "有点想笑，又有点恼",
    ("joy", "sadness"): "有点怀念，也有点低落",
    ("joy", "fear"): "有点期待，也有点紧张",
    ("joy", "disgust"): "有点想笑，也有点嫌",
    ("joy", "surprise"): "有点惊喜",
    ("anger", "sadness"): "有点委屈，也生气",
    ("anger", "fear"): "有点慌，也在强撑",
    ("anger", "disgust"): "有点恼，也觉得对方在挑衅",
    ("anger", "surprise"): "有点错愕，也有点恼",
    ("sadness", "fear"): "有点不安，也低落",
    ("sadness", "disgust"): "有点失望，也有点嫌",
    ("sadness", "surprise"): "有点愣住，也有点怅然",
    ("fear", "disgust"): "有点排斥，也有点不安",
    ("fear", "surprise"): "有点被吓到",
    ("disgust", "surprise"): "有点无语，也有点嫌",
}
_TRIPLE_LABELS = {
    ("joy", "anger", "sadness"): "有点哭笑不得",
    ("anger", "sadness", "disgust"): "有点怨，也懒得遮掩",
    ("joy", "sadness", "fear"): "有点患得患失",
    ("anger", "fear", "disgust"): "有点羞愤",
}
_EMOTION_LABELS = {
    "joy": "开心",
    "anger": "恼",
    "sadness": "低落",
    "fear": "不安",
    "disgust": "嫌",
    "surprise": "意外",
}
_STATE_VERSION = 1


@dataclass
class _Mood:
    """One user's emotional vector in one conversation."""

    base: dict[str, float] = field(default_factory=dict)
    cause: str = ""
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        for emotion in _BASE_EMOTIONS:
            self.base.setdefault(emotion, 0.0)


class EmotionModule(PluginModule):
    """A small, event-driven emotion layer for one persona.

    Args:
        data_dir: Directory holding emotion.json.
        sensitivity: Multiplier for event-induced changes.
        decay_minutes: Base half-life used when an emotion has no override.
    """

    manifest = ModuleManifest(
        key="emotion",
        name="情绪",
        summary="会开心、会恼、会低落；情绪来自正在发生的事，不来自随机。",
        config_group="dlc_emotion",
        default_enabled=False,
    )

    def __init__(
        self,
        data_dir: "Path | str | None" = None,
        *,
        sensitivity: float = 0.6,
        decay_minutes: int = 45,
    ) -> None:
        super().__init__()
        self.data_dir = Path(data_dir) if data_dir else None
        self.path = self.data_dir / "emotion.json" if self.data_dir else None
        self.sensitivity = max(0.1, min(2.0, float(sensitivity)))
        self.decay_minutes = max(5, int(decay_minutes))
        self.chats: dict[str, dict[str, _Mood]] = {}
        self._last_seen: dict[tuple[str, str], tuple[str, float]] = {}
        self.ledger = RelationLedger(
            self.data_dir / "relations.json" if self.data_dir else None,
            sensitivity=self.sensitivity,
        )
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for umo, users in (raw.get("chats") or {}).items():
            if not isinstance(users, dict):
                continue
            bucket = self.chats.setdefault(str(umo), {})
            for user_id, item in users.items():
                if not isinstance(item, dict):
                    continue
                base = item.get("base") or {}
                mood = _Mood(
                    base={
                        emotion: max(0.0, min(1.0, float(base.get(emotion, 0.0))))
                        for emotion in _BASE_EMOTIONS
                    },
                    cause=str(item.get("cause") or "")[:120],
                    updated_at=float(item.get("updated_at") or 0.0),
                )
                bucket[str(user_id)] = mood

    def _save(self) -> None:
        if self.path is None:
            return
        payload: dict[str, Any] = {"version": _STATE_VERSION, "chats": {}}
        for umo, users in self.chats.items():
            payload["chats"][umo] = {
                user_id: {
                    "base": mood.base,
                    "cause": mood.cause,
                    "updated_at": mood.updated_at,
                }
                for user_id, mood in users.items()
                if any(value >= 0.01 for value in mood.base.values())
            }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temp.replace(self.path)
        except OSError as exc:
            logger.debug(f"[情绪] 写入失败: {exc}")

    @staticmethod
    def _sender_id(event: Any) -> str:
        try:
            return str(event.get_sender_id() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _text(event: Any) -> str:
        try:
            return str(event.get_message_str() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _persona_name(persona: str) -> str:
        match = re.search(r"你的名字是([^\s，。,.]+)", persona or "")
        if match:
            return match.group(1).strip()
        return "璃月"

    def _directed(self, event: Any, is_private: bool, persona: str = "") -> bool:
        if is_private:
            return True
        try:
            if bool(getattr(event, "is_at_or_wake_command", False)):
                return True
        except Exception:
            pass
        text = self._text(event)
        name = self._persona_name(persona)
        return bool(text and name and name in text)

    def _decay(self, mood: _Mood, now: float) -> None:
        if mood.updated_at <= 0:
            mood.updated_at = now
            return
        minutes = max(0.0, (now - mood.updated_at) / 60.0)
        for emotion in _BASE_EMOTIONS:
            half_life = _HALF_LIFE_MINUTES.get(emotion, self.decay_minutes)
            mood.base[emotion] *= 0.5 ** (minutes / half_life)
        mood.updated_at = now

    def _add(self, mood: _Mood, **deltas: float) -> None:
        for emotion, delta in deltas.items():
            if emotion not in mood.base:
                continue
            value = mood.base[emotion] + float(delta) * self.sensitivity
            mood.base[emotion] = max(0.0, min(1.0, value))

    def _appraise(self, text: str, mood: _Mood, umo: str, user_id: str) -> bool:
        now = time.time()
        self._decay(mood, now)
        changed = False
        cause = ""

        insult = re.search(
            r"闭嘴|滚|傻|蠢|装|烦|别说话|去死|废物|垃圾|恶心|有病|神经|"
            r"你算|凭什么|少来|别装|讨厌",
            text,
        )
        praise = re.search(
            r"可爱|好看|漂亮|喜欢|厉害|聪明|谢谢|温柔|有趣|牛|强|厉害",
            text,
        )
        apology = re.search(r"对不起|抱歉|别生气|我错|抱抱|安慰|没事", text)
        dismiss = re.search(r"随便|无所谓|懒得|不理|别烦|少来|爱怎样怎样", text)
        conflict = re.search(r"吵|争|怼|凭什么|你算|不服|来啊", text)
        surprise = re.search(r"居然|竟然|卧槽|什么鬼|吓|居然还", text)

        if insult:
            self._add(mood, anger=0.26, disgust=0.15, joy=-0.06)
            cause = text
            changed = True
        if praise:
            self._add(mood, joy=0.20, anger=-0.05, sadness=-0.05)
            cause = text
            changed = True
        if apology:
            self._add(mood, anger=-0.22, sadness=-0.10, joy=0.04)
            cause = text
            changed = True
        if dismiss:
            self._add(mood, sadness=0.10, disgust=0.08, joy=-0.04)
            cause = text
            changed = True
        if conflict:
            self._add(mood, anger=0.16, disgust=0.06, fear=0.04)
            cause = text
            changed = True
        if surprise:
            self._add(mood, surprise=0.18)
            cause = cause or text
            changed = True

        key = (umo, user_id)
        previous = self._last_seen.get(key)
        if previous:
            previous_text, previous_at = previous
            if now - previous_at < 600:
                overlap = len(set(text) & set(previous_text))
                if overlap >= max(4, int(len(text) * 0.6)):
                    self._add(mood, anger=0.08, disgust=0.05)
                    cause = cause or text
                    changed = True
        self._last_seen[key] = (text, now)

        if changed:
            mood.cause = re.sub(r"\s+", " ", cause).strip()[:60]
            mood.updated_at = now
        return changed

    async def on_event(self, ctx: FeatureContext) -> None:
        if not self._directed(ctx.event, ctx.is_private, ctx.persona):
            return
        user_id = self._sender_id(ctx.event) or "unknown"
        bucket = self.chats.setdefault(ctx.umo, {})
        mood = bucket.setdefault(user_id, _Mood())
        text = self._text(ctx.event)
        if self._appraise(text, mood, ctx.umo, user_id):
            self._save()
        if self.ledger.appraise(
            ctx.umo,
            user_id,
            text,
            is_private=ctx.is_private,
            group_id=ctx.group_id,
        ):
            self.ledger.save()

    def block_for(self, event: Any, is_private: bool) -> str:
        try:
            umo = str(event.unified_msg_origin)
        except Exception:
            return ""
        bucket = self.chats.get(umo, {})
        user_id = self._sender_id(event) or "unknown"
        mood = bucket.get(user_id)
        blocks: list[str] = []
        if mood is not None:
            self._decay(mood, time.time())
            top = sorted(mood.base.items(), key=lambda item: item[1], reverse=True)
            strong = [(name, value) for name, value in top if value >= 0.18]
            if strong:
                label = self._label(strong)
                cause = mood.cause.strip()
                if label:
                    blocks.append(
                        f"[此刻]\n{label}——{cause}"
                        if cause
                        else f"[此刻]\n{label}"
                    )
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            group_id = ""
        relation_block = self.ledger.block_for(
            umo, user_id, is_private, group_id=group_id
        )
        if relation_block:
            blocks.append(relation_block)
        return "\n\n".join(blocks)

    def should_withdraw(self, umo: str) -> bool:
        """Tell whether the group attitude should suppress proactive speech.

        Args:
            umo: Conversation origin.

        Returns:
            True when the persona no longer wants to initiate in this group.
        """
        return self.ledger.should_withdraw(umo)

    def _label(self, strong: list[tuple[str, float]]) -> str:
        names = [name for name, _ in strong]
        if len(names) >= 3:
            key3 = tuple(sorted(names[:3]))
            if key3 in _TRIPLE_LABELS:
                return _TRIPLE_LABELS[key3]
        if len(names) >= 2:
            key2 = tuple(sorted(names[:2]))
            return _PAIR_LABELS.get(
                key2,
                f"有点{_EMOTION_LABELS.get(names[0], '复杂')}，也有点{_EMOTION_LABELS.get(names[1], '复杂')}",
            )
        return _SINGLE_LABELS.get(names[0], "有点复杂")

    async def close(self) -> None:
        self._save()
        self.ledger.save()
