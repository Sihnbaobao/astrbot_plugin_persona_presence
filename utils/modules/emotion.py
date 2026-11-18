"""Emotion DLC: a small, decaying emotional state for the persona.

The state is a vector of six base emotions, not one label. Pair and triple
combinations produce a short inner-state note for the reply prompt. Events are
appraised only when the message is actually directed at the persona; ordinary
group chatter does not move the state.
"""

from __future__ import annotations

import asyncio
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
_APPRAISAL_CUES = re.compile(
    r"你|是不是|装|随便|行|厉害|呵|6|哦|嗯|？|\?|凭什么|少来"
)
_APPRAISAL_PROMPT = """你是角色 {name} 的内心判断器，不是客服，也不是在回复。

看这句话对她意味着什么，只输出一个 JSON，不要解释：
{{"intent":"挑衅|羞辱|命令|越界|玩笑|关心|夸奖|敷衍|普通","intensity":0.0,"cause":"不超过30字"}}

规则：
- 玩笑和熟人互损不要判成羞辱。
- 只有真的越界、命令、贬低才叫挑衅/羞辱。
- 普通陈述就输出普通。
- intensity 表示这件事对她有多重，0.1 到 1.0。
"""


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
        context: Any = None,
        provider_id: str = "",
        appraisal_timeout: int = 8,
        appraisal_enabled: bool = True,
    ) -> None:
        super().__init__(context)
        self.data_dir = Path(data_dir) if data_dir else None
        self.path = self.data_dir / "emotion.json" if self.data_dir else None
        self.sensitivity = max(0.1, min(2.0, float(sensitivity)))
        self.decay_minutes = max(5, int(decay_minutes))
        self.provider_id = str(provider_id or "").strip()
        self.appraisal_timeout = max(3, int(appraisal_timeout))
        self.appraisal_enabled = bool(appraisal_enabled)
        self._appraisal_tasks: set[asyncio.Task] = set()
        self._last_appraisal: dict[tuple[str, str], float] = {}
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

        severe = re.search(
            r"闭嘴|滚(?:开|蛋|吧|啊)|傻|蠢|废物|垃圾|恶心|有病|神经|"
            r"你算|凭什么|少来|(?:你|别|少)装|装(?:什么|模作样|给谁看|傻|死)|"
            r"(?:你|真|好|别|少)烦|烦死|讨厌",
            text,
        )
        praise = re.search(
            r"(?<!不)(?:喜欢|可爱|好看|漂亮|厉害|聪明|谢谢|温柔|有趣|牛)", text
        )
        apology = re.search(r"对不起|抱歉|别生气|我错|抱抱|安慰", text)
        dismiss = re.search(r"随便|无所谓|懒得|不理|别烦|爱怎样怎样", text)
        conflict = re.search(r"吵|争|怼|凭什么|你算|不服|来啊", text)
        surprise = re.search(r"居然|竟然|卧槽|什么鬼|吓|居然还", text)

        # One turn produces one dominant appraisal, not a stack of keywords.
        if severe:
            self._add(mood, anger=0.26, disgust=0.15, joy=-0.06)
            cause = text
            changed = True
        elif conflict:
            self._add(mood, anger=0.16, disgust=0.06, fear=0.04)
            cause = text
            changed = True
        elif praise:
            self._add(mood, joy=0.20, anger=-0.05, sadness=-0.05)
            cause = text
            changed = True
        elif apology:
            self._add(mood, anger=-0.22, sadness=-0.10, joy=0.04)
            cause = text
            changed = True
        elif dismiss:
            self._add(mood, sadness=0.10, disgust=0.08, joy=-0.04)
            cause = text
            changed = True

        if surprise and not severe:
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
        locally_changed = self._appraise(text, mood, ctx.umo, user_id)
        if locally_changed:
            self._save()
        if self.ledger.appraise(
            ctx.umo,
            user_id,
            text,
            is_private=ctx.is_private,
            group_id=ctx.group_id,
        ):
            self.ledger.save()
        if not locally_changed:
            self._schedule_semantic(ctx, text, user_id)

    def _schedule_semantic(
        self, ctx: FeatureContext, text: str, user_id: str
    ) -> None:
        """Queue one semantic appraisal when local rules are inconclusive.

        Args:
            ctx: Turn context.
            text: Incoming message text.
            user_id: Sender identifier.
        """
        if not self.appraisal_enabled or self.context is None:
            return
        if not text or len(text) > 160 or not _APPRAISAL_CUES.search(text):
            return
        now = time.time()
        key = (ctx.umo, user_id)
        if now - self._last_appraisal.get(key, 0.0) < 120:
            return
        self._last_appraisal[key] = now
        task = asyncio.create_task(
            self._semantic_appraise(
                ctx.umo,
                user_id,
                text,
                ctx.is_private,
                ctx.persona,
            )
        )
        self._appraisal_tasks.add(task)
        task.add_done_callback(self._appraisal_tasks.discard)

    async def _semantic_appraise(
        self,
        umo: str,
        user_id: str,
        text: str,
        is_private: bool,
        persona: str,
    ) -> None:
        """Ask a small model what the message means, then apply the result.

        Args:
            umo: Conversation origin.
            user_id: Sender identifier.
            text: Incoming message text.
            is_private: Whether this is a private chat.
            persona: Persona prompt, used only for the character name.
        """
        try:
            if self.provider_id:
                provider = self.context.get_provider_by_id(self.provider_id)
            else:
                provider = await self.context.get_using_provider_async(umo)
            if provider is None:
                return
            name = self._persona_name(persona)
            prompt = (
                "场景："
                + ("私聊" if is_private else "群聊")
                + "\n消息："
                + text
                + "\n只输出 JSON。"
            )
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    contexts=[],
                    system_prompt=_APPRAISAL_PROMPT.format(name=name),
                    session_id=umo,
                ),
                timeout=self.appraisal_timeout,
            )
            raw = str(getattr(response, "completion_text", "") or "")
            payload = self._parse_appraisal(raw)
            if not payload:
                return
            self._apply_semantic(umo, user_id, text, payload, is_private)
        except asyncio.TimeoutError:
            logger.debug("[情绪] 语义评估超时")
        except Exception as exc:
            logger.debug(f"[情绪] 语义评估失败: {exc}")

    @staticmethod
    def _parse_appraisal(raw: str) -> dict[str, Any] | None:
        """Extract the appraisal JSON from a model response.

        Args:
            raw: Raw completion text.

        Returns:
            A payload with intent and intensity, or None.
        """
        if not raw:
            return None
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        intent = str(payload.get("intent") or "").strip()
        if intent not in {
            "挑衅",
            "羞辱",
            "命令",
            "越界",
            "玩笑",
            "关心",
            "夸奖",
            "敷衍",
            "普通",
        }:
            return None
        try:
            intensity = max(0.0, min(1.0, float(payload.get("intensity", 0.0))))
        except (TypeError, ValueError):
            intensity = 0.0
        payload["intent"] = intent
        payload["intensity"] = intensity
        payload["cause"] = str(payload.get("cause") or "")[:60]
        return payload

    def _apply_semantic(
        self,
        umo: str,
        user_id: str,
        text: str,
        payload: dict[str, Any],
        is_private: bool,
    ) -> None:
        """Apply a semantic appraisal to the short-term mood.

        Args:
            umo: Conversation origin.
            user_id: Sender identifier.
            text: Original message.
            payload: Parsed appraisal.
            is_private: Whether this is a private chat.
        """
        intent = payload["intent"]
        intensity = payload["intensity"]
        if intensity <= 0.0 or intent == "普通":
            return
        bucket = self.chats.get(umo)
        mood = bucket.get(user_id) if bucket else None
        if mood is None:
            return
        relation = self.ledger.relations.get(umo, {}).get(user_id)
        warmth = relation.warmth if relation is not None else 0.0
        public = not is_private
        if intent in {"挑衅", "羞辱", "越界"}:
            if warmth >= 0.35:
                self._add(mood, sadness=0.18 * intensity, anger=0.10 * intensity)
            else:
                self._add(
                    mood,
                    anger=0.35 * intensity,
                    disgust=0.18 * intensity,
                )
            if public:
                self._add(mood, anger=0.06 * intensity)
        elif intent == "命令":
            self._add(mood, anger=0.22 * intensity, disgust=0.08 * intensity)
        elif intent == "玩笑":
            self._add(mood, joy=0.12 * intensity, anger=0.04 * intensity)
        elif intent == "关心":
            self._add(mood, joy=0.14 * intensity, sadness=-0.08 * intensity)
        elif intent == "夸奖":
            self._add(mood, joy=0.12 * intensity)
        elif intent == "敷衍":
            self._add(mood, sadness=0.12 * intensity, disgust=0.06 * intensity)
        cause = payload.get("cause") or text
        mood.cause = re.sub(r"\s+", " ", str(cause)).strip()[:60]
        mood.updated_at = time.time()
        self._save()
        logger.debug(f"[情绪] 语义评估: {intent} {intensity:.2f}")

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
            strong = [(name, value) for name, value in top if value >= 0.12]
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
        for task in list(self._appraisal_tasks):
            task.cancel()
        if self._appraisal_tasks:
            await asyncio.gather(*self._appraisal_tasks, return_exceptions=True)
        self._save()
        self.ledger.save()
