"""Curiosity: looking things up because the persona wants to know.

Curiosity only records things the persona itself becomes interested in during
reflection; group questions are answered by the normal reply/tool path. When the
caps allow, the pack searches the web for it, asks the model to summarise what
it found in its own words, and writes that into the notebook as knowledge.

Nothing is invented: if the search returns nothing usable, the curiosity stays
pending instead of being answered from memory.
"""

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any

import aiohttp

from astrbot.api import logger

from .base import FeatureContext, ModuleManifest, PluginModule
from .curiosity_store import CuriosityStore

_TAVILY_ENDPOINT = "https://api.tavily.com/search"

_REFLECT_MIN_MESSAGES = 15
_REFLECT_INTERVAL_SECONDS = 45 * 60

_REFLECT_PROMPT = """你是这个人格本人：
{persona}

你平时在意这些：
{interests}

刚才群里在聊：
{lines}

这里面有没有什么你自己真的想弄明白的？
只写你会在意的；如果只是群友在问、你本人没兴趣，就留空。

topic 写一个完整、能单独搜到的名字或事，别把代词、称谓、时间词、半句话当成话题；
query 带上作品名、群名或前后语境。
只输出 JSON：{{"topic": "你好奇的东西，没有就留空", "query": "你会输入搜索框的话，带上下文", "why": "为什么好奇，一句话"}}"""

_SUMMARISE_PROMPT = """你是这个人格本人：
<persona>
{persona}
</persona>

你因为自己的兴趣去查了「{term}」。下面是从网上搜到的资料：

{material}

请用这个人的性格、语气和关注点，用三到五行完整地说清楚它是什么，不要写到一半就停。要求：
1. 第一句必须直接给出结论：它是什么、指谁、怎么回事；不要先寒暄、不要只丢几个词。
2. 只写资料里能支持的内容，资料不足就明确说“资料里没有明确说法”，不要硬凑。
3. 不要编造、不要补充资料外的知识。
4. 只输出一段连续的笔记，不要写成聊天对话、不要分行角色扮演、不要写“璃月：”“我：”这种说话人标签。
5. 不要说“作为助手”“为您查询”“以下是资料”这种助手口吻；这就是你自己看完之后在心里记下的一笔，用第一人称，可以带自己的偏好和感受。
6. 总长度控制在 {limit} 字以内，最后一句必须是完整句子，不要写到一半停住。"""


_BAD_TERM_FRAGMENTS = (
    "什么",
    "怎么",
    "为什么",
    "到底",
    "反正",
    "知道",
    "觉得",
    "你们",
    "我们",
    "他们",
    "你",
    "我",
    "他",
    "她",
    "它",
    "这",
    "那",
    "吗",
    "呢",
    "吧",
    "啊",
    "呀",
    "求求",
    "先问",
    "有病",
    "小说名",
    "冷知识",
    "出处",
    "最忧郁",
    "可实际",
    "好的好",
)


def _usable_term(term: str) -> bool:
    """Reject fragments that are not a searchable topic.

    Args:
        term: Candidate from an explicit question.

    Returns:
        True when it looks like a name or phrase, not a sentence fragment.
    """
    cleaned = str(term or "").strip()
    if len(cleaned) < 2:
        return False
    if len(cleaned) > 16 and not re.search(r"[A-Za-z]", cleaned):
        return False
    return not any(fragment in cleaned for fragment in _BAD_TERM_FRAGMENTS)


class CuriosityModule(PluginModule):
    """Turns unknown words into web lookups and stored knowledge.

    Args:
        context: AstrBot star context for the model call.
        notebook: The persona's notebook, already enabled.
        settings: Normalised settings for this pack.
    """

    manifest = ModuleManifest(
        key="curiosity",
        name="好奇心",
        summary="看不懂的词会自己去查，查到的记下来。",
        config_group="dlc_curiosity",
        default_enabled=False,
    )

    def __init__(
        self,
        context: Any = None,
        notebook: Any = None,
        settings: Any = None,
        store: Any = None,
    ):
        super().__init__(context)
        self.notebook = notebook
        self.settings = settings
        self.store = store if store is not None else CuriosityStore(None)
        self._daily_key = ""
        self._daily_count = 0
        self._last_lookup_at = 0.0
        self._running = False
        self._buffers: dict[str, list[str]] = {}
        self._last_reflect_at: dict[str, float] = {}
        self._attempts: dict[str, int] = {}
        self._no_key_warned = False
        self._added_key = ""
        self._added_count = 0

    def needs_attention(self) -> bool:
        """Tell whether the next turn may need a resolved persona/provider.

        Returns:
            True when a lookup is pending or the reflection buffer is ready.
        """
        if self.store.pending(1):
            return True
        return any(
            len(lines) >= _REFLECT_MIN_MESSAGES - 1 for lines in self._buffers.values()
        )

    def _today_add_count(self, now: datetime | None = None) -> int:
        """Read how many new topics were admitted today.

        Args:
            now: Local time; defaults to now.

        Returns:
            Number of newly queued topics.
        """
        today = (now or datetime.now()).strftime("%Y-%m-%d")
        if self._added_key != today:
            self._added_key = today
            self._added_count = 0
        return self._added_count

    def _can_admit(self, source: str, now: datetime | None = None) -> bool:
        """Tell whether a new topic may enter the queue.

        Self-generated interest topics obey the backlog soft limit and the
        daily admission budget. Manual page queries bypass both.

        Args:
            source: Where the topic came from.
            now: Local time; defaults to now.

        Returns:
            True when a new topic may be queued.
        """
        if source == "manual":
            return True
        daily = int(getattr(self.settings, "new_topics_per_day", 0) or 0)
        if daily > 0 and self._today_add_count(now) >= daily:
            return False
        soft = int(getattr(self.settings, "pending_soft_limit", 0) or 0)
        if soft > 0 and len(self.store.pending(1000)) >= soft:
            return False
        return True

    def _note_added(self, now: datetime | None = None) -> None:
        """Count one newly admitted topic.

        Args:
            now: Local time; defaults to now.
        """
        self._today_add_count(now)
        self._added_count += 1

    def cleanup_pending(self) -> int:
        """Drop queued topics that are sentence fragments rather than topics.

        Returns:
            Number of removed entries.
        """
        removed = 0
        for entry in list(self.store.entries):
            if entry.kind != "curious":
                continue
            if not _usable_term(entry.text):
                if self.store.drop(entry.text):
                    removed += 1
        return removed

    def knowledge_block(self, text: str) -> str:
        """Render curiosity knowledge relevant to one message.

        Args:
            text: Current message text.

        Returns:
            A prompt block, or an empty string.
        """
        return self.store.knowledge_block(text)

    async def on_message(
        self, event: Any, is_private: bool, ctx: FeatureContext | None = None
    ) -> None:
        """Collect group chatter and let the persona reflect on it later.

        Args:
            event: The AstrBot message event.
            is_private: Whether the message came from a private chat.
            ctx: Optional base-resolved turn context.
        """
        if is_private:
            return
        try:
            text = str(event.get_message_str() or "").strip()
        except Exception:
            text = str(getattr(event, "message_str", "") or "").strip()
        if not text:
            return

        # Group questions are answered by the normal reply/tool path. Curiosity
        # only records things the persona itself becomes curious about during
        # reflection; otherwise every "X是什么" becomes a lookup task.

        soft = int(getattr(self.settings, "pending_soft_limit", 0) or 0)
        if soft > 0 and len(self.store.pending(1000)) >= soft:
            return

        group_key = str(ctx.group_id or "") if ctx is not None else ""
        buffer = self._buffers.setdefault(group_key, [])
        buffer.append(text[:120])
        if len(buffer) > 40:
            del buffer[:-40]
        if len(buffer) < _REFLECT_MIN_MESSAGES:
            return
        if (
            time.time() - self._last_reflect_at.get(group_key, 0.0)
            < _REFLECT_INTERVAL_SECONDS
        ):
            return
        self._last_reflect_at[group_key] = time.time()
        chunk = list(buffer)
        buffer.clear()
        try:
            asyncio.create_task(self._reflect_and_research(chunk, ctx))
        except Exception as exc:
            logger.warning(f"[好奇心] 反思调度失败: {exc}")

    async def _reflect_and_research(
        self, lines: list[str], ctx: FeatureContext | None = None
    ) -> None:
        """Reflect on a batch, then look up whatever it became curious about.

        Args:
            lines: Recent group messages.
            ctx: Optional base-resolved turn context.
        """
        try:
            await self.reflect(lines, ctx=ctx)
        finally:
            try:
                await self.research(ctx=ctx)
            except Exception as exc:
                logger.debug(f"[好奇心] 反思后查询失败: {exc}")

    async def reflect(self, lines: list[str], ctx: FeatureContext | None = None) -> str:
        """Let the persona say whether anything made it genuinely curious.

        Args:
            lines: Recent group messages.

        Returns:
            The term it wants to understand, or an empty string.
        """
        if not self._can_admit("interest"):
            return ""
        provider_id = await self._resolve_provider_id(ctx)
        if not provider_id or not lines:
            return ""
        persona_text = await self._persona_text(ctx)
        group_id = str(ctx.group_id or "") if ctx is not None else ""
        interests = self.notebook.all_lines(group_id)[:8] if self.notebook else []
        interest_text = (
            "\n".join("- " + line for line in interests)
            if interests
            else "（还没记下特别在意的东西）"
        )
        prompt = _REFLECT_PROMPT.format(
            persona=persona_text,
            interests=interest_text,
            lines="\n".join(lines[-20:]),
        )
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                max_tokens=240,
            )
            text = str(getattr(response, "completion_text", "") or "")
        except Exception as exc:
            logger.debug(f"[好奇心] 反思失败: {exc}")
            return ""

        payload = self._parse_json(text)
        if not isinstance(payload, dict):
            return ""
        topic = str(payload.get("topic") or "").strip()
        query = str(payload.get("query") or topic).strip()
        why = str(payload.get("why") or "").strip()
        if topic in ("", "无", "没有", "none", "None"):
            return ""
        if self.store.add(
            topic,
            source="interest",
            query=query,
            why=why,
            umo=str(getattr(ctx, "umo", "") or ""),
        ):
            self._note_added()
            self._attempts.pop(topic, None)
            logger.info(
                f"[好奇心] 对「{topic}」产生兴趣：{why or '说不清，就是想知道'}；"
                f"搜索词：{query}"
            )
            return topic
        return ""

    @staticmethod
    def _parse_json(text: str) -> object | None:
        """Parse the first JSON object from a model reply.

        Args:
            text: Raw model output.

        Returns:
            The parsed object, or None.
        """
        raw = str(text or "").strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(raw[start : end + 1])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _useless_summary(summary: str) -> bool:
        """Tell whether a search summary contains no actual answer.

        Args:
            summary: Model-written summary.

        Returns:
            True when it only says the material was insufficient.
        """
        text = str(summary or "").strip()
        if len(text) < 6:
            return True
        # Roleplay fragments such as "璃月：蓝原？" are not a note; a
        # legitimate note may still start with "结论：" and end in a period.
        if (
            re.search(r"(?m)^[^\n：]{1,12}：", text)
            and "\n" in text
            and "。" not in text
        ):
            return True
        if re.match(r"^[?？]", text):
            return True
        markers = (
            "没查到",
            "没有查到",
            "没有找到",
            "资料不足",
            "无法确定",
            "不能确定",
            "无法回答",
            "没查明白",
            "没弄明白",
            "资料里没有明确",
        )
        return any(marker in text for marker in markers)

    async def research(
        self,
        force: bool = False,
        term: str = "",
        ctx: FeatureContext | None = None,
    ) -> str:
        """Look up one pending curiosity and store the answer.

        Args:
            force: Ignore the daily cap and quiet hours (used for verification).
            term: Look this word up instead of the queued curiosity.
            ctx: Optional base-resolved turn context.

        Returns:
            A short result description, or an empty string when nothing ran.
        """
        if self._running:
            return ""
        urgent = bool(term)
        if term:
            target = str(term).strip()
            if not target:
                return ""
            # 手动查询的词先入队，查完才能改成「知道」
            self.store.add(target, source="manual", query=target)
            search_query = target
            research_umo = ""
            self._attempts.pop(target, None)
        else:
            pending = self.store.pending(1)
            if not pending:
                return ""
            entry = pending[0]
            target = entry.text
            urgent = entry.source == "manual"
            search_query = str(entry.query or target).strip()
            research_umo = str(entry.umo or "")

        now = ctx.now if ctx is not None else datetime.now()
        if not force:
            if self._today_count(now) >= self.settings.daily_cap:
                return ""
            if self._in_quiet_hours(now):
                return ""
            gap = int(getattr(self.settings, "min_gap_minutes", 0) or 0)
            if (
                not urgent
                and gap > 0
                and self._last_lookup_at
                and time.time() - self._last_lookup_at < gap * 60
            ):
                return ""

        # Do not spend a classification call when the search itself cannot run.
        if not str(getattr(self.settings, "tavily_key", "") or "").strip():
            if not self._no_key_warned:
                logger.info("[好奇心] 未配置 Tavily key，跳过联网查询")
                self._no_key_warned = True
            return ""

        term = target
        self._last_lookup_at = time.time()
        self._running = True
        try:
            logger.info(f"[好奇心] 去查：「{search_query}」")
            material = await self._search(search_query)
            if not material:
                self._record_failure(term)
                logger.info(f"[好奇心] 没搜到「{term}」的资料，先留着")
                return ""
            summary = await self._summarise(term, material, ctx, umo=research_umo)
            if not summary:
                self._record_failure(term)
                return ""
            if self._useless_summary(summary):
                self.store.drop(term)
                self._attempts.pop(term, None)
                logger.info(f"[好奇心] 「{term}」没有可用信息，丢弃")
                return ""
            if self.store.resolve(term, summary):
                self._attempts.pop(term, None)
                self._bump_daily(now)
                logger.info(f"[好奇心] 查到「{term}」：{summary[:60]}")
                return summary
        except Exception as exc:
            logger.warning(f"[好奇心] 查询失败: {exc}")
        finally:
            self._running = False
        return ""

    async def _search(self, term: str) -> str:
        """Run one web search and flatten the result into text.

        Args:
            term: What to look for.

        Returns:
            Concatenated titles and snippets, or an empty string.
        """
        key = str(getattr(self.settings, "tavily_key", "") or "").strip()
        if not key:
            return ""
        query = str(term or "").strip()
        if len(query) <= 8 and " " not in query:
            query = f"{query} 是什么意思"
        payload = {
            "api_key": key,
            "query": query,
            "max_results": int(getattr(self.settings, "results", 3) or 3),
            "include_answer": True,
            "search_depth": "basic",
        }
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(_TAVILY_ENDPOINT, json=payload) as response:
                if response.status != 200:
                    logger.warning(f"[好奇心] 搜索返回 {response.status}")
                    return ""
                data = await response.json()
        parts: list[str] = []
        answer = str(data.get("answer") or "").strip()
        if answer:
            parts.append(f"搜索摘要：{answer}"[:500])
        for item in (data.get("results") or [])[:4]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            content = str(item.get("content") or "").strip()
            if title or content:
                parts.append(f"{title}：{content}"[:400])
        return "\n".join(parts)

    async def _summarise(
        self,
        term: str,
        material: str,
        ctx: FeatureContext | None = None,
        umo: str = "",
    ) -> str:
        """Ask the model to turn search results into a short note.

        Args:
            term: What was searched.
            material: Flattened search results.
            ctx: Optional base-resolved turn context.
            umo: Conversation origin when the summary runs in the background.

        Returns:
            The summary, or an empty string.
        """
        provider_id = await self._resolve_provider_id(ctx)
        if not provider_id:
            return ""
        limit = int(getattr(self.settings, "max_chars", 400) or 400)
        persona_text = await self._persona_text(ctx, umo=umo)
        prompt = _SUMMARISE_PROMPT.format(
            persona=persona_text or "（未提供人格，但仍以自己的第一人称）",
            term=term,
            material=material[:2000],
            limit=limit,
        )
        # 推理型模型可能把额度花在思考上，正文很短；给两次更大的预算。
        last_text = ""
        for budget in (1200, 2000):
            try:
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    max_tokens=budget,
                )
            except Exception as exc:
                logger.debug(f"[好奇心] 总结失败: {exc}")
                continue
            text = str(getattr(response, "completion_text", "") or "").strip()
            if not text:
                continue
            last_text = text
            if len(text) >= 80:
                break
        if not last_text:
            return ""
        if len(last_text) <= limit:
            return last_text
        clipped = last_text[:limit]
        boundary = max(
            clipped.rfind(mark) for mark in "。！？!?；;" + chr(10)
        )
        if boundary >= limit // 2:
            return clipped[: boundary + 1].rstrip()
        return clipped.rstrip() + "…"

    async def _persona_text(
        self, ctx: FeatureContext | None = None, umo: str = ""
    ) -> str:
        """Read the resolved persona prompt for this turn.

        Args:
            ctx: Optional base-resolved turn context.
            umo: Conversation origin when no turn context is available.

        Returns:
            A short persona prompt, or an empty string.
        """
        selected = str(getattr(self.settings, "persona_id", "") or "").strip()
        if selected:
            try:
                persona = self.context.persona_manager.get_persona_v3_by_id(selected)
                prompt = str((persona or {}).get("prompt", "") or "").strip()
                if prompt:
                    return prompt[:1800]
            except Exception as exc:
                logger.debug(f"[好奇心] 指定人格读取失败: {exc}")
        if ctx is not None and ctx.persona:
            return ctx.persona[:1800]
        try:
            persona = await self.context.persona_manager.get_default_persona_v3(
                (ctx.umo if ctx is not None else "") or umo or None
            )
            return str(persona.get("prompt", "") or "")[:1800]
        except Exception as exc:
            logger.debug(f"[好奇心] 读取人格失败: {exc}")
            return ""

    async def _resolve_provider_id(self, ctx: FeatureContext | None = None) -> str:
        """Resolve the chat provider for summarising.

        Args:
            ctx: Optional base-resolved turn context.

        Returns:
            A provider id, or an empty string.
        """
        if getattr(self.settings, "provider_id", ""):
            return self.settings.provider_id
        if ctx is not None and ctx.provider_id:
            return ctx.provider_id
        try:
            if ctx is not None:
                provider = await self.context.get_using_provider_async(ctx.umo)
            else:
                provider = await self.context.get_using_provider_async()
            return str(provider.meta().id) if provider else ""
        except Exception:
            return ""

    def _record_failure(self, term: str) -> None:
        """Back off after a failed lookup instead of retrying every message.

        Args:
            term: The curiosity that could not be resolved.
        """
        attempts = self._attempts.get(term, 0) + 1
        self._attempts[term] = attempts
        if attempts >= 2:
            self.store.drop(term)
            self._attempts.pop(term, None)
            logger.info(f"[好奇心] 「{term}」连续 {attempts} 次没有结果，先放弃")

    def _today_count(self, now: datetime | None = None) -> int:
        """Read how many lookups ran today.

        Args:
            now: Time-zone-aware current time; defaults to local time.

        Returns:
            Today's count.
        """
        today = (now or datetime.now()).strftime("%Y-%m-%d")
        if self._daily_key != today:
            self._daily_key = today
            self._daily_count = 0
        return self._daily_count

    def _bump_daily(self, now: datetime | None = None) -> None:
        """Count one completed lookup.

        Args:
            now: Time-zone-aware current time; defaults to local time.
        """
        self._today_count(now)
        self._daily_count += 1

    def _in_quiet_hours(self, now: datetime | None = None) -> bool:
        """Tell whether the current time is inside the quiet window.

        Args:
            now: Time-zone-aware current time; defaults to local time.

        Returns:
            True when the pack should stay quiet.
        """
        window = str(getattr(self.settings, "quiet_hours", "") or "").strip()
        if "-" not in window:
            return False
        try:
            start_text, end_text = window.split("-", 1)
            start = datetime.strptime(start_text.strip(), "%H:%M").time()
            end = datetime.strptime(end_text.strip(), "%H:%M").time()
        except ValueError:
            return False
        current = (now or datetime.now()).time()
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end
