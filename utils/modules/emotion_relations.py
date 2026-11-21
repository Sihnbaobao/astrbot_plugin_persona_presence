"""Long-lived relationship and group attitudes for the emotion DLC.

Short-term emotions decay in minutes. This ledger deliberately does not: a
person can become less welcome over days, and a group can become somewhere the
persona no longer wants to speak. It only updates on direct, significant turns;
casual group chatter never moves it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

_STATE_VERSION = 1
_SEVERE_RE = re.compile(
    r"去死|废物|垃圾|恶心|滚(?:开|蛋|吧|啊)|闭嘴|傻|蠢|有病|神经|"
    r"你算|凭什么|少来|(?:你|别|少)装|装(?:什么|模作样|给谁看|傻|死)"
)
_CONFLICT_RE = re.compile(r"吵|争|怼|凭什么|你算|不服|来啊")
_PRAISE_RE = re.compile(
    r"(?:你|璃月).{0,6}(?:可爱|好看|漂亮|厉害|聪明|温柔|有趣|牛|强)"
    r"|(?<!不)(?:喜欢|爱)(?:你|璃月)|谢谢(?:你|璃月)?"
)
_APOLOGY_RE = re.compile(r"对不起|抱歉|别生气|我错|抱抱|安慰|没事")


@dataclass
class _Relation:
    warmth: float = 0.0
    trust: float = 0.2
    resentment: float = 0.0
    bottomed_out: bool = False
    cause: str = ""
    name: str = ""
    negative_events: int = 0
    positive_events: int = 0
    last_seen_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        self.warmth = max(-1.0, min(1.0, float(self.warmth)))
        self.trust = max(0.0, min(1.0, float(self.trust)))
        self.resentment = max(0.0, min(1.0, float(self.resentment)))


@dataclass
class _GroupAttitude:
    comfort: float = 0.0
    withdrawal: float = 0.0
    cause: str = ""
    name: str = ""
    negative_events: int = 0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        self.comfort = max(-1.0, min(1.0, float(self.comfort)))
        self.withdrawal = max(0.0, min(1.0, float(self.withdrawal)))


class RelationLedger:
    """Persist per-user and per-group attitudes across conversations.

    Args:
        path: JSON file for the ledger.
        sensitivity: Multiplier for event-induced changes.
    """

    def __init__(
        self,
        path: "Path | str | None" = None,
        *,
        sensitivity: float = 0.6,
    ) -> None:
        self.path = Path(path) if path else None
        self.sensitivity = max(0.1, min(2.0, float(sensitivity)))
        self.relations: dict[str, dict[str, _Relation]] = {}
        self.groups: dict[str, _GroupAttitude] = {}
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
        for umo, users in (raw.get("relations") or {}).items():
            if not isinstance(users, dict):
                continue
            bucket = self.relations.setdefault(str(umo), {})
            for user_id, item in users.items():
                if not isinstance(item, dict):
                    continue
                bucket[str(user_id)] = _Relation(
                    warmth=float(item.get("warmth", 0.0)),
                    trust=float(item.get("trust", 0.2)),
                    resentment=float(item.get("resentment", 0.0)),
                    bottomed_out=bool(item.get("bottomed_out", False)),
                    cause=str(item.get("cause") or "")[:120],
                    name=str(item.get("name") or "")[:40],
                    negative_events=int(item.get("negative_events", 0) or 0),
                    positive_events=int(item.get("positive_events", 0) or 0),
                    last_seen_at=float(item.get("last_seen_at") or 0.0),
                    updated_at=float(item.get("updated_at") or 0.0),
                )
        for umo, item in (raw.get("groups") or {}).items():
            if not isinstance(item, dict):
                continue
            self.groups[str(umo)] = _GroupAttitude(
                comfort=float(item.get("comfort", 0.0)),
                withdrawal=float(item.get("withdrawal", 0.0)),
                cause=str(item.get("cause") or "")[:120],
                name=str(item.get("name") or "")[:40],
                negative_events=int(item.get("negative_events", 0) or 0),
                updated_at=float(item.get("updated_at") or 0.0),
            )
        self._unify_people()

    def save(self) -> None:
        if self.path is None:
            return
        payload: dict[str, Any] = {
            "version": _STATE_VERSION,
            "relations": {
                umo: {
                    user_id: {
                        "warmth": rel.warmth,
                        "trust": rel.trust,
                        "resentment": rel.resentment,
                        "bottomed_out": rel.bottomed_out,
                        "cause": rel.cause,
                        "name": rel.name,
                        "negative_events": rel.negative_events,
                        "positive_events": rel.positive_events,
                        "last_seen_at": rel.last_seen_at,
                        "updated_at": rel.updated_at,
                    }
                    for user_id, rel in users.items()
                    if rel.warmth != 0.0
                    or rel.trust != 0.2
                    or rel.resentment > 0.01
                    or rel.bottomed_out
                }
                for umo, users in self.relations.items()
            },
            "groups": {
                umo: {
                    "comfort": item.comfort,
                    "withdrawal": item.withdrawal,
                    "cause": item.cause,
                    "name": item.name,
                    "negative_events": item.negative_events,
                    "updated_at": item.updated_at,
                }
                for umo, item in self.groups.items()
                if item.comfort != 0.0 or item.withdrawal > 0.01
            },
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
            logger.debug(f"[关系] 写入失败: {exc}")

    def _clamp_relation(self, rel: _Relation) -> None:
        rel.warmth = max(-1.0, min(1.0, rel.warmth))
        rel.trust = max(0.0, min(1.0, rel.trust))
        rel.resentment = max(0.0, min(1.0, rel.resentment))

    def _unify_people(self) -> None:
        """Point every context at the same relation object for one user.

        A person can talk in a group and in private; their long-term attitude
        must not fork into two different people. Short-term emotion remains
        per conversation, but the ledger row is shared.
        """
        by_user: dict[str, list[_Relation]] = {}
        for bucket in self.relations.values():
            for user_id, rel in bucket.items():
                by_user.setdefault(str(user_id), []).append(rel)
        for user_id, relations in by_user.items():
            if len(relations) < 2:
                continue
            canonical = max(
                relations,
                key=lambda rel: (
                    rel.updated_at,
                    rel.bottomed_out,
                    rel.resentment,
                    abs(rel.warmth),
                    rel.trust,
                ),
            )
            for bucket in self.relations.values():
                if user_id in bucket:
                    bucket[user_id] = canonical

    def _find_relation(self, user_id: str) -> _Relation | None:
        """Return the best existing relation for a user across contexts."""
        found = [
            rel
            for bucket in self.relations.values()
            for uid, rel in bucket.items()
            if str(uid) == str(user_id)
        ]
        if not found:
            return None
        return max(
            found,
            key=lambda rel: (
                rel.updated_at,
                rel.bottomed_out,
                rel.resentment,
                abs(rel.warmth),
                rel.trust,
            ),
        )

    def appraise(
        self,
        umo: str,
        user_id: str,
        text: str,
        *,
        is_private: bool,
        group_id: str = "",
        sender_name: str = "",
        group_name: str = "",
    ) -> bool:
        """Update long-lived attitudes from one direct event.

        Args:
            umo: Conversation origin.
            user_id: Sender identifier.
            text: Message text.
            is_private: Whether this is a private chat.
            group_id: Group id for group-level attitude.

        Returns:
            True when any attitude changed.
        """
        if not text:
            return False
        now = time.time()
        bucket = self.relations.setdefault(umo, {})
        relation = bucket.get(user_id)
        created = False
        if relation is None:
            relation = self._find_relation(user_id)
            if relation is None:
                relation = _Relation()
                bucket[user_id] = relation
                created = True
        changed = False
        # A person's long-term attitude follows them across group and private
        # chats even though short-term moods stay in their own conversation.
        if not created:
            for other_bucket in self.relations.values():
                if user_id in other_bucket and other_bucket[user_id] is not relation:
                    other_bucket[user_id] = relation
                    changed = True
            if bucket.get(user_id) is not relation:
                bucket[user_id] = relation
                changed = True
        if sender_name:
            relation.name = sender_name[:40]
        if not created:
            last_seen = float(getattr(relation, "last_seen_at", 0.0) or 0.0)
            if last_seen <= 0 or now - last_seen > 60:
                relation.last_seen_at = now
                changed = True
        severe = bool(_SEVERE_RE.search(text))
        conflict = bool(_CONFLICT_RE.search(text))
        praise = bool(_PRAISE_RE.search(text))
        apology = bool(_APOLOGY_RE.search(text))

        if severe:
            relation.resentment += 0.18 * self.sensitivity
            relation.warmth -= 0.08 * self.sensitivity
            relation.trust -= 0.03 * self.sensitivity
            relation.cause = text[:60]
            relation.negative_events += 1
            changed = True
        if conflict:
            relation.resentment += 0.06 * self.sensitivity
            relation.warmth -= 0.02 * self.sensitivity
            relation.cause = relation.cause or text[:60]
            relation.negative_events += 1
            changed = True
        if praise:
            relation.warmth += 0.04 * self.sensitivity
            relation.trust += 0.02 * self.sensitivity
            if not relation.bottomed_out:
                relation.resentment -= 0.02 * self.sensitivity
            relation.cause = relation.cause or text[:60]
            relation.positive_events += 1
            changed = True
        if apology:
            if not relation.bottomed_out:
                relation.resentment -= 0.10 * self.sensitivity
                relation.warmth += 0.02 * self.sensitivity
            relation.cause = relation.cause or text[:60]
            relation.positive_events += 1
            changed = True

        if relation.resentment >= 0.85:
            relation.bottomed_out = True
        self._clamp_relation(relation)
        if relation.bottomed_out:
            relation.resentment = max(0.85, relation.resentment)
        relation.updated_at = now if changed else relation.updated_at
        if created and not changed:
            bucket.pop(user_id, None)
            if not bucket:
                self.relations.pop(umo, None)

        if not is_private and group_id:
            group = self.groups.setdefault(group_id, _GroupAttitude())
            if group_name:
                group.name = group_name[:40]
            if severe:
                group.withdrawal += 0.10 * self.sensitivity
                group.comfort -= 0.06 * self.sensitivity
                group.cause = text[:60]
                group.negative_events += 1
                changed = True
            elif conflict:
                group.withdrawal += 0.04 * self.sensitivity
                group.comfort -= 0.02 * self.sensitivity
                group.cause = group.cause or text[:60]
                group.negative_events += 1
                changed = True
            elif praise:
                group.comfort += 0.02 * self.sensitivity
                group.cause = group.cause or text[:60]
                changed = True
            group.withdrawal = max(0.0, min(1.0, group.withdrawal))
            group.comfort = max(-1.0, min(1.0, group.comfort))
            group.updated_at = now if changed else group.updated_at

        return changed

    def block_for(
        self, umo: str, user_id: str, is_private: bool, group_id: str = ""
    ) -> str:
        """Render short relationship and group notes for the reply prompt.

        Args:
            umo: Conversation origin.
            user_id: Sender identifier.
            is_private: Whether this is a private chat.

        Returns:
            A prompt block, or an empty string.
        """
        blocks: list[str] = []
        relation = self.relations.get(umo, {}).get(user_id)
        if relation is not None:
            if relation.bottomed_out:
                blocks.append("[对这个人的感觉]\n已经不想再给这个人好脸色了。")
            elif relation.resentment >= 0.55:
                blocks.append("[对这个人的感觉]\n对这个人有点戒心，不太想多解释。")
            elif relation.warmth >= 0.35:
                blocks.append("[对这个人的感觉]\n还算愿意接这个人的话。")
        if not is_private:
            group = self.groups.get(group_id)
            if group is not None and group.withdrawal >= 0.55:
                blocks.append("[对这个群的感觉]\n有点待不住，不太想在这里开口。")
        return "\n\n".join(blocks)

    def should_withdraw(self, umo: str) -> bool:
        """Tell whether the group attitude should suppress proactive speech.

        Args:
            umo: Conversation origin.

        Returns:
            True when the persona no longer wants to initiate in this group.
        """
        group = self.groups.get(umo)
        return bool(group is not None and group.withdrawal >= 0.70)
