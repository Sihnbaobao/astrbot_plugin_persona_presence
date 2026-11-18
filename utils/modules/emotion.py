"""Emotion DLC: a small, decaying emotional state for the persona.

The state is a vector of six base emotions, not one label. Pair and triple
combinations produce a short inner-state note for the reply prompt. Events are
appraised only when the message is actually directed at the persona; ordinary
group chatter does not move the state.
"""

from __future__ import annotations

import asyncio
import hashlib
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
_APPRAISAL_CUES = re.compile(r"你|是不是|装|随便|行|厉害|呵|6|哦|嗯|？|\?|凭什么|少来")
_APPRAISAL_PROMPT = """你是{name}。

{persona_block}{relationship_block}
你正看着手机。有人对你说了一句话。

像这样：
对方：你们 AI 真可爱
你心里：又拿我当玩具。
→ 调侃 0.6

对方：你今天这身好看
你心里：……算你有眼光。
→ 夸奖 0.4

对方：今天好累
你心里：……活该。
→ 普通 0.2

如果这个人平时已经让你不舒服，或者这句话像客套、捧杀，不要因为字面夸奖就开心。

现在，把这一下的反应记成 JSON：
{{"thought":"心里冒出的那句话，不超过20字","intent":"挑衅|羞辱|命令|越界|玩笑|调侃|关心|夸奖|敷衍|普通","intensity":0.0}}
"""


@dataclass
class _Mood:
    """One user's emotional vector in one conversation."""

    base: dict[str, float] = field(default_factory=dict)
    cause: str = ""
    name: str = ""
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
        body_link: bool = True,
        body_dir: "Path | str | None" = None,
    ) -> None:
        super().__init__(context)
        self.data_dir = Path(data_dir) if data_dir else None
        self.path = self.data_dir / "emotion.json" if self.data_dir else None
        self.mood_path = self.data_dir / "mood.json" if self.data_dir else None
        self.sensitivity = max(0.1, min(2.0, float(sensitivity)))
        self.decay_minutes = max(5, int(decay_minutes))
        self.provider_id = str(provider_id or "").strip()
        self.appraisal_timeout = max(3, int(appraisal_timeout))
        self.appraisal_enabled = bool(appraisal_enabled)
        self._appraisal_tasks: set[asyncio.Task] = set()
        self._last_appraisal: dict[tuple[str, str], float] = {}
        self.body_link = bool(body_link)
        self.body_dir = Path(body_dir) if body_dir else None
        self._persona_prompt = ""
        self._name = ""
        self._body_cache: tuple[float, dict[str, Any]] = (0.0, {})
        self._persona_by_umo: dict[str, str] = {}
        self._prompt_by_key: dict[str, str] = {}
        self.chats: dict[str, dict[str, dict[str, _Mood]]] = {}
        self.global_moods: dict[str, _Mood] = {}
        self._last_seen: dict[tuple[str, str], tuple[str, float]] = {}
        self.ledger = RelationLedger(
            self.data_dir / "relations.json" if self.data_dir else None,
            sensitivity=self.sensitivity,
        )
        self._load()
        self._backfill_names()

    def _backfill_names(self) -> None:
        """Fill missing display names from the local image archive.

        Older emotion records only stored user ids. The image archive already
        keeps sender names from past messages, so it can repair those records
        without another platform lookup.
        """
        if self.data_dir is None:
            return
        archive = self.data_dir / "image_archive.json"
        if not archive.is_file():
            return
        try:
            raw = json.loads(archive.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        names: dict[str, str] = {}
        for entry in (raw.get("entries") or []) if isinstance(raw, dict) else []:
            if not isinstance(entry, dict):
                continue
            user_id = str(entry.get("sender_id") or "")
            name = str(entry.get("sender_name") or "").strip()
            if user_id and name:
                names[user_id] = name[:40]
        if not names:
            return
        changed = False
        for users in self.chats.values():
            for user_id, mood in users.items():
                if not mood.name and user_id in names:
                    mood.name = names[user_id]
                    changed = True
        for users in self.ledger.relations.values():
            for user_id, relation in users.items():
                if not relation.name and user_id in names:
                    relation.name = names[user_id]
                    changed = True
        if changed:
            self._save()
            self.ledger.save()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for umo, section in (raw.get("chats") or {}).items():
            if not isinstance(section, dict):
                continue
            # Old files stored {user_id: mood}; wrap them under one key so a
            # persona switch does not lose the mood that was already there.
            if any(
                isinstance(item, dict) and "base" in item for item in section.values()
            ):
                section = {"legacy": section}
            bucket = self.chats.setdefault(str(umo), {})
            for persona_key, users in section.items():
                if not isinstance(users, dict):
                    continue
                target = bucket.setdefault(str(persona_key), {})
                for user_id, item in users.items():
                    if not isinstance(item, dict):
                        continue
                    target[str(user_id)] = self._mood_from_item(item)
        for persona_key, item in (raw.get("global") or {}).items():
            if isinstance(item, dict):
                self.global_moods[str(persona_key)] = self._mood_from_item(item)
        prompts = raw.get("prompts") or {}
        if isinstance(prompts, dict):
            self._prompt_by_key = {
                str(key): str(value)[:600]
                for key, value in prompts.items()
                if isinstance(value, str)
            }

    @staticmethod
    def _mood_from_item(item: dict[str, Any]) -> _Mood:
        """Rebuild one stored mood from JSON."""
        base = item.get("base") or {}
        return _Mood(
            base={
                emotion: max(0.0, min(1.0, float(base.get(emotion, 0.0))))
                for emotion in _BASE_EMOTIONS
            },
            cause=str(item.get("cause") or "")[:120],
            name=str(item.get("name") or "")[:40],
            updated_at=float(item.get("updated_at") or 0.0),
        )

    @staticmethod
    def _mood_to_item(mood: _Mood) -> dict[str, Any]:
        """Serialise one mood to the JSON shape used on disk."""
        return {
            "base": mood.base,
            "cause": mood.cause,
            "name": mood.name,
            "updated_at": mood.updated_at,
        }

    def _save(self) -> None:
        if self.path is None:
            return
        payload: dict[str, Any] = {
            "version": _STATE_VERSION,
            "chats": {},
            "global": {},
            "prompts": {},
        }
        for umo, personas in self.chats.items():
            saved: dict[str, Any] = {}
            for persona_key, users in personas.items():
                rows = {
                    user_id: self._mood_to_item(mood)
                    for user_id, mood in users.items()
                    if any(value >= 0.01 for value in mood.base.values())
                }
                if rows:
                    saved[persona_key] = rows
            if saved:
                payload["chats"][umo] = saved
        payload["global"] = {
            key: self._mood_to_item(mood)
            for key, mood in self.global_moods.items()
            if any(value >= 0.01 for value in mood.base.values())
        }
        payload["prompts"] = dict(self._prompt_by_key)
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
    def _sender_name(event: Any) -> str:
        """Read a sender display name without trusting platform shape.

        Args:
            event: AstrBot message event.

        Returns:
            A short name, or an empty string.
        """
        try:
            return str(event.get_sender_name() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _group_name(event: Any) -> str:
        """Read a group display name without trusting platform shape.

        Args:
            event: AstrBot message event.

        Returns:
            A short name, or an empty string.
        """
        try:
            return str(event.get_group_name() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _text(event: Any) -> str:
        try:
            return str(event.get_message_str() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _persona_key(persona: str) -> str:
        """Return the stable key of the persona a turn belongs to."""
        text = (persona or "").strip()
        if not text:
            return "default"
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _persona_name(persona: str) -> str:
        match = re.search(r"你的名字是([^\s，。,.]+)", persona or "")
        return match.group(1).strip() if match else ""

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

    def _decay(self, mood: _Mood, now: float, scale: float = 1.0) -> None:
        if mood.updated_at <= 0:
            mood.updated_at = now
            return
        minutes = max(0.0, (now - mood.updated_at) / 60.0)
        for emotion in _BASE_EMOTIONS:
            half_life = _HALF_LIFE_MINUTES.get(emotion, self.decay_minutes)
            mood.base[emotion] *= 0.5 ** (minutes / (half_life * scale))
        mood.updated_at = now

    def _add(self, mood: _Mood, **deltas: float) -> None:
        for emotion, delta in deltas.items():
            if emotion not in mood.base:
                continue
            value = mood.base[emotion] + float(delta) * self.sensitivity
            mood.base[emotion] = max(0.0, min(1.0, value))

    def _apply_global(
        self,
        persona_key: str,
        mood: _Mood,
        before: dict[str, float],
        text: str,
    ) -> None:
        """Fold one local event into the persona-wide background mood.

        Args:
            persona_key: Key of the persona this turn belongs to.
            mood: Local mood after appraisal.
            before: Local mood before appraisal.
            text: Message text, used when no appraised cause exists.
        """
        now = time.time()
        global_mood = self.global_moods.setdefault(persona_key, _Mood())
        self._decay(global_mood, now, scale=3.0)
        for emotion in _BASE_EMOTIONS:
            delta = mood.base.get(emotion, 0.0) - before.get(emotion, 0.0)
            if delta:
                global_mood.base[emotion] = max(
                    0.0, min(1.0, global_mood.base[emotion] + delta * 0.5)
                )
        if mood.cause:
            global_mood.cause = mood.cause
        elif text:
            global_mood.cause = text[:60]
        global_mood.updated_at = now
        self._publish_mood()

    def _publish_mood(self) -> None:
        """Publish the persona-wide mood for the body plugin to read.

        The body layer consumes this file in the opposite direction, so a bad
        week can suppress appetite or stretch the follicular phase. Writing it
        here keeps the contract one file per direction.
        """
        if self.mood_path is None:
            return
        personas: dict[str, Any] = {}
        now = time.time()
        for key, mood in self.global_moods.items():
            self._decay(mood, now, scale=3.0)
            strong = self._effective(mood, {})
            if not strong:
                continue
            name = self._persona_name(self._prompt_by_key.get(key, "")) or key
            personas[name] = {
                "persona_key": key,
                "name": name,
                "base": {
                    emotion: round(mood.base[emotion], 3) for emotion in _BASE_EMOTIONS
                },
                "label": self._label(strong),
                "cause": mood.cause,
                "updated_at": mood.updated_at,
            }
        payload = {"version": 1, "personas": personas}
        try:
            self.mood_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.mood_path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temp.replace(self.mood_path)
        except OSError as exc:
            logger.debug(f"[情绪] 心情文件写入失败: {exc}")

    def _praise_match(self, text: str) -> bool:
        """Tell whether the text is a compliment aimed at this persona.

        Args:
            text: Incoming message text.

        Returns:
            True when the compliment has a plausible second-person target.
        """
        name = re.escape(self._name) if self._name else "你"
        return bool(
            re.search(
                rf"(?:你|{name}).{{0,6}}(?:可爱|好看|漂亮|厉害|聪明|温柔|有趣|牛|强)"
                rf"|(?<!不)(?:喜欢|爱)(?:你|{name})|谢谢(?:你|{name})?",
                text,
            )
        )

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
        praise = self._praise_match(text)
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
            relation = self.ledger.relations.get(umo, {}).get(user_id)
            resentment = relation.resentment if relation is not None else 0.0
            bottomed = bool(relation and relation.bottomed_out)
            warmth = relation.warmth if relation is not None else 0.0
            if bottomed or resentment >= 0.55:
                # A compliment from someone who keeps making her uncomfortable
                # reads as coaxing or mockery, not affection.
                self._add(mood, joy=-0.04, anger=0.06, disgust=0.08)
            elif resentment >= 0.30:
                self._add(mood, joy=0.06, disgust=0.03)
            elif warmth >= 0.35:
                self._add(mood, joy=0.24, anger=-0.05, sadness=-0.05)
            else:
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
            dominant = max(mood.base, key=mood.base.get)
            cause_is_relevant = (
                (bool(severe) and dominant in {"anger", "disgust"})
                or (bool(conflict) and dominant in {"anger", "disgust", "fear"})
                or (bool(praise) and dominant == "joy")
                or (bool(apology) and dominant in {"anger", "sadness"})
                or (bool(dismiss) and dominant in {"sadness", "disgust"})
                or (bool(surprise) and dominant == "surprise")
            )
            if cause_is_relevant and cause:
                mood.cause = re.sub(r"\s+", " ", cause).strip()[:60]
            mood.updated_at = now
        return changed

    async def on_event(self, ctx: FeatureContext) -> None:
        if ctx.persona:
            self._persona_prompt = ctx.persona
        if not self._directed(ctx.event, ctx.is_private, ctx.persona):
            return
        user_id = self._sender_id(ctx.event) or "unknown"
        sender_name = self._sender_name(ctx.event)
        group_name = self._group_name(ctx.event)
        persona_key = self._persona_key(ctx.persona)
        self._persona_by_umo[ctx.umo] = persona_key
        if ctx.persona:
            self._prompt_by_key[persona_key] = ctx.persona[:600]
            self._name = self._persona_name(ctx.persona)
        bucket = self.chats.setdefault(ctx.umo, {}).setdefault(persona_key, {})
        mood = bucket.setdefault(user_id, _Mood())
        if sender_name:
            mood.name = sender_name[:40]
        text = self._text(ctx.event)
        before = dict(mood.base)
        locally_changed = self._appraise(text, mood, ctx.umo, user_id)
        if locally_changed:
            self._apply_global(persona_key, mood, before, text)
            self._save()
        if self.ledger.appraise(
            ctx.umo,
            user_id,
            text,
            is_private=ctx.is_private,
            group_id=ctx.group_id,
            sender_name=sender_name,
            group_name=group_name,
        ):
            self.ledger.save()
        if not locally_changed or (not ctx.is_private and self._praise_match(text)):
            self._schedule_semantic(ctx, text, user_id)

    def _schedule_semantic(self, ctx: FeatureContext, text: str, user_id: str) -> None:
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
            persona: Persona prompt used to appraise the event in character.
        """
        try:
            if self.provider_id:
                provider = self.context.get_provider_by_id(self.provider_id)
            else:
                provider = await self.context.get_using_provider_async(umo)
            if provider is None:
                return
            name = self._persona_name(persona)
            relation = self.ledger.relations.get(umo, {}).get(user_id)
            relation_text = ""
            if relation is not None:
                relation_text = (
                    f"亲近 {relation.warmth:.2f}，信任 {relation.trust:.2f}，"
                    f"不满 {relation.resentment:.2f}"
                    + ("，已经触及底线" if relation.bottomed_out else "")
                )
            persona_block = (
                "\n你平时：\n" + persona[:1200].strip() + "\n"
                if persona.strip()
                else "\n"
            )
            relationship_block = (
                "\n你跟这个人：" + relation_text + "\n" if relation_text else "\n"
            )
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
                    system_prompt=_APPRAISAL_PROMPT.format(
                        name=name,
                        persona_block=persona_block,
                        relationship_block=relationship_block,
                    ),
                    session_id=umo,
                ),
                timeout=self.appraisal_timeout,
            )
            raw = str(getattr(response, "completion_text", "") or "")
            payload = self._parse_appraisal(raw)
            if not payload:
                return
            self._apply_semantic(
                umo,
                user_id,
                text,
                payload,
                is_private,
                self._persona_key(persona),
            )
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
            "调侃",
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
        payload["thought"] = str(payload.get("thought") or "")[:60]
        return payload

    def _apply_semantic(
        self,
        umo: str,
        user_id: str,
        text: str,
        payload: dict[str, Any],
        is_private: bool,
        persona_key: str,
    ) -> None:
        """Apply a semantic appraisal to the short-term mood.

        Args:
            umo: Conversation origin.
            user_id: Sender identifier.
            text: Original message.
            payload: Parsed appraisal.
            is_private: Whether this is a private chat.
            persona_key: Persona the mood belongs to.
        """
        intent = payload["intent"]
        intensity = payload["intensity"]
        if intensity <= 0.0 or intent == "普通":
            return
        personas = self.chats.get(umo, {})
        bucket = personas.get(persona_key) or personas.get("legacy", {})
        mood = bucket.get(user_id)
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
        elif intent == "调侃":
            # A compliment that is really teasing an AI is not affection.
            self._add(mood, joy=-0.20 * intensity, anger=0.08 * intensity)
        elif intent == "关心":
            self._add(mood, joy=0.14 * intensity, sadness=-0.08 * intensity)
        elif intent == "夸奖":
            relation = self.ledger.relations.get(umo, {}).get(user_id)
            resentment = relation.resentment if relation is not None else 0.0
            bottomed = bool(relation and relation.bottomed_out)
            warmth = relation.warmth if relation is not None else 0.0
            if bottomed or resentment >= 0.55:
                self._add(
                    mood,
                    joy=-0.06 * intensity,
                    anger=0.05 * intensity,
                    disgust=0.06 * intensity,
                )
            elif resentment >= 0.30:
                self._add(mood, joy=0.04 * intensity)
            elif warmth >= 0.35:
                self._add(mood, joy=0.18 * intensity)
            else:
                self._add(mood, joy=0.12 * intensity)
        elif intent == "敷衍":
            self._add(mood, sadness=0.12 * intensity, disgust=0.06 * intensity)
        cause = payload.get("thought") or payload.get("cause") or text
        mood.cause = re.sub(r"\s+", " ", str(cause)).strip()[:60]
        mood.updated_at = time.time()
        self._save()
        logger.debug(f"[情绪] 语义评估: {intent} {intensity:.2f}")

    def snapshot(self) -> dict[str, Any]:
        """Return a read-only view of current moods, relations and groups.

        Returns:
            A JSON-serialisable snapshot for the control page.
        """
        now = time.time()
        moods: list[dict[str, Any]] = []
        for umo, personas in self.chats.items():
            for persona_key, users in personas.items():
                for user_id, mood in users.items():
                    self._decay(mood, now)
                    strong = self._effective(mood, {})
                    if not strong:
                        continue
                    moods.append(
                        {
                            "umo": umo,
                            "persona": self._prompt_by_key.get(persona_key, "")[:40]
                            or persona_key,
                            "user_id": user_id,
                            "name": mood.name,
                            "base": {
                                name: round(mood.base.get(name, 0.0), 3)
                                for name in _BASE_EMOTIONS
                            },
                            "label": self._label(strong),
                            "cause": mood.cause,
                            "intensity": round(strong[0][1], 3),
                            "updated_at": mood.updated_at,
                        }
                    )
        moods.sort(key=lambda item: item["intensity"], reverse=True)
        relations: list[dict[str, Any]] = []
        for umo, users in self.ledger.relations.items():
            for user_id, rel in users.items():
                relations.append(
                    {
                        "umo": umo,
                        "user_id": user_id,
                        "name": rel.name,
                        "warmth": round(rel.warmth, 3),
                        "trust": round(rel.trust, 3),
                        "resentment": round(rel.resentment, 3),
                        "bottomed_out": rel.bottomed_out,
                        "cause": rel.cause,
                        "negative_events": rel.negative_events,
                        "positive_events": rel.positive_events,
                        "updated_at": rel.updated_at,
                    }
                )
        relations.sort(
            key=lambda item: (item["bottomed_out"], item["resentment"]),
            reverse=True,
        )
        groups: list[dict[str, Any]] = []
        for group_id, group in self.ledger.groups.items():
            groups.append(
                {
                    "group_id": group_id,
                    "name": group.name,
                    "comfort": round(group.comfort, 3),
                    "withdrawal": round(group.withdrawal, 3),
                    "cause": group.cause,
                    "negative_events": group.negative_events,
                    "updated_at": group.updated_at,
                }
            )
        groups.sort(key=lambda item: item["withdrawal"], reverse=True)
        global_rows: list[dict[str, Any]] = []
        for persona_key, mood in self.global_moods.items():
            self._decay(mood, now, scale=3.0)
            strong = self._effective(mood, {})
            if not strong:
                continue
            global_rows.append(
                {
                    "persona": self._prompt_by_key.get(persona_key, "")[:40]
                    or persona_key,
                    "base": {
                        name: round(mood.base.get(name, 0.0), 3)
                        for name in _BASE_EMOTIONS
                    },
                    "label": self._label(strong),
                    "cause": mood.cause,
                    "intensity": round(strong[0][1], 3),
                    "updated_at": mood.updated_at,
                }
            )
        global_rows.sort(key=lambda item: item["intensity"], reverse=True)
        return {
            "moods": moods,
            "global_moods": global_rows,
            "relations": relations,
            "groups": groups,
            "counts": {
                "moods": len(moods),
                "global_moods": len(global_rows),
                "relations": len(relations),
                "groups": len(groups),
            },
        }

    def _body_path(self) -> "Path | None":
        """Return the body plugin's vitals file, or None when unavailable."""
        if self.body_dir is None:
            try:
                from astrbot.core.utils.astrbot_path import (
                    get_astrbot_plugin_data_path,
                )

                self.body_dir = (
                    Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_body_needs"
                )
            except Exception:
                self.body_link = False
                return None
        return self.body_dir / "vitals.json"

    def _body_vitals(self, persona_prompt: str) -> dict[str, Any]:
        """Read the body snapshot that belongs to this persona.

        The body plugin keys its file by persona id, while this module only has
        the persona prompt, so the id is matched as a substring of the prompt.
        A single recorded persona needs no match at all.

        Args:
            persona_prompt: Persona prompt for the current turn.

        Returns:
            The matching snapshot, or an empty dict.
        """
        if not self.body_link:
            return {}
        path = self._body_path()
        if path is None or not path.is_file():
            return {}
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        cached_at, cached = self._body_cache
        if cached_at != mtime:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            personas = payload.get("personas") if isinstance(payload, dict) else None
            cached = personas if isinstance(personas, dict) else {}
            self._body_cache = (mtime, cached)
        if not cached:
            return {}
        if persona_prompt:
            matches = [key for key in cached if key and key in persona_prompt]
            if matches:
                return cached[max(matches, key=len)]
        if len(cached) == 1:
            return next(iter(cached.values()))
        return {}

    def _body_bias(self, persona_prompt: str) -> tuple[dict[str, float], str]:
        """Map the body snapshot onto the six base emotions.

        The body never writes these emotions directly: it supplies tension,
        distress and valence, and the mapping below decides how much anger,
        sadness, joy, fear and disgust that body state adds on top of the
        event-driven mood.

        Args:
            persona_prompt: Persona prompt for the current turn.

        Returns:
            A per-emotion bias and the body line, both possibly empty.
        """
        vitals = self._body_vitals(persona_prompt)
        if not vitals:
            return {}, ""
        try:
            tension = max(0.0, min(1.0, float(vitals.get("tension") or 0.0)))
            distress = max(0.0, min(1.0, float(vitals.get("distress") or 0.0)))
            valence = max(-1.0, min(1.0, float(vitals.get("valence") or 0.0)))
            dirt = max(0.0, min(1.0, float(vitals.get("dirt") or 0.0)))
        except (TypeError, ValueError):
            return {}, ""
        bias = {
            "joy": -0.30 * distress,
            "anger": 0.34 * tension,
            "sadness": 0.32 * max(0.0, -valence) + 0.12 * distress,
            "fear": 0.10 * tension,
            "disgust": 0.24 * dirt,
            "surprise": 0.0,
        }
        return bias, str(vitals.get("line") or "").strip()[:80]

    @staticmethod
    def _effective(
        mood: "_Mood | None", bias: dict[str, float]
    ) -> list[tuple[str, float]]:
        """Blend a mood with the body bias and return the strong emotions."""
        base = mood.base if mood is not None else {}
        values = {
            name: max(0.0, min(1.0, base.get(name, 0.0) + bias.get(name, 0.0)))
            for name in _BASE_EMOTIONS
        }
        top = sorted(values.items(), key=lambda item: item[1], reverse=True)
        return [(name, value) for name, value in top if value >= 0.12]

    def block_for(self, event: Any, is_private: bool) -> str:
        try:
            umo = str(event.unified_msg_origin)
        except Exception:
            return ""
        persona_key = self._persona_by_umo.get(umo, "default")
        personas = self.chats.get(umo, {})
        bucket = personas.get(persona_key) or personas.get("legacy", {})
        user_id = self._sender_id(event) or "unknown"
        mood = bucket.get(user_id)
        prompt = self._prompt_by_key.get(persona_key) or self._persona_prompt
        body_bias, body_line = self._body_bias(prompt)
        blocks: list[str] = []
        if body_line:
            blocks.append(f"[身体]\n{body_line}")
        if mood is not None:
            self._decay(mood, time.time())
        strong = self._effective(mood, body_bias)
        if strong:
            label = self._label(strong)
            cause = (mood.cause.strip() if mood is not None else "") or body_line
            if label:
                blocks.append(
                    f"[此刻]\n{label}——{cause}" if cause else f"[此刻]\n{label}"
                )
        else:
            # No event is driving this user right now; fall back to the persona's
            # slower background mood so she still has a mood between messages.
            global_mood = self.global_moods.get(persona_key)
            if global_mood is not None:
                self._decay(global_mood, time.time(), scale=3.0)
                strong_global = self._effective(global_mood, body_bias)
                if strong_global:
                    label = self._label(strong_global)
                    cause = global_mood.cause.strip() or body_line
                    if label:
                        blocks.append(
                            f"[心情]\n{label}——{cause}" if cause else f"[心情]\n{label}"
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
        self._publish_mood()
        for task in list(self._appraisal_tasks):
            task.cancel()
        if self._appraisal_tasks:
            await asyncio.gather(*self._appraisal_tasks, return_exceptions=True)
        self._save()
        self.ledger.save()
