"""Cross-group sight for the persona plugin.

Lets the persona recall what it recently saw in its other groups without
merging those conversations, so a message in group B can reference group A
the way a person who reads several groups would.

The module only reads AstrBot core state (message_history_manager and
db.get_umo_aliases) and reuses MemoryInjector.get_memories for the optional
long-term memory fan-out. No third-party plugin internals are touched and no
memory scope configuration is changed, so an upstream update of the memory
plugin can only degrade recall, never the sight layer.
"""

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from astrbot.api.all import *

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

# Per-message text budget inside the injected block.
_MAX_TEXT_CHARS = 80
# Per-memory text budget for the optional long-term recall.
_MAX_MEMORY_CHARS = 400

# Text that openly points at another group; a hit is enough to recall it.
_GENERIC_REFERENCE_MARKERS = (
    "另一个群",
    "別的群",
    "别的群",
    "其他群",
    "其它群",
    "隔壁群",
    "其他群里",
    "别的群里",
    "那个群",
    "那群",
)

# Text that refers back to something already said. Recalls only when the
# sentence also names someone who spoke in another group, which keeps the
# trigger precise instead of matching every "那个".
_ANAPHOR_MARKERS = (
    "刚才",
    "刚刚",
    "刚说",
    "之前说",
    "昨天说",
    "上次",
    "提到过",
    "说过的",
    "那个事",
    "那件事",
)

_EMPTY_MEMORY_MARKERS = ("当前没有任何记忆", "没有找到相关记忆")

# Fragments too common in group names to prove the message targets that group.
_GENERIC_NAME_FRAGMENTS = {
    "插件",
    "测试",
    "交流",
    "基地",
    "聊天",
    "群聊",
    "动漫",
    "音乐",
    "游戏",
    "乐园",
}


@dataclass
class GroupSighting:
    """Recent messages the persona saw in one other group.

    Attributes:
        umo: Unified message origin of that group.
        name: Human readable group name.
        messages: Chronological sighting records.
    """

    umo: str
    name: str
    messages: list[dict[str, Any]] = field(default_factory=list)


class CrossGroupSight:
    """Keeps a bounded, per-group view of what the persona recently read.

    Args:
        context: AstrBot star context used for core history and alias lookups.
        enabled: Whether the sight layer is active at all.
        enabled_groups: Group whitelist inherited from the plugin config.
        buffer_size: Maximum retained messages per group.
        window_seconds: How far back a sighting stays usable.
        max_groups: Maximum groups quoted in a single injection.
        max_per_group: Maximum messages quoted per group.
        always_on: Keep a short baseline digest even without a reference, so
            awareness does not depend on naming a group first.
        baseline_per_group: Messages quoted per group in that baseline.
        denylist: Groups whose content must never leave them.
        store_path: Optional JSON file keeping sightings across restarts.
    """

    def __init__(
        self,
        context: "Context",
        *,
        enabled: bool = False,
        enabled_groups: list[str] | None = None,
        buffer_size: int = 200,
        window_seconds: int = 86_400,
        max_groups: int = 3,
        max_per_group: int = 5,
        always_on: bool = True,
        baseline_per_group: int = 2,
        denylist: list[str] | None = None,
        store_path: "Path | str | None" = None,
    ) -> None:
        self.context = context
        self.enabled = enabled
        self.enabled_groups = [str(item) for item in (enabled_groups or [])]
        self.buffer_size = max(10, int(buffer_size))
        self.window_seconds = max(60, int(window_seconds))
        self.max_groups = max(1, int(max_groups))
        self.max_per_group = max(1, int(max_per_group))
        self.always_on = bool(always_on)
        self.baseline_per_group = max(1, int(baseline_per_group))
        self.store_path = Path(store_path) if store_path else None
        self._last_flush = 0.0
        self.denylist = {
            str(item).strip() for item in (denylist or []) if str(item).strip()
        }

        self._buffers: dict[str, deque[dict[str, Any]]] = {}
        self._names: dict[str, str] = {}
        self._backfilled: set[str] = set()
        self._load_store()

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    async def observe(self, event: AstrMessageEvent) -> None:
        """Record one group message for later cross-group recall.

        Args:
            event: Incoming group message event.
        """
        if not self.enabled:
            return
        try:
            if event.is_private_chat():
                return
            umo = event.unified_msg_origin
            group_id = self._group_id(umo)
            if self.enabled_groups and group_id not in self.enabled_groups:
                return

            text = (event.get_message_str() or "").strip()
            if not text:
                return

            buffer = self._buffer(umo)
            if not buffer and umo not in self._backfilled:
                await self._backfill(umo, group_id)

            buffer.append(
                {
                    "ts": self._event_timestamp(event),
                    "sender": str(
                        event.get_sender_name() or event.get_sender_id() or ""
                    ),
                    "text": text[:_MAX_TEXT_CHARS],
                }
            )
            self._maybe_flush()
        except Exception as exc:
            logger.debug(f"[CrossGroup] failed to record observation: {exc}")

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------

    async def collect(self, event: AstrMessageEvent) -> list[GroupSighting]:
        """Select the other groups worth quoting for the current message.

        Args:
            event: Current group message event.

        Returns:
            The groups to quote, most relevant first.
        """
        if not self.enabled or event.is_private_chat():
            return []

        try:
            current_umo = event.unified_msg_origin
            text = (event.get_message_str() or "").strip()
            if not text:
                return []

            # Quiet groups never fire observe(), so seed them from history the
            # first time they matter instead of waiting for a new message.
            await self._ensure_all_backfilled(current_umo)

            now = time.time()
            recent_by_group: dict[str, list[dict[str, Any]]] = {}
            for umo, buffer in self._buffers.items():
                if umo == current_umo or self._is_denied(umo):
                    continue
                recent = [
                    item for item in buffer if now - item["ts"] <= self.window_seconds
                ]
                if recent:
                    recent_by_group[umo] = recent
            if not recent_by_group:
                return []

            await self._ensure_names(tuple(recent_by_group))

            # A referenced group is quoted in full; everything else keeps a
            # short baseline so awareness never depends on naming a group.
            focused = self._pick_groups(text, recent_by_group)
            selected = list(focused)
            if self.always_on:
                for umo in sorted(
                    recent_by_group,
                    key=lambda item: recent_by_group[item][-1]["ts"],
                    reverse=True,
                ):
                    if len(selected) >= self.max_groups:
                        break
                    if umo not in selected:
                        selected.append(umo)
            if not selected:
                return []
            return self._build_sightings(selected, recent_by_group, set(focused))
        except Exception as exc:
            logger.debug(f"[CrossGroup] failed to collect sightings: {exc}")
            return []

    def render(self, sightings: list[GroupSighting]) -> str:
        """Format sightings for prompt injection.

        Args:
            sightings: Groups returned by collect().

        Returns:
            The injection block, or an empty string when there is nothing to say.
        """
        if not sightings:
            return ""

        lines = ["[背景见闻 · 你在其他群看到的，不属于本群]"]
        for sighting in sightings:
            for message in sighting.messages:
                sender = message.get("sender") or "某人"
                # Keep every sighting on one line so the digest stays scannable.
                text = " ".join(str(message.get("text") or "").split())
                if not text:
                    continue
                lines.append(
                    f"· {sighting.name} {self._clock(message['ts'])} {sender}：{text}"
                )
        lines.append("")
        lines.append(
            "[以上是你在别的群看到的，不是本群发生的事。被问到时可以直接说出"
            "这里的具体内容，说成「我在另一个群看到…」；没列在这儿的就别编，"
            "直接说那边没看到；没人问起就别主动提。]"
        )
        return "\n".join(lines)

    async def recall_memories(
        self,
        event: AstrMessageEvent,
        sightings: list[GroupSighting],
        *,
        mode: str,
        version: str = "v1",
        top_k: int = 5,
        persona_compat_mode: str = "auto",
    ) -> str:
        """Recall long-term memories from the related groups as extra background.

        The calls reuse the plugin's existing memory adapter and simply query
        other session ids, so no memory scope configuration is required.

        Args:
            event: Current group message event.
            sightings: Groups selected for the current message.
            mode: Resolved memory mode ("livingmemory" or "legacy").
            version: LivingMemory architecture version.
            top_k: Memories requested per group.
            persona_compat_mode: Persona id compatibility mode.

        Returns:
            A formatted memory block, or an empty string.
        """
        if not self.enabled or not sightings or not mode:
            return ""

        query = (event.get_message_str() or "").strip()
        if not query:
            return ""

        from .memory_injector import MemoryInjector

        chunks: list[str] = []
        seen: set[str] = set()
        for sighting in sightings:
            mock_event = SimpleNamespace(
                unified_msg_origin=sighting.umo,
                message_str=query,
            )
            try:
                text = await MemoryInjector.get_memories(
                    self.context,
                    mock_event,
                    mode=mode,
                    top_k=top_k,
                    version=version,
                    persona_compat_mode=persona_compat_mode,
                )
            except Exception as exc:
                logger.debug(
                    f"[CrossGroup] memory recall failed for {sighting.umo}: {exc}"
                )
                continue

            text = (text or "").strip()
            if not text or any(marker in text for marker in _EMPTY_MEMORY_MARKERS):
                continue
            if text in seen:
                continue
            seen.add(text)
            chunks.append(f"· （{sighting.name}）{text[:_MAX_MEMORY_CHARS]}")

        if not chunks:
            return ""
        return "\n".join(["[其他群的相关记忆]", *chunks])

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _pick_groups(
        self,
        text: str,
        recent_by_group: dict[str, list[dict[str, Any]]],
    ) -> list[str]:
        """Choose which other groups the current message is talking about.

        Args:
            text: Current message text.
            recent_by_group: Groups with usable recent messages.

        Returns:
            Selected session ids, most relevant first.
        """
        selected: list[str] = []

        # 1. The message names another group, abbreviations included such as
        # "蓝原群" for "蓝色星原奇波养殖基地".
        for umo in recent_by_group:
            name = self._names.get(umo) or ""
            if self._refers_to_group(text, name):
                selected.append(umo)

        # 2. The message points at "another group" without naming it.
        if not selected and any(
            marker in text for marker in _GENERIC_REFERENCE_MARKERS
        ):
            selected = sorted(
                recent_by_group,
                key=lambda umo: recent_by_group[umo][-1]["ts"],
                reverse=True,
            )

        # 3. The message refers back and names someone seen in another group.
        if not selected and any(marker in text for marker in _ANAPHOR_MARKERS):
            for umo, recent in recent_by_group.items():
                speakers = {item.get("sender") or "" for item in recent}
                if any(len(sp) >= 2 and sp in text for sp in speakers):
                    selected.append(umo)

        return selected[: self.max_groups]

    @staticmethod
    def _refers_to_group(text: str, name: str) -> bool:
        """Check whether a message names a group, abbreviations included.

        The full name is matched directly. Shorter contiguous runs of the
        message also match when they are an in-order abbreviation of the name
        starting from its first character, so "蓝原群" resolves to
        "蓝色星原奇波养殖基地" while an unrelated word such as "插件" does not
        resolve to "bot插件测试".

        Args:
            text: Current message text.
            name: Group display name.

        Returns:
            True when the message appears to reference that group.
        """
        if not text or len(name) < 2:
            return False

        trimmed = name.replace("群", "").strip()
        if len(trimmed) < 2:
            return False
        if trimmed in text:
            return True

        # Chinese has no word boundaries, so scan every window that starts on
        # the name's first character and accept the longest abbreviation.
        haystack = trimmed.casefold()
        head = haystack[0]
        lowered = text.casefold()
        for index, char in enumerate(lowered):
            for length in (6, 5, 4, 3, 2):
                window = lowered[index : index + length]
                if len(window) < 2:
                    continue
                # A contiguous slice of the name, such as "星原", is enough
                # unless the slice is a generic word seen in many group names.
                if window in haystack and window not in _GENERIC_NAME_FRAGMENTS:
                    return True
                # Otherwise only accept an abbreviation anchored on the first
                # character, so "蓝原" works while "原来" does not.
                if char == head and CrossGroupSight._is_subsequence(window, haystack):
                    return True
        return False

    @staticmethod
    def _is_subsequence(needle: str, haystack: str) -> bool:
        """Check whether needle characters appear in order inside haystack.

        Args:
            needle: Candidate abbreviation.
            haystack: Group name to match against.

        Returns:
            True when every needle character appears in order.
        """
        position = 0
        for char in needle:
            position = haystack.find(char, position)
            if position < 0:
                return False
            position += 1
        return True

    async def _ensure_all_backfilled(self, current_umo: str) -> None:
        """Seed every enabled group buffer once, even if it has been quiet.

        Args:
            current_umo: Session of the message being answered; it is skipped.
        """
        if not self.enabled_groups:
            return
        prefix = current_umo.split(":", 1)[0] if ":" in current_umo else ""
        if not prefix:
            return
        for group_id in self.enabled_groups:
            umo = f"{prefix}:GroupMessage:{group_id}"
            if umo == current_umo or umo in self._backfilled:
                continue
            # Persisted sightings are newer than history rows, so only seed
            # groups that have nothing loaded yet.
            if self._buffers.get(umo):
                continue
            await self._backfill(umo, group_id)

    def _build_sightings(
        self,
        selected: list[str],
        recent_by_group: dict[str, list[dict[str, Any]]],
        focused: set[str] | None = None,
    ) -> list[GroupSighting]:
        """Materialize the selected groups with their quoted messages.

        Args:
            selected: Session ids to quote.
            recent_by_group: Source messages per session.
            focused: Sessions that were referenced explicitly; they keep the
                full per-group budget while the rest stay on the baseline.

        Returns:
            Sightings ready for render().
        """
        focused = focused or set()
        sightings: list[GroupSighting] = []
        for umo in selected:
            messages = recent_by_group.get(umo) or []
            if not messages:
                continue
            limit = self.max_per_group if umo in focused else self.baseline_per_group
            sightings.append(
                GroupSighting(
                    umo=umo,
                    name=self._names.get(umo) or f"群{self._group_id(umo)}",
                    messages=messages[-limit:],
                )
            )
        return sightings

    async def _ensure_names(self, umos: tuple[str, ...]) -> None:
        """Resolve group display names through the core alias table.

        Args:
            umos: Sessions still missing a cached name.
        """
        missing = [umo for umo in umos if umo not in self._names]
        if not missing:
            return
        try:
            database = self.context.get_db()
            aliases = await database.get_umo_aliases(missing)
        except Exception as exc:
            logger.debug(f"[CrossGroup] failed to resolve group names: {exc}")
            aliases = []

        resolved = set()
        for item in aliases or []:
            umo = getattr(item, "umo", "")
            if not umo:
                continue
            name = (
                (getattr(item, "user_alias", "") or "")
                or (getattr(item, "auto_name", "") or "")
            ).strip()
            if name:
                self._names[umo] = name
            resolved.add(umo)

        # Cache the fallback too, otherwise every turn retries the lookup.
        for umo in missing:
            if umo not in resolved:
                self._names[umo] = ""

    async def _backfill(self, umo: str, group_id: str) -> None:
        """Seed one group buffer from the core platform history.

        Args:
            umo: Session to seed.
            group_id: Group id used only for logging.
        """
        self._backfilled.add(umo)
        manager = getattr(self.context, "message_history_manager", None)
        if manager is None:
            return

        platform_id = umo.split(":", 1)[0]
        try:
            rows = await manager.get(
                platform_id=platform_id,
                user_id=umo,
                page=1,
                page_size=self.max_per_group,
            )
        except Exception as exc:
            logger.debug(f"[CrossGroup] history backfill failed for {group_id}: {exc}")
            return

        buffer = self._buffer(umo)
        for row in rows or []:
            text = self._history_text(getattr(row, "content", ""))
            if not text:
                continue
            buffer.append(
                {
                    "ts": self._row_timestamp(row),
                    "sender": str(getattr(row, "sender_name", "") or ""),
                    "text": text[:_MAX_TEXT_CHARS],
                }
            )

    @staticmethod
    def _history_text(content: Any) -> str:
        """Extract plain text from a stored platform message record.

        Args:
            content: Raw content column value.

        Returns:
            Concatenated text, or an empty string.
        """
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (TypeError, ValueError):
                return content.strip()
        if not isinstance(content, list):
            return ""

        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            data = block.get("data") or {}
            if block_type == "text":
                parts.append(str(data.get("text") or ""))
            elif block_type == "image":
                parts.append("[图片]")
            elif block_type == "face":
                parts.append("[表情]")
        return " ".join(part for part in parts if part).strip()

    @staticmethod
    def _row_timestamp(row: Any) -> float:
        """Read a history row timestamp as a POSIX value.

        Args:
            row: Stored platform history row.

        Returns:
            Floating point timestamp, defaulting to now.
        """
        raw = getattr(row, "created_at", None) or getattr(row, "updated_at", None)
        if isinstance(raw, datetime):
            return raw.timestamp()
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw).timestamp()
            except ValueError:
                return time.time()
        return time.time()

    def flush(self) -> None:
        """Persist the sighting buffers so a restart keeps recent context.

        The store is a small JSON file next to the plugin data; it is optional
        and every failure is non fatal, because the layer works in memory.
        """
        if self.store_path is None:
            return
        try:
            payload = {
                "saved_at": time.time(),
                "groups": {
                    umo: list(buffer) for umo, buffer in self._buffers.items() if buffer
                },
                "names": dict(self._names),
            }
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.store_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp_path, self.store_path)
            self._last_flush = time.time()
        except Exception as exc:
            logger.debug(f"[CrossGroup] failed to persist sightings: {exc}")

    def _maybe_flush(self) -> None:
        """Persist at most once every few seconds to avoid disk churn."""
        if time.time() - self._last_flush < 5:
            return
        self.flush()

    def _load_store(self) -> None:
        """Restore persisted sightings and group names from disk."""
        if self.store_path is None or not self.store_path.exists():
            return
        try:
            payload = json.loads(self.store_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug(f"[CrossGroup] failed to load persisted sightings: {exc}")
            return

        groups = payload.get("groups") or {}
        for umo, entries in groups.items():
            if not isinstance(entries, list):
                continue
            buffer = self._buffer(str(umo))
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                text = str(entry.get("text") or "").strip()
                ts = entry.get("ts")
                if not text or not isinstance(ts, (int, float)):
                    continue
                buffer.append(
                    {
                        "ts": float(ts),
                        "sender": str(entry.get("sender") or ""),
                        "text": text[:_MAX_TEXT_CHARS],
                    }
                )

        names = payload.get("names") or {}
        for umo, name in names.items():
            if name:
                self._names[str(umo)] = str(name)

    def _buffer(self, umo: str) -> deque[dict[str, Any]]:
        """Return the bounded message buffer for one group.

        Args:
            umo: Unified message origin.

        Returns:
            The group buffer, created on first use.
        """
        buffer = self._buffers.get(umo)
        if buffer is None:
            buffer = deque(maxlen=self.buffer_size)
            self._buffers[umo] = buffer
        return buffer

    def _is_denied(self, umo: str) -> bool:
        """Check whether a group is excluded from cross-group quoting.

        Args:
            umo: Unified message origin.

        Returns:
            True when the group must stay private.
        """
        if not self.denylist:
            return False
        return umo in self.denylist or self._group_id(umo) in self.denylist

    @staticmethod
    def _group_id(umo: str) -> str:
        """Extract the trailing chat id from a unified message origin.

        Args:
            umo: Unified message origin.

        Returns:
            The chat id, or the original string when it cannot be split.
        """
        return umo.rsplit(":", 1)[-1] if ":" in umo else umo

    @staticmethod
    def _event_timestamp(event: AstrMessageEvent) -> float:
        """Read the platform timestamp of an event.

        Args:
            event: Incoming message event.

        Returns:
            Floating point timestamp, defaulting to now.
        """
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "timestamp", None)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return time.time()
        return value if value > 0 else time.time()

    @staticmethod
    def _clock(ts: float) -> str:
        """Format a timestamp as local hour:minute.

        Args:
            ts: POSIX timestamp.

        Returns:
            Clock label such as 20:31.
        """
        try:
            return datetime.fromtimestamp(float(ts)).strftime("%H:%M")
        except (TypeError, ValueError, OSError):
            return "--:--"
