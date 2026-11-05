"""Life sight for the persona plugin.

Gives the persona a short, neutral note about what it has actually been doing
on its own time, so a question like "what were you watching" is answered from
real experience instead of a blank. The note carries no obligation to speak:
whether it mentions any of this stays a persona decision.

The only data source is the watch log written by the Bilibili companion
plugin. It is read defensively: a missing or reshaped file just means no note.
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api.all import *

_MAX_TITLE_CHARS = 60
# 只有问到"最近在做什么"时才带状态，否则会把一次观看当成喜好答出去。
_ACTIVITY_QUERY = re.compile(
    r"在?\s*(?:干嘛|干什么|做什么|干啥|忙什么|忙啥|在忙)"
    r"|干(?:嘛|什么)呢"
    r"|最近.{0,4}(?:干|做|忙|看|刷|玩)"
    r"|这阵子"
    r"|(?:在|正在)(?:看|刷|玩|听|弄)(?:什么|啥)"
    r"|看(?:了|过|啥|什么)(?:视频|片子|番)"
    r"|看(?:了|过)(?:什么|啥|哪些|哪个)"
    r"|(?:什么|啥)(?:视频|片子|番)"
    r"|刷(?:了|到)(?:什么|啥|视频)"
)
# Only these questions are allowed to surface the concrete video title.
_CONTENT_QUERY = re.compile(
    r"(?:什么|啥|哪个|哪些|哪部)(?:视频|片子|番|剧|内容|电影)"
    r"|看了(?:什么|啥|哪些|哪个|哪部)"
    r"|看的是(?:什么|啥|哪个|哪部)"
    r"|看(?:了|过)?(?:什么|啥)"
)

# A record older than this is no longer "what I'm doing right now".
_FRESH_MINUTES = 90
_MAX_REVIEW_CHARS = 80
_ACTION_LABELS = {
    "👍点赞": "点了赞",
    "🪙投币": "投了币",
    "⭐收藏": "收藏了",
    "💬评论": "留了言",
}


class LifeSight:
    """Reads recent real-life records and renders them as background notes.

    Args:
        store_path: Path to the companion watch log, or None to disable.
        enabled: Whether the note is built at all.
        data_dir: Directory holding the companion plan file.
    """

    def __init__(
        self,
        store_path: "Path | str | None" = None,
        *,
        enabled: bool = False,
        data_dir: "Path | str | None" = None,
    ) -> None:
        self.store_path = Path(store_path) if store_path else None
        self.enabled = enabled
        if data_dir:
            self.data_dir = Path(data_dir)
        elif self.store_path is not None:
            self.data_dir = self.store_path.parent
        else:
            self.data_dir = None

    @staticmethod
    def looks_like_activity_question(text: str) -> bool:
        """Tell whether the message is asking what the persona has been doing.

        The state is only worth injecting for that kind of question; anywhere
        else it leaks into unrelated answers.

        Args:
            text: The incoming message text.

        Returns:
            True when the message asks about recent activity.
        """
        return bool(_ACTIVITY_QUERY.search(text or ""))

    @staticmethod
    def looks_like_content_question(text: str) -> bool:
        """Tell whether the message asks for the concrete video/content.

        Args:
            text: The incoming message text.

        Returns:
            True when the sender asked what was watched, not just what the
            persona has been up to.
        """
        return bool(_CONTENT_QUERY.search(text or ""))

    def state_lines(
        self, *, include_title: bool = True, fresh_only: bool = False
    ) -> list[str]:
        """Collect the state lines the persona can answer from.

        Args:
            include_title: Include the concrete video title.
            fresh_only: Omit watch records older than the freshness window.

        Returns:
            One line per available source, possibly empty.
        """
        if not self.enabled or self.data_dir is None:
            return []

        lines: list[str] = []
        # 先给"此刻"和最近做过的事，模型才不至于只能答"发呆"。
        lines.append("· 现在是 " + datetime.now().strftime("%H:%M"))
        recent = self._recent_done_line()
        if recent:
            lines.append(recent)
        minutes, watch_text = self._last_watch()
        is_fresh = minutes is not None and minutes <= _FRESH_MINUTES
        if watch_text and (not fresh_only or is_fresh):
            # 活动层级先说：人在刷视频，不是在"看某个具体视频"。
            if is_fresh:
                lines.append("· 刚刚（不到 90 分钟前）看过 B 站")
            else:
                age = self._relative_age(minutes)
                lines.append(f"· 最近一次看 B 站：{age}")
            # 标题只在被追问"什么视频"时才用得上。
            if include_title:
                lines.append(f"· 那次看的是：{watch_text}")
        plan_text = self._plan_line()
        if plan_text:
            lines.append(plan_text)
        return lines

    def proactive_lines(self) -> list[str]:
        """Return a short, title-free view for proactive prompts.

        Returns:
            Fresh activity lines suitable for ambient interjections.
        """
        return self.state_lines(include_title=False, fresh_only=True)

    def _recent_done_line(self) -> str:
        """Describe a plan item finished within the last two hours.

        Returns:
            A formatted line, or an empty string when nothing is that recent.
        """
        data = self._read_json("autonomous_plan_today.json")
        if not isinstance(data, dict) or data.get("date") != self._today():
            return ""

        now = datetime.now()
        latest: tuple[datetime, str] | None = None
        for key, label in (
            ("dynamic_watch_times", "看关注动态"),
            ("special_follow_times", "看特别关注"),
            ("bangumi_times", "追番"),
            ("proactive_times", "刷了会儿视频"),
        ):
            for value in data.get(key) or []:
                try:
                    clock = datetime.strptime(str(value).strip(), "%H:%M")
                except ValueError:
                    continue
                moment = now.replace(
                    hour=clock.hour, minute=clock.minute, second=0, microsecond=0
                )
                if moment > now:
                    continue
                if (now - moment).total_seconds() > 7200:
                    continue
                if latest is None or moment > latest[0]:
                    latest = (moment, label)
        if latest is None:
            return ""
        return f"· 刚才{latest[1]}（{latest[0].strftime('%H:%M')}）"

    def _plan_line(self) -> str:
        """Summarize the remaining plan for today.

        Returns:
            A formatted line, or an empty string.
        """
        data = self._read_json("autonomous_plan_today.json")
        if not isinstance(data, dict) or data.get("date") != self._today():
            return ""

        entries: list[tuple[str, str]] = []
        for key, label in (
            ("proactive_times", "主动刷一次"),
            ("dynamic_watch_times", "看关注动态"),
            ("special_follow_times", "看特别关注"),
            ("bangumi_times", "追番"),
        ):
            for value in data.get(key) or []:
                text = str(value).strip()
                if text:
                    entries.append((text, label))
        if not entries:
            return ""

        entries.sort()
        current = datetime.now().strftime("%H:%M")
        pending = [entry for entry in entries if entry[0] >= current]
        if pending:
            shown = "、".join(f"{when} {label}" for when, label in pending[:3])
            return f"· 待会要做的：{shown}"
        shown = "、".join(f"{when} {label}" for when, label in entries[-2:])
        return f"· 今天安排过的：{shown}"

    def _last_watch(self) -> tuple[int | None, str]:
        """Describe the most recent watch record with its age.

        Returns:
            A tuple of (minutes since the record, formatted line).
            Minutes is None when the timestamp cannot be read.
        """
        if self.store_path is None:
            return None, ""
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except Exception:
            return None, ""
        if not isinstance(raw, list):
            return None, ""

        for record in reversed(raw):
            if not isinstance(record, dict):
                continue
            if str(record.get("evaluation_status") or "") == "failed":
                continue
            title = str(record.get("title") or "").strip()[:_MAX_TITLE_CHARS]
            if not title:
                continue
            age = self._age_minutes(record.get("time"))
            head = f"看了《{title}》"
            extras = []
            try:
                if record.get("score") is not None:
                    extras.append(f"{float(record['score']):g}分")
            except (TypeError, ValueError):
                pass
            actions = [
                _ACTION_LABELS.get(str(item), str(item))
                for item in (record.get("actions") or [])
            ]
            if actions:
                extras.append("、".join(actions))
            line = head
            line += "｜" + "｜".join(extras) if extras else ""
            return age, line
        return None, ""

    @classmethod
    def _relative_age(cls, minutes: int | None) -> str:
        """Render an elapsed-minute count as a short phrase.

        Args:
            minutes: Minutes elapsed, or None.

        Returns:
            A phrase such as "5 小时前".
        """
        if minutes is None or minutes < 0:
            return ""
        if minutes < 60:
            return f"{minutes} 分钟前"
        if minutes < 60 * 24:
            return f"{minutes // 60} 小时前"
        return f"{minutes // (60 * 24)} 天前"

    @classmethod
    def _age_minutes(cls, value: Any) -> int | None:
        """Return how many minutes ago a record happened.

        Args:
            value: Raw timestamp field.

        Returns:
            Elapsed minutes, or None when unparsable.
        """
        parsed = cls._parse_time(value)
        if parsed is None:
            return None
        return int((datetime.now() - parsed).total_seconds() // 60)

    @classmethod
    def _relative(cls, value: Any) -> str:
        """Describe how long ago a record happened.

        Args:
            value: Raw timestamp field.

        Returns:
            A phrase such as "12 分钟前" or "今天早上".
        """
        parsed = cls._parse_time(value)
        if parsed is None:
            return ""
        minutes = int((datetime.now() - parsed).total_seconds() // 60)
        if minutes < 0:
            return ""
        if minutes < 60:
            return f"{minutes} 分钟前"
        if minutes < 60 * 12:
            return f"{minutes // 60} 小时前"
        if minutes < 60 * 36:
            return "昨天"
        return f"{minutes // (60 * 24)} 天前"

    def _read_json(self, name: str) -> Any:
        """Read one companion JSON file.

        Args:
            name: File name inside the data directory.

        Returns:
            Parsed content, or None when unreadable.
        """
        if self.data_dir is None:
            return None
        try:
            return json.loads((self.data_dir / name).read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _today() -> str:
        """Read today's date key.

        Returns:
            Date formatted as YYYY-MM-DD.
        """
        return datetime.now().strftime("%Y-%m-%d")

    def _parse_time(value: Any) -> datetime | None:
        """Parse the log timestamp written in local time.

        Args:
            value: Raw timestamp field.

        Returns:
            A datetime, or None when the value cannot be parsed.
        """
        text = str(value or "").strip()
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(text, pattern)
            except ValueError:
                continue
        return None

    @classmethod
    def _record_timestamp(cls, value: Any) -> float:
        """Read a log timestamp as a POSIX value.

        Args:
            value: Raw timestamp field.

        Returns:
            POSIX timestamp, defaulting to now when unparsable.
        """
        parsed = cls._parse_time(value)
        return parsed.timestamp() if parsed else time.time()

    @classmethod
    def _clock(cls, value: Any) -> str:
        """Format a log timestamp as local hour:minute.

        Args:
            value: Raw timestamp field.

        Returns:
            Clock label such as 04:45.
        """
        parsed = cls._parse_time(value)
        return parsed.strftime("%H:%M") if parsed else "--:--"
