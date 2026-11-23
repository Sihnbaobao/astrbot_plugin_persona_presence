"""Persistent rolling AI summaries for group topics.

The thread tracker is in-memory and resets with the process. This store keeps
the latest one-sentence AI summary per chat on disk so a long-running topic
can still be recognised after a restart, even when its early turns are gone.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_MAX_RECORDS = 200
_MAX_SUMMARY = 600


class TopicDigestStore:
    """Small JSON-backed store of rolling per-chat topic summaries."""

    def __init__(self, data_dir: str | Path) -> None:
        """Load the digest file.

        Args:
            data_dir: Plugin data directory.
        """
        self.path = Path(data_dir) / "topic_digest.json"
        self._data: dict[str, dict[str, Any]] = {}
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {
                        str(key): value
                        for key, value in raw.items()
                        if isinstance(value, dict)
                    }
        except Exception:
            self._data = {}

    def summary_for(self, umo: str) -> str:
        """Return the latest stored summary for one conversation.

        Args:
            umo: Conversation origin.

        Returns:
            The stored summary, or an empty string.
        """
        entry = self._data.get(str(umo), {})
        return str(entry.get("summary") or "")[:_MAX_SUMMARY]

    def update(
        self,
        umo: str,
        summary: str,
        *,
        participants: list[str] | None = None,
        message_count: int = 0,
    ) -> None:
        """Store a new rolling summary for one conversation.

        Args:
            umo: Conversation origin.
            summary: One-sentence AI topic summary.
            participants: Recent human display names.
            message_count: Number of turns represented by the summary.
        """
        clean = " ".join(str(summary or "").split())[:_MAX_SUMMARY]
        if not clean:
            return
        self._data[str(umo)] = {
            "summary": clean,
            "participants": [str(name)[:40] for name in (participants or [])[:8]],
            "message_count": max(0, int(message_count or 0)),
            "updated_at": time.time(),
        }
        while len(self._data) > _MAX_RECORDS:
            oldest = min(
                self._data,
                key=lambda key: float(self._data[key].get("updated_at") or 0),
            )
            self._data.pop(oldest, None)
        self._save()

    def _save(self) -> None:
        """Write the digest atomically."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception:
            pass
