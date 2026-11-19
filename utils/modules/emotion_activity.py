"""Read the persona's music, Bilibili and QQ-space activity into emotion.

The companion plugins already persist what the persona did: songs it listened
to, videos it watched, and Qzone actions it took. This bridge tails those
files instead of importing another plugin. It only reads forward from the
position recorded at first sight, so installing the bridge never replays old
history as a burst of mood.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

_SOURCE_VERSION = 1
_MAX_EVENTS = 3


class ActivityBridge:
    """Tail companion-plugin activity files and turn them into mood deltas.

    Args:
        data_dir: Persona plugin data directory for the cursor file.
        plugin_data_dir: AstrBot's plugin-data root; auto-detected when omitted.
    """

    def __init__(
        self,
        data_dir: "Path | str | None" = None,
        plugin_data_dir: "Path | str | None" = None,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir else None
        self.state_path = (
            self.data_dir / "activity_state.json" if self.data_dir else None
        )
        self.plugin_data_dir = (
            Path(plugin_data_dir) if plugin_data_dir else self._default_plugin_data()
        )
        self.state: dict[str, Any] = self._load_state()
        self._seeded = bool(self.state)

    @staticmethod
    def _default_plugin_data() -> "Path | None":
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            return Path(get_astrbot_plugin_data_path())
        except Exception:
            return None

    def _load_state(self) -> dict[str, Any]:
        if self.state_path is None or not self.state_path.is_file():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temp.replace(self.state_path)
        except OSError as exc:
            logger.debug(f"[情绪-活动] 游标写入失败: {exc}")

    def _music_path(self) -> "Path | None":
        if self.plugin_data_dir is None:
            return None
        path = self.plugin_data_dir / "astrbot_plugin_music_card" / "listened.jsonl"
        return path if path.is_file() else None

    def _bili_path(self) -> "Path | None":
        if self.plugin_data_dir is None:
            return None
        path = self.plugin_data_dir / "astrbot_plugin_bili_whale" / "watched.jsonl"
        return path if path.is_file() else None

    def _qzone_db(self) -> "Path | None":
        if self.plugin_data_dir is None:
            return None
        root = self.plugin_data_dir / "astrbot_plugin_kongjian"
        if not root.is_dir():
            return None
        candidates = sorted(root.glob("posts_*.db"))
        return candidates[-1] if candidates else None

    @staticmethod
    def _read_jsonl(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
        try:
            size = path.stat().st_size
        except OSError:
            return [], offset
        if size < offset:
            offset = 0
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                raw = handle.read()
                offset = handle.tell()
        except OSError:
            return [], offset
        events: list[dict[str, Any]] = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                events.append(item)
        return events, offset

    @staticmethod
    def _music_event(item: dict[str, Any]) -> dict[str, Any] | None:
        name = str(item.get("name") or "").strip()
        if not name:
            return None
        artist = str(item.get("artist") or "").strip()
        title = f"《{name}》" + (f"（{artist}）" if artist else "")
        try:
            score = int(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        liked = item.get("like") is True or item.get("liked") is True
        if liked or score >= 7:
            return {
                "source": "music",
                "cause": f"听了{title}",
                "deltas": {"joy": 0.08 + min(score, 10) * 0.014},
            }
        if item.get("like") is False or score <= 3:
            return {
                "source": "music",
                "cause": f"听了{title}，不太喜欢",
                "deltas": {"disgust": 0.07, "anger": 0.03},
            }
        return {
            "source": "music",
            "cause": f"听了{title}",
            "deltas": {"joy": 0.03},
        }

    @staticmethod
    def _bili_event(item: dict[str, Any]) -> dict[str, Any] | None:
        title = str(item.get("title") or "").strip()
        if not title:
            return None
        shared = item.get("shared") is True
        deep = item.get("deep") is True
        note = str(item.get("note") or "").strip()
        try:
            score = int(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if shared or score >= 7 or deep or note:
            return {
                "source": "bili",
                "cause": f"刷到《{title}》",
                "deltas": {"joy": 0.10, "surprise": 0.03},
            }
        return {
            "source": "bili",
            "cause": f"刷到《{title}》",
            "deltas": {"joy": 0.03},
        }

    @staticmethod
    def _qzone_event(row: tuple[Any, ...]) -> dict[str, Any] | None:
        action, _uin, name, snippet = row[:4]
        action = str(action or "").strip().lower()
        name = str(name or "").strip() or "一个群友"
        snippet = str(snippet or "").strip()
        if not action:
            return None
        if action in {"like", "comment", "reply"}:
            return {
                "source": "qzone",
                "cause": f"在空间{ '点赞' if action == 'like' else '回复' }了{name}的动态",
                "deltas": {"joy": 0.06},
            }
        if action == "view":
            return {
                "source": "qzone",
                "cause": f"看了{name}的空间",
                "deltas": {"joy": 0.02},
            }
        return None

    def _poll_music(self) -> list[dict[str, Any]]:
        path = self._music_path()
        if path is None:
            return []
        key = "music"
        section = self.state.setdefault(key, {})
        if "offset" not in section:
            try:
                section["offset"] = path.stat().st_size
            except OSError:
                section["offset"] = 0
            return []
        raw, offset = self._read_jsonl(path, int(section.get("offset", 0) or 0))
        section["offset"] = offset
        events = []
        for item in raw[-_MAX_EVENTS:]:
            event = self._music_event(item)
            if event:
                events.append(event)
        return events

    def _poll_bili(self) -> list[dict[str, Any]]:
        path = self._bili_path()
        if path is None:
            return []
        key = "bili"
        section = self.state.setdefault(key, {})
        if "offset" not in section:
            try:
                section["offset"] = path.stat().st_size
            except OSError:
                section["offset"] = 0
            return []
        raw, offset = self._read_jsonl(path, int(section.get("offset", 0) or 0))
        section["offset"] = offset
        events = []
        for item in raw[-_MAX_EVENTS:]:
            event = self._bili_event(item)
            if event:
                events.append(event)
        return events

    def _poll_qzone(self) -> list[dict[str, Any]]:
        path = self._qzone_db()
        if path is None:
            return []
        key = "qzone"
        section = self.state.setdefault(key, {})
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return []
        if "last_id" not in section:
            try:
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                    row = db.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM qzone_actions"
                    ).fetchone()
                section["last_id"] = int(row[0] or 0) if row else 0
                section["mtime"] = mtime
            except sqlite3.Error:
                section["last_id"] = 0
            return []
        if section.get("mtime") == mtime:
            return []
        last_id = int(section.get("last_id", 0) or 0)
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                rows = db.execute(
                    "SELECT action, uin, name, snippet, created_at FROM qzone_actions "
                    "WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (last_id, _MAX_EVENTS),
                ).fetchall()
                max_row = db.execute(
                    "SELECT COALESCE(MAX(id), ?) FROM qzone_actions", (last_id,)
                ).fetchone()
        except sqlite3.Error:
            return []
        section["mtime"] = mtime
        if max_row:
            section["last_id"] = int(max_row[0] or last_id)
        events = []
        for row in rows:
            event = self._qzone_event(row)
            if event:
                events.append(event)
        return events

    def poll(self) -> list[dict[str, Any]]:
        """Return newly observed activities since the previous poll.

        Returns:
            Up to a few events per source, each with a cause and emotion deltas.
        """
        if self.plugin_data_dir is None:
            return []
        events = self._poll_music() + self._poll_bili() + self._poll_qzone()
        self._seeded = True
        self.state.setdefault("version", _SOURCE_VERSION)
        self._save_state()
        return events
