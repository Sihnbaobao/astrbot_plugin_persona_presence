"""
回复处理器模块（精简版）
负责调用AI生成回复

作者: Sihnbaobao（重构）
版本: 0.0.3

重构要点（REFACTOR_DESIGN.md）：
- 删除 SYSTEM_REPLY_PROMPT（约100行系统行为指令）—— 这是群聊人格漂移的最大来源
- 回复请求的 system_prompt 使用当前会话最终生效的人格
- prompt 以纯上下文为主；仅在群聊涉及其他用户时追加最小对象边界提示
- 保留标记机制：on_llm_request 钩子（priority=-1）据此恢复完整 prompt，
  同时保留其他插件（emotionai/livingmemory 等）对请求的注入
- 保留短消息占位机制：event.request_llm() 的 prompt 传当前消息短文本，
  供向量检索类插件（livingmemory）召回；本插件钩子再换回完整上下文
"""

import re

from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent
from astrbot.core.provider.entities import ProviderRequest

from .ai_error_formatter import format_ai_error

# 详细日志开关（与 main.py 同款方式：单独用 if 控制）
DEBUG_MODE: bool = False

# 标记键名，用于标识请求来自本插件
PLUGIN_REQUEST_MARKER = "_group_chat_plus_request"
# 存储插件自定义上下文的键名（供 on_llm_request 恢复）
PLUGIN_CUSTOM_CONTEXTS = "_group_chat_plus_contexts"
# 存储插件自定义系统提示词（人格）的键名
PLUGIN_CUSTOM_SYSTEM_PROMPT = "_group_chat_plus_system_prompt"
# 存储插件自定义完整 prompt 的键名（供 on_llm_request 恢复）
PLUGIN_CUSTOM_PROMPT = "_group_chat_plus_prompt"
# 存储图片 URL 列表的键名
PLUGIN_IMAGE_URLS = "_group_chat_plus_image_urls"
# 存储插件自身工具集（ToolSet）的键名，用于在 on_llm_request 钩子中合并
PLUGIN_FUNC_TOOL = "_group_chat_plus_func_tool"
# 存储当前用户消息原文（短字符串），供向量检索类插件（livingmemory）的记忆召回
PLUGIN_CURRENT_MESSAGE = "_group_chat_plus_current_message"


class ReplyHandler:
    """
    回复处理器（精简版）

    主要功能：
    1. 构建回复提示词（上下文 + 必要的对象边界提示）
    2. 调用AI生成回复（event.request_llm）
    3. 检测是否已被其他插件处理
    """

    # Keep the reply-generation prompt limited to context framing.
    PROMPT_ENDING = "\n\n---\n以上是消息上下文，请直接输出你的回复。"

    @staticmethod
    def remove_echo_prefix(reply_text: str, current_message: str) -> str:
        """Remove a current-message phrase echoed as a sentence opener.

        Args:
            reply_text: The generated plain-text reply.
            current_message: The current user's raw message.

        Returns:
            The reply with a mechanical phrase-plus-particle opener removed when
            the remaining reply still contains meaningful content.
        """
        if not reply_text or not current_message:
            return reply_text

        leading_match = re.match(r"^[\s.。…]+", reply_text)
        leading = leading_match.group(0) if leading_match else ""
        body = reply_text[len(leading) :]
        particles = "啊吧呢哦噢哇呀诶欸呐啦嘛咯喽耶哎"
        separator_pattern = r"^[\s.。…!?！？,，、:：;；~～—-]*"
        max_candidate_length = min(24, len(body) - 1)

        for candidate_length in range(max_candidate_length, 1, -1):
            candidate = body[:candidate_length]
            if not re.fullmatch(r"[A-Za-z0-9\u3400-\u9fff]+", candidate):
                continue
            if body[candidate_length] not in particles:
                continue
            if candidate not in current_message:
                continue

            remainder = body[candidate_length + 1 :]
            separator_match = re.match(separator_pattern, remainder)
            if separator_match:
                remainder = remainder[separator_match.end() :]
            if not remainder.strip():
                continue
            return f"{leading}{remainder}".strip()

        return reply_text

    @staticmethod
    async def generate_reply(
        event: AstrMessageEvent,
        context: Context,
        formatted_message: str,
        extra_prompt: str,
        prompt_mode: str = "append",
        image_urls: list = None,
        audio_urls: list = None,
        include_sender_info: bool = True,
        include_timestamp: bool = True,
        history_messages: list = None,
        smart_batch_reply_hint: str = "",
        reply_context_hint: str = "",
        allow_tools: bool = True,
    ) -> ProviderRequest:
        """
        生成AI回复（精简版）

        系统提示词只含人格设定；prompt 以纯上下文为主，
        仅在需要时追加最小对象边界提示，避免代替其他用户作答。

        Args:
            event: 消息事件
            context: Context对象
            formatted_message: 格式化后的完整上下文（历史+当前消息，含发送者标注）
            extra_prompt: 用户自定义补充提示词（可覆盖或追加）
            prompt_mode: 提示词模式，append=拼接，override=覆盖
            image_urls: 图片URL列表（用于多模态AI）
            audio_urls: 音频URL列表
            include_sender_info: 是否包含发送者信息
            include_timestamp: 是否包含时间戳
            history_messages: 历史消息列表（保留参数以兼容调用，构建contexts用）
            smart_batch_reply_hint: Smart并发批次提示（可选追加消息说明）
            reply_context_hint: 群聊对象边界提示，仅在当前消息涉及其他用户时追加
            allow_tools: 是否把 AstrBot 工具集附加到请求；预生成纯文本时关闭

        Returns:
            ProviderRequest对象
        """
        if image_urls is None:
            image_urls = []
        if audio_urls is None:
            audio_urls = []
        if history_messages is None:
            history_messages = []

        # 群聊历史中所有非 bot 消息均为 role="user"，LLM 无法从结构区分发送者，
        # 因此 contexts 保持为空，全部上下文以文本形式包含在 prompt 中
        # （每条消息均已标注 [时间] 昵称(ID): 内容）
        contexts = []

        try:
            # 发送者标注（"谁在说话"的必要信息，非行为指令）
            sender_emphasis = ""
            sender_id = event.get_sender_id()
            sender_name = event.get_sender_name()
            if include_sender_info:
                if sender_name:
                    sender_emphasis = (
                        f"[系统信息-当前发送者] {sender_name}（ID:{sender_id}）"
                    )
                else:
                    sender_emphasis = f"[系统信息-当前发送者] 用户ID:{sender_id}"

            smart_hint_text = (smart_batch_reply_hint or "").strip()
            reply_context_hint_text = (reply_context_hint or "").strip()

            if prompt_mode == "override" and extra_prompt and extra_prompt.strip():
                # 覆盖模式：用户自定义提示词完全替代默认内容
                full_prompt = (
                    extra_prompt.strip()
                    + "\n\n"
                    + sender_emphasis
                    + "\n"
                    + formatted_message
                    + (
                        ("\n" + reply_context_hint_text)
                        if reply_context_hint_text
                        else ""
                    )
                    + (("\n" + smart_hint_text) if smart_hint_text else "")
                    + ReplyHandler.PROMPT_ENDING
                )
            else:
                # 拼接模式（默认）：上下文与可选对象边界提示
                full_prompt = (
                    sender_emphasis
                    + "\n"
                    + formatted_message
                    + (
                        ("\n" + reply_context_hint_text)
                        if reply_context_hint_text
                        else ""
                    )
                    + (("\n" + smart_hint_text) if smart_hint_text else "")
                    + ReplyHandler.PROMPT_ENDING
                )

            logger.debug(
                f"正在调用AI生成回复（当前发送者：{sender_name or '未知'}，ID:{sender_id}）..."
            )

            # Speculative formal replies deliberately run without tools.
            func_tools_mgr = None
            plugin_tool_set = None
            if allow_tools:
                func_tools_mgr = context.get_llm_tool_manager()
                try:
                    if hasattr(func_tools_mgr, "get_full_tool_set"):
                        plugin_tool_set = func_tools_mgr.get_full_tool_set()
                    else:
                        plugin_tool_set = func_tools_mgr
                except Exception:
                    pass

            # Resolve the final persona for this conversation on every request.
            # This keeps session-forced and conversation-selected personas in sync
            # without passing a conversation to request_llm (which would duplicate
            # the plugin's existing official-history save path).
            system_prompt = ""
            conversation = None
            try:
                conversation_manager = getattr(context, "conversation_manager", None)
                if conversation_manager is not None:
                    conversation_id = (
                        await conversation_manager.get_curr_conversation_id(
                            event.unified_msg_origin
                        )
                    )
                    if conversation_id:
                        conversation = await conversation_manager.get_conversation(
                            event.unified_msg_origin, conversation_id
                        )

                persona_manager = context.persona_manager
                active_persona = None
                if hasattr(persona_manager, "resolve_selected_persona"):
                    config_getter = getattr(context, "get_config", None)
                    provider_settings = {}
                    if callable(config_getter):
                        provider_config = config_getter(umo=event.unified_msg_origin)
                        provider_settings = (
                            provider_config.get("provider_settings", {})
                            if provider_config
                            else {}
                        )
                    (
                        _,
                        active_persona,
                        _,
                        _,
                    ) = await persona_manager.resolve_selected_persona(
                        umo=event.unified_msg_origin,
                        conversation_persona_id=getattr(
                            conversation, "persona_id", None
                        ),
                        platform_name=(
                            event.get_platform_name()
                            if hasattr(event, "get_platform_name")
                            else ""
                        ),
                        provider_settings=provider_settings,
                    )

                if active_persona is None:
                    active_persona = await persona_manager.get_default_persona_v3(
                        event.unified_msg_origin
                    )
                system_prompt = active_persona.get("prompt", "") or ""

                begin_dialogs = active_persona.get("_begin_dialogs_processed", [])
                if begin_dialogs:
                    dialog_parts = []
                    for dialog in begin_dialogs:
                        role = dialog.get("role", "user")
                        content = dialog.get("content", "")
                        if role == "user":
                            dialog_parts.append(f"用户: {content}")
                        elif role == "assistant":
                            dialog_parts.append(f"AI: {content}")
                    if dialog_parts:
                        full_prompt += (
                            "\n=== 预设对话 ===\n" + "\n".join(dialog_parts) + "\n\n"
                        )
                if DEBUG_MODE:
                    logger.debug(
                        f"Resolved reply persona prompt length: {len(system_prompt)}"
                    )
            except Exception as e:
                logger.warning(f"Failed to resolve the active persona: {e}")
                try:
                    default_persona = (
                        await context.persona_manager.get_default_persona_v3(
                            event.unified_msg_origin
                        )
                    )
                    system_prompt = default_persona.get("prompt", "") or ""
                except Exception as fallback_error:
                    logger.warning(
                        f"Failed to resolve the default persona: {fallback_error}"
                    )

            # 标记请求来源，供 on_llm_request 钩子识别
            event.set_extra(PLUGIN_REQUEST_MARKER, True)
            event.set_extra(PLUGIN_CUSTOM_CONTEXTS, contexts)
            event.set_extra(PLUGIN_CUSTOM_SYSTEM_PROMPT, system_prompt)
            event.set_extra(PLUGIN_CUSTOM_PROMPT, full_prompt)
            event.set_extra(PLUGIN_IMAGE_URLS, image_urls)
            event.set_extra("_plugin_audio_urls", audio_urls)
            event.set_extra(PLUGIN_FUNC_TOOL, plugin_tool_set)

            # 短消息占位：供向量检索类插件（livingmemory）作为召回查询词，
            # 本插件 on_llm_request 钩子（priority=-1）会把 req.prompt 换回完整上下文
            current_message_for_retrieval = event.get_message_str() or ""
            # 单独无信息@消息时 get_message_str() 返回 ""，用占位符避免空 prompt
            prompt_for_request = current_message_for_retrieval or "[空消息]"
            event.set_extra(PLUGIN_CURRENT_MESSAGE, prompt_for_request)

            if DEBUG_MODE:
                logger.debug("🔧 已设置插件标记，将通过 event.request_llm() 调用 AI")
                logger.debug(f"  - system_prompt 长度: {len(system_prompt)}")
                logger.debug(f"  - full_prompt 长度: {len(full_prompt)}")
                logger.debug(f"  - image_urls 数量: {len(image_urls)}")
                logger.debug(
                    f"  - 向量检索用短消息长度: {len(current_message_for_retrieval)}"
                )

            return event.request_llm(
                prompt=prompt_for_request,
                func_tool_manager=func_tools_mgr,
                tool_set=plugin_tool_set,
                session_id=event.session_id,
                image_urls=image_urls,
                audio_urls=audio_urls,
                contexts=contexts,
                system_prompt=system_prompt,
            )

        except Exception as e:
            logger.error(f"{format_ai_error(e, '生成AI回复')}")
            try:
                event.set_extra("_group_chat_plus_reply_error", True)
            except Exception:
                pass
            return event.plain_result(
                f"生成回复时发生错误: {format_ai_error(e, '生成AI回复')}"
            )

    @staticmethod
    async def generate_speculative_reply(
        event: AstrMessageEvent,
        context: Context,
        formatted_message: str,
        extra_prompt: str,
        prompt_mode: str = "append",
        history_messages: list | None = None,
        smart_batch_reply_hint: str = "",
        reply_context_hint: str = "",
    ) -> str | None:
        """Generate a buffered direct private text reply without executing tools.

        Args:
            event: Message event used to resolve persona and provider selection.
            context: AstrBot context used to access the active provider.
            formatted_message: Full context for the current private turn.
            extra_prompt: User-configured reply prompt extension.
            prompt_mode: Prompt extension mode.
            history_messages: Preserved compatibility argument for prompt building.
            smart_batch_reply_hint: Smart batch context appended to the prompt.
            reply_context_hint: Additional context boundary text.

        Returns:
            Plain generated text, or None when speculative generation is unavailable.

        Raises:
            asyncio.CancelledError: Propagates cancellation so a follower can discard
                the in-flight request without committing its result.
        """
        request = await ReplyHandler.generate_reply(
            event,
            context,
            formatted_message,
            extra_prompt,
            prompt_mode,
            image_urls=[],
            audio_urls=[],
            history_messages=history_messages,
            smart_batch_reply_hint=smart_batch_reply_hint,
            reply_context_hint=reply_context_hint,
            allow_tools=True,
        )
        if not isinstance(request, ProviderRequest):
            event.set_extra("_group_chat_plus_reply_error", False)
            return None

        prompt = event.get_extra(PLUGIN_CUSTOM_PROMPT) or request.prompt or ""
        system_prompt = (
            event.get_extra(PLUGIN_CUSTOM_SYSTEM_PROMPT) or request.system_prompt or ""
        )
        provider = await context.get_using_provider_async(event.unified_msg_origin)
        if provider is None:
            logger.info(
                "[Private Speculative] No active provider; using normal reply path"
            )
            return None

        response = await provider.text_chat(
            prompt=prompt,
            session_id=request.session_id or event.session_id,
            contexts=request.contexts,
            system_prompt=system_prompt,
            image_urls=[],
            audio_urls=[],
            func_tool=request.func_tool,
            extra_user_content_parts=request.extra_user_content_parts,
            model=request.model,
        )
        if getattr(response, "tools_call_args", None) or getattr(
            response, "tools_call_name", None
        ):
            logger.info(
                "[Private Speculative] Tool call requested; falling back to the full Agent path"
            )
            return None
        if getattr(response, "role", "") != "assistant":
            return None
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text and getattr(response, "result_chain", None):
            text = response.result_chain.get_plain_text().strip()
        return text or None

    @staticmethod
    def check_if_already_replied(event: AstrMessageEvent) -> bool:
        """
        检查消息是否已被其他插件处理

        通过 _has_send_oper 标记判断，该标记在 event.send() 被调用后永久置为 True，
        不受框架 clear_result() 影响。

        Args:
            event: 消息事件

        Returns:
            True=已有回复，False=尚未回复
        """
        return getattr(event, "_has_send_oper", False)
