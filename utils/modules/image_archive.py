"""A small, bounded image archive with model-assisted curation.

The archive keeps originals on disk and metadata in JSON. It is deliberately
separate from the chat transcript: an image can be recalled by its tags or
description long after the platform URL expired. All retrieval is scoped to the
conversation that originally received the image.
"""

import hashlib
import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("astrbot")

_MAX_ENTRIES = 2000

# Tags that make an archived image unsuitable for automatic sending.
_SEND_BLOCK_TAGS = (
    "r18",
    "色情",
    "性暗示",
    "露骨",
    "血腥",
    "暴力",
    "证件",
    "二维码",
    "聊天记录",
    "截图",
    "真人照片",
)


def send_block_reason(tags: list | None) -> str:
    """Return the first tag that blocks sending, or an empty string.

    Args:
        tags: Archive entry tags.

    Returns:
        The blocking tag, or an empty string when the image is sendable.
    """
    text = " ".join(str(tag).lower() for tag in (tags or []))
    for tag in _SEND_BLOCK_TAGS:
        if tag in text:
            return tag
    return ""


@dataclass
class ArchiveEntry:
    """One stored image and the context needed to find it again.

    Attributes:
        id: Stable archive id.
        file: Relative path under the archive directory.
        sha: Content hash used for de-duplication.
        size: File size in bytes.
        chat_id: Conversation that owns the image.
        umo: Unified message origin for persona resolution.
        is_group: Whether the source was a group.
        sender_id: Original sender id.
        sender_name: Original sender display name.
        message_id: Platform message id, when available.
        created_at: Unix timestamp when the image was received.
        description: Short model-written description.
        tags: Searchable tags.
        score: Model interest score, 0 to 1.
        favorite: Whether the image is worth keeping for review.
        sendable: Whether the user allowed this image to be sent as a meme.
        analysis_pending: Whether vision analysis still needs a retry.
        analysis_attempts: Number of failed analysis attempts.
        analysis_last_try: Last analysis attempt timestamp.
        analysis_error: Last analysis failure reason.
        status: active or deleted.
        review_at: Next "do I still like this?" time.
        reviewed_at: Last review time.
    """

    id: str
    file: str
    sha: str
    size: int
    chat_id: str
    is_group: bool
    sender_id: str
    sender_name: str
    message_id: str
    created_at: float
    chat_ids: list = field(default_factory=list)
    umo: str = ""
    description: str = ""
    tags: list = field(default_factory=list)
    score: float = 0.0
    favorite: bool = False
    sendable: bool = False
    analysis_pending: bool = False
    analysis_attempts: int = 0
    analysis_last_try: float = 0.0
    analysis_error: str = ""
    status: str = "active"
    review_at: float = 0.0
    reviewed_at: float = 0.0

    def __post_init__(self) -> None:
        """Keep the legacy single chat id and the chat binding list in sync."""
        chats = [str(item) for item in (self.chat_ids or []) if str(item)]
        primary = str(self.chat_id or "")
        if primary and primary not in chats:
            chats.insert(0, primary)
        self.chat_ids = chats


class ImageArchive:
    """Store, retrieve, prune and review a bounded image collection.

    Args:
        data_dir: Plugin data directory.
        enabled: Whether the archive is active.
        max_mb: Hard disk budget in megabytes.
        ordinary_days: Days before an ordinary image is deleted.
        review_days: Days before a favorite is reviewed again.
        retrieval_days: Default look-back window for recall.
        min_score: Score at or above which an image is treated as a favorite.
    """

    def __init__(
        self,
        data_dir: "Path | str",
        *,
        enabled: bool = True,
        max_mb: int = 500,
        ordinary_days: int = 7,
        review_days: int = 30,
        retrieval_days: int = 30,
        min_score: float = 0.6,
    ) -> None:
        self.root = Path(data_dir) / "images"
        self.meta_path = Path(data_dir) / "image_archive.json"
        self.enabled = bool(enabled)
        self.max_bytes = max(1, int(max_mb)) * 1024 * 1024
        self.ordinary_days = max(1, int(ordinary_days))
        self.review_days = max(1, int(review_days))
        self.retrieval_days = max(1, int(retrieval_days))
        self.min_score = max(0.0, min(1.0, float(min_score)))
        self.entries: list = []
        self._load()
        self._merge_duplicate_entries()
        self.prune()

    def ingest(
        self,
        source: "Path | str",
        *,
        chat_id: str,
        is_group: bool,
        sender_id: str = "",
        sender_name: str = "",
        message_id: str = "",
        umo: str = "",
        description: str = "",
        tags: list | None = None,
        score: float = 0.0,
        favorite: bool | None = None,
        now: float | None = None,
    ) -> "ArchiveEntry | None":
        """Copy one image into the archive and record its metadata.

        Args:
            source: Existing image file path.
            chat_id: Conversation that owns the image.
            is_group: Whether this came from a group.
            sender_id: Original sender id.
            sender_name: Original sender name.
            message_id: Platform message id.
            umo: Unified message origin for persona resolution.
            description: Model-written description.
            tags: Searchable tags.
            score: Interest score from the model.
            favorite: Explicit keep decision; defaults to score >= min_score.
            now: Unix timestamp; defaults to now.

        Returns:
            The new entry, an existing entry for the same image, or None.
        """
        if not self.enabled:
            return None
        path = Path(source)
        if not path.is_file():
            return None
        try:
            got = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
        for entry in self.entries:
            if entry.status == "active" and entry.sha == got:
                self.bind_chat(entry, str(chat_id))
                return entry
        if path.stat().st_size > self.max_bytes:
            return None

        suffix = path.suffix.lower()[:8] or ".img"
        relative = Path(got[:2]) / f"{got}{suffix}"
        target = self.root / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(path, target)
        except OSError as exc:
            logger.debug(f"[图片库] 复制失败: {exc}")
            return None

        stamp = float(now if now is not None else time.time())
        keep = bool(score >= self.min_score) if favorite is None else bool(favorite)
        entry = ArchiveEntry(
            id=got[:16],
            file=str(relative),
            sha=got,
            size=target.stat().st_size,
            chat_id=str(chat_id),
            chat_ids=[str(chat_id)],
            is_group=bool(is_group),
            sender_id=str(sender_id),
            sender_name=str(sender_name)[:80],
            message_id=str(message_id),
            created_at=stamp,
            umo=str(umo),
            description=str(description or "").strip()[:500],
            tags=[str(tag).strip()[:40] for tag in (tags or []) if str(tag).strip()][
                :12
            ],
            score=max(0.0, min(1.0, float(score))),
            favorite=keep,
            analysis_pending=not str(description or "").strip(),
            analysis_attempts=0,
            analysis_last_try=0.0,
            review_at=stamp + self.review_days * 86400 if keep else 0.0,
        )
        self.entries.append(entry)
        self._enforce_capacity()
        self._save()
        logger.info(
            f"[图片库] 收纳 {entry.id} ({entry.size // 1024}KB, "
            f"{'保留' if keep else '普通'}) chat={chat_id}"
        )
        return entry

    @staticmethod
    def matches_chat(stored: str, requested: str) -> bool:
        """Compare archive keys, tolerating legacy raw ids."""
        left = str(stored or "")
        right = str(requested or "")
        if not left or not right:
            return False
        return left == right or right.endswith("_" + left) or left.endswith("_" + right)

    @classmethod
    def matches_entry(cls, entry: "ArchiveEntry", requested: str) -> bool:
        """Tell whether an entry was received in the requested conversation."""
        chats = list(entry.chat_ids or []) + ([entry.chat_id] if entry.chat_id else [])
        return any(cls.matches_chat(chat, requested) for chat in chats)

    def bind_chat(self, entry: "ArchiveEntry", chat_id: str) -> bool:
        """Bind an existing image to another conversation without duplicating it."""
        if not chat_id or self.matches_entry(entry, chat_id):
            return False
        entry.chat_ids.append(str(chat_id))
        self._save()
        return True

    def find_by_hash(self, source: "Path | str") -> "ArchiveEntry | None":
        """Return the active entry for the same image bytes, in any chat."""
        try:
            got = hashlib.sha256(Path(source).read_bytes()).hexdigest()
        except OSError:
            return None
        for entry in self.entries:
            if entry.status == "active" and entry.sha == got:
                return entry
        return None

    def find_duplicate(
        self, chat_id: str, source: "Path | str"
    ) -> "ArchiveEntry | None":
        """Return an existing entry for the same image bytes in any chat.

        Args:
            chat_id: Conversation scope; kept for call compatibility.
            source: Candidate image file.

        Returns:
            The matching entry, or None.
        """
        return self.find_by_hash(source)

    def pending_analysis(
        self,
        limit: int = 3,
        *,
        retry_seconds: int = 600,
        max_attempts: int = 1,
        now: float | None = None,
    ) -> list:
        """Return images whose vision analysis needs another attempt."""
        current = float(now if now is not None else time.time())
        due = [
            entry
            for entry in self.entries
            if entry.status == "active"
            and entry.analysis_pending
            and entry.analysis_attempts < max(0, int(max_attempts))
            and (
                not entry.analysis_last_try
                or current - entry.analysis_last_try >= max(0, int(retry_seconds))
            )
        ]
        due.sort(key=lambda entry: (bool(entry.analysis_last_try), entry.created_at))
        return due[: max(1, int(limit))]

    def mark_analysis_failed(
        self,
        entry_id: str,
        *,
        reason: str = "",
        now: float | None = None,
    ) -> int:
        """Record one failed analysis and keep the image for manual retry."""
        stamp = float(now if now is not None else time.time())
        for entry in self.entries:
            if entry.id != entry_id or entry.status != "active":
                continue
            entry.analysis_pending = True
            entry.analysis_attempts += 1
            entry.analysis_last_try = stamp
            entry.analysis_error = str(reason or "")[:300]
            self._save()
            return entry.analysis_attempts
        return 0

    def update_analysis(
        self,
        entry_id: str,
        *,
        description: str,
        tags: list | None = None,
        score: float = 0.0,
        favorite: bool | None = None,
        now: float | None = None,
    ) -> bool:
        """Apply a successful vision analysis and update curation state."""
        stamp = float(now if now is not None else time.time())
        for entry in self.entries:
            if entry.id != entry_id or entry.status != "active":
                continue
            entry.description = str(description or "").strip()[:500]
            if tags is not None:
                entry.tags = [
                    str(tag).strip()[:40] for tag in tags if str(tag).strip()
                ][:12]
            entry.score = max(0.0, min(1.0, float(score)))
            if favorite is None:
                favorite = entry.score >= self.min_score
            entry.favorite = bool(favorite)
            entry.analysis_pending = False
            entry.analysis_attempts = 0
            entry.analysis_last_try = 0.0
            entry.analysis_error = ""
            entry.review_at = (
                stamp + self.review_days * 86400 if entry.favorite else 0.0
            )
            self._save()
            return True
        return False

    def search(
        self,
        chat_id: str,
        query: str = "",
        *,
        days: int | None = None,
        limit: int = 3,
        now: float | None = None,
    ) -> list:
        """Find active images in one conversation.

        Args:
            chat_id: Conversation scope.
            query: Optional keywords from the user's request.
            days: Look-back window; defaults to retrieval_days.
            limit: Maximum results.
            now: Unix timestamp; defaults to now.

        Returns:
            Matching entries, favorites and recent images first.
        """
        if not self.enabled:
            return []
        current = float(now if now is not None else time.time())
        window = max(1, int(days if days is not None else self.retrieval_days))
        cutoff = current - window * 86400
        tokens = {
            token
            for token in str(query or "").replace("，", " ").replace("。", " ").split()
            if len(token) >= 2
        }
        found: list = []
        for entry in self.entries:
            if entry.status != "active" or not self.matches_entry(entry, str(chat_id)):
                continue
            if entry.created_at < cutoff:
                continue
            haystack = f"{entry.description} {' '.join(entry.tags)}"
            hits = sum(1 for token in tokens if token in haystack)
            found.append((hits, entry.score, entry))
        found.sort(key=lambda item: (-item[0], -item[1], -item[2].created_at))
        return [entry for _hits, _score, entry in found[: max(1, int(limit))]]

    def set_sendable(self, entry_id: str, sendable: bool) -> bool:
        """Allow or forbid sending one archived image as a meme.

        Args:
            entry_id: Archive id.
            sendable: New manual send permission.

        Returns:
            True when the entry exists and the new state is allowed.
        """
        for entry in self.entries:
            if entry.id != entry_id or entry.status != "active":
                continue
            entry.sendable = bool(sendable)
            self._save()
            return True
        return False

    def due_for_review(self, limit: int = 3, now: float | None = None) -> list:
        """Return favorites whose review time has arrived."""
        current = float(now if now is not None else time.time())
        due = [
            entry
            for entry in self.entries
            if entry.status == "active"
            and entry.favorite
            and entry.review_at
            and entry.review_at <= current
        ]
        due.sort(key=lambda entry: entry.review_at)
        return due[: max(1, int(limit))]

    def review(self, entry_id: str, keep: bool, *, now: float | None = None) -> bool:
        """Apply one model review decision.

        Args:
            entry_id: Archive id.
            keep: Whether the model still wants it.
            now: Unix timestamp; defaults to now.

        Returns:
            True when the entry was found and updated.
        """
        stamp = float(now if now is not None else time.time())
        for entry in self.entries:
            if entry.id != entry_id or entry.status != "active":
                continue
            if keep:
                entry.favorite = True
                entry.review_at = stamp + self.review_days * 86400
                entry.reviewed_at = stamp
            else:
                self._delete_entry(entry)
            self._save()
            return True
        return False

    def remove(self, entry_id: str) -> bool:
        """Remove one image by archive id."""
        for entry in self.entries:
            if entry.id == entry_id and entry.status == "active":
                self._delete_entry(entry)
                self._save()
                return True
        return False

    def prune(self, now: float | None = None) -> int:
        """Delete expired ordinary images and enforce the disk budget."""
        if not self.enabled:
            return 0
        current = float(now if now is not None else time.time())
        before = len(self.entries)
        kept = []
        for entry in self.entries:
            if entry.status != "active":
                continue
            if (
                not entry.favorite
                and current - entry.created_at > self.ordinary_days * 86400
            ):
                self._delete_file(entry)
                continue
            kept.append(entry)
        self.entries = kept
        self._enforce_capacity()
        if len(self.entries) != before:
            self._save()
        return before - len(self.entries)

    def stats(self) -> dict:
        """Return storage and curation counts for diagnostics."""
        active = [entry for entry in self.entries if entry.status == "active"]
        return {
            "enabled": self.enabled,
            "count": len(active),
            "favorites": sum(1 for entry in active if entry.favorite),
            "bytes": sum(entry.size for entry in active),
            "max_bytes": self.max_bytes,
        }

    def thumb_data_url(self, entry: "ArchiveEntry", max_size: int = 360) -> str:
        """Build a cached JPEG thumbnail as a data URL.

        Args:
            entry: Archive entry to preview.
            max_size: Maximum width/height in pixels.

        Returns:
            A data URL, or an empty string when thumbnailing fails.
        """
        import base64
        from io import BytesIO

        thumb_dir = self.root / ".thumbs"
        thumb_path = thumb_dir / f"{entry.id}.jpg"
        try:
            if thumb_path.is_file():
                return (
                    "data:image/jpeg;base64,"
                    + base64.b64encode(thumb_path.read_bytes()).decode()
                )
            from PIL import Image as PILImage

            source = self.root / entry.file
            if not source.is_file():
                return ""
            with PILImage.open(source) as image:
                image = image.convert("RGB")
                image.thumbnail((max_size, max_size))
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=78)
            data = buffer.getvalue()
            thumb_dir.mkdir(parents=True, exist_ok=True)
            thumb_path.write_bytes(data)
            return "data:image/jpeg;base64," + base64.b64encode(data).decode()
        except Exception as exc:
            logger.debug(f"[图片库] 缩略图失败: {exc}")
            return ""

    def preview_data_url(self, entry: "ArchiveEntry", max_size: int = 1600) -> str:
        """Build a cached large JPEG preview for the lightbox."""
        import base64
        from io import BytesIO

        thumb_dir = self.root / ".thumbs"
        thumb_path = thumb_dir / f"{entry.id}-preview.jpg"
        try:
            if thumb_path.is_file():
                return (
                    "data:image/jpeg;base64,"
                    + base64.b64encode(thumb_path.read_bytes()).decode()
                )
            from PIL import Image as PILImage

            source = self.root / entry.file
            if not source.is_file():
                return ""
            with PILImage.open(source) as image:
                image = image.convert("RGB")
                image.thumbnail((max_size, max_size))
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=84)
            data = buffer.getvalue()
            thumb_dir.mkdir(parents=True, exist_ok=True)
            thumb_path.write_bytes(data)
            return "data:image/jpeg;base64," + base64.b64encode(data).decode()
        except Exception as exc:
            logger.debug(f"[图片库] 大图预览失败: {exc}")
            return ""

    def _delete_entry(self, entry: "ArchiveEntry") -> None:
        entry.status = "deleted"
        self._delete_file(entry)

    def _delete_file(self, entry: "ArchiveEntry") -> None:
        # A file may be shared by another chat's entry; only remove it when no
        # other active entry references the same relative path.
        if any(
            other is not entry and other.status == "active" and other.file == entry.file
            for other in self.entries
        ):
            return
        path = self.root / entry.file
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug(f"[图片库] 删除失败: {exc}")

    def _enforce_capacity(self) -> None:
        active = [entry for entry in self.entries if entry.status == "active"]
        total = sum(entry.size for entry in active)
        if total <= self.max_bytes:
            return
        # Ordinary images go first, oldest first; then low-score favorites.
        active.sort(
            key=lambda entry: (
                entry.favorite,
                entry.score if entry.favorite else 0.0,
                entry.created_at,
            )
        )
        for entry in active:
            if total <= self.max_bytes:
                break
            total -= entry.size
            self._delete_entry(entry)
        self.entries = [entry for entry in self.entries if entry.status == "active"]

    def _load(self) -> None:
        try:
            payload = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            return
        for raw in payload.get("entries", []):
            try:
                entry = ArchiveEntry(**raw)
            except (TypeError, ValueError):
                continue
            if entry.status == "active":
                self.entries.append(entry)

    def _merge_duplicate_entries(self) -> None:
        """Collapse legacy cross-chat copies into one content-addressed entry."""
        seen: dict[str, "ArchiveEntry"] = {}
        merged: list = []
        changed = False
        for entry in self.entries:
            first = seen.get(entry.sha)
            if first is None:
                seen[entry.sha] = entry
                merged.append(entry)
                continue
            changed = True
            for chat in entry.chat_ids or [entry.chat_id]:
                if not self.matches_entry(first, chat):
                    first.chat_ids.append(chat)
            if entry.sendable:
                first.sendable = True
            if not first.description and entry.description:
                first.description = entry.description
                first.tags = list(entry.tags)
                first.score = entry.score
                first.favorite = entry.favorite
                first.analysis_pending = entry.analysis_pending
                first.analysis_error = entry.analysis_error
                first.review_at = entry.review_at
            if entry.file != first.file:
                try:
                    (self.root / entry.file).unlink(missing_ok=True)
                except OSError:
                    pass
        if changed:
            self.entries = merged
            self._save()

    def _save(self) -> None:
        try:
            self.meta_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.meta_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "entries": [
                            asdict(entry)
                            for entry in self.entries
                            if entry.status == "active"
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.meta_path)
        except OSError as exc:
            logger.debug(f"[图片库] 写入元数据失败: {exc}")


__all__ = ["ArchiveEntry", "ImageArchive"]
