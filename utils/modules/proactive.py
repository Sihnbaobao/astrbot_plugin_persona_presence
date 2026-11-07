"""Proactive participation: proposing a line instead of sending one.

This pack keeps a short rolling view of each group and decides whether it has
anything worth adding. It never owns delivery: :meth:`propose` returns a
:class:`Proposal`, and the base decides whether the line is allowed, filters
it, saves it to history and sends it.
"""

import json
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .base import FeatureContext, ModuleManifest, PluginModule, Proposal

_MAX_BUFFER = 24
_STATE_VERSION = 1

_SPEAK_PROMPT = """现在是 {clock}。群里没人叫你，也不是在回答谁。

{room_block}
{persona_block}
最近群里：
{recent_messages}
{recent_block}
你可以：
- react：接一句很短的吐槽或反应，要贴着上面的内容；
  不汇报自己的生活，不编造没发生的事。
- meme：只发一张表情冒泡，meme_query 写想表达的感觉；可以带一句很短的话。
- silent：没什么可接的就沉默，不要硬找话。

只输出 JSON：
{{"action":"react|meme|silent","text":"最多{max_chars}字","meme_query":"表情要表达的感觉"}}"""


@dataclass
class ProactiveSettings:
    """Normalised settings for the proactive pack.

    Attributes:
        groups: Group ids this pack may speak in.
        all_groups: Whether every group enabled for the plugin is allowed.
        cooldown_minutes: Silence required after one proactive line.
        min_gap_seconds: Minimum gap between two generation attempts.
        daily_cap: Optional hard limit per local day; 0 means no hard cap.
        history_ttl_days: Days to keep the recent-action log; 0 keeps all.
        global_cooldown_minutes: Silence across all groups after any line.
        min_lull_seconds: Minimum quiet time in a group before speaking.
        max_lull_seconds: Maximum quiet time; a dead room is left alone.
        quiet_hours: Window such as "01:00-07:00" with no proactive lines.
        min_messages: Context messages required before considering.
        base_probability: Chance to ask the model once gates pass.
        max_chars: Longest acceptable line.
        provider_id: Chat provider, empty for the current default.
    """

    groups: set = field(default_factory=set)
    all_groups: bool = False
    cooldown_minutes: int = 10
    min_gap_seconds: int = 90
    daily_cap: int = 0
    history_ttl_days: int = 3
    global_cooldown_minutes: int = 30
    min_lull_seconds: int = 90
    max_lull_seconds: int = 900
    quiet_hours: str = "01:00-07:00"
    min_messages: int = 3
    base_probability: float = 0.35
    max_chars: int = 30
    provider_id: str = ""


@dataclass
class _GroupState:
    """Rolling view of one group.

    Attributes:
        lines: Recent chat lines, oldest first.
        last_seen: Timestamp of the last message.
        last_gap: Quiet seconds before the latest message.
        last_spoke: Timestamp of the last proactive line.
        last_try: Timestamp of the last generation attempt.
    """

    lines: deque = field(default_factory=lambda: deque(maxlen=_MAX_BUFFER))
    last_seen: float = 0.0
    last_gap: float = 0.0
    last_spoke: float = 0.0
    last_try: float = 0.0


class ProactiveModule(PluginModule):
    """Decides when to propose a line in a group without being addressed.

    Args:
        context: AstrBot star context, used for the model call.
        settings: Normalised configuration for this pack.
        persona_provider: Optional async fallback returning the persona prompt.
        interest_provider: Callable returning what the persona cares about.
        state_provider: Callable returning what it recently experienced.
        knowledge_provider: Callable returning what it recently learned.
        data_dir: Directory for the pack's own state file.
    """

    manifest = ModuleManifest(
        key="proactive",
        name="主动发言",
        summary="没被叫到也可能插一句，像群里活着的人。",
        config_group="dlc_proactive",
        default_enabled=False,
    )

    def __init__(
        self,
        context: Any = None,
        settings: Any = None,
        persona_provider: Any = None,
        interest_provider: Any = None,
        state_provider: Any = None,
        knowledge_provider: Any = None,
        data_dir: "Path | str | None" = None,
    ) -> None:
        super().__init__(context)
        self.settings = settings
        self.persona_provider = persona_provider
        self.interest_provider = interest_provider
        self.state_provider = state_provider
        self.knowledge_provider = knowledge_provider
        self.data_dir = Path(data_dir) if data_dir else None
        self.groups: dict[str, _GroupState] = {}
        self._history = deque(maxlen=30)
        self._last_spoke_any = 0.0
        self._daily_key = ""
        self._daily_count = 0
        self._load_state()

    # ------------------------------------------------------------------
    # Base hooks
    # ------------------------------------------------------------------

    def _allows_group(self, group: str) -> bool:
        """Tell whether this group is in the pack's scope.

        Args:
            group: Group identifier.

        Returns:
            True when the group is explicitly listed or all enabled groups
            are allowed.
        """
        return bool(group) and (
            bool(getattr(self.settings, "all_groups", False))
            or group in self.settings.groups
        )

    async def on_event(self, ctx: FeatureContext) -> None:
        """Record one group message in the rolling buffer.

        Args:
            ctx: Read-only turn context resolved by the base.
        """
        if ctx.is_private:
            return
        group = str(ctx.group_id or "")
        if not self._allows_group(group):
            return

        state = self.groups.setdefault(group, _GroupState())
        try:
            text = (ctx.event.get_message_str() or "").strip()
        except Exception:
            text = ""
        sender = ""
        try:
            sender = str(ctx.event.get_sender_name() or "").strip()
        except Exception:
            sender = ""
        if not text:
            text = "[图片或表情]"
        state.lines.append(f"{sender}：{text}" if sender else text)
        now = time.time()
        state.last_gap = max(0.0, now - state.last_seen) if state.last_seen else 0.0
        state.last_seen = now

    def can_propose(self, ctx: FeatureContext) -> bool:
        """Tell whether the cheap gates allow a proposal attempt.

        Args:
            ctx: Read-only turn context resolved by the base.

        Returns:
            True when the pack is ready to spend a model call.
        """
        group = str(ctx.group_id or "")
        if ctx.is_private or not self._allows_group(group):
            return False
        state = self.groups.get(group)
        if state is None:
            return False
        return self._should_consider(state, ctx.now)

    async def propose(self, ctx: FeatureContext) -> Proposal | None:
        """Ask the model whether it wants to add one line.

        Args:
            ctx: Read-only turn context resolved by the base.

        Returns:
            A proposal, or None when the pack stays silent.
        """
        group = str(ctx.group_id or "")
        if not self.can_propose(ctx):
            return None

        state = self.groups.get(group)
        if state is None:
            return None

        state.last_try = time.time()
        if random.random() > self.settings.base_probability:
            self._record("skip", group, reason="random")
            return None

        persona = str(ctx.persona or "").strip()
        if not persona and self.persona_provider is not None:
            try:
                persona = await self.persona_provider(ctx.umo)
            except Exception as exc:
                logger.debug(f"[主动发言] 人格读取失败: {exc}")
        persona_block = f"你的人格设定：{persona[:400]}" if persona else ""

        recent_lines = [str(line) for line in list(state.lines)[-8:]]
        recent_messages = "\n".join("- " + line for line in recent_lines) or "（没有）"
        recent_attempts = self._recent_attempts(6)
        recent_block = (
            "你最近主动说过或试过这些，不要重复同一个主题或句式：\n"
            + "\n".join("- " + line for line in recent_attempts)
            if recent_attempts
            else ""
        )

        room_block = self._room_block(state)
        prompt = _SPEAK_PROMPT.format(
            clock=ctx.now.strftime("%H:%M"),
            room_block=room_block,
            persona_block=persona_block,
            recent_messages=recent_messages,
            recent_block=recent_block,
            max_chars=self.settings.max_chars,
        )

        provider_id = self.settings.provider_id or ctx.provider_id
        if not provider_id:
            provider_id = await self._resolve_provider_id(ctx.umo)
        if not provider_id:
            self._record("skip", group, reason="no_provider")
            return None
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                max_tokens=160,
            )
            reply = str(getattr(response, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.warning(f"[主动发言] 生成失败: {exc}")
            self._record("error", group, reason=str(exc)[:120])
            return None

        reply = reply.strip().strip('"“”')
        payload: dict[str, Any] | None = None
        if reply.startswith("{"):
            try:
                parsed = json.loads(reply)
                if isinstance(parsed, dict):
                    payload = parsed
            except (TypeError, ValueError):
                payload = None

        if isinstance(payload, dict):
            action = str(payload.get("action") or "react").strip().lower()
            text = str(payload.get("text") or "").strip()
            meme_query = str(payload.get("meme_query") or "").strip()
        else:
            action = "react"
            text = reply
            meme_query = ""

        if action == "silent" or (not payload and (not reply or "沉默" in reply)):
            self._record("silent", group)
            return None

        if action == "meme":
            if not meme_query:
                self._record("skip", group, reason="meme_query_empty")
                return None
            meme_query = meme_query[:80]
            if text and len(text) > self.settings.max_chars:
                self._record("skip", group, text=text[:120], reason="too_long")
                return None
            if self._too_similar(text or meme_query):
                self._record(
                    "skip", group, text=(text or meme_query)[:120], reason="repeat"
                )
                return None
            self._record("proposed", group, text=text or meme_query)
            logger.info(f"[主动发言] 想冒泡表情: {meme_query[:60]}")
            return Proposal(
                text=text,
                source="proactive",
                action="meme",
                meme_query=meme_query,
                reason="meme",
                group_id=group,
                created_at=time.time(),
            )

        if not text or "沉默" in text:
            self._record("silent", group)
            return None
        if len(text) > self.settings.max_chars:
            self._record("skip", group, text=text[:120], reason="too_long")
            logger.info(
                f"[主动发言] 想接但超过 {self.settings.max_chars} 字: {text[:60]}"
            )
            return None
        if self._too_similar(text):
            self._record("skip", group, text=text[:120], reason="repeat")
            logger.info(f"[主动发言] 想说的和最近重复，跳过: {text[:60]}")
            return None
        self._record("proposed", group, text=text)
        logger.info(f"[主动发言] 想接一句: {text}")
        return Proposal(
            text=text,
            source="proactive",
            action="react",
            reason="react",
            group_id=group,
            created_at=time.time(),
        )

    async def on_proposal_sent(self, ctx: FeatureContext, proposal: Proposal) -> None:
        """Record a line the base actually delivered.

        Args:
            ctx: Read-only turn context resolved by the base.
            proposal: The proposal that was delivered.
        """
        group = str(ctx.group_id or proposal.group_id or "")
        if not group:
            return
        state = self.groups.setdefault(group, _GroupState())
        state.last_spoke = time.time()
        self._last_spoke_any = state.last_spoke
        self._today_count(ctx.now)
        self._daily_count += 1
        sent_text = proposal.text or proposal.meme_query or "[表情包]"
        self._record("sent", group, text=sent_text)
        self._save_state()
        logger.info(f"[主动发言] 群 {group} 插了一句: {sent_text}")

    def note_skip(self, group: str, reason: str) -> None:
        """Record a proposal the base chose not to deliver.

        Args:
            group: Group identifier.
            reason: Short human-readable reason.
        """
        self._record("skip", group, reason=reason)

    async def close(self) -> None:
        """Persist the pack's own state."""
        self._save_state()

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------

    async def dry_run(self, group: str, extra_context: str = "") -> dict:
        """Generate one proactive line without sending it.

        Args:
            group: Group identifier to imagine speaking in.
            extra_context: Optional self-material prompt, not a group transcript.

        Returns:
            A payload describing what it wanted to say.
        """
        state = self.groups.get(group)
        room_block = self._room_block(state) if state else "群里现在没有明显的对话。"

        persona = ""
        if self.persona_provider is not None:
            try:
                persona = await self.persona_provider("")
            except Exception:
                persona = ""
        recent_lines = [str(line) for line in list(state.lines)[-8:]] if state else []
        if extra_context:
            recent_lines.append("（此刻的由头：" + extra_context.strip()[:200] + "）")
        recent_messages = "\n".join("- " + line for line in recent_lines) or "（没有）"
        recent_attempts = self._recent_attempts(6)
        recent_block = (
            "你最近主动说过或试过这些，不要重复同一个主题或句式：\n"
            + "\n".join("- " + line for line in recent_attempts)
            if recent_attempts
            else ""
        )
        prompt = _SPEAK_PROMPT.format(
            clock=datetime.now().strftime("%H:%M"),
            room_block=room_block,
            persona_block=("你的人格设定：" + persona[:400]) if persona else "",
            recent_messages=recent_messages,
            recent_block=recent_block,
            max_chars=self.settings.max_chars,
        )
        try:
            provider_id = await self._resolve_provider_id()
            if not provider_id:
                return {"ok": False, "reason": "没有可用的模型"}
            response = await self.context.llm_generate(
                chat_provider_id=provider_id, prompt=prompt, max_tokens=200
            )
            reply = str(getattr(response, "completion_text", "") or "").strip()
        except Exception as exc:
            return {"ok": False, "reason": f"生成失败: {exc}"}
        clean = reply.strip().strip('"“”')
        silent = not clean or "沉默" in clean
        over_limit = bool(clean) and len(clean) > self.settings.max_chars
        repeat = bool(clean) and not silent and self._too_similar(clean)
        wanted = bool(clean) and not silent and not over_limit and not repeat
        reason = (
            "silent"
            if silent
            else ("too_long" if over_limit else ("repeat" if repeat else ""))
        )
        self._record("dryrun", group, text=clean[:160], reason=reason)
        return {
            "ok": True,
            "group": group,
            "would_speak": wanted,
            "line": clean if clean and not silent else "",
            "over_limit": over_limit,
            "repeat": repeat,
            "max_chars": self.settings.max_chars,
            "buffered": len(state.lines) if state is not None else 0,
        }

    def _recent_sent(self, limit: int = 3) -> list[str]:
        """Read the latest delivered lines to avoid repeating them.

        Args:
            limit: Maximum distinct lines.

        Returns:
            Newest first.
        """
        lines: list[str] = []
        for item in reversed(self._history):
            if item.get("kind") != "sent":
                continue
            text = str(item.get("text") or "").strip()
            if text and text not in lines:
                lines.append(text)
            if len(lines) >= max(1, int(limit)):
                break
        return lines

    def _recent_attempts(self, limit: int = 6) -> list[str]:
        """Read recent proposed and delivered lines for diversity checks.

        Args:
            limit: Maximum distinct non-empty lines.

        Returns:
            Newest first.
        """
        lines: list[str] = []
        for item in reversed(self._history):
            if item.get("kind") not in {"sent", "proposed", "skip"}:
                continue
            text = str(item.get("text") or "").strip()
            if text and text not in lines:
                lines.append(text)
            if len(lines) >= max(1, int(limit)):
                break
        return lines

    @staticmethod
    def _bigrams(text: str) -> set[str]:
        """Tokenise text for a cheap near-duplicate check.

        Args:
            text: Generated or recently sent line.

        Returns:
            Chinese character pairs and lower-cased Latin words.
        """
        raw = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(text or ""))
        tokens: set[str] = set()
        chinese = re.findall(r"[\u4e00-\u9fff]+", raw)
        for chunk in chinese:
            for index in range(len(chunk) - 1):
                tokens.add(chunk[index : index + 2])
        for word in re.findall(r"[A-Za-z0-9]{2,}", raw):
            tokens.add(word.lower())
        return tokens

    def _too_similar(self, text: str) -> bool:
        """Tell whether a line repeats a recent proactive line.

        Args:
            text: Candidate line.

        Returns:
            True when it shares too much wording with a delivered line.
        """
        tokens = self._bigrams(text)
        if len(tokens) < 2:
            return False
        for old in self._recent_attempts(8):
            other = self._bigrams(old)
            if not other:
                continue
            shared = len(tokens & other)
            threshold = max(2, int(min(len(tokens), len(other)) * 0.4))
            if shared >= threshold:
                return True
        return False

    def _prune_history(self, now: float | None = None) -> int:
        """Drop action-log entries older than the retention window.

        Args:
            now: Unix timestamp; defaults to current time.

        Returns:
            Number of removed entries.
        """
        ttl = max(0, int(getattr(self.settings, "history_ttl_days", 0) or 0))
        if ttl <= 0:
            return 0
        cutoff = float(now if now is not None else time.time()) - ttl * 86400
        before = len(self._history)
        self._history = deque(
            [item for item in self._history if float(item.get("at") or 0) >= cutoff],
            maxlen=30,
        )
        return before - len(self._history)

    def _record(self, kind: str, group: str, text: str = "", reason: str = "") -> None:
        """Append one observation to the in-memory activity log.

        Args:
            kind: Event kind, such as skip, silent, proposed or sent.
            group: Group identifier.
            text: Generated or proposed text.
            reason: Short reason for a skip or failure.
        """
        self._history.append(
            {
                "kind": str(kind or ""),
                "group": str(group or ""),
                "text": str(text or "")[:160],
                "reason": str(reason or "")[:160],
                "at": time.time(),
            }
        )

    def _gate_reason(self, group: str, state: _GroupState, now: datetime) -> str:
        """Explain why a group cannot speak right now.

        Args:
            group: Group identifier.
            state: The group state.
            now: Time-zone-aware current time.

        Returns:
            A short reason, or an empty string when eligible.
        """
        s = self.settings
        if not self._allows_group(group):
            return "群不在允许范围"
        if len(state.lines) < s.min_messages:
            return f"消息不足（{len(state.lines)}/{s.min_messages}）"
        now_ts = time.time()
        lull = float(state.last_gap or 0.0) or (now_ts - (state.last_seen or 0.0))
        if s.min_lull_seconds > 0 and lull < s.min_lull_seconds:
            wait = int(s.min_lull_seconds - lull)
            return f"群里还在聊，{wait} 秒后再看"
        if s.max_lull_seconds > 0 and lull > s.max_lull_seconds:
            return "群里太安静，不硬找话说"
        if now_ts - state.last_try < s.min_gap_seconds:
            wait = int(s.min_gap_seconds - (now_ts - state.last_try))
            return f"刚试过，{wait} 秒后可再试"
        if now_ts - state.last_spoke < s.cooldown_minutes * 60:
            wait = int(s.cooldown_minutes * 60 - (now_ts - state.last_spoke))
            return f"本群冷却中，{wait} 秒后可再试"
        if (
            s.global_cooldown_minutes > 0
            and now_ts - self._last_spoke_any < s.global_cooldown_minutes * 60
        ):
            wait = int(s.global_cooldown_minutes * 60 - (now_ts - self._last_spoke_any))
            return f"全局冷却中，{wait} 秒后可再试"
        count = self._today_count(now)
        if s.daily_cap > 0 and count >= s.daily_cap:
            return f"今日已达上限（{count}/{s.daily_cap}）"
        if self._in_quiet_hours(now):
            return "静默时段"
        return ""

    def snapshot(self, now: datetime | None = None) -> dict:
        """Return a page-friendly view of the pack state.

        Args:
            now: Time-zone-aware current time; defaults to local time.

        Returns:
            Group states, counters, settings and recent activity.
        """
        current = now or datetime.now()
        if self._prune_history():
            self._save_state()
        group_ids = set(self.groups)
        if not self.settings.all_groups:
            group_ids.update(self.settings.groups)
        groups = []
        for group in sorted(group_ids):
            state = self.groups.get(group) or _GroupState()
            reason = self._gate_reason(group, state, current)
            groups.append(
                {
                    "group": group,
                    "eligible": not reason,
                    "reason": reason,
                    "buffered": len(state.lines),
                    "last_seen": state.last_seen,
                    "last_gap": round(state.last_gap, 1),
                    "last_spoke": state.last_spoke,
                    "last_try": state.last_try,
                }
            )
        return {
            "today_count": self._today_count(current),
            "daily_cap": self.settings.daily_cap,
            "global_cooldown_minutes": self.settings.global_cooldown_minutes,
            "min_lull_seconds": self.settings.min_lull_seconds,
            "max_lull_seconds": self.settings.max_lull_seconds,
            "last_spoke_any": self._last_spoke_any,
            "quiet_hours": self.settings.quiet_hours,
            "min_messages": self.settings.min_messages,
            "base_probability": self.settings.base_probability,
            "max_chars": self.settings.max_chars,
            "all_groups": self.settings.all_groups,
            "configured_groups": sorted(self.settings.groups),
            "groups": groups,
            "history": list(self._history)[-20:][::-1],
        }

    def _should_consider(self, state: _GroupState, now: datetime) -> bool:
        """Apply the cheap gates before spending a model call.

        Args:
            state: The group being considered.
            now: Time-zone-aware current time resolved by the base.

        Returns:
            True when it is worth asking the model.
        """
        s = self.settings
        now_ts = time.time()
        if len(state.lines) < s.min_messages:
            return False
        lull = float(state.last_gap or 0.0) or (now_ts - (state.last_seen or 0.0))
        if s.min_lull_seconds > 0 and lull < s.min_lull_seconds:
            return False
        if s.max_lull_seconds > 0 and lull > s.max_lull_seconds:
            return False
        if now_ts - state.last_try < s.min_gap_seconds:
            return False
        if now_ts - state.last_spoke < s.cooldown_minutes * 60:
            return False
        if (
            s.global_cooldown_minutes > 0
            and now_ts - self._last_spoke_any < s.global_cooldown_minutes * 60
        ):
            return False
        if s.daily_cap > 0 and self._today_count(now) >= s.daily_cap:
            return False
        return not self._in_quiet_hours(now)

    @staticmethod
    def _room_block(state: _GroupState) -> str:
        """Describe whether the room is active without exposing its content.

        Args:
            state: The group state.

        Returns:
            A one-line timing cue, never the transcript.
        """
        age = max(0.0, time.time() - (state.last_seen or 0.0))
        if age < 120:
            return "群里刚刚还在聊天，但没人叫你。"
        if age < 600:
            return "群里刚安静下来。"
        return "群里已经安静了一阵。"

    def _in_quiet_hours(self, now: datetime | None = None) -> bool:
        """Tell whether the current local time is inside the quiet window.

        Args:
            now: Time-zone-aware current time; defaults to local time.

        Returns:
            True when the pack should stay silent.
        """
        window = str(self.settings.quiet_hours or "").strip()
        if "-" not in window:
            return False
        try:
            start_text, end_text = window.split("-", 1)
            start = datetime.strptime(start_text.strip(), "%H:%M").time()
            end = datetime.strptime(end_text.strip(), "%H:%M").time()
        except ValueError:
            return False
        current = (now or datetime.now()).time()
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    @staticmethod
    def _collect(provider: Any, limit: int) -> list[str]:
        """Read a short list from an optional provider.

        Args:
            provider: Callable returning a list of strings.
            limit: Maximum lines to keep.

        Returns:
            The trimmed list, or an empty list.
        """
        if provider is None:
            return []
        try:
            lines = provider() or []
        except Exception as exc:
            logger.debug(f"[主动发言] 读取素材失败: {exc}")
            return []
        if not isinstance(lines, list):
            return []
        return [str(line) for line in lines[:limit] if str(line).strip()]

    async def _resolve_provider_id(self, umo: str = "") -> str:
        """Resolve the chat provider for generation.

        Args:
            umo: Unified message origin used for session-specific providers.

        Returns:
            A provider id, or an empty string when none is available.
        """
        if self.settings.provider_id:
            return self.settings.provider_id
        try:
            provider = await self.context.get_using_provider_async(umo or None)
            return str(provider.meta().id) if provider else ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _today_count(self, now: datetime | None = None) -> int:
        """Read how many proactive lines were sent today.

        Args:
            now: Time-zone-aware current time; defaults to local time.

        Returns:
            The count for the current local date.
        """
        today = (now or datetime.now()).strftime("%Y-%m-%d")
        if self._daily_key != today:
            self._daily_key = today
            self._daily_count = 0
        return self._daily_count

    def _state_path(self) -> Path | None:
        """Resolve the state file path.

        Returns:
            The path, or None when no directory is configured.
        """
        if self.data_dir is None:
            return None
        return self.data_dir / "proactive_state.json"

    def _save_state(self) -> None:
        """Write counters and cooldowns to disk."""
        path = self._state_path()
        if path is None:
            return
        payload = {
            "version": _STATE_VERSION,
            "daily_key": self._daily_key or datetime.now().strftime("%Y-%m-%d"),
            "daily_count": self._daily_count,
            "last_spoke_any": self._last_spoke_any,
            "last_spoke": {
                group: state.last_spoke
                for group, state in self.groups.items()
                if state.last_spoke
            },
            "history": list(self._history)[-20:],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.debug(f"[主动发言] 状态写入失败: {exc}")

    def _load_state(self) -> None:
        """Restore counters and cooldowns left by a previous run."""
        path = self._state_path()
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        self._daily_key = str(payload.get("daily_key") or "")
        try:
            self._daily_count = int(payload.get("daily_count") or 0)
        except (TypeError, ValueError):
            self._daily_count = 0
        try:
            self._last_spoke_any = float(payload.get("last_spoke_any") or 0.0)
        except (TypeError, ValueError):
            self._last_spoke_any = 0.0
        for group, ts in (payload.get("last_spoke") or {}).items():
            try:
                self.groups.setdefault(str(group), _GroupState()).last_spoke = float(ts)
            except (TypeError, ValueError):
                continue
        for item in payload.get("history") or []:
            if not isinstance(item, dict):
                continue
            try:
                at = float(item.get("at") or 0.0)
            except (TypeError, ValueError):
                continue
            self._history.append(
                {
                    "kind": str(item.get("kind") or ""),
                    "group": str(item.get("group") or ""),
                    "text": str(item.get("text") or "")[:160],
                    "reason": str(item.get("reason") or "")[:160],
                    "at": at,
                }
            )
        self._prune_history()
