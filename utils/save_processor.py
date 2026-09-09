"""
回复保存域（SaveMixin）。

从主插件类拆出的独立功能域：消息发送后的历史保存、去重落库、
交错工具调用记录、批次汇总提示等。通过 self 访问 PersonaPresence 状态与工具。
"""

import time

from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import filter

from .context_manager import ContextManager
from .message_cleaner import MessageCleaner
from .message_processor import MessageProcessor  # noqa: F403
from .probability_manager import ProbabilityManager


class SaveMixin:
    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """
        消息发送后的钩子，保存AI回复与用户消息到官方对话系统
        """
        try:
            is_private = event.is_private_chat()
            chat_id = event.get_group_id() if not is_private else event.get_sender_id()
            chat_id = ProbabilityManager.get_chat_key(
                event.get_platform_name(), is_private, chat_id
            )

            message_id = self._get_processing_id(event)

            async with self.concurrent_lock:
                if message_id not in self.processing_sessions:
                    return

                self._chat_flow_owners[chat_id] = {
                    "owner": "normal",
                    "processing_id": message_id,
                    "started_at": time.time(),
                }

                # 双重防护：检查此消息是否已经保存过
                if message_id in self._saved_messages:
                    return

                # 多轮工具调用支持：检查agent是否已完成
                is_agent_done = message_id in self._agent_done_flags

                ai_error_flag_early = (
                    hasattr(self, "_ai_error_message_ids")
                    and message_id in self._ai_error_message_ids
                )

                if not is_agent_done:
                    pending_count = len(self._pending_bot_replies.get(message_id, []))

                    # 异常终止信号检测：AI错误标记 或 非LLM终端响应
                    force_done = False
                    if ai_error_flag_early and pending_count > 0:
                        force_done = True
                        logger.warning(
                            f"[消息发送后] 检测到 AI 调用错误标记，强制完成 agent "
                            f"以保存 {pending_count} 段累积回复"
                        )
                    elif pending_count > 0:
                        _result_obj = event._result if event._result else None
                        if _result_obj:
                            try:
                                _is_llm = _result_obj.is_llm_result()
                            except Exception:
                                _is_llm = False
                            if not _is_llm and _result_obj.chain:
                                _text = "".join(
                                    self._coerce_component_text(
                                        getattr(c, "text", None)
                                    )
                                    for c in _result_obj.chain
                                ).strip()
                                if _text:
                                    force_done = True
                                    logger.warning(
                                        f"[消息发送后] agent_done 标志未设置但收到非 LLM 终端响应"
                                        f"（长度 {len(_text)} 字符），强制保存 {pending_count} 段累积回复"
                                    )

                    if force_done:
                        self._agent_done_flags.add(message_id)
                        is_agent_done = True
                    else:
                        logger.info(
                            f"[消息发送后] agent尚未完成（多轮工具调用中），"
                            f"已累积 {pending_count} 段回复，等待agent完成后统一保存"
                        )
                        return

                # agent已完成，清除标记并进行最终保存
                del self.processing_sessions[message_id]
                self._agent_done_flags.discard(message_id)

            # 重复消息拦截检查
            is_duplicate_blocked = message_id in self._duplicate_blocked_messages
            if is_duplicate_blocked:
                del self._duplicate_blocked_messages[message_id]
                logger.info(
                    f"[消息发送后] 会话 {chat_id} 检测到重复消息拦截标记，将跳过AI消息保存，但继续保存用户消息"
                )

            if not is_duplicate_blocked and (
                not event._result or not hasattr(event._result, "chain")
            ):
                logger.info(f"[消息发送后] 会话 {chat_id} 没有result或chain，跳过")
                return

            result_obj = event._result if event._result else None
            is_llm_result = False
            if result_obj:
                try:
                    is_llm_result = bool(result_obj.is_llm_result())
                except Exception:
                    is_llm_result = False

            ai_error_flag = (
                hasattr(self, "_ai_error_message_ids")
                and message_id in self._ai_error_message_ids
            )

            if not is_duplicate_blocked and not is_llm_result and not ai_error_flag:
                logger.info(f"[消息发送后] 会话 {chat_id} 不是LLM结果，跳过")
                return

            # 提取回复文本
            displayed_bot_reply_text = ""
            original_bot_reply_text = ""
            bot_reply_to_save = None
            is_empty_reply = False
            accumulated_texts = []

            _has_pending = bool(self._pending_bot_replies.get(message_id, []))
            _should_pop_pending = is_llm_result or ai_error_flag or _has_pending

            if _should_pop_pending and not is_duplicate_blocked:
                accumulated_texts = self._pending_bot_replies.pop(message_id, [])
                if hasattr(self, "raw_reply_cache"):
                    self.raw_reply_cache.pop(message_id, "")

                if accumulated_texts:
                    original_bot_reply_text = "\n".join(accumulated_texts)
                    displayed_bot_reply_text = original_bot_reply_text
                    if len(accumulated_texts) > 1:
                        logger.info(
                            f"[消息发送后] 🔧 多轮工具调用：合并了 {len(accumulated_texts)} 段AI回复，"
                            f"总长度: {len(original_bot_reply_text)} 字符"
                        )
                else:
                    displayed_bot_reply_text = "".join(
                        self._coerce_component_text(getattr(comp, "text", None))
                        for comp in result_obj.chain
                    )
                    original_bot_reply_text = displayed_bot_reply_text

                if not original_bot_reply_text:
                    is_empty_reply = True
                    logger.warning(
                        f"[消息发送后] 会话 {chat_id} 回复文本为空，进入降级保存：仅保存用户消息与缓存上下文"
                    )

            if not is_empty_reply and is_private:
                pending_boundary_reply = event.get_extra(
                    "_persona_private_boundary_reply"
                )
                if isinstance(pending_boundary_reply, dict):
                    event.set_extra("_persona_private_boundary_reply", None)
                    pending_chat_key = str(pending_boundary_reply.get("chat_key") or "")
                    expected_boundary = pending_boundary_reply.get("boundary")
                    active_boundary = self._private_conversation_state.get(
                        pending_chat_key
                    )
                    if (
                        pending_chat_key == chat_id
                        and active_boundary is expected_boundary
                    ):
                        # The message was sent, so the accepted boundary can now be replaced.
                        self._cancel_private_wake(pending_chat_key)
                        self._private_conversation_state.clear(pending_chat_key)
                        self._private_conversation_state.clear_pending_messages(
                            pending_chat_key
                        )
                        new_boundary = self._private_conversation_state.record_reply(
                            pending_chat_key, displayed_bot_reply_text
                        )
                        if new_boundary:
                            self._schedule_private_wake(
                                event, pending_chat_key, new_boundary
                            )
                            boundary_label = (
                                "dismissive_review_hint"
                                if new_boundary.kind == "dismissive"
                                else new_boundary.kind
                            )
                            logger.info(
                                "[Private boundary] committed after visible reply "
                                "state=%s mode=%s reopen_in=%ss wake_in=%ss "
                                "expires_in=%ss",
                                boundary_label,
                                getattr(new_boundary, "sleep_mode", "none"),
                                new_boundary.seconds_until_reopen(),
                                new_boundary.seconds_until_wake(),
                                new_boundary.seconds_until_expiry(),
                            )
                        elif self.debug_mode:
                            logger.info(
                                "[Private boundary] accepted boundary cleared "
                                "after visible reply"
                            )
                    elif self.debug_mode:
                        logger.warning(
                            "[Private boundary] skipped stale post-send transition "
                            "for chat %s",
                            pending_chat_key or chat_id,
                        )

            # 保存AI消息
            if _should_pop_pending and not is_duplicate_blocked and not is_empty_reply:
                if self.debug_mode:
                    logger.info(
                        f"【消息发送后】会话 {chat_id} - 保存AI回复，长度: {len(original_bot_reply_text)} 字符"
                    )

                bot_reply_to_save = original_bot_reply_text

                # 构建交错排列的工具调用+文本回复（保留时间顺序）
                interleaved_reply = self._build_interleaved_tool_reply(
                    event, accumulated_texts
                )
                if interleaved_reply:
                    bot_reply_to_save = interleaved_reply
                    logger.info("[工具调用] 已构建交错排列的工具调用记录到AI回复历史")

                await ContextManager.save_bot_message(
                    event, bot_reply_to_save, self.context
                )

                # 记录到最近回复缓存（用于后续去重）
                try:
                    already_cached = False
                    if chat_id in self.recent_replies_cache:
                        for recent in self.recent_replies_cache[chat_id][-3:]:
                            if (
                                recent.get("content", "")
                                == original_bot_reply_text.strip()
                            ):
                                already_cached = True
                                break
                    if not already_cached:
                        if chat_id not in self.recent_replies_cache:
                            self.recent_replies_cache[chat_id] = []
                        self.recent_replies_cache[chat_id].append(
                            {
                                "content": original_bot_reply_text,
                                "timestamp": time.time(),
                            }
                        )
                        max_cache_size = min(
                            max(10, self.duplicate_filter_check_count * 2),
                            self._DUPLICATE_CACHE_SIZE_LIMIT,
                        )
                        if len(self.recent_replies_cache[chat_id]) > max_cache_size:
                            self.recent_replies_cache[chat_id] = (
                                self.recent_replies_cache[chat_id][-max_cache_size:]
                            )
                except Exception:
                    pass

            # 获取用户消息（优先使用并发安全的缓存快照）
            message_to_save = ""
            smart_batch_messages = self._smart_batch_snapshots.pop(message_id, [])

            last_cached = self._message_cache_snapshots.pop(message_id, None)
            if not last_cached:
                if chat_id in self.pending_messages_cache:
                    for cached_msg in reversed(self.pending_messages_cache[chat_id]):
                        if (
                            isinstance(cached_msg, dict)
                            and cached_msg.get("message_id") == message_id
                        ):
                            last_cached = cached_msg
                            break

            if (
                last_cached
                and isinstance(last_cached, dict)
                and "content" in last_cached
            ):
                raw_content = last_cached["content"]

                trigger_type = None
                if last_cached.get("has_trigger_keyword"):
                    trigger_type = "keyword"
                elif last_cached.get("is_at_message"):
                    trigger_type = "at"
                else:
                    trigger_type = "ai_decision"

                message_to_save = MessageProcessor.add_metadata_from_cache(
                    raw_content,
                    last_cached.get("sender_id", event.get_sender_id()),
                    last_cached.get("sender_name", event.get_sender_name()),
                    last_cached.get("message_timestamp")
                    or last_cached.get("timestamp"),
                    self.include_timestamp,
                    self.include_sender_info,
                    last_cached.get("mention_info"),
                    trigger_type,
                    last_cached.get("poke_info"),
                    last_cached.get("is_empty_at", False),
                    "",
                    last_cached.get("is_at_all_message", False),
                    persistent_poke_event_text=last_cached.get(
                        "persistent_poke_event_text", ""
                    )
                    if isinstance(last_cached, dict)
                    else "",
                )
                message_to_save = MessageCleaner.clean_message(message_to_save)

            # 如果缓存中没有，尝试从当前消息提取
            if not message_to_save:
                logger.warning(
                    "[消息发送后] ⚠️ 缓存中无消息，从event提取消息（不应该发生）"
                )
                processed = MessageCleaner.extract_raw_message_from_event(
                    event, self_id=str(event.get_self_id())
                )
                mention_info = (
                    last_cached.get("mention_info")
                    if isinstance(last_cached, dict)
                    else None
                )
                if processed:
                    message_to_save = MessageProcessor.add_metadata_to_message(
                        event,
                        processed,
                        self.include_timestamp,
                        self.include_sender_info,
                        mention_info,
                        None,
                        None,
                        False,
                        "",
                        "",
                        is_at_all_message=bool(
                            last_cached.get("is_at_all_message", False)
                            if isinstance(last_cached, dict)
                            else False
                        ),
                        persistent_poke_event_text=last_cached.get(
                            "persistent_poke_event_text", ""
                        )
                        if isinstance(last_cached, dict)
                        else "",
                    )
                    message_to_save = MessageCleaner.clean_message(message_to_save)

            if not message_to_save:
                logger.warning("[消息发送后] 无法获取用户消息，跳过官方保存")
                return

            if self.debug_mode:
                logger.info(
                    f"[消息发送后] 准备保存到官方系统的消息: {message_to_save[:300]}..."
                )

            # 准备需要转正的缓存消息
            current_msg_timestamp = None
            current_msg_id = None
            if last_cached:
                current_msg_timestamp = last_cached.get("timestamp")
                current_msg_id = last_cached.get("message_id")

            async with self.concurrent_lock:
                processing_msg_ids = set(self.processing_sessions.keys())

            cached_messages_to_convert = self.cache_manager.prepare_cache_for_save(
                chat_id=chat_id,
                current_msg_id=current_msg_id,
                current_msg_timestamp=current_msg_timestamp,
                processing_msg_ids=processing_msg_ids,
                proactive_processing=False,
            )

            # Smart 批次消息转正
            for _smart_msg in smart_batch_messages:
                _smart_content = ContextManager._content_to_safe_text(
                    _smart_msg.get("content", "")
                )
                if not _smart_content:
                    continue
                _smart_trigger = (
                    "keyword"
                    if _smart_msg.get("has_trigger_keyword")
                    else ("at" if _smart_msg.get("is_at_message") else "ai_decision")
                )
                _smart_message_to_save = MessageProcessor.add_metadata_from_cache(
                    _smart_content,
                    _smart_msg.get("sender_id", "unknown"),
                    _smart_msg.get("sender_name", "未知用户"),
                    _smart_msg.get("message_timestamp") or _smart_msg.get("timestamp"),
                    self.include_timestamp,
                    self.include_sender_info,
                    _smart_msg.get("mention_info"),
                    _smart_trigger,
                    _smart_msg.get("poke_info"),
                    _smart_msg.get("is_empty_at", False),
                    "",
                    _smart_msg.get("is_at_all_message", False),
                    persistent_poke_event_text=_smart_msg.get(
                        "persistent_poke_event_text", ""
                    ),
                )
                _smart_message_to_save = MessageCleaner.clean_message(
                    _smart_message_to_save
                )
                if _smart_message_to_save:
                    cached_messages_to_convert.append(
                        {
                            "role": "user",
                            "content": _smart_message_to_save,
                            "sender_id": _smart_msg.get("sender_id", "unknown"),
                            "sender_name": _smart_msg.get("sender_name", "未知用户"),
                            "message_timestamp": _smart_msg.get("message_timestamp")
                            or _smart_msg.get("timestamp"),
                            "message_id": _smart_msg.get("message_id", ""),
                            "image_urls": _smart_msg.get("image_urls", []),
                        }
                    )

            # Delayed private wake events keep older pending messages in the
            # event extras so the normal after-send path can save them with the
            # synthetic event's latest message.
            private_wake_backlog = (
                event.get_extra("_persona_private_wake_backlog", []) or []
            )
            if private_wake_backlog:
                existing_message_ids = {
                    str(message.get("message_id"))
                    for message in cached_messages_to_convert
                    if message.get("message_id")
                }
                for backlog_message in private_wake_backlog:
                    if not isinstance(backlog_message, dict):
                        continue
                    backlog_id = str(backlog_message.get("message_id") or "")
                    if backlog_id and backlog_id in existing_message_ids:
                        continue
                    backlog_copy = dict(backlog_message)
                    backlog_copy["role"] = "user"
                    cached_messages_to_convert.append(backlog_copy)
                    if backlog_id:
                        existing_message_ids.add(backlog_id)

            # 确定是否保存AI回复
            if is_duplicate_blocked:
                bot_to_save = None
                logger.info(
                    f"[消息发送后] 准备保存: 缓存{len(cached_messages_to_convert)}条 + 当前用户消息（跳过AI回复，重复消息已拦截）"
                )
            elif not is_llm_result and ai_error_flag:
                bot_to_save = None
                logger.info(
                    f"[消息发送后] 准备保存: 缓存{len(cached_messages_to_convert)}条 + 当前用户消息（跳过AI回复，AI调用错误）"
                )
            else:
                bot_to_save = bot_reply_to_save
                logger.info(
                    f"[消息发送后] 准备保存: 缓存{len(cached_messages_to_convert)}条 + 当前对话(用户+AI)"
                )

            success = await ContextManager.save_to_official_conversation_with_cache(
                event,
                cached_messages_to_convert,
                message_to_save,
                bot_to_save,
                self.context,
            )

            if success:
                logger.info("[消息发送后] ✅ Phase-1 成功保存到官方对话系统")
                # 清理已保存的缓存消息
                self.cache_manager.clear_saved_cache(
                    chat_id=chat_id,
                    current_msg_id=current_msg_id,
                    current_msg_timestamp=current_msg_timestamp,
                    processing_msg_ids=processing_msg_ids,
                    proactive_processing=False,
                )

                # Phase-2: 保存窗口缓冲消息（Smart批次）
                try:
                    window_buffered_to_convert = (
                        self.cache_manager.prepare_window_buffered_for_save(
                            chat_id=chat_id,
                            processing_msg_ids=processing_msg_ids,
                        )
                    )
                    if window_buffered_to_convert:
                        wb_saved_msg_ids = set()
                        for wb_msg in self.cache_manager.get_window_buffered_messages(
                            chat_id
                        ):
                            msg_id = wb_msg.get("message_id")
                            if msg_id:
                                wb_saved_msg_ids.add(msg_id)

                        phase2_success = await ContextManager.save_to_official_conversation_with_cache(
                            event,
                            window_buffered_to_convert,
                            None,
                            None,
                            self.context,
                        )
                        if phase2_success:
                            self.cache_manager.clear_window_buffered_cache(
                                chat_id, saved_msg_ids=wb_saved_msg_ids
                            )
                            self._smart_batch_snapshots.pop(message_id, None)
                            logger.info("[消息发送后] Phase-2: ✅ 窗口缓冲消息已转正")
                except Exception as phase2_err:
                    logger.warning(
                        f"[消息发送后] Phase-2: ⚠️ 窗口缓冲消息处理异常（降级：留在缓存）: {phase2_err}"
                    )

                # 标记消息已保存（防止分段消息重复保存）
                self._saved_messages[message_id] = time.time()

                # 清理过期的已保存标记（保留最近5分钟内的）
                try:
                    cutoff_time = time.time() - 300
                    items_snapshot = list(self._saved_messages.items())
                    expired_ids = [
                        msg_id
                        for msg_id, timestamp in items_snapshot
                        if timestamp < cutoff_time
                    ]
                    for msg_id in expired_ids:
                        self._saved_messages.pop(msg_id, None)
                except Exception:
                    pass
            else:
                logger.warning("[消息发送后] ⚠️ 保存到官方对话系统失败")
                if self.debug_mode:
                    logger.info("[消息发送后] 保存失败，缓存保留（待下次使用或清理）")

            if hasattr(self, "_ai_error_message_ids"):
                try:
                    self._ai_error_message_ids.discard(message_id)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"[消息发送后] 保存AI回复时发生错误: {e}", exc_info=True)

    async def _save_user_messages_on_duplicate_block(
        self, event: AstrMessageEvent, message_id: str
    ):
        """
        重复拦截后保存用户消息到官方对话系统

        当 on_decorating_result 检测到重复并清空 result 后，框架不会调用 after_message_sent。
        此方法确保用户消息和缓存消息仍然被保存到官方对话系统，防止上下文脱节。
        """
        try:
            is_private = event.is_private_chat()
            chat_id = event.get_group_id() if not is_private else event.get_sender_id()
            chat_id = ProbabilityManager.get_chat_key(
                event.get_platform_name(), is_private, chat_id
            )
            # Duplicate results bypass after_message_sent; clear all reply-side
            # state here so a later event cannot reuse the blocked response.
            self._pending_bot_replies.pop(message_id, None)
            self._agent_done_flags.discard(message_id)
            self._duplicate_blocked_messages.pop(message_id, None)
            self.raw_reply_cache.pop(message_id, None)
            last_cached = self._message_cache_snapshots.pop(message_id, None)
            smart_batch_messages = self._smart_batch_snapshots.pop(message_id, [])

            if not last_cached:
                if self.debug_mode:
                    logger.info("[重复拦截-保存] 无缓存快照，跳过用户消息保存")
                return

            if not isinstance(last_cached, dict) or "content" not in last_cached:
                return

            raw_content = last_cached["content"]

            trigger_type = None
            if last_cached.get("has_trigger_keyword"):
                trigger_type = "keyword"
            elif last_cached.get("is_at_message"):
                trigger_type = "at"
            else:
                trigger_type = "ai_decision"

            message_to_save = MessageProcessor.add_metadata_from_cache(
                raw_content,
                last_cached.get("sender_id", event.get_sender_id()),
                last_cached.get("sender_name", event.get_sender_name()),
                last_cached.get("message_timestamp") or last_cached.get("timestamp"),
                self.include_timestamp,
                self.include_sender_info,
                last_cached.get("mention_info"),
                trigger_type,
                last_cached.get("poke_info"),
                last_cached.get("is_empty_at", False),
                "",
                last_cached.get("is_at_all_message", False),
                persistent_poke_event_text=last_cached.get(
                    "persistent_poke_event_text", ""
                ),
            )
            message_to_save = MessageCleaner.clean_message(message_to_save)

            if not message_to_save:
                return

            current_msg_timestamp = last_cached.get("timestamp")
            current_msg_id = last_cached.get("message_id")

            async with self.concurrent_lock:
                processing_msg_ids = set(self.processing_sessions.keys())

            cached_messages_to_convert = self.cache_manager.prepare_cache_for_save(
                chat_id=chat_id,
                current_msg_id=current_msg_id,
                current_msg_timestamp=current_msg_timestamp,
                processing_msg_ids=processing_msg_ids,
                proactive_processing=False,
            )

            for _smart_msg in smart_batch_messages:
                _smart_content = ContextManager._content_to_safe_text(
                    _smart_msg.get("content", "")
                )
                if not _smart_content:
                    continue
                _smart_trigger = (
                    "keyword"
                    if _smart_msg.get("has_trigger_keyword")
                    else ("at" if _smart_msg.get("is_at_message") else "ai_decision")
                )
                _smart_message_to_save = MessageProcessor.add_metadata_from_cache(
                    _smart_content,
                    _smart_msg.get("sender_id", "unknown"),
                    _smart_msg.get("sender_name", "未知用户"),
                    _smart_msg.get("message_timestamp") or _smart_msg.get("timestamp"),
                    self.include_timestamp,
                    self.include_sender_info,
                    _smart_msg.get("mention_info"),
                    _smart_trigger,
                    _smart_msg.get("poke_info"),
                    _smart_msg.get("is_empty_at", False),
                    "",
                    _smart_msg.get("is_at_all_message", False),
                    persistent_poke_event_text=_smart_msg.get(
                        "persistent_poke_event_text", ""
                    ),
                )
                _smart_message_to_save = MessageCleaner.clean_message(
                    _smart_message_to_save
                )
                if _smart_message_to_save:
                    cached_messages_to_convert.append(
                        {
                            "role": "user",
                            "content": _smart_message_to_save,
                            "sender_id": _smart_msg.get("sender_id", "unknown"),
                            "sender_name": _smart_msg.get("sender_name", "未知用户"),
                            "message_timestamp": _smart_msg.get("message_timestamp")
                            or _smart_msg.get("timestamp"),
                            "message_id": _smart_msg.get("message_id", ""),
                            "image_urls": _smart_msg.get("image_urls", []),
                        }
                    )

            success = await ContextManager.save_to_official_conversation_with_cache(
                event,
                cached_messages_to_convert,
                message_to_save,
                None,
                self.context,
            )

            if success:
                logger.info("[重复拦截-保存] ✅ 用户消息已保存到官方对话系统")
                self.cache_manager.clear_saved_cache(
                    chat_id=chat_id,
                    current_msg_id=current_msg_id,
                    current_msg_timestamp=current_msg_timestamp,
                    processing_msg_ids=processing_msg_ids,
                    proactive_processing=False,
                )

                # Phase-2: 保存窗口缓冲消息
                try:
                    window_buffered_to_convert = (
                        self.cache_manager.prepare_window_buffered_for_save(
                            chat_id=chat_id,
                            processing_msg_ids=processing_msg_ids,
                        )
                    )
                    if window_buffered_to_convert:
                        wb_saved_msg_ids = set()
                        for wb_msg in self.cache_manager.get_window_buffered_messages(
                            chat_id
                        ):
                            msg_id = wb_msg.get("message_id")
                            if msg_id:
                                wb_saved_msg_ids.add(msg_id)
                        phase2_success = await ContextManager.save_to_official_conversation_with_cache(
                            event,
                            window_buffered_to_convert,
                            None,
                            None,
                            self.context,
                        )
                        if phase2_success:
                            self.cache_manager.clear_window_buffered_cache(
                                chat_id, saved_msg_ids=wb_saved_msg_ids
                            )
                            self._smart_batch_snapshots.pop(message_id, None)
                except Exception as phase2_err:
                    logger.warning(
                        f"[重复拦截-保存] Phase-2 窗口缓冲消息处理异常: {phase2_err}"
                    )
            else:
                logger.warning("[重复拦截-保存] ⚠️ 保存用户消息失败")

        except Exception as e:
            logger.warning(
                f"[重复拦截-保存] 保存用户消息时发生错误: {e}", exc_info=True
            )

    async def _finalize_bot_reply_save(self, event: AstrMessageEvent, message_id: str):
        """
        多轮工具调用支持：在agent完成但无最终文本时，保存之前累积的回复
        """
        try:
            is_private = event.is_private_chat()
            chat_id = event.get_group_id() if not is_private else event.get_sender_id()
            chat_id = ProbabilityManager.get_chat_key(
                event.get_platform_name(), is_private, chat_id
            )

            accumulated_texts = self._pending_bot_replies.pop(message_id, [])
            if not accumulated_texts:
                return

            original_bot_reply_text = "\n".join(accumulated_texts)
            if not original_bot_reply_text.strip():
                return

            logger.info(
                f"[_finalize_bot_reply_save] 保存 {len(accumulated_texts)} 段累积的AI回复，"
                f"总长度: {len(original_bot_reply_text)} 字符"
            )

            bot_reply_to_save = original_bot_reply_text

            interleaved_reply = self._build_interleaved_tool_reply(
                event, accumulated_texts
            )
            if interleaved_reply:
                bot_reply_to_save = interleaved_reply
                logger.info("[工具调用] 兜底保存: 已构建交错排列的工具调用记录")

            await ContextManager.save_bot_message(
                event, bot_reply_to_save, self.context
            )

            try:
                if chat_id not in self.recent_replies_cache:
                    self.recent_replies_cache[chat_id] = []
                self.recent_replies_cache[chat_id].append(
                    {"content": original_bot_reply_text, "timestamp": time.time()}
                )
            except Exception:
                pass

            if hasattr(self, "raw_reply_cache"):
                self.raw_reply_cache.pop(message_id, None)

            async with self.concurrent_lock:
                self.processing_sessions.pop(message_id, None)
                self._agent_done_flags.discard(message_id)

            # 获取用户消息并保存
            message_to_save = ""
            smart_batch_messages = self._smart_batch_snapshots.pop(message_id, [])
            last_cached = self._message_cache_snapshots.pop(message_id, None)
            if (
                last_cached
                and isinstance(last_cached, dict)
                and "content" in last_cached
            ):
                raw_content = last_cached["content"]
                trigger_type = None
                if last_cached.get("has_trigger_keyword"):
                    trigger_type = "keyword"
                elif last_cached.get("is_at_message"):
                    trigger_type = "at"
                else:
                    trigger_type = "ai_decision"

                message_to_save = MessageProcessor.add_metadata_from_cache(
                    raw_content,
                    last_cached.get("sender_id", event.get_sender_id()),
                    last_cached.get("sender_name", event.get_sender_name()),
                    last_cached.get("message_timestamp")
                    or last_cached.get("timestamp"),
                    self.include_timestamp,
                    self.include_sender_info,
                    last_cached.get("mention_info"),
                    trigger_type,
                    last_cached.get("poke_info"),
                    last_cached.get("is_empty_at", False),
                    "",
                    last_cached.get("is_at_all_message", False),
                    persistent_poke_event_text=last_cached.get(
                        "persistent_poke_event_text", ""
                    )
                    if isinstance(last_cached, dict)
                    else "",
                )
                message_to_save = MessageCleaner.clean_message(message_to_save)

            if not message_to_save:
                processed = MessageCleaner.extract_raw_message_from_event(
                    event, self_id=str(event.get_self_id())
                )
                mention_info = (
                    last_cached.get("mention_info")
                    if isinstance(last_cached, dict)
                    else None
                )
                if processed:
                    message_to_save = MessageProcessor.add_metadata_to_message(
                        event,
                        processed,
                        self.include_timestamp,
                        self.include_sender_info,
                        mention_info,
                        None,
                        None,
                        False,
                        "",
                        "",
                        is_at_all_message=bool(
                            last_cached.get("is_at_all_message", False)
                            if isinstance(last_cached, dict)
                            else False
                        ),
                        persistent_poke_event_text=last_cached.get(
                            "persistent_poke_event_text", ""
                        )
                        if isinstance(last_cached, dict)
                        else "",
                    )
                    message_to_save = MessageCleaner.clean_message(message_to_save)

            if message_to_save:
                extra_cached_messages = []
                for _smart_msg in smart_batch_messages:
                    _smart_content = ContextManager._content_to_safe_text(
                        _smart_msg.get("content", "")
                    )
                    if not _smart_content:
                        continue
                    _trigger_type = (
                        "keyword"
                        if _smart_msg.get("has_trigger_keyword")
                        else (
                            "at" if _smart_msg.get("is_at_message") else "ai_decision"
                        )
                    )
                    _smart_message_to_save = MessageProcessor.add_metadata_from_cache(
                        _smart_content,
                        _smart_msg.get("sender_id", "unknown"),
                        _smart_msg.get("sender_name", "未知用户"),
                        _smart_msg.get("message_timestamp")
                        or _smart_msg.get("timestamp"),
                        self.include_timestamp,
                        self.include_sender_info,
                        _smart_msg.get("mention_info"),
                        _trigger_type,
                        _smart_msg.get("poke_info"),
                        _smart_msg.get("is_empty_at", False),
                        "",
                        _smart_msg.get("is_at_all_message", False),
                        persistent_poke_event_text=_smart_msg.get(
                            "persistent_poke_event_text", ""
                        ),
                    )
                    _smart_message_to_save = MessageCleaner.clean_message(
                        _smart_message_to_save
                    )
                    if _smart_message_to_save:
                        extra_cached_messages.append(
                            {
                                "role": "user",
                                "content": _smart_message_to_save,
                                "sender_id": _smart_msg.get("sender_id", "unknown"),
                                "sender_name": _smart_msg.get(
                                    "sender_name", "未知用户"
                                ),
                                "message_timestamp": _smart_msg.get("message_timestamp")
                                or _smart_msg.get("timestamp"),
                                "message_id": _smart_msg.get("message_id", ""),
                                "image_urls": _smart_msg.get("image_urls", []),
                            }
                        )

                await ContextManager.save_to_official_conversation_with_cache(
                    event,
                    extra_cached_messages,
                    message_to_save,
                    bot_reply_to_save,
                    self.context,
                )

            self._saved_messages[message_id] = time.time()

        except Exception as e:
            logger.error(f"[_finalize_bot_reply_save] 保存失败: {e}", exc_info=True)

    def _build_interleaved_tool_reply(
        self, event: AstrMessageEvent, pending_texts: list
    ) -> str:
        """构建交错排列的工具调用+文本回复，保留时间顺序。"""
        try:
            req = event.get_extra("provider_request")
            if not req or not getattr(req, "tool_calls_result", None):
                return ""

            tool_calls_list = req.tool_calls_result
            if not isinstance(tool_calls_list, list):
                tool_calls_list = [tool_calls_list]

            interleaved_parts = []
            text_index = 0

            for tcr in tool_calls_list:
                tool_calls_info = getattr(tcr, "tool_calls_info", None)
                tool_results = getattr(tcr, "tool_calls_result", []) or []

                has_intermediate_text = False
                if tool_calls_info and tool_calls_info.content:
                    content_list = (
                        tool_calls_info.content
                        if isinstance(tool_calls_info.content, list)
                        else []
                    )
                    for part in content_list:
                        text = self._coerce_component_text(getattr(part, "text", None))
                        if text.strip():
                            has_intermediate_text = True
                            break

                if has_intermediate_text and text_index < len(pending_texts):
                    interleaved_parts.append(pending_texts[text_index])
                    text_index += 1

                if tool_calls_info and getattr(tool_calls_info, "tool_calls", None):
                    for i, tc in enumerate(tool_calls_info.tool_calls):
                        if hasattr(tc, "function"):
                            func_name = tc.function.name
                            func_args = tc.function.arguments or ""
                        elif isinstance(tc, dict):
                            func = tc.get("function", {})
                            func_name = func.get("name", "未知工具")
                            func_args = func.get("arguments", "")
                        else:
                            continue

                        if len(func_args) > 200:
                            func_args = func_args[:200] + "..."

                        result_preview = ""
                        if i < len(tool_results):
                            result_content = getattr(tool_results[i], "content", None)
                            if isinstance(result_content, str):
                                result_preview = result_content
                            elif isinstance(result_content, list):
                                texts = []
                                for p in result_content:
                                    text = self._coerce_component_text(
                                        getattr(p, "text", None)
                                    )
                                    if text:
                                        texts.append(text)
                                    elif isinstance(p, dict) and isinstance(
                                        p.get("text"), str
                                    ):
                                        texts.append(p["text"])
                                result_preview = " ".join(texts)
                            elif result_content is not None:
                                result_preview = str(result_content)

                            if len(result_preview) > 500:
                                result_preview = result_preview[:500] + "..."

                        result_preview = result_preview.strip()
                        if result_preview:
                            tool_line = f"- {func_name}({func_args}) → {result_preview}"
                        else:
                            tool_line = f"- {func_name}({func_args}) → (无返回)"

                        interleaved_parts.append(
                            "[工具调用记录开始]\n" + tool_line + "\n[工具调用记录结束]"
                        )

            while text_index < len(pending_texts):
                interleaved_parts.append(pending_texts[text_index])
                text_index += 1

            if not interleaved_parts:
                return ""

            return "\n".join(interleaved_parts)

        except Exception as e:
            logger.warning(f"[工具调用交错] 构建失败: {e}", exc_info=True)
            return ""
