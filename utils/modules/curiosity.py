"""Curiosity: looking things up because the persona wants to know.

An unknown word in a group chat becomes a question worth answering. When the
caps allow, the pack searches the web for it, asks the model to summarise what
it found in its own words, and writes that into the notebook as knowledge.

Nothing is invented: if the search returns nothing usable, the curiosity stays
pending instead of being answered from memory.
"""

import asyncio
import re
import time
from datetime import datetime
from typing import Any

import aiohttp

from astrbot.api import logger

from .base import ModuleManifest, PluginModule

_TAVILY_ENDPOINT = "https://api.tavily.com/search"

# "X 是什么" / "X 什么意思" / "X 是啥意思"
_QUESTION_PATTERN = re.compile(
    r"(?:^|[\s，。！？,.!?~])"
    r"([A-Za-z][A-Za-z0-9 .\-]{1,24}|[\u4e00-\u9fffA-Za-z0-9]{2,12})"
    r"\s*(?:是什么|什么意思|是啥意思|啥意思|是谁)"
)
# 引号里的词：「X」 “X” "X"
_QUOTE_PATTERN = re.compile(r"[「“]([\u4e00-\u9fffA-Za-z0-9]{2,12})[」”]")
# 「有谁/有人知道 X 吗」这种问法
_ASK_PATTERN = re.compile(
    r"(?:有(?:人|谁)知道|请问一下|问一下)\s*([^\s，。！？?是]{2,12}?)\s*(?:吗|嘛|呢|[？?]|$)"
)
_ASK_STOP_PREFIXES = ("怎么", "什么", "哪", "为什么", "如何", "咋", "谁")

_REFLECT_PROMPT = """你是这个人格本人：
{persona}

刚才群里在聊这些：
{lines}

这里有没有你其实不确定、或者根本不认识的名称？（番剧、游戏、品牌、角色、梗、缩写）
- 你确定自己知道的名称不算，别人随口说的日常词也不算。
- 只有真的存在知识缺口才写；没有就留空，不要勉强找东西来查。
只输出 JSON：{{"gap": "那个名称，没有就留空"}}"""

_CLASSIFY_PROMPT = """你是这个人格本人：
{persona}

群里有人问起「{term}」。先判断它属于哪一类：
- known：日常词、常见说法，或你本来就懂的东西；不需要记，也不该拿百科去解释。
- slang：群里的黑话、谐音、内部称呼或梗；应该结合群里的说话语境理解，不能用百科词典义盖过群里的用法。
- meh：跟你的兴趣和此刻处境都没关系，你并不想弄明白。
- lookup：你确实不懂、而且真的想知道的名词、作品或术语；值得查一下。
只输出 JSON：{{"kind": "known|slang|meh|lookup"}}"""

_SUMMARISE_PROMPT = """你在查一个自己不懂的词。下面是从网上搜到的资料。

词：{term}

资料：
{material}

请用两三行说清楚它是什么。要求：
1. 只写资料里能支持的内容，资料不足就直说没查到什么有用的。
2. 不要编造、不要补充资料外的知识。
3. 用第一人称的口气，像自己看完之后总结的。"""


_FILLER_PREFIXES = (
    "有没有人知道",
    "有谁知道",
    "想问一下",
    "请问一下",
    "一下",
    "大家知道",
    "有人知道",
    "谁知道",
    "请问",
    "问一下",
    "知道",
    "这个",
    "那个",
    "你们",
    "懂不懂",
)
_FILLER_SUFFIXES = ("吗", "呢", "啊", "呀", "的", "嘛", "是")


def _strip_filler(term: str) -> str:
    """Remove conversational padding around a term.

    Args:
        term: Raw captured term.

    Returns:
        The bare term.
    """
    cleaned = str(term or "").strip()
    changed = True
    while changed:
        changed = False
        for prefix in _FILLER_PREFIXES:
            if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
                cleaned = cleaned[len(prefix) :].strip()
                changed = True
        for suffix in _FILLER_SUFFIXES:
            if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
                cleaned = cleaned[: -len(suffix)].strip()
                changed = True
    return cleaned


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
        depends=["notebook"],
    )

    def __init__(self, context: Any = None, notebook: Any = None, settings: Any = None):
        super().__init__(context)
        self.notebook = notebook
        self.settings = settings
        self._daily_key = ""
        self._daily_count = 0
        self._running = False
        self._buffer: list[str] = []
        self._last_reflect = 0.0

    async def on_message(self, event: Any, is_private: bool) -> None:
        """Collect group chatter and let the persona reflect on it later.

        Args:
            event: The AstrBot message event.
            is_private: Whether the message came from a private chat.
        """
        if is_private or self.notebook is None:
            return
        try:
            text = str(event.get_message_str() or "").strip()
        except Exception:
            text = str(getattr(event, "message_str", "") or "").strip()
        if not text:
            return
        self._buffer.append(text[:120])
        if len(self._buffer) > 40:
            del self._buffer[:-40]
        if len(self._buffer) < 25:
            return
        if time.time() - self._last_reflect < 2 * 3600:
            return
        self._last_reflect = time.time()
        chunk = list(self._buffer)
        self._buffer.clear()
        try:
            asyncio.create_task(self.reflect(chunk))
        except Exception as exc:
            logger.warning(f"[好奇心] 反思调度失败: {exc}")

    async def reflect(self, lines: list[str]) -> str:
        """Let the persona say whether anything made it genuinely curious.

        Args:
            lines: Recent group messages.

        Returns:
            The term it wants to understand, or an empty string.
        """
        provider_id = await self._resolve_provider_id()
        if not provider_id or not lines:
            return ""
        persona_text = ""
        try:
            persona = await self.context.persona_manager.get_default_persona_v3()
            persona_text = str(persona.get("prompt", "") or "")[:600]
        except Exception as exc:
            logger.debug(f"[好奇心] 读取人格失败: {exc}")
        prompt = _REFLECT_PROMPT.format(
            persona=persona_text, lines="\n".join(lines[-20:])
        )
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                max_tokens=200,
            )
            text = str(getattr(response, "completion_text", "") or "")
        except Exception as exc:
            logger.debug(f"[好奇心] 反思失败: {exc}")
            return ""
        match = re.search(r'"gap"\s*:\s*"([^"]{1,24})"', text)
        term = match.group(1).strip() if match else ""
        if term in ("", "无", "没有", "none", "None"):
            return ""
        if self.notebook.add_curiosity(term):
            logger.info(f"[好奇心] 发现自己不太懂：{term}")
            return term
        return ""

    @staticmethod
    def _detect(text: str) -> list[str]:
        """Extract terms worth looking up from one message.

        Args:
            text: Message text.

        Returns:
            Distinct terms, most explicit first.
        """
        found: list[str] = []
        for pattern in (_QUESTION_PATTERN, _QUOTE_PATTERN, _ASK_PATTERN):
            for match in pattern.finditer(text or ""):
                term = _strip_filler(match.group(1))
                if term.startswith(_ASK_STOP_PREFIXES):
                    continue
                if any(word in term for word in ("是什么", "什么意思", "啥意思")):
                    continue
                if len(term) >= 2 and term not in found:
                    found.append(term)
        return found[:2]

    async def research(self, force: bool = False, term: str = "") -> str:
        """Look up one pending curiosity and store the answer.

        Args:
            force: Ignore the daily cap and quiet hours (used for verification).
            term: Look this word up instead of the queued curiosity.

        Returns:
            A short result description, or an empty string when nothing ran.
        """
        if self.notebook is None or self._running:
            return ""
        if term:
            target = str(term).strip()
            if not target:
                return ""
            # 手动查询的词先入队，查完才能改成「知道」
            self.notebook.add_curiosity(target)
        else:
            pending = self.notebook.pending_curiosities(1)
            if not pending:
                return ""
            target = pending[0].text
        if not force:
            if self._today_count() >= self.settings.daily_cap:
                return ""
            if self._in_quiet_hours():
                return ""

        term = target
        self._running = True
        try:
            # 先判断这个词值不值得联网查：日常词和群内黑话都不该被百科义覆盖。
            kind = await self._classify(term)
            if kind != "lookup":
                self.notebook.drop_curiosity(term)
                logger.info(f"[好奇心] 「{term}」判定为 {kind}，不联网查")
                return ""
            material = await self._search(term)
            if not material:
                logger.info(f"[好奇心] 没搜到「{term}」的资料，先留着")
                return ""
            summary = await self._summarise(term, material)
            if not summary:
                return ""
            if self.notebook.resolve_curiosity(term, summary):
                self._bump_daily()
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
        payload = {
            "api_key": key,
            "query": term,
            "max_results": int(getattr(self.settings, "results", 3) or 3),
            "include_answer": False,
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
        for item in (data.get("results") or [])[:4]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            content = str(item.get("content") or "").strip()
            if title or content:
                parts.append(f"{title}：{content}"[:400])
        return "\n".join(parts)

    async def _classify(self, term: str) -> str:
        """Decide whether a term deserves a web lookup.

        Args:
            term: The term someone asked about.

        Returns:
            One of known, slang or lookup; lookup on failure.
        """
        provider_id = await self._resolve_provider_id()
        if not provider_id:
            return "lookup"
        persona_text = ""
        try:
            persona = await self.context.persona_manager.get_default_persona_v3()
            persona_text = str(persona.get("prompt", "") or "")[:600]
        except Exception as exc:
            logger.debug(f"[好奇心] 读取人格失败: {exc}")
        prompt = _CLASSIFY_PROMPT.format(term=term, persona=persona_text)
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                max_tokens=200,
            )
            text = str(getattr(response, "completion_text", "") or "")
        except Exception as exc:
            logger.debug(f"[好奇心] 分类失败: {exc}")
            return "lookup"
        match = re.search(r'"kind"\s*:\s*"(known|slang|meh|lookup)"', text)
        return match.group(1) if match else "lookup"

    async def _summarise(self, term: str, material: str) -> str:
        """Ask the model to turn search results into a short note.

        Args:
            term: What was searched.
            material: Flattened search results.

        Returns:
            The summary, or an empty string.
        """
        provider_id = await self._resolve_provider_id()
        if not provider_id:
            return ""
        prompt = _SUMMARISE_PROMPT.format(term=term, material=material[:2000])
        # 推理型模型偶尔把额度花在思考上，正文为空，给一次更大的机会。
        for budget in (400, 900):
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
            if text:
                limit = int(getattr(self.settings, "max_chars", 120) or 120)
                return text[:limit]
        return ""

    async def _resolve_provider_id(self) -> str:
        """Resolve the chat provider for summarising.

        Returns:
            A provider id, or an empty string.
        """
        if getattr(self.settings, "provider_id", ""):
            return self.settings.provider_id
        try:
            provider = await self.context.get_using_provider_async()
            return str(provider.meta().id) if provider else ""
        except Exception:
            return ""

    def _today_count(self) -> int:
        """Read how many lookups ran today.

        Returns:
            Today's count.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if self._daily_key != today:
            self._daily_key = today
            self._daily_count = 0
        return self._daily_count

    def _bump_daily(self) -> None:
        """Count one completed lookup."""
        self._daily_count += 1

    def _in_quiet_hours(self) -> bool:
        """Tell whether the current time is inside the quiet window.

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
        now = datetime.now().time()
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end
