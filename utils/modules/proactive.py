"""Proactive participation: speaking up without being addressed.

This pack keeps a short rolling view of each group, decides on its own whether
there is anything worth saying, and sends that line when it feels like it.
Silence is the normal outcome; the thresholds exist so it stays rare.
"""

import json
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.all import *

from .base import ModuleManifest, PluginModule

_MAX_BUFFER = 24
_STATE_VERSION = 1

_SPEAK_PROMPT = """下面是群里刚才的几句聊天。没有人叫你，你只是路过听到了。

{context}

现在是 {clock}。

{persona_block}
{interest_block}
{state_block}
这几句里有你在意的、或者想接一句的，就随口接一句。
要是跟你没关系、你也懒得理，就只输出：沉默"""


@dataclass
class ProactiveSettings:
    """Normalised settings for the proactive pack.

    Attributes:
        groups: Group ids this pack may speak in.
        cooldown_minutes: Silence required after one proactive line.
        min_gap_seconds: Minimum gap between two generation attempts.
        daily_cap: Maximum proactive lines per local day.
        quiet_hours: Window such as "01:00-07:00" with no proactive lines.
        min_messages: Context messages required before considering.
        base_probability: Chance to ask the model once gates pass.
        max_chars: Longest acceptable line.
        provider_id: Chat provider, empty for the current default.
    """

    groups: set = field(default_factory=set)
    cooldown_minutes: int = 10
    min_gap_seconds: int = 90
    daily_cap: int = 20
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
        last_spoke: Timestamp of the last proactive line.
        last_try: Timestamp of the last generation attempt.
    """

    lines: deque = field(default_factory=lambda: deque(maxlen=_MAX_BUFFER))
    last_seen: float = 0.0
    last_spoke: float = 0.0
    last_try: float = 0.0


class ProactiveModule(PluginModule):
    """Decides when to speak up in a group without being addressed.

    Args:
        context: AstrBot star context, used for the model call and sending.
        settings: Normalised configuration for this pack.
        persona_provider: Async callable returning the persona prompt.
        interest_provider: Callable returning what the persona cares about.
        state_provider: Callable returning what it recently experienced.
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
        data_dir: "Path | str | None" = None,
    ) -> None:
        super().__init__(context)
        self.settings = settings
        self.persona_provider = persona_provider
        self.interest_provider = interest_provider
        self.state_provider = state_provider
        self.data_dir = Path(data_dir) if data_dir else None
        self.groups: dict[str, _GroupState] = {}
        self._daily_key = ""
        self._daily_count = 0
        self._load_state()

    # ------------------------------------------------------------------
    # Base hooks
    # ------------------------------------------------------------------

    async def on_message(self, event: Any, is_private: bool) -> None:
        """Record a group message and consider speaking up.

        Args:
            event: The AstrBot message event.
            is_private: Whether the message came from a private chat.
        """
        if is_private:
            return
        group = str(event.get_group_id() or "")
        if not group or group not in self.settings.groups:
            return

        state = self.groups.setdefault(group, _GroupState())
        text = (event.get_message_str() or "").strip()
        sender = ""
        try:
            sender = str(event.get_sender_name() or "").strip()
        except Exception:
            sender = ""
        if not text:
            text = "[图片或表情]"
        state.lines.append(f"{sender}：{text}" if sender else text)
        state.last_seen = time.time()

        if not await self._should_consider(state):
            return
        state.last_try = time.time()
        await self._try_speak(event, group, state)

    async def dry_run(self, group: str, extra_context: str = "") -> dict:
        """Generate one proactive line without sending it.

        Args:
            group: Group identifier to imagine speaking in.
            extra_context: Optional fake transcript used when the buffer is empty.

        Returns:
            A payload describing what it wanted to say.
        """
        state = self.groups.get(group)
        lines = list(state.lines) if state is not None else []
        if extra_context:
            lines = [extra_context]
        if not lines:
            return {"ok": False, "reason": "这个群还没有可用的对话记录"}

        persona = ""
        if self.persona_provider is not None:
            try:
                persona = await self.persona_provider("")
            except Exception:
                persona = ""
        interests = self._collect(self.interest_provider, limit=10)
        recent = self._collect(self.state_provider, limit=3)
        prompt = _SPEAK_PROMPT.format(
            context="\n".join(lines),
            clock=datetime.now().strftime("%H:%M"),
            persona_block=("你的人格设定：" + persona[:400]) if persona else "",
            interest_block=(
                "你平时在意这些：\n" + "\n".join("- " + x for x in interests)
                if interests
                else ""
            ),
            state_block=(
                "你最近真实经历过的：\n" + "\n".join("- " + x for x in recent)
                if recent
                else ""
            ),
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
        wanted = bool(reply) and "沉默" not in reply
        return {
            "ok": True,
            "group": group,
            "would_speak": wanted,
            "line": reply if wanted else "",
            "context_lines": len(lines),
        }

    async def close(self) -> None:
        """Persist the pack's own state."""
        self._save_state()

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------

    async def _should_consider(self, state: _GroupState) -> bool:
        """Apply the cheap gates before spending a model call.

        Args:
            state: The group being considered.

        Returns:
            True when it is worth asking the model.
        """
        s = self.settings
        now = time.time()
        if len(state.lines) < s.min_messages:
            return False
        if now - state.last_try < s.min_gap_seconds:
            return False
        if now - state.last_spoke < s.cooldown_minutes * 60:
            return False
        if self._today_count() >= s.daily_cap:
            return False
        return not self._in_quiet_hours()

    def _in_quiet_hours(self) -> bool:
        """Tell whether the current local time is inside the quiet window.

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
        now = datetime.now().time()
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end

    async def _try_speak(self, event: Any, group: str, state: _GroupState) -> None:
        """Ask the model whether it wants to say something, then send it.

        Args:
            event: The message event that triggered the check.
            group: Group identifier.
            state: Rolling state for that group.
        """
        s = self.settings
        if random.random() > s.base_probability:
            return

        persona = ""
        if self.persona_provider is not None:
            try:
                persona = await self.persona_provider(event.unified_msg_origin)
            except Exception as exc:
                logger.debug(f"[主动发言] 人格读取失败: {exc}")
        persona_block = f"你的人格设定：{persona[:400]}" if persona else ""

        interests = self._collect(self.interest_provider, limit=10)
        interest_block = (
            "你平时在意这些：\n" + "\n".join("- " + line for line in interests)
            if interests
            else ""
        )
        recent = self._collect(self.state_provider, limit=3)
        state_block = (
            "你最近真实经历过的：\n" + "\n".join("- " + line for line in recent)
            if recent
            else ""
        )

        prompt = _SPEAK_PROMPT.format(
            context="\n".join(state.lines),
            clock=datetime.now().strftime("%H:%M"),
            persona_block=persona_block,
            interest_block=interest_block,
            state_block=state_block,
        )
        try:
            provider_id = await self._resolve_provider_id()
            if not provider_id:
                return
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                max_tokens=120,
            )
            reply = str(getattr(response, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.warning(f"[主动发言] 生成失败: {exc}")
            return

        reply = reply.strip().strip('"“”')
        if not reply or "沉默" in reply or len(reply) > s.max_chars:
            return

        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain([Plain(reply)]),
            )
        except Exception as exc:
            logger.warning(f"[主动发言] 发送失败: {exc}")
            return

        state.last_spoke = time.time()
        self._bump_daily()
        logger.info(f"[主动发言] 群 {group} 插了一句: {reply}")

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

    async def _resolve_provider_id(self) -> str:
        """Resolve the chat provider for generation.

        Returns:
            A provider id, or an empty string when none is available.
        """
        if self.settings.provider_id:
            return self.settings.provider_id
        try:
            provider = await self.context.get_using_provider_async()
            return str(provider.meta().id) if provider else ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _today_count(self) -> int:
        """Read how many proactive lines were sent today.

        Returns:
            The count for the current local date.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if self._daily_key != today:
            self._daily_key = today
            self._daily_count = 0
        return self._daily_count

    def _bump_daily(self) -> None:
        """Count one proactive line and persist the state."""
        self._daily_count += 1
        self._save_state()

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
            "last_spoke": {
                group: state.last_spoke
                for group, state in self.groups.items()
                if state.last_spoke
            },
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
        for group, ts in (payload.get("last_spoke") or {}).items():
            try:
                self.groups.setdefault(str(group), _GroupState()).last_spoke = float(ts)
            except (TypeError, ValueError):
                continue
