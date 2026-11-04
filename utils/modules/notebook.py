"""The persona's own notebook: what it cares about, and how it grows.

Entries are things the persona likes, dislikes, is doing, or keeps thinking
about. They come from two honest sources: what it actually did (videos it
liked) and what it actually said over time (batch reflection). Nothing is
invented at prompt time.

Growth follows the same shape as a self-learning system: observations pile up
until a batch is worth reflecting on, reflection proposes candidates with a
confidence, repeated evidence makes an entry stronger, and silence lets it fade.
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

_MAX_ENTRIES = 200
_MAX_INJECT = 3
_MAX_PENDING = 200
_WORD_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,8}|[A-Za-z0-9_]{3,}")
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]+")
_KIND_LABELS = {
    "like": "喜欢",
    "dislike": "讨厌",
    "doing": "在惦记",
    "person": "在意",
    "knows": "知道",
    "jargon": "黑话",
}

_STOP_WORDS = {
    "这个",
    "那个",
    "什么",
    "怎么",
    "可以",
    "就是",
    "不是",
    "一个",
    "我们",
    "你们",
    "他们",
    "自己",
    "时候",
    "感觉",
    "觉得",
    "知道",
    "现在",
    "刚才",
}

_REFLECT_PROMPT = """你在帮一个人格整理它自己的本子。只输出 JSON，不要解释。

{persona_block}

它最近说过这些话：
{conversation}

它本子里已经记着：
{existing}

请找出它这几轮里表现出来的喜好、厌恶、惦记的事或在意的人。
要求：
1. 只写对话里能看出来的，不要推测、不要补充它没表露过的东西。
2. 没有值得记的就返回空数组。
3. evidence 写对应那句原话的片段，方便核对。

输出格式：
{{"entries": [{{"text": "讨厌被反复追问", "kind": "dislike",
  "confidence": 0.8, "evidence": "别问了"}}]}}"""


@dataclass
class NotebookEntry:
    """One thing the persona cares about.

    Attributes:
        text: What it is, in its own words.
        kind: One of like, dislike, doing or person.
        weight: How much it currently matters, 0 to 1.
        confidence: How sure the extraction was when first written.
        evidence: How many times it has been observed.
        source: Where the entry came from.
        created_at: Unix timestamp when it was first written.
        last_seen: Unix timestamp when it was last confirmed.
    """

    text: str
    kind: str = "like"
    weight: float = 0.5
    confidence: float = 0.5
    evidence: int = 1
    source: str = "manual"
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def keywords(self) -> list[str]:
        """Extract matchable keywords from the entry.

        Returns:
            Distinct keywords of two or more characters.
        """
        found = [
            word for word in _WORD_PATTERN.findall(self.text) if word not in _STOP_WORDS
        ]
        return list(dict.fromkeys(found))


class Notebook:
    """Stores, matches and grows the persona's own notes.

    Args:
        data_dir: Directory holding notebook.json.
        enabled: Whether the notebook is consulted at all.
        max_inject: Maximum entries quoted in one prompt block.
        decay_days: Days after which an untouched entry scores half as much.
        min_weight: Entries below this weight are not shown to the model.
    """

    def __init__(
        self,
        data_dir: "Path | str | None" = None,
        *,
        enabled: bool = False,
        max_inject: int = _MAX_INJECT,
        decay_days: int = 30,
        min_weight: float = 0.3,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir else None
        self.enabled = bool(enabled)
        self.max_inject = max(1, int(max_inject))
        self.decay_days = max(1, int(decay_days))
        self.min_weight = float(min_weight)
        self.entries: list[NotebookEntry] = []
        self.pending: list[dict] = []
        self._load()

    def match(self, text: str) -> list[NotebookEntry]:
        """Find the entries touched by one message.

        Matching uses shared character pairs rather than whole words, so
        "在补推理小说" still answers "你在看什么推理小说".

        Args:
            text: The incoming message text.

        Returns:
            Matching entries, most relevant first.
        """
        if not self.enabled or not text:
            return []
        message_pairs = _bigrams(text)
        if not message_pairs:
            return []
        self.apply_decay()
        scored: list[tuple[int, float, NotebookEntry]] = []
        for entry in self.entries:
            if entry.weight < self.min_weight:
                continue
            shared = len(_bigrams(entry.text) & message_pairs)
            if shared >= _required_overlap(entry.text):
                scored.append((shared, entry.weight, entry))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return [entry for _hits, _weight, entry in scored[: self.max_inject]]

    def block_for(self, text: str) -> str:
        """Render the notebook lines relevant to one message.

        Args:
            text: The incoming message text.

        Returns:
            A prompt block, or an empty string when nothing matches.
        """
        matched = self.match(text)
        if not matched:
            return ""
        lines = [
            "- "
            + _KIND_LABELS.get(entry.kind, entry.kind)
            + "："
            + (entry.text[:90] + "…" if len(entry.text) > 90 else entry.text)
            for entry in matched
        ]
        return (
            "[你在意的东西]\n"
            + "\n".join(lines)
            + "\n（这些是它自己记着的事，跟当前话题有关才用；没提到就别硬扯。）"
        )

    def all_lines(self) -> list[str]:
        """Render every usable entry as a plain list, strongest first.

        Returns:
            One line per entry.
        """
        ordered = sorted(self.entries, key=lambda e: -e.weight)
        return [
            _KIND_LABELS.get(entry.kind, entry.kind)
            + "："
            + entry.text
            + "（证据 "
            + str(entry.evidence)
            + " 次）"
            for entry in ordered
            if entry.weight >= self.min_weight
        ]

    def apply_decay(self) -> None:
        """Fade entries that have not been confirmed for a while."""
        now = time.time()
        span = self.decay_days * 86400
        changed = False
        for entry in self.entries:
            idle = now - entry.last_seen
            if idle <= span:
                continue
            factor = 0.5 ** (idle / span)
            new_weight = max(0.0, entry.weight * factor)
            if abs(new_weight - entry.weight) > 0.01:
                entry.weight = new_weight
                changed = True
        if changed:
            self._save()

    def merge_candidate(
        self,
        text: str,
        kind: str = "like",
        confidence: float = 0.5,
        min_confidence: float = 0.6,
    ) -> bool:
        """Fold one reflected candidate into the notebook.

        Args:
            text: What it is.
            kind: Entry kind.
            confidence: Extraction confidence, 0 to 1.
            min_confidence: Minimum confidence for a brand new entry.

        Returns:
            True when a new entry was created.
        """
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        now = time.time()
        candidate_pairs = _bigrams(cleaned)
        for entry in self.entries:
            shared = len(candidate_pairs & _bigrams(entry.text))
            if shared >= _required_overlap(cleaned):
                entry.evidence += 1
                entry.weight = min(1.0, entry.weight + 0.15)
                entry.confidence = max(entry.confidence, float(confidence))
                entry.last_seen = now
                self._save()
                return False
        if confidence < min_confidence:
            return False
        self.entries.append(
            NotebookEntry(
                text=cleaned,
                kind=kind,
                weight=min(1.0, 0.4 + 0.4 * float(confidence)),
                confidence=float(confidence),
                source="learned",
            )
        )
        if len(self.entries) > _MAX_ENTRIES:
            self.entries.sort(key=lambda e: -e.weight)
            self.entries = self.entries[:_MAX_ENTRIES]
        self._save()
        return True

    def add(self, text: str, kind: str = "like", source: str = "manual") -> bool:
        """Add one hand written entry.

        Args:
            text: What it is.
            kind: Entry kind.
            source: Where it came from.

        Returns:
            True when a new entry was stored.
        """
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        if any(entry.text == cleaned for entry in self.entries):
            return False
        self.entries.append(
            NotebookEntry(text=cleaned, kind=kind, weight=0.7, source=source)
        )
        self._save()
        return True

    def graph(self, limit: int = 60) -> dict:
        """Build a small node/edge view of what the persona remembers.

        Nodes are entries, edges link entries that share a character pair, so
        related things cluster together when the page draws the network.

        Args:
            limit: Maximum entries to include.

        Returns:
            A payload with nodes, edges and counts for the settings page.
        """
        entries = sorted(self.entries, key=lambda e: -e.weight)[: max(1, limit)]
        nodes = []
        pairs_by_id: dict[str, set[str]] = {}
        for index, entry in enumerate(entries):
            node_id = "n" + str(index)
            pairs_by_id[node_id] = _bigrams(entry.text)
            nodes.append(
                {
                    "id": node_id,
                    "label": entry.text[:12],
                    "kind": entry.kind,
                    "kind_label": _KIND_LABELS.get(entry.kind, entry.kind),
                    "text": entry.text,
                    "weight": round(entry.weight, 2),
                    "evidence": entry.evidence,
                    "source": entry.source,
                    "inject": "- "
                    + _KIND_LABELS.get(entry.kind, entry.kind)
                    + "："
                    + entry.text,
                }
            )
        edges = [{"from": "me", "to": node["id"], "weight": 1} for node in nodes]
        ids = [node["id"] for node in nodes]
        for i, left in enumerate(ids):
            for right in ids[i + 1 :]:
                shared = len(pairs_by_id[left] & pairs_by_id[right])
                if shared >= 2:
                    edges.append({"from": left, "to": right, "weight": shared})
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return {
            "enabled": self.enabled,
            "total": len(self.entries),
            "counts": counts,
            "pending_curiosity": len([e for e in self.entries if e.kind == "curious"]),
            "nodes": nodes,
            "edges": edges,
        }

    def add_jargon(
        self, term: str, meaning: str, group: str = "", alt: str = ""
    ) -> bool:
        """Store a piece of group slang with its meaning.

        Args:
            term: The slang word or phrase.
            meaning: What it means inside that group.
            group: Group id the slang belongs to.
            alt: Other common senses, kept as a short tail.

        Returns:
            True when the slang was newly stored.
        """
        word = str(term or "").strip()
        sense = str(meaning or "").strip()
        if len(word) < 2 or not sense:
            return False
        for entry in self.entries:
            if entry.kind == "jargon" and entry.text.startswith(word + " = "):
                return False
        text = f"{word} = {sense[:120]}"
        extra = str(alt or "").strip()
        if extra and len(extra) <= 40:
            text += f"（另：{extra}）"
        self.entries.append(
            NotebookEntry(
                text=text,
                kind="jargon",
                weight=0.6,
                source=f"group:{group}" if group else "jargon",
            )
        )
        self._save()
        return True

    def add_curiosity(self, term: str) -> bool:
        """Record something the persona wants to look up.

        Args:
            term: The thing it does not understand yet.

        Returns:
            True when a new curiosity was stored.
        """
        cleaned = str(term or "").strip()
        if not cleaned:
            return False
        if any(
            entry.kind == "curious" and entry.text == cleaned for entry in self.entries
        ):
            return False
        self.entries.append(
            NotebookEntry(text=cleaned, kind="curious", weight=0.6, source="curiosity")
        )
        self._save()
        return True

    def pending_curiosities(self, limit: int = 3) -> list["NotebookEntry"]:
        """List things it still wants to look up.

        Args:
            limit: Maximum entries to return.

        Returns:
            Curiosity entries, oldest first.
        """
        items = [entry for entry in self.entries if entry.kind == "curious"]
        items.sort(key=lambda entry: entry.created_at)
        return items[: max(1, int(limit))]

    def resolve_curiosity(self, term: str, summary: str) -> bool:
        """Turn one curiosity into something it now knows.

        Args:
            term: The thing that was looked up.
            summary: What it found, in its own words.

        Returns:
            True when the curiosity was replaced by a knowledge entry.
        """
        summary = str(summary or "").strip()
        if not summary:
            return False
        for entry in self.entries:
            if entry.kind == "curious" and entry.text == term:
                entry.kind = "knows"
                entry.text = term + "\uff1a" + summary
                entry.weight = 0.75
                entry.evidence = 1
                entry.last_seen = time.time()
                self._save()
                return True
        return False

    def remove(self, text: str) -> bool:
        """Delete one entry by its exact text.

        Args:
            text: The stored text.

        Returns:
            True when an entry was removed.
        """
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        before = len(self.entries)
        self.entries = [
            entry for entry in self.entries if entry.text.strip() != cleaned
        ]
        if len(self.entries) != before:
            self._save()
            return True
        return False

    def rename(self, text: str, new_text: str) -> bool:
        """Rewrite one entry's text.

        Args:
            text: Current stored text.
            new_text: Replacement text.

        Returns:
            True when an entry was updated.
        """
        cleaned = str(text or "").strip()
        replacement = str(new_text or "").strip()
        if not cleaned or not replacement:
            return False
        for entry in self.entries:
            if entry.text.strip() == cleaned:
                entry.text = replacement
                entry.source = "manual"
                entry.last_seen = time.time()
                self._save()
                return True
        return False

    def drop_curiosity(self, term: str) -> bool:
        """Remove a queued curiosity without storing an answer.

        Used when a word turns out to be everyday language or group slang that
        must not be explained by a dictionary lookup.

        Args:
            term: The queued term.

        Returns:
            True when an entry was removed.
        """
        cleaned = str(term or "").strip()
        if not cleaned:
            return False
        before = len(self.entries)
        self.entries = [
            entry
            for entry in self.entries
            if not (entry.kind == "curious" and entry.text.strip() == cleaned)
        ]
        if len(self.entries) != before:
            self._save()
            return True
        return False

    def observe(self, user_text: str, bot_text: str) -> None:
        """Queue one exchange for the next reflection batch.

        Args:
            user_text: What the other person said.
            bot_text: What the persona answered.
        """
        if not self.enabled:
            return
        user_text = str(user_text or "").strip()
        bot_text = str(bot_text or "").strip()
        if not bot_text:
            return
        self.pending.append({"user": user_text[:200], "bot": bot_text[:200]})
        if len(self.pending) > _MAX_PENDING:
            self.pending = self.pending[-_MAX_PENDING:]

    def build_reflect_prompt(self, persona_prompt: str, limit: int = 20) -> str:
        """Render the reflection prompt for the queued exchanges.

        Args:
            persona_prompt: The persona the entries belong to.
            limit: Maximum exchanges to include.

        Returns:
            The prompt text.
        """
        conversation = "\n".join(
            "对方：" + str(item.get("user", "")) + "\n它：" + str(item.get("bot", ""))
            for item in self.pending[-limit:]
        )
        existing = "\n".join("- " + line for line in self.all_lines()[:20]) or "（空）"
        persona_block = (
            "这个人的性格设定：" + persona_prompt[:400] if persona_prompt else ""
        )
        return _REFLECT_PROMPT.format(
            persona_block=persona_block,
            conversation=conversation or "（空）",
            existing=existing,
        )

    def absorb_reflection(
        self, text: str, min_confidence: float = 0.6
    ) -> tuple[int, int]:
        """Parse a reflection reply and fold it into the notebook.

        Args:
            text: Raw model output.
            min_confidence: Minimum confidence for a new entry.

        Returns:
            A tuple of (new entries, strengthened entries).
        """
        payload = _extract_json(text)
        if not isinstance(payload, dict):
            return 0, 0
        before = len(self.entries)
        for item in payload.get("entries") or []:
            if not isinstance(item, dict):
                continue
            try:
                confidence = float(item.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            self.merge_candidate(
                str(item.get("text") or ""),
                kind=str(item.get("kind") or "like"),
                confidence=confidence,
                min_confidence=min_confidence,
            )
        new_count = len(self.entries) - before
        strengthened = max(0, len(payload.get("entries") or []) - new_count)
        self.pending.clear()
        self._save()
        return new_count, strengthened

    def _path(self) -> Path | None:
        """Resolve the notebook file path.

        Returns:
            The path, or None when no directory is configured.
        """
        if self.data_dir is None:
            return None
        return self.data_dir / "notebook.json"

    def _save(self) -> None:
        """Write every entry and the pending queue to disk."""
        path = self._path()
        if path is None:
            return
        payload = {
            "version": 2,
            "entries": [
                {
                    "text": entry.text,
                    "kind": entry.kind,
                    "weight": round(entry.weight, 4),
                    "confidence": round(entry.confidence, 4),
                    "evidence": entry.evidence,
                    "source": entry.source,
                    "created_at": entry.created_at,
                    "last_seen": entry.last_seen,
                }
                for entry in self.entries
            ],
            "pending": self.pending[-_MAX_PENDING:],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(path)
        except Exception as exc:
            logger.debug(f"[记忆本] 写入失败: {exc}")

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
        self.pending = [
            item for item in (payload.get("pending") or []) if isinstance(item, dict)
        ][-_MAX_PENDING:]
        for item in payload.get("entries") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            # 旧版把"看过视频"当成"喜欢"，属于噪音，不再载入。
            if str(item.get("source") or "") == "watch":
                continue
            now = time.time()
            self.entries.append(
                NotebookEntry(
                    text=text,
                    kind=str(item.get("kind") or "like"),
                    weight=float(item.get("weight") or 0.5),
                    confidence=float(item.get("confidence") or 0.5),
                    evidence=int(item.get("evidence") or 1),
                    source=str(item.get("source") or "manual"),
                    created_at=float(item.get("created_at") or now),
                    last_seen=float(item.get("last_seen") or now),
                )
            )


def _bigrams(text: str) -> set[str]:
    """Split text into shared-comparable character pairs.

    Args:
        text: Any text.

    Returns:
        The set of adjacent Chinese character pairs.
    """
    pairs: set[str] = set()
    for chunk in _CJK_PATTERN.findall(str(text or "")):
        for index in range(len(chunk) - 1):
            pair = chunk[index : index + 2]
            if pair[0] in _STOP_CHARS and pair[1] in _STOP_CHARS:
                continue
            pairs.add(pair)
    return pairs


def _required_overlap(text: str) -> int:
    """Decide how many shared pairs count as a real match.

    Args:
        text: The shorter side of the comparison.

    Returns:
        Minimum shared pair count.
    """
    return 1


_STOP_CHARS = set("的了是在我你他她它们么呢吧啊这那个和与就都也很不有会要到说着过")


def _extract_json(text: str):
    """Extract the first JSON object from a model reply.

    Args:
        text: Raw model output that may be wrapped in code fences.

    Returns:
        The parsed object, or None.
    """
    cleaned = (text or "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except (TypeError, ValueError):
        return None
