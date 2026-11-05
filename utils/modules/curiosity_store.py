"""Separate storage for curiosity topics and looked-up knowledge.

Curiosity results are not persona memories: they are things the persona looked
up on its own. Keeping them out of the notebook keeps the memory network
readable while still letting the reply path use them as background.
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

_MAX_PENDING = 50
_MAX_KNOWLEDGE = 300
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]+")
_WORD_PATTERN = re.compile(r"[A-Za-z0-9_]{2,}")
_STOP_CHARS = set("的了是在我你他她它们么呢吧啊这那个和与就都也很不有会要到说着过")


def _tokens(text: str) -> set[str]:
    """Split text into matchable Chinese pairs and Latin words.

    Args:
        text: Any text.

    Returns:
        A set of comparison tokens.
    """
    raw = str(text or "")
    tokens: set[str] = set()
    for chunk in _CJK_PATTERN.findall(raw):
        for index in range(len(chunk) - 1):
            pair = chunk[index : index + 2]
            if pair[0] in _STOP_CHARS and pair[1] in _STOP_CHARS:
                continue
            tokens.add(pair)
    for word in _WORD_PATTERN.findall(raw):
        tokens.add(word.lower())
    return tokens


@dataclass
class CuriosityEntry:
    """One thing the persona was curious about.

    Attributes:
        text: The topic as the persona understood it.
        kind: ``curious`` while pending, ``knows`` after a lookup.
        query: Search query used for the lookup.
        source: Why it entered the queue, such as interest or question.
        why: One-line reason it was curious.
        summary: Short answer after a successful lookup.
        umo: Conversation origin used to resolve its default persona.
        created_at: Unix timestamp when it was queued.
        resolved_at: Unix timestamp when the lookup finished.
        forget_stage: 0 fresh, 1/2 observing, 3 delete on next cycle.
        last_used_at: Last time the knowledge was injected into a reply.
        use_count: How many times it has been used.
        forget_updated_at: Last time the forgetting stage advanced.
    """

    text: str
    kind: str = "curious"
    query: str = ""
    source: str = "interest"
    why: str = ""
    summary: str = ""
    umo: str = ""
    created_at: float = field(default_factory=time.time)
    resolved_at: float = 0.0
    forget_stage: int = 0
    last_used_at: float = 0.0
    use_count: int = 0
    forget_updated_at: float = 0.0


class CuriosityStore:
    """Persists pending curiosity topics and finished knowledge.

    Args:
        data_dir: Directory holding ``curiosity.json``; None keeps it in memory.
        pending_ttl_days: Drop unresolved topics after this many days; 0 keeps.
        knowledge_ttl_days: Optional hard cap for knowledge; 0 uses staged forgetting.
        forgetting_cycle_days: Days between forgetting stages.
        forgetting_grace_days: New knowledge is safe for this many days.
    """

    def __init__(
        self,
        data_dir: "Path | str | None" = None,
        pending_ttl_days: int = 7,
        knowledge_ttl_days: int = 0,
        forgetting_cycle_days: int = 7,
        forgetting_grace_days: int = 14,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir else None
        self.pending_ttl_days = max(0, int(pending_ttl_days))
        self.knowledge_ttl_days = max(0, int(knowledge_ttl_days))
        self.forgetting_cycle_days = max(1, int(forgetting_cycle_days))
        self.forgetting_grace_days = max(0, int(forgetting_grace_days))
        self.entries: list[CuriosityEntry] = []
        self._load()
        self.prune()

    def pending(self, limit: int = 3) -> list[CuriosityEntry]:
        """List unresolved curiosities, oldest first.

        Args:
            limit: Maximum entries to return.

        Returns:
            Pending curiosity entries.
        """
        items = [entry for entry in self.entries if entry.kind == "curious"]
        # Explicit questions and manual lookups should not wait behind a long
        # backlog of ambient reflections.
        priority = {"question": 0, "manual": 0, "interest": 1}
        items.sort(
            key=lambda entry: (
                priority.get(entry.source, 1),
                entry.created_at,
            )
        )
        return items[: max(1, int(limit))]

    def knowledge(self, limit: int = 20) -> list[CuriosityEntry]:
        """List finished knowledge, newest first.

        Args:
            limit: Maximum entries to return.

        Returns:
            Resolved knowledge entries.
        """
        items = [entry for entry in self.entries if entry.kind == "knows"]
        items.sort(
            key=lambda entry: entry.resolved_at or entry.created_at, reverse=True
        )
        return items[: max(1, int(limit))]

    def add(
        self,
        term: str,
        source: str = "interest",
        query: str = "",
        why: str = "",
        umo: str = "",
    ) -> bool:
        """Queue one curiosity topic.

        Args:
            term: The topic the persona wants to understand.
            source: Why it entered the queue.
            query: Search query; defaults to the topic.
            why: One-line reason it was curious.
            umo: Conversation origin for persona resolution.

        Returns:
            True when a new topic was queued.
        """
        cleaned = str(term or "").strip()
        if not cleaned:
            return False
        candidate = _tokens(cleaned)
        for entry in self.entries:
            if entry.text == cleaned:
                return False
            if len(candidate) < 3:
                continue
            other = _tokens(entry.text)
            if len(other) < 3:
                continue
            shared = len(candidate & other)
            if shared / min(len(candidate), len(other)) >= 0.75:
                return False
        self.entries.append(
            CuriosityEntry(
                text=cleaned,
                source=str(source or "interest"),
                query=str(query or "").strip(),
                why=str(why or "").strip(),
                umo=str(umo or "").strip(),
            )
        )
        self._trim()
        self._save()
        return True

    def resolve(self, term: str, summary: str) -> bool:
        """Turn a pending topic into knowledge.

        Args:
            term: Topic text.
            summary: Short answer.

        Returns:
            True when the topic was found and resolved.
        """
        cleaned = str(summary or "").strip()
        if not cleaned:
            return False
        for entry in self.entries:
            if entry.kind == "curious" and entry.text == term:
                entry.kind = "knows"
                entry.summary = cleaned
                entry.resolved_at = time.time()
                self._trim()
                self._save()
                return True
        return False

    def drop(self, term: str) -> bool:
        """Remove a pending topic.

        Args:
            term: Topic text.

        Returns:
            True when something was removed.
        """
        before = len(self.entries)
        self.entries = [
            entry
            for entry in self.entries
            if not (entry.kind == "curious" and entry.text == term)
        ]
        if len(self.entries) != before:
            self._save()
            return True
        return False

    def remove(self, text: str) -> bool:
        """Remove any entry by exact display topic.

        Args:
            text: Topic text.

        Returns:
            True when an entry was removed.
        """
        before = len(self.entries)
        self.entries = [entry for entry in self.entries if entry.text != text]
        if len(self.entries) != before:
            self._save()
            return True
        return False

    def update(
        self,
        text: str,
        *,
        query: str | None = None,
        summary: str | None = None,
        why: str | None = None,
        new_text: str | None = None,
    ) -> bool:
        """Edit one entry in place.

        Args:
            text: Current topic text.
            query: New search query, when provided.
            summary: New summary, when provided.
            why: New reason, when provided.
            new_text: New topic text, when provided.

        Returns:
            True when an entry was changed.
        """
        renamed = str(new_text or "").strip()
        for entry in self.entries:
            if entry.text != text:
                continue
            if renamed and renamed != entry.text:
                if any(other.text == renamed for other in self.entries):
                    return False
                entry.text = renamed
            if query is not None:
                entry.query = str(query or "").strip()
            if why is not None:
                entry.why = str(why or "").strip()
            if summary is not None and entry.kind == "knows":
                entry.summary = str(summary or "").strip()
            self._save()
            return True
        return False

    def prune(self, now: float | None = None) -> int:
        """Drop entries older than their retention window.

        Args:
            now: Unix timestamp; defaults to the current time.

        Returns:
            Number of removed entries.
        """
        current = float(now if now is not None else time.time())
        kept: list[CuriosityEntry] = []
        changed = False
        for entry in self.entries:
            if entry.kind == "curious":
                stamp = float(entry.created_at or 0.0)
                if (
                    self.pending_ttl_days > 0
                    and current - stamp > self.pending_ttl_days * 86400
                ):
                    changed = True
                    continue
                kept.append(entry)
                continue

            stamp = float(entry.resolved_at or entry.created_at or 0.0)
            if (
                self.knowledge_ttl_days > 0
                and current - stamp > self.knowledge_ttl_days * 86400
            ):
                changed = True
                continue
            if (
                self.forgetting_grace_days > 0
                and current - stamp < self.forgetting_grace_days * 86400
            ):
                kept.append(entry)
                continue
            if entry.source == "manual" or entry.use_count >= 3:
                if entry.forget_stage != 0:
                    entry.forget_stage = 0
                    changed = True
                kept.append(entry)
                continue

            cycle_start = current - self.forgetting_cycle_days * 86400
            if entry.last_used_at >= cycle_start:
                if entry.forget_stage != 0:
                    entry.forget_stage = 0
                    changed = True
                kept.append(entry)
                continue

            stage_ts = float(entry.forget_updated_at or stamp or 0.0)
            cycle = self.forgetting_cycle_days * 86400
            elapsed_cycles = int((current - stage_ts) // cycle)
            if elapsed_cycles > 0:
                entry.forget_stage = min(
                    3, int(entry.forget_stage or 0) + elapsed_cycles
                )
                entry.forget_updated_at = stage_ts + elapsed_cycles * cycle
                changed = True
            if entry.forget_stage >= 3:
                changed = True
                continue
            kept.append(entry)

        removed = len(self.entries) - len(kept)
        if changed or removed:
            self.entries = kept
            self._save()
        return removed

    def touch(self, text: str, now: float | None = None) -> bool:
        """Mark one knowledge entry as used and refresh its freshness.

        Args:
            text: Topic text.
            now: Unix timestamp; defaults to current time.

        Returns:
            True when an entry was touched.
        """
        stamp = float(now if now is not None else time.time())
        for entry in self.entries:
            if entry.kind != "knows" or entry.text != text:
                continue
            entry.last_used_at = stamp
            entry.use_count += 1
            entry.forget_stage = 0
            self._save()
            return True
        return False

    def knowledge_block(self, text: str, limit: int = 2) -> str:
        """Render knowledge relevant to one message.

        Args:
            text: Current message text.
            limit: Maximum entries to quote.

        Returns:
            A prompt block, or an empty string.
        """
        wanted = _tokens(text)
        if not wanted:
            return ""
        scored: list[tuple[int, float, CuriosityEntry]] = []
        for entry in self.entries:
            if entry.kind != "knows":
                continue
            hit = len(_tokens(entry.text + " " + entry.summary) & wanted)
            if hit:
                scored.append((hit, entry.resolved_at or entry.created_at, entry))
        if not scored:
            return ""
        scored.sort(key=lambda item: (-item[0], -item[1]))
        selected = scored[: max(1, int(limit))]
        for _hit, _ts, entry in selected:
            self.touch(entry.text)
        lines = [
            "- " + entry.text + "：" + entry.summary[:160]
            for _hit, _ts, entry in selected
        ]
        return (
            "[你之前好奇查过的东西]\n"
            + "\n".join(lines)
            + "\n（这是你自己查到的背景，不是本群共同记忆；相关才提，不要反复说。）"
        )

    def _trim(self) -> None:
        """Bound pending and knowledge lists."""
        pending = [e for e in self.entries if e.kind == "curious"]
        if len(pending) > _MAX_PENDING:
            drop = set(
                id(e)
                for e in sorted(pending, key=lambda e: e.created_at)[:-_MAX_PENDING]
            )
            self.entries = [e for e in self.entries if id(e) not in drop]
        knowledge = [e for e in self.entries if e.kind == "knows"]
        if len(knowledge) > _MAX_KNOWLEDGE:
            drop = set(
                id(e)
                for e in sorted(knowledge, key=lambda e: e.resolved_at or e.created_at)[
                    :-_MAX_KNOWLEDGE
                ]
            )
            self.entries = [e for e in self.entries if id(e) not in drop]

    def _path(self) -> Path | None:
        """Resolve the curiosity file path.

        Returns:
            The path, or None for in-memory stores.
        """
        if self.data_dir is None:
            return None
        return self.data_dir / "curiosity.json"

    def _save(self) -> None:
        """Write all entries to disk."""
        path = self._path()
        if path is None:
            return
        payload = {
            "version": 1,
            "entries": [
                {
                    "text": entry.text,
                    "kind": entry.kind,
                    "query": entry.query,
                    "source": entry.source,
                    "why": entry.why,
                    "summary": entry.summary,
                    "umo": entry.umo,
                    "created_at": entry.created_at,
                    "resolved_at": entry.resolved_at,
                    "forget_stage": entry.forget_stage,
                    "last_used_at": entry.last_used_at,
                    "use_count": entry.use_count,
                    "forget_updated_at": entry.forget_updated_at,
                }
                for entry in self.entries
            ],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(path)
        except Exception as exc:
            logger.debug(f"[好奇心] 知识写入失败: {exc}")

    def _load(self) -> None:
        """Read entries left by a previous run."""
        path = self._path()
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        for item in payload.get("entries") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            try:
                created_at = float(item.get("created_at") or time.time())
                resolved_at = float(item.get("resolved_at") or 0.0)
                forget_stage = int(item.get("forget_stage") or 0)
                last_used_at = float(item.get("last_used_at") or 0.0)
                use_count = int(item.get("use_count") or 0)
                forget_updated_at = float(item.get("forget_updated_at") or 0.0)
            except (TypeError, ValueError):
                continue
            self.entries.append(
                CuriosityEntry(
                    text=text,
                    kind=str(item.get("kind") or "curious"),
                    query=str(item.get("query") or ""),
                    source=str(item.get("source") or "interest"),
                    why=str(item.get("why") or ""),
                    summary=str(item.get("summary") or ""),
                    umo=str(item.get("umo") or ""),
                    created_at=created_at,
                    resolved_at=resolved_at,
                    forget_stage=forget_stage,
                    last_used_at=last_used_at,
                    use_count=use_count,
                    forget_updated_at=forget_updated_at,
                )
            )
        self._trim()
