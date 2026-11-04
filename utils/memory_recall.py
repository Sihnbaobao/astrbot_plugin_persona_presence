"""Cue-based recall for background knowledge.

Background notes such as other-group chatter or what the persona watched should
surface only when the current message actually points at them. Similarity
alone is not enough: two sentences can look alike and mean different things,
so a candidate must clear a score gate plus a lexical anchor, or a high score
gate alone, and it can be double checked by a cheap model call.

The module is intentionally free of storage concerns; callers hand in plain
items and get back the ones worth showing.
"""

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api.all import *

# Chinese runs and alphanumeric tokens longer than one character are the only
# anchors that count; single characters are far too common to prove anything.
_ANCHOR_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}")
_STOP_ANCHORS = {
    "这个",
    "那个",
    "什么",
    "怎么",
    "就是",
    "可以",
    "我们",
    "你们",
    "他们",
    "现在",
    "今天",
    "刚才",
    "一个",
    "没有",
    "知道",
}


@dataclass
class MemoryItem:
    """One piece of background the persona may remember.

    Attributes:
        source: Origin bucket, such as "group" or "life".
        label: Human readable origin, such as a group name or a video title.
        text: The remembered content.
        timestamp: When it happened.
        importance: Relative weight in the 0-1 range.
        extra: Optional formatted lines rendered under the headline.
    """

    source: str
    label: str
    text: str
    timestamp: float
    importance: float = 0.5
    extra: list[str] = field(default_factory=list)


class RecallGate:
    """Scores and filters candidate memories for one message.

    Args:
        context: AstrBot star context, used for embeddings and verification.
        embedding_provider_id: Preferred embedding provider id; empty picks the
            first available one.
        mid_threshold: Score needed when a lexical anchor also matches.
        high_threshold: Score that stands on its own without an anchor.
        decay_hours: Half-life style time constant for recency weighting.
        verify_enabled: Whether to spend one short model call on a candidate.
        verify_provider_id: Chat provider for verification; empty uses default.
    """

    def __init__(
        self,
        context: Any,
        *,
        embedding_provider_id: str = "",
        mid_threshold: float = 0.45,
        high_threshold: float = 0.62,
        decay_hours: float = 12.0,
        verify_enabled: bool = True,
        verify_provider_id: str = "",
    ) -> None:
        self.context = context
        self.embedding_provider_id = embedding_provider_id
        self.mid_threshold = float(mid_threshold)
        self.high_threshold = float(high_threshold)
        self.decay_seconds = max(1.0, float(decay_hours)) * 3600
        self.verify_enabled = bool(verify_enabled)
        self.verify_provider_id = verify_provider_id
        self._embedding_cache: dict[str, list[float]] = {}

    async def recall(
        self, query: str, items: list[MemoryItem], *, top_k: int = 3
    ) -> list[MemoryItem]:
        """Select the memories the current message actually points at.

        Args:
            query: Current message text.
            items: Candidate memories.
            top_k: Maximum memories returned.

        Returns:
            Relevant memories, best first. Usually empty.
        """
        query = (query or "").strip()
        if not query or not items:
            return []

        query_vector = await self._embed(query)
        scored: list[tuple[float, float, MemoryItem]] = []
        now = time.time()
        for item in items:
            similarity = 0.0
            if query_vector:
                item_vector = await self._embed(item.text)
                if item_vector:
                    similarity = self._cosine(query_vector, item_vector)
            if similarity < self.mid_threshold:
                continue
            recency = math.exp(-max(0.0, now - item.timestamp) / self.decay_seconds)
            score = similarity * recency * (0.5 + 0.5 * item.importance)
            anchored = self.has_anchor(query, f"{item.label} {item.text}")
            if similarity >= self.high_threshold:
                scored.append((score, similarity, item))
            elif anchored:
                scored.append((score, similarity, item))

        if not scored:
            return []

        scored.sort(key=lambda entry: entry[0], reverse=True)
        picked = [entry[2] for entry in scored[: max(1, top_k)]]

        if not self.verify_enabled:
            return picked

        verified: list[MemoryItem] = []
        for item in picked:
            if await self._verify(query, item):
                verified.append(item)
        return verified

    @staticmethod
    def has_anchor(query: str, text: str) -> bool:
        """Check whether two strings share a usable lexical anchor.

        Args:
            query: Current message text.
            text: Candidate memory text.

        Returns:
            True when a shared anchor of at least two characters exists.
        """
        query_tokens = {
            token for token in _ANCHOR_PATTERN.findall(query or "")
            if token not in _STOP_ANCHORS
        }
        if not query_tokens:
            return False
        for token in _ANCHOR_PATTERN.findall(text or ""):
            if token in _STOP_ANCHORS:
                continue
            if token in query_tokens:
                return True
            for candidate in query_tokens:
                if len(token) >= 2 and (token in candidate or candidate in token):
                    return True
        return False

    async def _verify(self, query: str, item: MemoryItem) -> bool:
        """Ask a cheap model whether the memory really matches the message.

        Args:
            query: Current message text.
            item: Candidate memory.

        Returns:
            True when the model agrees they are about the same thing.
        """
        prompt = (
            "判断两段话是否在说同一件事。只回答 yes 或 no。\n"
            f"背景：{item.label}｜{item.text[:120]}\n"
            f"当前消息：{query[:120]}\n"
            "如果只是用词相近、话题不同，回答 no。"
        )
        try:
            kwargs = {"prompt": prompt, "max_tokens": 4}
            if self.verify_provider_id:
                kwargs["chat_provider_id"] = self.verify_provider_id
            response = await self.context.llm_generate(**kwargs)
        except Exception as exc:
            logger.debug(f"[Recall] verification unavailable: {exc}")
            return False

        text = str(getattr(response, "completion_text", "") or "").strip().lower()
        return text.startswith("y") or "是" in text[:4]

    async def _embed(self, text: str) -> list[float] | None:
        """Embed text with the configured provider, caching the result.

        Args:
            text: Text to embed.

        Returns:
            The embedding vector, or None when unavailable.
        """
        cached = self._embedding_cache.get(text)
        if cached is not None:
            return cached
        try:
            provider = self._pick_embedding_provider()
            if provider is None:
                return None
            vector = await provider.get_embedding(text[:500])
        except Exception as exc:
            logger.debug(f"[Recall] embedding unavailable: {exc}")
            return None
        if not vector:
            return None
        if len(self._embedding_cache) > 512:
            self._embedding_cache.clear()
        self._embedding_cache[text] = vector
        return vector

    def _pick_embedding_provider(self):
        """Select an embedding provider.

        Returns:
            The preferred or first available provider, else None.
        """
        try:
            providers = self.context.get_all_embedding_providers()
        except Exception:
            return None
        if not providers:
            return None
        if self.embedding_provider_id:
            for provider in providers:
                try:
                    if provider.meta().id == self.embedding_provider_id:
                        return provider
                except Exception:
                    continue
        return providers[0]

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        """Compute cosine similarity between two vectors.

        Args:
            left: First vector.
            right: Second vector.

        Returns:
            Similarity in the -1..1 range, or 0 when a vector is empty.
        """
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if not left_norm or not right_norm:
            return 0.0
        return dot / (left_norm * right_norm)
