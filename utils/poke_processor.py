"""
戳一戳处理域（PokeMixin）。

从主插件类拆出的独立功能域，通过 self 访问 PersonaPresence 的状态与工具
（缓存管理器、调试开关、消息处理工具等），保持行为不变。
"""

import asyncio
import random
import time

from astrbot.api.all import *

from .context_manager import ContextManager
from .message_processor import MessageProcessor  # noqa: F403  （与 main.py 保持一致）
from .probability_manager import ProbabilityManager


class PokeMixin:
    def _is_poke_enabled_in_group(self, chat_id: str) -> bool:
        """检查当前群组是否在戳一戳功能白名单中。"""
        if not self.poke_enabled_groups or len(self.poke_enabled_groups) == 0:
            return True
        chat_id_str = str(chat_id)
        if chat_id_str in self.poke_enabled_groups:
            return True
        if self.debug_mode:
            logger.info(f"【戳一戳白名单】群组 {chat_id} 不在白名单中，禁止戳一戳功能")
        return False

    async def _check_poke_message(self, event: AstrMessageEvent) -> dict:
        """
        检测是否为戳一戳消息（仅支持QQ平台的aiocqhttp消息事件）

        模式：
        1. ignore模式：忽略所有戳一戳消息
        2. bot_only模式：只处理戳机器人的消息
        3. all模式：接受所有戳一戳消息
        """
        try:
            poke_mode = self.poke_message_mode

            if event.get_platform_name() != "aiocqhttp":
                return {"is_poke": False, "should_ignore": False}

            raw_message = getattr(event.message_obj, "raw_message", None)
            if not raw_message:
                return {"is_poke": False, "should_ignore": False}

            is_poke = (
                raw_message.get("post_type") == "notice"
                and raw_message.get("notice_type") == "notify"
                and raw_message.get("sub_type") == "poke"
            )

            if not is_poke:
                return {"is_poke": False, "should_ignore": False}

            if self.debug_mode:
                logger.info("【戳一戳检测】检测到戳一戳消息")

            # 白名单检查
            group_id = raw_message.get("group_id")
            if group_id:
                if not self._is_poke_enabled_in_group(str(group_id)):
                    return {"is_poke": True, "should_ignore": True}

            # 模式1: ignore
            if poke_mode == "ignore":
                if self.debug_mode:
                    logger.info("【戳一戳检测】当前模式为ignore，忽略此消息")
                return {"is_poke": True, "should_ignore": True}

            bot_id = raw_message.get("self_id")
            sender_id = raw_message.get("user_id")
            target_id = raw_message.get("target_id")
            group_id = raw_message.get("group_id")

            sender_name = await self._resolve_group_member_name(
                event,
                sender_id,
                group_id,
                fallback_name=event.get_sender_name() or "",
            )
            # notice 事件 fallback：从 raw_message 补充提取发送者昵称
            if (
                not sender_name
                or sender_name == str(sender_id)
                or sender_name == "未知用户"
            ):
                try:
                    _raw_sender = raw_message.get("sender")
                    if isinstance(_raw_sender, dict):
                        _nick = (
                            _raw_sender.get("nickname") or _raw_sender.get("card") or ""
                        )
                        if _nick and _nick.strip():
                            sender_name = _nick.strip()
                except Exception:
                    pass

            target_name = ""
            try:
                if group_id and target_id and str(target_id) != str(bot_id):
                    target_name = await self._resolve_group_member_name(
                        event, target_id, group_id, fallback_name=""
                    )
            except Exception as e:
                if self.debug_mode:
                    logger.info(f"【戳一戳检测】获取被戳者昵称失败: {e}")

            is_poke_bot = str(target_id) == str(bot_id)

            if self.debug_mode:
                logger.info(
                    f"【戳一戳检测】戳人者ID={sender_id}, 被戳者ID={target_id}, 机器人ID={bot_id}"
                )
                logger.info(f"【戳一戳检测】是否戳机器人: {is_poke_bot}")

            # 模式2: bot_only
            if poke_mode == "bot_only":
                if not is_poke_bot:
                    if self.debug_mode:
                        logger.info(
                            "【戳一戳检测】当前模式为bot_only，但戳的不是机器人，忽略此消息"
                        )
                    return {"is_poke": True, "should_ignore": True}
                else:
                    logger.info(
                        "✅ 检测到戳一戳消息（有人戳机器人），当前模式为bot_only，本插件将处理"
                    )
                    return {
                        "is_poke": True,
                        "should_ignore": False,
                        "poke_info": {
                            "is_poke_bot": True,
                            "sender_id": str(sender_id),
                            "sender_name": sender_name or "未知用户",
                            "target_id": str(target_id),
                            "target_name": "",
                        },
                    }

            # 模式3: all
            if poke_mode == "all":
                logger.info("✅ 检测到戳一戳消息，当前模式为all，本插件将处理")
                return {
                    "is_poke": True,
                    "should_ignore": False,
                    "poke_info": {
                        "is_poke_bot": is_poke_bot,
                        "sender_id": str(sender_id),
                        "sender_name": sender_name or "未知用户",
                        "target_id": str(target_id),
                        "target_name": target_name or "未知用户",
                    },
                }

            logger.warning(f"⚠️ 未知的戳一戳处理模式: {poke_mode}，默认忽略")
            return {"is_poke": True, "should_ignore": True}

        except Exception as e:
            logger.error(f"【戳一戳检测】发生错误: {e}", exc_info=True)
            return {"is_poke": False, "should_ignore": False}

    async def _do_poke_after_reply(
        self, event: AstrMessageEvent, user_id: str, is_private: bool, chat_id: str
    ):
        """回复后戳一戳功能（仅QQ+aiocqhttp）。"""
        try:
            if is_private:
                return

            if not self._is_poke_enabled_in_group(chat_id):
                return

            platform_name = event.get_platform_name()
            if platform_name != "aiocqhttp":
                return

            if random.random() > self.poke_after_reply_probability:
                return

            if self.poke_after_reply_delay > 0:
                await asyncio.sleep(self.poke_after_reply_delay)

            if not isinstance(event, AiocqhttpMessageEvent):
                logger.warning("[戳一戳] 事件类型不匹配，无法执行戳一戳")
                return

            try:
                client = event.bot
                payloads = {"user_id": int(user_id)}
                if chat_id:
                    payloads["group_id"] = int(chat_id)

                await client.api.call_action("send_poke", **payloads)

                if self.debug_mode:
                    logger.info(f"[戳一戳] ✅ 已戳一戳用户 {user_id} (群:{chat_id})")

                if self.poke_trace_enabled:
                    trace_key = ProbabilityManager.get_chat_key(
                        event.get_platform_name(), event.is_private_chat(), chat_id
                    )
                    self._register_poke_trace(trace_key, str(user_id))

                try:
                    target_name = await self._resolve_group_member_name(
                        event,
                        user_id,
                        chat_id,
                        fallback_name=event.get_sender_name() or "",
                    )
                    poke_event_text = MessageProcessor.build_persistent_poke_event_text(
                        {"target_id": str(user_id), "target_name": target_name},
                        perspective="assistant",
                    )
                    await self._save_poke_assistant_event(event, poke_event_text)
                except Exception as save_err:
                    logger.warning(
                        f"[戳一戳事件] 保存AI回复后戳一戳历史失败，已降级继续: {save_err}"
                    )

            except Exception as e:
                logger.error(f"[戳一戳] 执行戳一戳失败: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"[戳一戳] 戳一戳功能发生错误: {e}", exc_info=True)

    async def _maybe_reverse_poke_on_poke(
        self,
        event: AstrMessageEvent,
        poke_info: dict,
        is_private: bool,
        chat_id: str,
    ) -> bool:
        """
        在收到戳一戳消息且未被忽略时，按配置概率反向戳回发起戳一戳的用户。
        成功触发时返回True（本插件丢弃后续处理），否则返回False。
        """
        try:
            if self.poke_reverse_on_poke_probability <= 0:
                return False

            if is_private:
                return False

            if not self._is_poke_enabled_in_group(chat_id):
                return False

            if event.get_platform_name() != "aiocqhttp":
                return False

            if random.random() >= self.poke_reverse_on_poke_probability:
                return False

            if not isinstance(event, AiocqhttpMessageEvent):
                logger.warning("【反戳】事件类型不匹配，无法执行戳一戳")
                return False

            sender_id = poke_info.get("sender_id")
            if not sender_id:
                return False

            try:
                client = event.bot
                payloads = {"user_id": int(sender_id)}
                if chat_id:
                    payloads["group_id"] = int(chat_id)

                await client.api.call_action("send_poke", **payloads)
                if self.debug_mode:
                    logger.info(f"【反戳】✅ 已反戳用户 {sender_id} (群:{chat_id})")

                if self.poke_trace_enabled:
                    trace_key = ProbabilityManager.get_chat_key(
                        event.get_platform_name(), event.is_private_chat(), chat_id
                    )
                    self._register_poke_trace(trace_key, str(sender_id))

                # 保存用户的戳一戳事件到历史记录（用户视角）
                try:
                    user_poke_text = MessageProcessor.build_persistent_poke_event_text(
                        poke_info
                    )
                    if user_poke_text:
                        original_msg = (event.message_str or "").strip()
                        if not original_msg:
                            original_msg = "[ComponentType.Poke]"
                        formatted_user_msg = MessageProcessor.add_metadata_to_message(
                            event,
                            original_msg,
                            self.include_timestamp,
                            self.include_sender_info,
                            None,
                            None,
                            None,
                            persistent_poke_event_text=user_poke_text or "",
                        )
                        await ContextManager.save_user_message(
                            event,
                            formatted_user_msg,
                            self.context,
                        )
                        try:
                            await (
                                ContextManager.save_to_official_conversation_with_cache(
                                    event,
                                    [],
                                    formatted_user_msg,
                                    "",
                                    self.context,
                                    save_kind="poke_event",
                                )
                            )
                        except Exception as user_official_err:
                            logger.warning(
                                f"[戳一戳事件] 保存用户戳一戳到官方会话失败: {user_official_err}"
                            )
                except Exception as user_save_err:
                    logger.warning(
                        f"[戳一戳事件] 保存用户戳一戳事件到历史记录失败: {user_save_err}"
                    )

                # 保存AI视角的反戳事件
                try:
                    target_name = await self._resolve_group_member_name(
                        event,
                        sender_id,
                        chat_id,
                        fallback_name=poke_info.get("sender_name", "")
                        or event.get_sender_name()
                        or "",
                    )
                    poke_event_text = MessageProcessor.build_persistent_poke_event_text(
                        {"target_id": str(sender_id), "target_name": target_name},
                        perspective="assistant",
                    )
                    await self._save_poke_assistant_event(event, poke_event_text)
                except Exception as save_err:
                    logger.warning(
                        f"[戳一戳事件] 保存AI反戳历史失败，已降级继续: {save_err}"
                    )
            except Exception as e:
                logger.error(f"【反戳】执行反戳失败: {e}", exc_info=True)
                return False

            return True

        except Exception as e:
            logger.error(f"【反戳】反戳流程发生错误: {e}", exc_info=True)
            return False

    def _register_poke_trace(self, chat_id: str, user_id: str):
        """注册戳一戳追踪记录。"""
        try:
            if not self.poke_trace_enabled:
                return
            key = str(chat_id)
            store = self.poke_trace_records.get(key)
            if not isinstance(store, OrderedDict):
                store = OrderedDict()
                self.poke_trace_records[key] = store
            # 清理过期记录
            now_ts = time.time()
            for uid_exp in [u for u, exp in list(store.items()) if exp <= now_ts]:
                store.pop(uid_exp, None)
            uid = str(user_id)
            if uid in store:
                try:
                    del store[uid]
                except Exception:
                    pass
            while len(store) >= max(1, int(self.poke_trace_max_tracked_users)):
                try:
                    store.popitem(last=False)
                except Exception:
                    break
            expire_at = time.time() + max(1, int(self.poke_trace_ttl_seconds))
            store[uid] = expire_at
        except Exception as e:
            logger.error(f"[戳过对方追踪] 注册失败: {e}")

    def _check_and_consume_poke_trace(self, chat_id: str, user_id: str) -> bool:
        """检查并消费戳一戳追踪记录。"""
        try:
            if not self.poke_trace_enabled:
                return False
            key = str(chat_id)
            store = self.poke_trace_records.get(key)
            if not isinstance(store, OrderedDict):
                store = OrderedDict()
                self.poke_trace_records[key] = store
            # 清理过期记录
            now_ts = time.time()
            for uid_exp in [u for u, exp in list(store.items()) if exp <= now_ts]:
                store.pop(uid_exp, None)
            uid = str(user_id)
            exp = store.get(uid)
            if exp and exp > time.time():
                try:
                    del store[uid]
                except Exception:
                    pass
                return True
            return False
        except Exception as e:
            logger.error(f"[戳过对方追踪] 检查失败: {e}")
            return False

    async def _save_poke_assistant_event(
        self, event: AstrMessageEvent, poke_event_text: str
    ):
        """尽力保存AI视角的戳一戳事件消息。"""
        try:
            poke_event_text = (poke_event_text or "").strip()
            if not poke_event_text:
                return
            if event.get_platform_name() != "aiocqhttp":
                return
            if not self.context:
                return

            bot_id = event.get_self_id() or ""
            bot_name = "AI"
            try:
                if hasattr(event, "get_self_name") and callable(event.get_self_name):
                    bot_name = event.get_self_name() or "AI"
            except Exception:
                pass

            formatted_poke_text = MessageProcessor.add_metadata_from_cache(
                poke_event_text,
                sender_id=bot_id,
                sender_name=bot_name,
                message_timestamp=time.time(),
                include_timestamp=self.include_timestamp,
                include_sender_info=self.include_sender_info,
                mention_info=None,
                trigger_type=None,
                poke_info=None,
            )

            try:
                await ContextManager.save_bot_message(
                    event,
                    formatted_poke_text,
                    self.context,
                )
            except Exception as custom_err:
                logger.warning(
                    f"[戳一戳事件] 保存AI戳一戳事件到官方历史失败，已降级继续: {custom_err}"
                )

            try:
                await ContextManager.save_to_official_conversation_with_cache(
                    event,
                    [],
                    "",
                    formatted_poke_text,
                    self.context,
                    save_kind="poke_event",
                )
            except Exception as official_err:
                logger.warning(
                    f"[戳一戳事件] 保存AI戳一戳事件到官方会话失败，已降级继续: {official_err}"
                )
        except Exception as e:
            logger.warning(f"[戳一戳事件] 保存AI戳一戳事件时发生错误，已降级忽略: {e}")

    async def _resolve_group_member_name(
        self,
        event: AstrMessageEvent,
        user_id,
        group_id,
        fallback_name: str = "",
    ) -> str:
        """尽力解析群成员昵称，失败时回退到现有名称或ID。"""
        try:
            user_id_str = str(user_id or "").strip()
            group_id_str = str(group_id or "").strip()
            fallback_name = (fallback_name or "").strip()
            if (
                fallback_name
                and fallback_name != "未知用户"
                and fallback_name != user_id_str
            ):
                return fallback_name
            if not user_id_str or not group_id_str:
                return "未知用户"

            try:
                sender = getattr(getattr(event, "message_obj", None), "sender", None)
                if sender and str(getattr(sender, "user_id", "")) == user_id_str:
                    sender_nick = str(getattr(sender, "nickname", "") or "").strip()
                    if sender_nick and sender_nick != user_id_str:
                        return sender_nick
            except Exception:
                pass

            bot = getattr(event, "bot", None)
            call_action = getattr(bot, "call_action", None) if bot else None
            if not callable(call_action):
                api = getattr(bot, "api", None) if bot else None
                call_action = getattr(api, "call_action", None) if api else None
            if callable(call_action):
                try:
                    result = await call_action(
                        "get_group_member_info",
                        group_id=int(group_id_str)
                        if group_id_str.isdigit()
                        else group_id_str,
                        user_id=int(user_id_str)
                        if user_id_str.isdigit()
                        else user_id_str,
                    )
                    if isinstance(result, dict):
                        nick = (
                            result.get("card")
                            or result.get("nickname")
                            or result.get("user_name")
                            or ""
                        )
                        if nick:
                            return str(nick).strip()
                        data = result.get("data")
                        if isinstance(data, dict):
                            nick = (
                                data.get("card")
                                or data.get("nickname")
                                or data.get("user_name")
                                or ""
                            )
                            if nick:
                                return str(nick).strip()
                except Exception as e:
                    if self.debug_mode:
                        logger.info(
                            f"[戳一戳检测] get_group_member_info 获取昵称失败: {e}"
                        )

            return "未知用户"
        except Exception as e:
            if self.debug_mode:
                logger.info(f"[戳一戳检测] 解析群成员昵称失败，使用回退值: {e}")
            return "未知用户"
