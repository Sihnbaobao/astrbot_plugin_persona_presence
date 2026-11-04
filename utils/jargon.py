"""Mine group-specific slang from chat history.

The pipeline mirrors what mature learning plugins do:

1. Count repeated character n-grams to pre-filter candidates.
2. Ask the model which candidates are actually group slang.
3. Infer the meaning three ways (with context, word only, then compare)
   so a dictionary definition never wins over the local sense.
"""

import json
import re
from collections import Counter

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_STOP_TERMS = {
    "什么",
    "怎么",
    "这个",
    "那个",
    "我们",
    "你们",
    "他们",
    "可以",
    "就是",
    "没有",
    "不是",
    "知道",
    "觉得",
    "现在",
    "时候",
    "一下",
    "真的",
    "还是",
    "然后",
    "因为",
    "所以",
    "但是",
}

# 纯语气词，不算黑话
_FILLER_CHARS = set("哈呵嘿嘻嗯啊哦噢呃额草bbb")

_EXTRACT_PROMPT = """判断下面这些词里，哪些是只有这个群的人才懂的说法：缩写、梗、内部称呼、谐音、特定用法。

候选词：{terms}

聊天记录：
{context}

只输出 JSON 数组，最多 2 条：[{{"term": "词", "sample": "用到它的原句"}}]
看起来能直接理解的普通词不要选；没有就输出 []"""

_MEANING_CONTEXT_PROMPT = """群里的一段对话里出现了「{term}」：

{context}

结合上下文推断「{term}」在这个群里是什么意思。
只输出 JSON：{{"meaning": "这个群里的用法，一句话", "alt": "其他常见义项，一句话，没有就留空"}}
如果上下文不足以判断，输出 {{"no_info": true}}"""

_MEANING_ONLY_PROMPT = """「{term}」是某个群聊里的说法。仅凭这个词猜它的意思。
只输出 JSON：{{"meaning": "含义"}}；如果没把握，输出 {{"no_info": true}}"""

_COMPARE_PROMPT = """「{term}」有两次推断：

推断A（结合群聊上下文）：{a}
推断B（仅凭这个词）：{b}

哪一次更可能是这个群里的真实意思？只输出 JSON：
{{"meaning": "最终含义", "confidence": 0.0-1.0}}
两次都不可信就输出 {{"no_info": true}}"""


def candidate_terms(
    lines: list[str], min_count: int = 2, limit: int = 8
) -> list[tuple[str, int]]:
    """Collect repeated character n-grams as slang candidates.

    Args:
        lines: Recent chat lines from one group.
        min_count: How often a phrase must appear to qualify.
        limit: Maximum number of candidates to return.

    Returns:
        Candidate terms with their counts, longest and most frequent first.
    """
    counter: Counter[str] = Counter()
    for line in lines:
        for run in _CJK_RUN.findall(str(line or "")):
            size = len(run)
            for n in range(2, min(6, size) + 1):
                for start in range(size - n + 1):
                    gram = run[start : start + n]
                    if gram in _STOP_TERMS:
                        continue
                    if set(gram) <= _FILLER_CHARS:
                        continue
                    counter[gram] += 1

    picked: list[tuple[str, int]] = []
    for term, count in sorted(counter.items(), key=lambda kv: (-len(kv[0]), -kv[1])):
        if count < min_count:
            continue
        # 已经被选中的长词覆盖时跳过，避免「奶龙」和「奶龙吧」重复入选
        if any(term in longer for longer, _ in picked):
            continue
        picked.append((term, count))
        if len(picked) >= limit:
            break
    return picked


def build_extract_prompt(candidates: list[tuple[str, int]], lines: list[str]) -> str:
    """Build the prompt that filters candidates down to real slang.

    Args:
        candidates: Output of candidate_terms().
        lines: Recent chat lines used as evidence.

    Returns:
        A prompt asking for a JSON array of slang terms.
    """
    terms = "、".join(term for term, _ in candidates) or "（没有候选）"
    context = "\n".join(f"- {line}" for line in lines[-30:])
    return _EXTRACT_PROMPT.format(terms=terms, context=context)


def build_meaning_prompts(
    term: str, sample: str, lines: list[str]
) -> tuple[str, str, str]:
    """Build the three-step meaning inference prompts.

    Args:
        term: The slang term.
        sample: A line that used the term.
        lines: Recent chat lines for context.

    Returns:
        Two ready prompts plus the comparison template, which still holds
        {term}, {a} and {b} placeholders for a single format call.
    """
    context = "\n".join(f"- {line}" for line in lines[-20:])
    if sample:
        context = f"- {sample}\n{context}"
    return (
        _MEANING_CONTEXT_PROMPT.format(term=term, context=context),
        _MEANING_ONLY_PROMPT.format(term=term),
        _COMPARE_PROMPT,
    )


def parse_json_block(text: str) -> object | None:
    """Extract the first JSON value from a model reply.

    Args:
        text: Raw model output.

    Returns:
        The parsed value, or None when nothing parses.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    for opener, closer in (("[", "]"), ("{", "}")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None
