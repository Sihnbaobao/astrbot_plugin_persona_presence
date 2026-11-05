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
_GENERAL_SENSE_PREFIX = re.compile(r"^(?:在这个群里|这个群里|本群里|群里)\s*")
_KIND_LABELS = {
    "like": "喜欢",
    "dislike": "讨厌",
    "doing": "在惦记",
    "person": "在意",
    "knows": "知道",
    "jargon": "黑话",
}

# Legacy jargon arrived before the learner classified scope. These markers
# usually mean the entry points at a member or an event inside one group.
_GROUP_LOCAL_MARKERS = (
    "群里",
    "群成员",
    "群友",
    "本群",
    "这个群",
    "群内",
    "昵称",
    "角色账号",
    "称呼",
)


def _looks_group_local(text: str) -> bool:
    """Guess whether a legacy jargon entry is tied to one group.

    Args:
        text: Stored jargon text.

    Returns:
        True when the entry mentions a member, nickname or in-group event.
    """
    return any(marker in str(text or "") for marker in _GROUP_LOCAL_MARKERS)


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
        group_id: Group that owns a group-local entry; empty means global.
        cross_group: Whether a group-tagged entry is general enough to use in
            other groups. Person/group-only references stay False.
        query: Search query stored for a curiosity entry.
        created_at: Unix timestamp when it was first written.
        last_seen: Unix timestamp when it was last confirmed.
        forget_stage: 0 fresh, 1/2 observing, 3 delete on next cycle.
        last_used_at: Last time it was injected into a reply.
        use_count: How many times it has been used.
        forget_updated_at: Last time the forgetting stage advanced.
    """

    text: str
    kind: str = "like"
    weight: float = 0.5
    confidence: float = 0.5
    evidence: int = 1
    source: str = "manual"
    group_id: str = ""
    cross_group: bool = True
    query: str = ""
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    forget_stage: int = 0
    last_used_at: float = 0.0
    use_count: int = 0
    forget_updated_at: float = 0.0

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
        jargon_forgetting: Enable three-stage forgetting for group slang.
        jargon_cycle_days: Days between forgetting stages.
        jargon_grace_days: New slang is safe for this many days.
        protect_general_jargon: General internet slang never auto-forgets.
    """

    def __init__(
        self,
        data_dir: "Path | str | None" = None,
        *,
        enabled: bool = False,
        max_inject: int = _MAX_INJECT,
        decay_days: int = 30,
        min_weight: float = 0.3,
        jargon_forgetting: bool = True,
        jargon_cycle_days: int = 7,
        jargon_grace_days: int = 14,
        protect_general_jargon: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir else None
        self.enabled = bool(enabled)
        self.max_inject = max(1, int(max_inject))
        self.decay_days = max(1, int(decay_days))
        self.min_weight = float(min_weight)
        self.jargon_forgetting = bool(jargon_forgetting)
        self.jargon_cycle_days = max(1, int(jargon_cycle_days))
        self.jargon_grace_days = max(0, int(jargon_grace_days))
        self.protect_general_jargon = bool(protect_general_jargon)
        self.entries: list[NotebookEntry] = []
        self.pending: list[dict] = []
        self._load()

    def match(
        self,
        text: str,
        group_id: str = "",
        *,
        include_other_groups: bool = False,
    ) -> list[NotebookEntry]:
        """Find the entries touched by one message.

        Matching uses shared character pairs rather than whole words, so
        "在补推理小说" still answers "你在看什么推理小说". Group-local entries
        are only visible in their own group unless the caller explicitly asks
        for a labelled cross-group view.

        Args:
            text: The incoming message text.
            group_id: Current group, or an empty string for private chat.
            include_other_groups: Include entries owned by other groups.

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
            if entry.kind in {"curious", "knows"}:
                continue
            if entry.weight < self.min_weight:
                continue
            if entry.group_id and entry.group_id != group_id:
                # General internet slang may travel; a group/person-specific
                # reference only crosses when the caller explicitly asks for a
                # labelled cross-group view.
                if not entry.cross_group and not include_other_groups:
                    continue
            shared = len(_bigrams(entry.text) & message_pairs)
            if shared >= _required_overlap(entry.text):
                scored.append((shared, entry.weight, entry))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        selected = [entry for _hits, _weight, entry in scored[: self.max_inject]]
        if selected:
            now = time.time()
            for entry in selected:
                entry.last_seen = now
                entry.last_used_at = now
                entry.use_count += 1
                if entry.kind == "jargon":
                    entry.forget_stage = 0
            self._save()
        return selected

    def block_for(
        self,
        text: str,
        group_id: str = "",
        *,
        include_other_groups: bool = False,
    ) -> str:
        """Render the notebook lines relevant to one message.

        Args:
            text: The incoming message text.
            group_id: Current group, or an empty string for private chat.
            include_other_groups: Include labelled entries from other groups.

        Returns:
            A prompt block, or an empty string when nothing matches.
        """
        matched = self.match(text, group_id, include_other_groups=include_other_groups)
        if not matched:
            return ""
        lines = []
        has_cross_group = False
        for entry in matched:
            cross_group = bool(
                entry.group_id and entry.group_id != group_id and not entry.cross_group
            )
            has_cross_group = has_cross_group or cross_group
            prefix = "[来自其他群] " if cross_group else ""
            lines.append(
                "- "
                + prefix
                + _KIND_LABELS.get(entry.kind, entry.kind)
                + "："
                + (entry.text[:90] + "…" if len(entry.text) > 90 else entry.text)
            )
        note = "（这些是它自己记着的事，跟当前话题有关才用；没提到就别硬扯。）"
        if has_cross_group:
            note += (
                "（标了“来自其他群”的内容只是它在别的群看到/听过的见闻，"
                "不要当成这个群的共同经历；必要时说明是在别的群看到的。）"
            )
        return "[你在意的东西]\n" + "\n".join(lines) + "\n" + note

    def all_lines(self, group_id: str = "") -> list[str]:
        """Render usable entries as a plain list, strongest first.

        Group-local entries are only included for their own group. Passing no
        group intentionally keeps this view global so a pack cannot leak
        another group's jargon through a group-unaware prompt.

        Args:
            group_id: Current group, or an empty string for global entries only.

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
            if entry.kind not in {"curious", "knows"}
            and entry.weight >= self.min_weight
            and (not entry.group_id or entry.group_id == group_id or entry.cross_group)
        ]

    def apply_decay(self) -> None:
        """Fade ordinary memories and advance group-slang forgetting."""
        now = time.time()
        span = self.decay_days * 86400
        cycle = self.jargon_cycle_days * 86400
        changed = False
        kept: list[NotebookEntry] = []
        for entry in self.entries:
            if entry.kind == "jargon" and self.jargon_forgetting:
                # General internet slang is a shared asset; only genuinely
                # group/person-local references are allowed to fade.
                general = not entry.group_id or entry.cross_group
                if general and self.protect_general_jargon:
                    if entry.forget_stage:
                        entry.forget_stage = 0
                        changed = True
                    kept.append(entry)
                    continue
                created = float(entry.created_at or 0.0)
                if (
                    self.jargon_grace_days > 0
                    and now - created < self.jargon_grace_days * 86400
                ):
                    kept.append(entry)
                    continue
                used = (
                    max(
                        float(entry.last_seen or 0.0),
                        float(entry.last_used_at or 0.0),
                    )
                    >= now - cycle
                )
                if used:
                    if entry.forget_stage:
                        entry.forget_stage = 0
                        changed = True
                    kept.append(entry)
                    continue
                stage_ts = float(entry.forget_updated_at or entry.created_at or 0.0)
                elapsed_cycles = int((now - stage_ts) // cycle)
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
                continue

            idle = now - entry.last_seen
            if idle <= span:
                kept.append(entry)
                continue
            factor = 0.5 ** (idle / span)
            new_weight = max(0.0, entry.weight * factor)
            if abs(new_weight - entry.weight) > 0.01:
                entry.weight = new_weight
                changed = True
            kept.append(entry)
        if changed:
            self.entries = kept
            self._save()

    def merge_candidate(
        self,
        text: str,
        kind: str = "like",
        confidence: float = 0.5,
        min_confidence: float = 0.6,
        group_id: str = "",
    ) -> bool:
        """Fold one reflected candidate into the notebook.

        Only candidates with the same kind and group scope are folded into an
        existing entry. A dislike must never strengthen a like just because
        the two sentences share a noun.

        Args:
            text: What it is.
            kind: Entry kind.
            confidence: Extraction confidence, 0 to 1.
            min_confidence: Minimum confidence for a brand new entry.
            group_id: Group scope, or an empty string for global entries.

        Returns:
            True when a new entry was created.
        """
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        scope = str(group_id or "").strip()
        now = time.time()
        candidate_pairs = _bigrams(cleaned)
        for entry in self.entries:
            if entry.kind != kind or entry.group_id != scope:
                continue
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
                group_id=scope,
            )
        )
        self._enforce_limit()
        self._save()
        return True

    def add(
        self,
        text: str,
        kind: str = "like",
        source: str = "manual",
        group_id: str = "",
    ) -> bool:
        """Add one hand written entry.

        Args:
            text: What it is.
            kind: Entry kind.
            source: Where it came from.
            group_id: Group scope, or an empty string for global entries.

        Returns:
            True when a new entry was stored.
        """
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        scope = str(group_id or "").strip()
        if any(
            entry.text == cleaned and entry.group_id == scope for entry in self.entries
        ):
            return False
        self.entries.append(
            NotebookEntry(
                text=cleaned,
                kind=kind,
                weight=0.7,
                source=source,
                group_id=scope,
            )
        )
        self._enforce_limit()
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
        self.apply_decay()
        memory_entries = [
            entry for entry in self.entries if entry.kind not in {"curious", "knows"}
        ]
        entries = sorted(memory_entries, key=lambda e: -e.weight)[: max(1, limit)]
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
                    "group": entry.group_id,
                    "cross_group": entry.cross_group,
                    "forget_stage": entry.forget_stage,
                    "use_count": entry.use_count,
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
        for entry in memory_entries:
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return {
            "enabled": self.enabled,
            "total": len(memory_entries),
            "counts": counts,
            "pending_curiosity": 0,
            "nodes": nodes,
            "edges": edges,
        }

    def add_jargon(
        self,
        term: str,
        meaning: str,
        group: str = "",
        alt: str = "",
        cross_group: bool = True,
    ) -> bool:
        """Store a piece of group slang with its meaning.

        Args:
            term: The slang word or phrase.
            meaning: What it means inside that group.
            group: Group id the slang belongs to.
            alt: Other common senses, kept as a short tail.
            cross_group: Whether the term is general enough for other groups.

        Returns:
            True when the slang was newly stored.
        """
        word = str(term or "").strip()
        sense = str(meaning or "").strip()
        if cross_group:
            sense = _GENERAL_SENSE_PREFIX.sub("", sense)
        if len(word) < 2 or not sense:
            return False
        raw_group = str(group or "").strip()
        # A manual/test run has no real group id; its terms are treated as
        # general slang instead of being silently quarantined.
        scope = "" if raw_group in ("", "manual") else raw_group
        for entry in self.entries:
            if entry.kind != "jargon":
                continue
            head = entry.text.partition(" = ")[0].strip()
            same_term = head == word
            same_scope = entry.group_id == scope
            # "肥鱼" and "大肥鱼" are the same alias; a general entry should
            # absorb the shorter form instead of growing a second wrong head.
            alias = (
                entry.cross_group and len(head) >= 2 and (word in head or head in word)
            )
            if not ((same_term and (same_scope or entry.cross_group)) or alias):
                continue
            # Re-learning the same term is usage, not a duplicate: refresh
            # it so active slang does not decay just because it was known.
            if alias and len(word) > len(head):
                entry.text = word + entry.text[len(head) :]
            now = time.time()
            entry.evidence += 1
            entry.weight = min(1.0, entry.weight + 0.1)
            entry.last_seen = now
            entry.last_used_at = now
            entry.use_count += 1
            entry.forget_stage = 0
            self._save()
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
                group_id=scope,
                cross_group=bool(cross_group),
            )
        )
        self._enforce_limit()
        self._save()
        return True

    def add_curiosity(
        self, term: str, source: str = "curiosity", query: str = ""
    ) -> bool:
        """Record something the persona wants to look up.

        Args:
            term: The thing it does not understand yet.
            source: Where the curiosity came from, such as interest or question.
            query: Search query to use; defaults to the term itself.

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
            NotebookEntry(
                text=cleaned,
                kind="curious",
                weight=0.6,
                source=str(source or "curiosity"),
                query=str(query or "").strip(),
            )
        )
        self._enforce_limit()
        self._save()
        return True

    def extract_kinds(self, kinds: set[str]) -> list["NotebookEntry"]:
        """Remove and return entries of the given kinds.

        Args:
            kinds: Entry kinds to extract.

        Returns:
            The removed entries.
        """
        selected = [entry for entry in self.entries if entry.kind in kinds]
        if selected:
            self.entries = [entry for entry in self.entries if entry.kind not in kinds]
            self._save()
        return selected

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
        # Persist immediately so a reload does not throw away an unreflected batch.
        self._save()

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

    def _enforce_limit(self) -> None:
        """Trim the notebook to its hard cap, keeping the strongest entries."""
        if len(self.entries) <= _MAX_ENTRIES:
            return
        self.entries.sort(
            key=lambda entry: (entry.weight, entry.evidence), reverse=True
        )
        self.entries = self.entries[:_MAX_ENTRIES]

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
                    "group_id": entry.group_id,
                    "cross_group": bool(entry.cross_group),
                    "query": entry.query,
                    "created_at": entry.created_at,
                    "last_seen": entry.last_seen,
                    "forget_stage": entry.forget_stage,
                    "last_used_at": entry.last_used_at,
                    "use_count": entry.use_count,
                    "forget_updated_at": entry.forget_updated_at,
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
            source = str(item.get("source") or "manual")
            # 旧版把"看过视频"当成"喜欢"，属于噪音，不再载入。
            if source == "watch":
                continue
            now = time.time()
            kind = str(item.get("kind") or "like")
            legacy_manual_jargon = kind == "jargon" and (
                source in ("jargon", "manual") or source == "group:manual"
            )
            group_id = str(item.get("group_id") or "").strip()
            if group_id == "__manual__":
                group_id = ""
            if not group_id and source.startswith("group:"):
                raw = source[len("group:") :].strip()
                if "GroupMessage:" in raw:
                    raw = raw.rsplit("GroupMessage:", 1)[-1]
                if raw and raw != "manual":
                    group_id = raw
            raw_cross = item.get("cross_group")
            if legacy_manual_jargon:
                # Older builds may have written cross_group=False for a
                # manual run; manual runs are general by policy.
                group_id = ""
                cross_group = True
            elif raw_cross is None:
                cross_group = not _looks_group_local(text)
            else:
                cross_group = bool(raw_cross)
            if kind == "jargon" and cross_group and " = " in text:
                head, _, tail = text.partition(" = ")
                text = f"{head} = {_GENERAL_SENSE_PREFIX.sub('', tail)}"
            try:
                weight = float(item.get("weight") or 0.5)
                confidence = float(item.get("confidence") or 0.5)
                evidence = int(item.get("evidence") or 1)
                created_at = float(item.get("created_at") or now)
                last_seen = float(item.get("last_seen") or now)
                forget_stage = int(item.get("forget_stage") or 0)
                last_used_at = float(item.get("last_used_at") or 0.0)
                use_count = int(item.get("use_count") or 0)
                forget_updated_at = float(item.get("forget_updated_at") or 0.0)
            except (TypeError, ValueError):
                continue
            self.entries.append(
                NotebookEntry(
                    text=text,
                    kind=kind,
                    weight=weight,
                    confidence=confidence,
                    evidence=evidence,
                    source=source,
                    group_id=group_id,
                    cross_group=cross_group,
                    query=str(item.get("query") or ""),
                    created_at=created_at,
                    last_seen=last_seen,
                    forget_stage=forget_stage,
                    last_used_at=last_used_at,
                    use_count=use_count,
                    forget_updated_at=forget_updated_at,
                )
            )


def _bigrams(text: str) -> set[str]:
    """Split text into shared-comparable character pairs.

    Args:
        text: Any text.

    Returns:
        The set of adjacent Chinese character pairs plus lower-cased Latin
        tokens, so English names can match too.
    """
    pairs: set[str] = set()
    raw = str(text or "")
    for chunk in _CJK_PATTERN.findall(raw):
        for index in range(len(chunk) - 1):
            pair = chunk[index : index + 2]
            if pair[0] in _STOP_CHARS and pair[1] in _STOP_CHARS:
                continue
            pairs.add(pair)
    for token in re.findall(r"[A-Za-z0-9_]{2,}", raw):
        pairs.add(token.lower())
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
