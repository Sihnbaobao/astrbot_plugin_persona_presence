"""
上下文管理器模块
负责提取和管理历史消息上下文

主要功能：
- 从 AstrBot 官方存储读取和保存历史消息
- 格式化上下文供AI使用
- 保存用户消息和bot回复
- 支持缓存消息转正（避免上下文断裂）
- 详细的保存日志便于调试

作者: Sihnbaobao（重构）
版本: V1.2.3.hotfix.2
"""

import asyncio
import json
import re
from datetime import datetime

# 导入 MessageCleaner（延迟导入以避免循环依赖）
from typing import TYPE_CHECKING, Any

from astrbot.api.all import *
from astrbot.api.message_components import Plain

from .message_processor import MessageProcessor

if TYPE_CHECKING:
    from astrbot.core.db.po import PlatformMessageHistory
    from astrbot.core.star.context import Context

# 详细日志开关（与 main.py 同款方式：单独用 if 控制）
DEBUG_MODE: bool = False


class ContextManager:
    """
    上下文管理器

    负责历史消息的读取、保存和格式化：
    1. 从官方存储提取历史消息
    2. 控制上下文消息数量
    3. 格式化成AI可理解的文本
    """

    _official_save_lock: asyncio.Lock | None = None
    _official_save_lock_loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def _get_official_save_lock(cls) -> asyncio.Lock:
        """Return the serialization lock for the active event loop."""
        loop = asyncio.get_running_loop()
        if cls._official_save_lock is None or cls._official_save_lock_loop is not loop:
            cls._official_save_lock = asyncio.Lock()
            cls._official_save_lock_loop = loop
        return cls._official_save_lock

    @staticmethod
    def _coerce_plain_text(value: Any) -> str:
        """兼容某些平台 Plain.text=None 的情况。"""
        return value if isinstance(value, str) else ""

    @staticmethod
    def _message_to_dict(msg: AstrBotMessage) -> dict[str, Any]:
        """
        将 AstrBotMessage 对象转换为可JSON序列化的字典

        Args:
            msg: AstrBotMessage 对象

        Returns:
            字典表示
        """
        try:
            msg_dict = {
                "message_str": ContextManager._content_to_safe_text(
                    msg.message_str if hasattr(msg, "message_str") else ""
                ),
                "platform_name": msg.platform_name
                if hasattr(msg, "platform_name")
                else "",
                "timestamp": msg.timestamp if hasattr(msg, "timestamp") else 0,
                "type": msg.type.value
                if hasattr(msg, "type") and hasattr(msg.type, "value")
                else "OtherMessage",
                "group_id": msg.group_id if hasattr(msg, "group_id") else None,
                "self_id": msg.self_id if hasattr(msg, "self_id") else "",
                "session_id": msg.session_id if hasattr(msg, "session_id") else "",
                "message_id": msg.message_id if hasattr(msg, "message_id") else "",
            }

            # 处理发送者信息
            if hasattr(msg, "sender") and msg.sender:
                msg_dict["sender"] = {
                    "user_id": msg.sender.user_id
                    if hasattr(msg.sender, "user_id")
                    else "",
                    "nickname": msg.sender.nickname
                    if hasattr(msg.sender, "nickname")
                    else "",
                }
            else:
                msg_dict["sender"] = None

            return msg_dict
        except Exception as e:
            logger.error(f"转换消息对象为字典失败: {e}")
            # 返回最小字典
            return {"message_str": "", "timestamp": 0}

    @staticmethod
    def _content_block_to_text(block: Any) -> tuple[str, bool]:
        """提取单个 content block 的文本，并标记是否为非文本块。"""
        if block is None:
            return "", False

        if isinstance(block, str):
            return block, False

        if isinstance(block, (int, float, bool)):
            return str(block), False

        if not isinstance(block, dict):
            return str(block), False

        if "role" in block and "content" in block:
            return ContextManager._content_to_safe_text(block.get("content")), False

        block_type = str(block.get("type", "") or "").lower()

        text_value = block.get("text")
        if text_value is not None:
            return str(text_value), False

        data = block.get("data")
        if isinstance(data, dict) and data.get("text") is not None:
            return str(data.get("text") or ""), False

        nested_content = block.get("content")
        if isinstance(nested_content, (str, list, dict)):
            nested_text = ContextManager._content_to_safe_text(nested_content)
            if nested_text:
                return nested_text, False

        if block_type in {
            "image",
            "image_url",
            "input_image",
            "audio",
            "input_audio",
            "file",
            "video",
        }:
            return "", True

        if block_type:
            return "", True

        return str(block), False

    @staticmethod
    def _content_to_safe_text(content: Any) -> str:
        """将任意 content 兼容转换为安全字符串。"""
        if content is None:
            return ""

        if isinstance(content, str):
            return content

        if isinstance(content, (int, float, bool)):
            return str(content)

        if isinstance(content, dict):
            if "role" in content and "content" in content:
                return ContextManager._content_to_safe_text(content.get("content"))
            text, has_non_text = ContextManager._content_block_to_text(content)
            if text:
                return text
            return "[多模态消息]" if has_non_text else str(content)

        if isinstance(content, list):
            text_parts = []
            has_non_text = False
            only_image_blocks = bool(content)

            for item in content:
                text, item_has_non_text = ContextManager._content_block_to_text(item)
                if text:
                    text_parts.append(text)
                    only_image_blocks = False
                if item_has_non_text:
                    has_non_text = True
                    if not (
                        isinstance(item, dict)
                        and "image" in str(item.get("type", "") or "").lower()
                    ):
                        only_image_blocks = False
                elif not isinstance(item, dict):
                    only_image_blocks = False

            if text_parts:
                return "".join(text_parts)
            if has_non_text:
                return "[图片]" if only_image_blocks else "[多模态消息]"
            return ""

        return str(content)

    @staticmethod
    def _make_content_hashable(content: Any) -> Any:
        """将 content 转换为可哈希值，兼容多模态 list/dict。"""
        if isinstance(content, (list, dict)):
            try:
                return json.dumps(content, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                return ContextManager._content_to_safe_text(content)
        return content

    @staticmethod
    def _dict_to_message(msg_dict: dict[str, Any]) -> AstrBotMessage:
        """
        将字典转换回 AstrBotMessage 对象

        Args:
            msg_dict: 消息字典

        Returns:
            AstrBotMessage 对象
        """
        try:
            msg = AstrBotMessage()
            msg.message_str = ContextManager._content_to_safe_text(
                msg_dict.get("message_str", "")
            )
            msg.platform_name = msg_dict.get("platform_name", "")
            msg.timestamp = msg_dict.get("timestamp", 0)

            # 处理消息类型
            # MessageType 是字符串枚举，值如 "GroupMessage", "FriendMessage", "OtherMessage"
            msg_type = msg_dict.get("type", "OtherMessage")
            if isinstance(msg_type, str):
                # 从字符串值创建枚举
                msg.type = MessageType(msg_type)
            elif isinstance(msg_type, int):
                # 兼容旧格式：如果是整数，映射到对应的类型
                # 这是为了处理可能存在的旧数据
                type_map = {
                    0: MessageType.OTHER_MESSAGE,
                    1: MessageType.GROUP_MESSAGE,
                    2: MessageType.FRIEND_MESSAGE,
                }
                msg.type = type_map.get(msg_type, MessageType.OTHER_MESSAGE)
            else:
                # 如果已经是 MessageType 对象，直接使用
                msg.type = msg_type

            msg.group_id = msg_dict.get("group_id")
            msg.self_id = msg_dict.get("self_id", "")
            msg.session_id = msg_dict.get("session_id", "")
            msg.message_id = msg_dict.get("message_id", "")

            # 处理发送者信息
            sender_dict = msg_dict.get("sender")
            if sender_dict:
                msg.sender = MessageMember(
                    user_id=sender_dict.get("user_id", ""),
                    nickname=sender_dict.get("nickname", ""),
                )

            return msg
        except Exception as e:
            logger.error(f"从字典转换为消息对象失败: {e}")
            # 返回一个空的消息对象而不是 None，避免后续处理出错
            empty_msg = AstrBotMessage()
            empty_msg.message_str = ContextManager._content_to_safe_text(
                msg_dict.get("message_str", "")
            )
            empty_msg.timestamp = 0
            return empty_msg

    @staticmethod
    def _official_history_to_message(
        history_item: "PlatformMessageHistory",
        platform_name: str,
        is_private: bool,
        chat_id: str,
        bot_id: str,
    ) -> AstrBotMessage | None:
        """
        将官方 PlatformMessageHistory 对象转换为 AstrBotMessage

        Args:
            history_item: 官方历史记录对象
            platform_name: 平台名称
            is_private: 是否私聊
            chat_id: 聊天ID
            bot_id: 机器人ID

        Returns:
            AstrBotMessage 对象，转换失败返回 None
        """
        try:
            msg = AstrBotMessage()

            # 从 content 字段提取消息文本
            content = history_item.content
            message_text = ContextManager._content_to_safe_text(content)

            msg.message_str = message_text
            msg.platform_name = platform_name

            # 处理时间戳
            if hasattr(history_item, "created_at") and history_item.created_at:
                if isinstance(history_item.created_at, datetime):
                    msg.timestamp = int(history_item.created_at.timestamp())
                else:
                    msg.timestamp = 0
            else:
                msg.timestamp = 0

            # 设置消息类型
            msg.type = (
                MessageType.FRIEND_MESSAGE if is_private else MessageType.GROUP_MESSAGE
            )

            if not is_private:
                msg.group_id = chat_id

            # 设置发送者信息
            sender_id = history_item.sender_id or ""
            sender_name = history_item.sender_name or "未知用户"
            msg.sender = MessageMember(user_id=sender_id, nickname=sender_name)
            msg.self_id = bot_id
            msg.session_id = chat_id
            msg.message_id = f"official_{history_item.id}" if history_item.id else ""

            return msg

        except Exception as e:
            if DEBUG_MODE:
                logger.warning(f"转换官方历史记录失败: {e}")
            return None

    @staticmethod
    async def get_history_messages_with_fallback(
        event: AstrMessageEvent,
        max_messages: int,
        context: "Context" = None,
        cached_messages: list[AstrBotMessage] = None,
    ) -> list[AstrBotMessage]:
        """
        获取 AstrBot 官方历史消息记录

        读取官方历史并拼接待决策缓存消息。

        Args:
            event: 消息事件对象
            max_messages: 最大消息数量
                - 正数: 限制条数
                - 0: 不获取
                - -1: 不限制
            context: Context 对象（用于访问官方存储）
            cached_messages: 缓存的消息列表（尚未持久化的消息）

        Returns:
            历史消息列表（已按时间排序，包含缓存消息）
        """
        try:
            _sid = getattr(event, "session_id", "") or ""

            # 🔧 修复：确保 max_messages 是整数类型
            if not isinstance(max_messages, int):
                try:
                    max_messages = int(max_messages)
                except (ValueError, TypeError):
                    logger.warning(
                        f"⚠️ max_messages 值 '{max_messages}' 无法转换为整数，使用默认值 -1"
                    )
                    max_messages = -1

            # 如果配置为0,不获取历史消息
            if max_messages == 0:
                if DEBUG_MODE:
                    logger.debug("配置为不获取历史消息")
                # 即使不获取历史，也要返回缓存消息
                return cached_messages or []

            # 获取平台和聊天信息
            platform_name = event.get_platform_name()
            platform_id = event.get_platform_id()
            is_private = event.is_private_chat()
            chat_id = event.get_group_id() if not is_private else event.get_sender_id()
            bot_id = event.get_self_id()

            if not chat_id:
                logger.warning("无法获取聊天ID,跳过历史消息提取")
                return cached_messages or []

            # 硬上限保护
            HARD_LIMIT = 500
            if max_messages == -1:
                effective_limit = HARD_LIMIT
            else:
                effective_limit = min(max_messages, HARD_LIMIT)

            history: list[AstrBotMessage] = []

            # ========== 1. 优先尝试从官方存储读取 ==========
            if context and hasattr(context, "message_history_manager"):
                try:
                    if DEBUG_MODE:
                        logger.debug("[上下文管理器] 尝试从官方存储读取历史消息...")

                    official_history = await context.message_history_manager.get(
                        platform_id=platform_id,
                        user_id=event.unified_msg_origin,
                        page=1,
                        page_size=effective_limit,
                    )

                    if official_history and len(official_history) > 0:
                        # 转换官方格式为 AstrBotMessage
                        for item in official_history:
                            msg = ContextManager._official_history_to_message(
                                history_item=item,
                                platform_name=platform_name,
                                is_private=is_private,
                                chat_id=chat_id,
                                bot_id=bot_id,
                            )
                            if msg and msg.message_str:  # 只添加有内容的消息
                                history.append(msg)

                        if len(history) > 0:
                            logger.debug(
                                f"[上下文管理器] 从官方存储读取到 {len(history)} 条历史消息"
                            )
                    else:
                        if DEBUG_MODE:
                            logger.debug("[上下文管理器] 官方存储无历史消息")

                except Exception as e:
                    logger.warning(f"[上下文管理器] 从官方存储读取失败: {e}")

            # ========== 3. 拼接缓存消息 ==========
            # 🔧 v1.2.0 修复：改进缓存消息合并逻辑，确保缓存消息能正确拼接到上下文
            if cached_messages:
                # 构建历史消息的去重集合（使用 message_id 或 内容+发送者+时间戳 组合）
                history_dedup_set = set()
                for msg in history:
                    # 优先使用 message_id 去重
                    msg_id = getattr(msg, "message_id", None)
                    if (
                        msg_id
                        and not msg_id.startswith("cached_")
                        and not msg_id.startswith("official_")
                    ):
                        history_dedup_set.add(f"id:{msg_id}")

                    # 同时使用 内容+发送者+时间戳 组合去重（作为备用）
                    content = getattr(msg, "message_str", "") or ""
                    sender_id = ""
                    if hasattr(msg, "sender") and msg.sender:
                        sender_id = getattr(msg.sender, "user_id", "") or ""
                    ts = getattr(msg, "timestamp", 0) or 0
                    if content:  # 只有有内容的消息才加入去重集合
                        history_dedup_set.add(
                            f"content:{content}|sender:{sender_id}|ts:{ts}"
                        )

                # 过滤掉已经在历史中的缓存消息
                new_cached = []
                skipped_count = 0
                for cached_msg in cached_messages:
                    is_duplicate = False

                    # 检查 message_id 是否重复
                    cached_msg_id = getattr(cached_msg, "message_id", None)
                    if cached_msg_id and not cached_msg_id.startswith("cached_"):
                        if f"id:{cached_msg_id}" in history_dedup_set:
                            is_duplicate = True

                    # 如果 message_id 没有匹配，检查内容组合是否重复
                    if not is_duplicate:
                        cached_content = getattr(cached_msg, "message_str", "") or ""
                        cached_sender = ""
                        if hasattr(cached_msg, "sender") and cached_msg.sender:
                            cached_sender = (
                                getattr(cached_msg.sender, "user_id", "") or ""
                            )
                        cached_ts = getattr(cached_msg, "timestamp", 0) or 0

                        if cached_content:
                            dedup_key = f"content:{cached_content}|sender:{cached_sender}|ts:{cached_ts}"
                            if dedup_key in history_dedup_set:
                                is_duplicate = True

                    if not is_duplicate:
                        new_cached.append(cached_msg)
                        # 将新添加的消息也加入去重集合，避免缓存内部重复
                        if cached_msg_id:
                            history_dedup_set.add(f"id:{cached_msg_id}")
                        if cached_content:
                            history_dedup_set.add(
                                f"content:{cached_content}|sender:{cached_sender}|ts:{cached_ts}"
                            )
                    else:
                        skipped_count += 1

                if new_cached:
                    history.extend(new_cached)
                    logger.debug(
                        f"📦 [缓存拼接] 拼接了 {len(new_cached)} 条缓存消息到上下文"
                        + (
                            f"（跳过 {skipped_count} 条重复）"
                            if skipped_count > 0
                            else ""
                        )
                    )
                elif skipped_count > 0:
                    logger.debug(
                        f"📦 [缓存拼接] 所有 {skipped_count} 条缓存消息都已在历史中，无需拼接"
                    )

            # ========== 4. 按时间排序并截断 ==========
            # 按时间戳排序
            history.sort(
                key=lambda m: (
                    m.timestamp if hasattr(m, "timestamp") and m.timestamp else 0
                )
            )

            # 截断到有效限制
            if len(history) > effective_limit:
                history = history[-effective_limit:]

            logger.debug(f"[上下文管理器] 最终获取历史消息 {len(history)} 条")
            return history

        except Exception as e:
            logger.error(f"[上下文管理器] 获取历史消息失败: {e}")
            # 发生错误时，至少返回缓存消息
            return cached_messages or []

    @staticmethod
    async def format_context_for_ai(
        history_messages: list[AstrBotMessage],
        current_message: str,
        bot_id: str,
        include_timestamp: bool = True,
        include_sender_info: bool = True,
        window_buffered_messages: list = None,
        poke_notice: str = "",
    ) -> str:
        """
        将历史消息格式化为AI可理解的文本

        Args:
            history_messages: 历史消息列表
            current_message: 当前消息
            bot_id: 机器人ID，用于识别自己的回复
            include_timestamp: 是否包含时间戳（默认为True）
            include_sender_info: 是否包含发送者信息（默认为True）
            window_buffered_messages: 窗口缓冲消息列表（用于拼接到当前消息下方）

        Returns:
            格式化后的文本
        """
        try:
            formatted_parts = []

            # 如果有历史消息,添加历史消息部分
            if history_messages:
                if include_sender_info:
                    formatted_parts.append(
                        f"=== 历史消息上下文 ===\n"
                        f"[重要提示] 以下每条历史消息均已标注发送者的名字和用户ID（格式：名字(ID:用户ID): 消息内容）。\n"
                        f"其中 ID 为 {bot_id} 的消息是【你自己之前发出的回复】（前缀标有「【禁止重复-你的历史回复】」），你已经说过这些话了，绝对不能再重复相同或相似的内容。\n"
                        f"其余 ID 的消息是【其他用户发送的消息】，是别人说的话，不是你说的。\n"
                        f"群聊中可能有多个不同用户的发言，请仔细识别每条消息的发送者 ID，准确区分是谁在说话，不要混淆。"
                    )
                else:
                    formatted_parts.append(
                        "=== 历史消息上下文 ===\n"
                        "[重要提示] 以下历史消息中，前缀标有「【禁止重复-你的历史回复】」的消息是【你自己之前发出的回复】，你已经说过这些话了，绝对不能再重复。\n"
                        "其余消息均为【其他用户发送的消息】，是别人说的话，不是你说的。请仔细区分。"
                    )

                for msg in history_messages:
                    # 跳过无效的消息对象
                    if msg is None or not isinstance(msg, AstrBotMessage):
                        logger.warning(f"跳过无效的历史消息对象: {type(msg)}")
                        continue
                    # 获取发送者信息（如果需要）
                    sender_name = "未知用户"
                    sender_id = "unknown"
                    is_bot = False

                    if hasattr(msg, "sender") and msg.sender:
                        sender_name = msg.sender.nickname or "未知用户"
                        sender_id = msg.sender.user_id or "unknown"
                        # 判断是否是机器人自己的消息
                        # 确保类型一致性：统一转换为字符串进行比较
                        is_bot = str(sender_id) == str(bot_id)

                        # 调试日志（仅在第一条消息时输出，避免刷屏）
                        if formatted_parts and len(formatted_parts) == 1:
                            if DEBUG_MODE:
                                logger.debug(
                                    f"[上下文格式化] 机器人ID: {bot_id}, 当前消息发送者ID: {sender_id}, 是否为机器人: {is_bot}"
                                )

                    # 如果还没有判定为bot，尝试通过 self_id 判断
                    # 有时候消息没有正确的sender，但有self_id
                    if not is_bot and hasattr(msg, "self_id") and msg.self_id:
                        # 如果消息的 self_id 等于当前 bot_id，说明这是机器人发出的消息
                        # 但需要注意：self_id 通常表示"当前机器人的ID"
                        # 对于bot发送的消息，sender.user_id 应该等于 self_id
                        pass

                    # 获取消息时间（如果需要）
                    time_str = ""
                    if include_timestamp:
                        time_str = "未知时间"
                        if hasattr(msg, "timestamp") and msg.timestamp:
                            try:
                                dt = datetime.fromtimestamp(msg.timestamp)
                                weekday_names = [
                                    "周一",
                                    "周二",
                                    "周三",
                                    "周四",
                                    "周五",
                                    "周六",
                                    "周日",
                                ]
                                weekday = weekday_names[dt.weekday()]
                                time_str = dt.strftime(f"%Y-%m-%d {weekday} %H:%M:%S")
                            except Exception:
                                pass

                    # 获取消息内容
                    message_content = ""
                    if hasattr(msg, "message_str"):
                        message_content = ContextManager._content_to_safe_text(
                            msg.message_str
                        )
                    elif hasattr(msg, "message"):
                        # 简单提取文本
                        for comp in msg.message:
                            if isinstance(comp, Plain):
                                message_content += ContextManager._coerce_plain_text(
                                    comp.text
                                )

                    # 格式化消息（根据配置决定格式）
                    # 构建消息前缀部分
                    prefix_parts = []

                    # 添加时间戳（如果启用，且不是bot自己的消息，避免AI模仿时间戳格式）
                    if include_timestamp and time_str and not is_bot:
                        prefix_parts.append(f"[{time_str}]")

                    # 添加发送者信息（如果启用）
                    if include_sender_info:
                        if is_bot:
                            # AI自己的回复，醒目标注防止重复
                            prefix_parts.append(
                                f"【禁止重复-你的历史回复】{sender_name}(ID:{sender_id}):"
                            )
                        else:
                            # 其他用户的消息
                            prefix_parts.append(f"{sender_name}(ID:{sender_id}):")
                    else:
                        # 不包含发送者信息时，仍需要区分bot自己的消息
                        if is_bot:
                            prefix_parts.append("【禁止重复-你的历史回复】:")

                    # 组合完整消息
                    if prefix_parts:
                        formatted_msg = " ".join(prefix_parts) + " " + message_content
                    else:
                        formatted_msg = message_content

                    # 检测是否为缓存消息（未被回复的近期消息），通过 message_id 前缀判断
                    # cached_astrbot_messages 在合并时 message_id 被设为 f"cached_{timestamp}"
                    is_cached_msg = (
                        hasattr(msg, "message_id")
                        and msg.message_id
                        and str(msg.message_id).startswith("cached_")
                    )
                    if is_cached_msg:
                        formatted_msg = "【📦近期未回复】 " + formatted_msg

                    formatted_parts.append(formatted_msg)

                formatted_parts.append("")  # 空行分隔

            # 添加当前消息部分（强调重要性）
            formatted_parts.append("")  # 空行分隔
            formatted_parts.append("")  # 额外空行，增强视觉隔离
            formatted_parts.append("=" * 60)
            formatted_parts.append(
                "=== 以上全部是历史消息，你已经处理过了，不要重复回答 ==="
            )
            formatted_parts.append(
                "=== 【重要】以下是当前新消息（请优先关注这条消息的核心内容）==="
            )
            formatted_parts.append("=" * 60)
            safe_current_message = ContextManager._content_to_safe_text(current_message)
            formatted_parts.append(safe_current_message)
            formatted_parts.append("=" * 60)
            formatted_parts.append("")  # 额外空行，增强视觉隔离

            # 窗口缓冲消息区域（当前消息之后紧接着发的消息）
            try:
                if window_buffered_messages:
                    formatted_parts.append("")
                    formatted_parts.append(
                        "--- 以下是你收到这条消息后，同一用户或其他用户紧接着又发的消息 ---"
                    )
                    formatted_parts.append(
                        "这些追加消息帮助你理解完整对话背景。追加消息的发送者可能与当前对话对象不同，注意根据名字和ID区分。"
                    )

                    # 按时间排序
                    sorted_wb = sorted(
                        window_buffered_messages,
                        key=lambda m: (
                            m.get("message_timestamp") or m.get("timestamp", 0)
                        ),
                    )

                    for wb_msg in sorted_wb:
                        wb_sender_name = wb_msg.get("sender_name", "未知用户")
                        wb_sender_id = wb_msg.get("sender_id", "unknown")
                        wb_content = (
                            MessageProcessor.format_message_for_context_display(
                                ContextManager._content_to_safe_text(
                                    wb_msg.get("content", "")
                                ),
                                wb_msg.get("mention_info"),
                                wb_msg.get("is_at_all_message", False),
                                wb_msg.get("persistent_poke_event_text", ""),
                            )
                        )

                        # 时间格式化（与历史消息保持一致）
                        wb_time_str = ""
                        if include_timestamp:
                            msg_ts = wb_msg.get("message_timestamp") or wb_msg.get(
                                "timestamp"
                            )
                            if msg_ts:
                                try:
                                    dt = datetime.fromtimestamp(msg_ts)
                                    weekday_names = [
                                        "周一",
                                        "周二",
                                        "周三",
                                        "周四",
                                        "周五",
                                        "周六",
                                        "周日",
                                    ]
                                    weekday = weekday_names[dt.weekday()]
                                    wb_time_str = f"[{dt.strftime(f'%Y-%m-%d {weekday} %H:%M:%S')}] "
                                except Exception:
                                    pass

                        if include_sender_info:
                            formatted_parts.append(
                                f"{wb_time_str}{wb_sender_name}(ID:{wb_sender_id}): {wb_content}"
                            )
                        else:
                            formatted_parts.append(f"{wb_time_str}{wb_content}")

                    formatted_parts.append("--- 以上为紧接着的追加消息 ---")

                    if DEBUG_MODE:
                        logger.debug(
                            f"[上下文格式化] 已拼接 {len(sorted_wb)} 条窗口缓冲消息到当前消息下方"
                        )
            except Exception as e:
                logger.warning(f"[上下文格式化] 窗口缓冲消息拼接失败，降级忽略: {e}")

            result = "\n".join(formatted_parts)

            # 戳一戳提示追加在分隔符之外（与消息内容分离，仅运行时提示 AI）
            if poke_notice and poke_notice.strip():
                try:
                    result = result.rstrip() + "\n\n" + poke_notice.strip()
                except Exception:
                    pass

            if DEBUG_MODE:
                logger.debug(f"上下文格式化完成,总长度: {len(result)} 字符")
            return result

        except Exception as e:
            logger.error(f"格式化上下文时发生错误: {e}")
            # 发生错误时,至少返回当前消息
            return ContextManager._content_to_safe_text(current_message)

    @staticmethod
    def calculate_context_size(
        history_messages: list[AstrBotMessage], current_message: str
    ) -> int:
        """
        计算上下文总消息数（含当前消息）

        Args:
            history_messages: 历史消息列表
            current_message: 当前消息

        Returns:
            总消息数
        """
        return len(history_messages) + 1

    @staticmethod
    async def save_user_message(
        event: AstrMessageEvent,
        message_text: str,
        context: "Context" = None,
    ) -> bool:
        """
        保存用户消息到 AstrBot 官方平台历史

        Args:
            event: 消息事件
            message_text: 用户消息（可能已包含元数据）
            context: Context对象（可选）

        Returns:
            是否成功
        """
        try:
            # 导入 MessageCleaner
            from .message_cleaner import MessageCleaner

            # 🔧 修复：更强的清理，确保所有系统提示词被移除
            cleaned_message = MessageCleaner.clean_message(message_text)
            if not cleaned_message:
                # 如果清理后为空，使用原消息
                cleaned_message = message_text

            # 🔧 修复：二次清理，确保戳一戳和系统提示完全被移除
            # 检测更多的系统提示词特征
            if (
                "[系统提示]" in cleaned_message
                or "[戳一戳提示]" in cleaned_message
                or "[戳过对方提示]" in cleaned_message
                or "[当前时间:" in cleaned_message
                or "[User ID:" in cleaned_message
                or "[当前情绪状态:" in cleaned_message
                or "=== 历史消息上下文 ===" in cleaned_message
                or "=== 背景信息 ===" in cleaned_message
                or "💭 相关记忆：" in cleaned_message
                or "=== 可用工具列表 ===" in cleaned_message
                or "[系统提示-工具提醒开始]" in cleaned_message
                or "[第三方插件补充信息]" in cleaned_message
                or "【当前对话对象】重要提醒" in cleaned_message
                or "【第一重要】识别当前发送者：" in cleaned_message
                or "紧接着又发的消息" in cleaned_message
                or "=== 以上全部是历史消息" in cleaned_message
                or "【禁止重复-你的历史回复】" in cleaned_message
            ):  # 如果仍然包含系统提示，再次清理
                import re

                cleaned_message = re.sub(
                    r"\n+\s*\[系统提示\][^\n]*", "", cleaned_message
                )
                cleaned_message = re.sub(
                    r"\n+\s*\[戳一戳提示\][^\n]*", "", cleaned_message
                )
                cleaned_message = re.sub(
                    r"\n+\s*\[戳过对方提示\][^\n]*", "", cleaned_message
                )
                # 清理额外的系统提示词
                cleaned_message = re.sub(
                    r"\[当前时间:\d{4}-\d{2}-\d{2}\s+周[一二三四五六日]\s+\d{2}:\d{2}:\d{2}\]",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"\[User ID:.*?Nickname:.*?\]", "", cleaned_message
                )
                cleaned_message = re.sub(r"\[当前情绪状态:.*?\]", "", cleaned_message)
                cleaned_message = re.sub(
                    r"=== 历史消息上下文 ===[\s\S]*?(?==== |$)", "", cleaned_message
                )
                cleaned_message = re.sub(
                    r"=== 背景信息 ===[\s\S]*?(?==== |$)", "", cleaned_message
                )
                cleaned_message = re.sub(
                    r"💭 相关记忆：[\s\S]*?(?==== |$)", "", cleaned_message
                )
                cleaned_message = re.sub(
                    r"=== 可用工具列表 ===[\s\S]*?(?=请根据上述对话|请开始回复|====|$)",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"\[系统提示-工具提醒开始\][\s\S]*?\[系统提示-工具提醒结束\]",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"\[第三方插件补充信息\][\s\S]*?\[第三方插件补充信息结束\]",
                    "",
                    cleaned_message,
                )
                # 🆕 逐插件标记清理（新格式，含描述文本）
                cleaned_message = re.sub(
                    r"\[第三方插件补充 - [^\]]+\]",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"\[第三方插件补充 - [^\]]+ 结束\]",
                    "",
                    cleaned_message,
                )
                # 逐插件 context 完整消息（开头标记+描述）
                cleaned_message = re.sub(
                    r"\[第三方插件注入上下文 - [^\]]+\]\n以下对话记录来自插件 '[^']+' 的提示词系统，请作为额外的对话上下文理解，与主对话历史融合参考。",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"\[第三方插件注入上下文 - [^\]]+ 结束\]",
                    "",
                    cleaned_message,
                )
                # 回退路径 context 完整消息（开头标记+描述）
                cleaned_message = re.sub(
                    r"\[第三方插件注入上下文\]\n以下对话记录来自其他插件的提示词系统，请作为额外的对话上下文理解，与主对话历史融合参考。",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"\[第三方插件注入上下文 结束\]",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"当前平台共有 \d+ 个可用工具:[\s\S]*?(?=请根据上述对话|请开始回复|====|$)",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"============+\n*.*?【当前对话对象】重要提醒.*?\n*============+[\s\S]*?(?=\n\n[^\s=]|$)",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"【第一重要】识别当前发送者：[\s\S]*?(?=请开始回复|====|$)",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"=+\n*.*?【重要】当前新消息.*?\n*=+", "", cleaned_message
                )
                # 清理窗口缓冲消息区域（追加消息提示词）
                cleaned_message = re.sub(
                    r"--- 以下是你收到这条消息后，同一用户或其他用户紧接着又发的消息 ---[\s\S]*?--- 以上为紧接着的追加消息 ---",
                    "",
                    cleaned_message,
                )
                # 清理历史/当前消息分隔线（format_context_for_ai 输出的边界标记）
                cleaned_message = re.sub(
                    r"=+\n*=== 以上全部是历史消息，你已经处理过了，不要重复回答 ===\n*=+",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"=== 【重要】以下是当前新消息（请优先关注这条消息的核心内容）===",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"=== 【重要】当前新消息（请优先关注这条消息的核心内容）===",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"【禁止重复-你的历史回复】",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"【📦近期未回复】\s*",
                    "",
                    cleaned_message,
                )
                cleaned_message = cleaned_message.strip()
                if DEBUG_MODE:
                    logger.debug("⚠️ [保存消息] 检测到系统提示残留，已二次清理")

            # 获取平台和聊天信息
            is_private = event.is_private_chat()
            chat_id = event.get_group_id() if not is_private else event.get_sender_id()

            if not chat_id:
                logger.warning("无法获取聊天ID,跳过消息保存")
                return False

            # 保存到官方历史管理器（platform_message_history表）
            # 注意：这个表和conversation不同，是用于平台消息记录的
            if context:
                try:
                    # 获取消息链并转换为dict格式，确保JSON可序列化
                    message_chain_dict = []
                    if hasattr(event, "message_obj") and hasattr(
                        event.message_obj, "message"
                    ):
                        for comp in event.message_obj.message:
                            try:
                                comp_dict = await comp.to_dict()
                                # 确保字典内容是JSON可序列化的
                                # 移除或转换不可序列化的对象（如Image对象）
                                if isinstance(comp_dict, dict):
                                    serializable_dict = {}
                                    for k, v in comp_dict.items():
                                        if k == "data" and isinstance(v, dict):
                                            # 处理data字段，确保其内容可序列化
                                            serializable_data = {}
                                            for dk, dv in v.items():
                                                # 只保留基本类型和字符串
                                                if isinstance(
                                                    dv,
                                                    (str, int, float, bool, type(None)),
                                                ):
                                                    serializable_data[dk] = dv
                                                elif isinstance(dv, (list, dict)):
                                                    # 尝试JSON序列化测试
                                                    try:
                                                        json.dumps(dv)
                                                        serializable_data[dk] = dv
                                                    except (TypeError, ValueError):
                                                        # 不可序列化，转为字符串
                                                        serializable_data[dk] = str(dv)
                                                else:
                                                    # 其他对象转为字符串
                                                    serializable_data[dk] = str(dv)
                                            serializable_dict[k] = serializable_data
                                        elif isinstance(
                                            v, (str, int, float, bool, type(None))
                                        ):
                                            serializable_dict[k] = v
                                        else:
                                            serializable_dict[k] = str(v)
                                    message_chain_dict.append(serializable_dict)
                                else:
                                    message_chain_dict.append(comp_dict)
                            except Exception as comp_err:
                                if DEBUG_MODE:
                                    logger.debug(f"组件转换失败，跳过: {comp_err}")
                                continue

                    if not message_chain_dict:
                        # 如果没有成功转换的消息链，创建纯文本消息
                        message_chain_dict = [
                            {"type": "text", "data": {"text": message_text}}
                        ]

                    # Serialize direct platform-history inserts with batch saves.
                    async with ContextManager._get_official_save_lock():
                        await context.message_history_manager.insert(
                            platform_id=event.get_platform_id(),
                            user_id=event.unified_msg_origin,
                            content=message_chain_dict,
                            sender_id=event.get_sender_id(),
                            sender_name=event.get_sender_name() or "未知用户",
                        )

                    if DEBUG_MODE:
                        logger.debug(
                            "用户消息已保存到官方历史管理器(platform_message_history)"
                        )

                except Exception as e:
                    logger.warning(
                        f"保存到官方历史管理器(platform_message_history)失败: {e}"
                    )
                    # 这不影响conversation_manager的保存

            return True

        except Exception as e:
            logger.error(f"保存用户消息失败: {e}")
            return False

    @staticmethod
    async def save_bot_message(
        event: AstrMessageEvent,
        bot_message_text: str,
        context: "Context" = None,
    ) -> bool:
        """
        保存 AI 回复到 AstrBot 官方平台历史

        Args:
            event: 消息事件
            bot_message_text: AI回复文本
            context: Context对象（可选）

        Returns:
            是否成功
        """
        try:
            # 导入 MessageCleaner
            from .message_cleaner import MessageCleaner

            # 🔧 修复：更强的清理，确保所有系统提示词被移除
            cleaned_message = MessageCleaner.clean_message(bot_message_text)
            if not cleaned_message:
                # 如果清理后为空，使用原消息
                cleaned_message = bot_message_text

            # 🔧 修复：二次清理，确保戳一戳和系统提示完全被移除
            # 检测更多的系统提示词特征
            if (
                "[系统提示]" in cleaned_message
                or "[戳一戳提示]" in cleaned_message
                or "[戳过对方提示]" in cleaned_message
                or "[当前时间:" in cleaned_message
                or "[User ID:" in cleaned_message
                or "[当前情绪状态:" in cleaned_message
                or "=== 历史消息上下文 ===" in cleaned_message
                or "=== 背景信息 ===" in cleaned_message
                or "💭 相关记忆：" in cleaned_message
                or "=== 可用工具列表 ===" in cleaned_message
                or "[系统提示-工具提醒开始]" in cleaned_message
                or "[第三方插件补充信息]" in cleaned_message
                or "【当前对话对象】重要提醒" in cleaned_message
                or "【第一重要】识别当前发送者：" in cleaned_message
                or "紧接着又发的消息" in cleaned_message
                or "=== 以上全部是历史消息" in cleaned_message
                or "【禁止重复-你的历史回复】" in cleaned_message
            ):  # 如果仍然包含系统提示，再次清理
                cleaned_message = re.sub(
                    r"\n+\s*\[系统提示\][^\n]*", "", cleaned_message
                )
                cleaned_message = re.sub(
                    r"\n+\s*\[戳一戳提示\][^\n]*", "", cleaned_message
                )
                cleaned_message = re.sub(
                    r"\n+\s*\[戳过对方提示\][^\n]*", "", cleaned_message
                )
                # 清理额外的系统提示词
                cleaned_message = re.sub(
                    r"\[当前时间:\d{4}-\d{2}-\d{2}\s+周[一二三四五六日]\s+\d{2}:\d{2}:\d{2}\]",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"\[User ID:.*?Nickname:.*?\]", "", cleaned_message
                )
                cleaned_message = re.sub(r"\[当前情绪状态:.*?\]", "", cleaned_message)
                cleaned_message = re.sub(
                    r"=== 历史消息上下文 ===[\s\S]*?(?==== |$)", "", cleaned_message
                )
                cleaned_message = re.sub(
                    r"=== 背景信息 ===[\s\S]*?(?==== |$)", "", cleaned_message
                )
                cleaned_message = re.sub(
                    r"💭 相关记忆：[\s\S]*?(?==== |$)", "", cleaned_message
                )
                cleaned_message = re.sub(
                    r"=== 可用工具列表 ===[\s\S]*?(?=请根据上述对话|请开始回复|====|$)",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"\[系统提示-工具提醒开始\][\s\S]*?\[系统提示-工具提醒结束\]",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"\[第三方插件补充信息\][\s\S]*?\[第三方插件补充信息结束\]",
                    "",
                    cleaned_message,
                )
                # 🆕 逐插件标记清理（新格式，含描述文本）
                cleaned_message = re.sub(
                    r"\[第三方插件补充 - [^\]]+\]",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"\[第三方插件补充 - [^\]]+ 结束\]",
                    "",
                    cleaned_message,
                )
                # 逐插件 context 完整消息（开头标记+描述）
                cleaned_message = re.sub(
                    r"\[第三方插件注入上下文 - [^\]]+\]\n以下对话记录来自插件 '[^']+' 的提示词系统，请作为额外的对话上下文理解，与主对话历史融合参考。",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"\[第三方插件注入上下文 - [^\]]+ 结束\]",
                    "",
                    cleaned_message,
                )
                # 回退路径 context 完整消息（开头标记+描述）
                cleaned_message = re.sub(
                    r"\[第三方插件注入上下文\]\n以下对话记录来自其他插件的提示词系统，请作为额外的对话上下文理解，与主对话历史融合参考。",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"\[第三方插件注入上下文 结束\]",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"当前平台共有 \d+ 个可用工具:[\s\S]*?(?=请根据上述对话|请开始回复|====|$)",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"============+\n*.*?【当前对话对象】重要提醒.*?\n*============+[\s\S]*?(?=\n\n[^\s=]|$)",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"【第一重要】识别当前发送者：[\s\S]*?(?=请开始回复|====|$)",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"=+\n*.*?【重要】当前新消息.*?\n*=+", "", cleaned_message
                )
                # 清理窗口缓冲消息区域（追加消息提示词）
                cleaned_message = re.sub(
                    r"--- 以下是你收到这条消息后，同一用户或其他用户紧接着又发的消息 ---[\s\S]*?--- 以上为紧接着的追加消息 ---",
                    "",
                    cleaned_message,
                )
                # 清理历史/当前消息分隔线（format_context_for_ai 输出的边界标记）
                cleaned_message = re.sub(
                    r"=+\n*=== 以上全部是历史消息，你已经处理过了，不要重复回答 ===\n*=+",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"=== 【重要】以下是当前新消息（请优先关注这条消息的核心内容）===",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"=== 【重要】当前新消息（请优先关注这条消息的核心内容）===",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"【禁止重复-你的历史回复】",
                    "",
                    cleaned_message,
                )
                cleaned_message = re.sub(
                    r"【📦近期未回复】\s*",
                    "",
                    cleaned_message,
                )
                cleaned_message = cleaned_message.strip()
                if DEBUG_MODE:
                    logger.debug("⚠️ [AI回复保存] 检测到系统提示残留，已二次清理")

            # 获取平台和聊天信息
            is_private = event.is_private_chat()
            chat_id = event.get_group_id() if not is_private else event.get_sender_id()

            if not chat_id:
                logger.warning("无法获取聊天ID,跳过消息保存")
                return False

            # 保存到官方历史管理器（platform_message_history表）
            # 注意：这个表和conversation不同，是用于平台消息记录的
            if context:
                try:
                    # 从event的result中获取消息链
                    result = event.get_result()
                    message_chain_dict = []

                    if result and hasattr(result, "chain") and result.chain:
                        # 转换消息链为dict格式，确保JSON可序列化
                        for comp in result.chain:
                            try:
                                comp_dict = await comp.to_dict()
                                # 确保字典内容是JSON可序列化的
                                if isinstance(comp_dict, dict):
                                    serializable_dict = {}
                                    for k, v in comp_dict.items():
                                        if k == "data" and isinstance(v, dict):
                                            # 处理data字段，确保其内容可序列化
                                            serializable_data = {}
                                            for dk, dv in v.items():
                                                # 只保留基本类型和字符串
                                                if isinstance(
                                                    dv,
                                                    (str, int, float, bool, type(None)),
                                                ):
                                                    serializable_data[dk] = dv
                                                elif isinstance(dv, (list, dict)):
                                                    # 尝试JSON序列化测试
                                                    try:
                                                        json.dumps(dv)
                                                        serializable_data[dk] = dv
                                                    except (TypeError, ValueError):
                                                        # 不可序列化，转为字符串
                                                        serializable_data[dk] = str(dv)
                                                else:
                                                    # 其他对象转为字符串
                                                    serializable_data[dk] = str(dv)
                                            serializable_dict[k] = serializable_data
                                        elif isinstance(
                                            v, (str, int, float, bool, type(None))
                                        ):
                                            serializable_dict[k] = v
                                        else:
                                            serializable_dict[k] = str(v)
                                    message_chain_dict.append(serializable_dict)
                                else:
                                    message_chain_dict.append(comp_dict)
                            except Exception as comp_err:
                                if DEBUG_MODE:
                                    logger.debug(f"组件转换失败，跳过: {comp_err}")
                                    continue

                    if not message_chain_dict:
                        # 如果没有消息链，创建纯文本消息
                        message_chain_dict = [
                            {"type": "text", "data": {"text": bot_message_text}}
                        ]

                    # Serialize direct platform-history inserts with batch saves.
                    async with ContextManager._get_official_save_lock():
                        await context.message_history_manager.insert(
                            platform_id=event.get_platform_id(),
                            user_id=event.unified_msg_origin,
                            content=message_chain_dict,
                            sender_id=event.get_self_id(),
                            sender_name="AstrBot",
                        )

                    if DEBUG_MODE:
                        logger.debug(
                            "AI回复消息已保存到官方历史管理器(platform_message_history)"
                        )

                except Exception as e:
                    logger.warning(
                        f"保存到官方历史管理器(platform_message_history)失败: {e}"
                    )
                    # 这不影响conversation_manager的保存

            return True

        except Exception as e:
            logger.error(f"保存AI消息失败: {e}")
            return False

    @staticmethod
    async def save_to_official_conversation(
        event: AstrMessageEvent, user_message: str, bot_message: str, context: "Context"
    ) -> bool:
        """
        保存消息到官方对话系统

        Args:
            event: 消息事件
            user_message: 用户消息（原始，不带元数据）
            bot_message: AI回复
            context: Context对象

        Returns:
            是否成功
        """
        try:
            # 1. 获取unified_msg_origin（会话标识）
            unified_msg_origin = event.unified_msg_origin
            if DEBUG_MODE:
                logger.debug(
                    f"[官方保存] 准备保存到官方对话系统，会话: {unified_msg_origin}"
                )

            # 2. 获取conversation_manager
            cm = context.conversation_manager

            # 3. 获取当前对话ID，如果没有则创建
            curr_cid = await cm.get_curr_conversation_id(unified_msg_origin)
            if not curr_cid:
                if DEBUG_MODE:
                    logger.debug(
                        f"[官方保存] 会话 {unified_msg_origin} 没有对话，创建新对话"
                    )
                # 获取群名作为标题
                chat_id = (
                    event.get_group_id()
                    if not event.is_private_chat()
                    else event.get_sender_id()
                )
                title = (
                    f"群聊 {chat_id}"
                    if not event.is_private_chat()
                    else f"私聊 {event.get_sender_name()}"
                )

                # 使用new_conversation创建
                curr_cid = await cm.new_conversation(
                    unified_msg_origin=unified_msg_origin,
                    platform_id=event.get_platform_id(),
                    title=title,
                    content=[],
                )
                if DEBUG_MODE:
                    logger.debug(f"[官方保存] 创建新对话ID: {curr_cid}")

            if not curr_cid:
                logger.warning("[官方保存] 无法创建或获取对话ID")
                return False

            # 4. 获取当前对话的历史记录
            conversation = await cm.get_conversation(
                unified_msg_origin=unified_msg_origin, conversation_id=curr_cid
            )

            # 5. 构建完整的历史列表（包含已有历史+新消息）
            if conversation and conversation.content:
                history_list = conversation.content
            else:
                history_list = []

            if DEBUG_MODE:
                logger.debug(f"[官方保存] 当前对话有 {len(history_list)} 条历史消息")

            # 6. 添加用户消息和AI回复
            history_list.append({"role": "user", "content": user_message})
            history_list.append({"role": "assistant", "content": bot_message})

            if DEBUG_MODE:
                logger.debug(
                    f"[官方保存] 准备保存，新增2条消息，总计 {len(history_list)} 条"
                )

            # 7. 使用官方API保存（参考旧插件的成功方法）
            success = await ContextManager._try_official_save(
                cm, unified_msg_origin, curr_cid, history_list
            )

            if success:
                logger.debug(
                    f"✅ [官方保存] 消息已保存到官方对话系统 (conversation_id: {curr_cid}, 总消息数: {len(history_list)})"
                )
                return True
            else:
                logger.error("[官方保存] 所有保存方法均失败")
                return False

        except Exception as e:
            logger.error(f"[官方保存] 保存到官方对话系统失败: {e}", exc_info=True)
            return False

    @staticmethod
    async def _try_official_save(
        cm, unified_msg_origin: str, conversation_id: str, history_list: list
    ) -> bool:
        """
        尝试多种方法保存到官方对话管理器

        Args:
            cm: conversation_manager对象
            unified_msg_origin: 会话来源标识
            conversation_id: 对话ID
            history_list: 历史消息列表

        Returns:
            是否成功
        """
        try:
            # 扩展的方法列表（完全按照旧插件）
            methods = [
                "update_conversation",  # 这是正确的主要保存方法
                "update_conversation_history",
                "set_conversation_history",
                "save_conversation_history",
                "save_history",
                # 追加式候选
                "append_conversation_history",
                "append_history",
                "add_conversation_history",
                "add_history",
                # 新增更多可能的API方法
                "update_history",
                "set_history",
                "store_conversation_history",
                "store_history",
                "record_conversation_history",
                "record_history",
            ]

            # 记录可用方法
            try:
                cm_type = type(cm).__name__
                available = [m for m in methods if hasattr(cm, m)]
                if DEBUG_MODE:
                    logger.debug(
                        f"[官方保存] CM类型={cm_type}, 对话ID={conversation_id}, 消息数={len(history_list)}"
                    )
                    logger.debug(f"[官方保存] 可用方法: {available}")
                    logger.debug(f"[官方保存] unified_msg_origin: {unified_msg_origin}")
            except Exception as e:
                logger.warning(f"[官方保存] 记录CM信息失败: {e}")

            # [优先] 使用官方标准 API 直接保存（签名: update_conversation(unified_msg_origin, conversation_id, history)）
            try:
                await cm.update_conversation(
                    unified_msg_origin,
                    conversation_id=conversation_id,
                    history=history_list,
                )
                logger.info(
                    f"[官方保存] ✅ 已通过 update_conversation 写入官方库(对话ID={conversation_id}, {len(history_list)}条)"
                )
                return True
            except TypeError as _te:
                logger.warning(f"[官方保存] update_conversation 类型不匹配: {_te}")
            except Exception as _e:
                logger.warning(f"[官方保存] update_conversation 调用失败: {_e}")

            # 优先尝试以列表直接保存（按照旧插件的方式）
            for m in methods:
                if hasattr(cm, m):
                    # 尝试位置参数+列表
                    try:
                        if DEBUG_MODE:
                            logger.debug(
                                f"[官方保存] >>> 尝试 {m} 使用列表参数，历史长度={len(history_list)}"
                            )
                        await getattr(cm, m)(
                            unified_msg_origin, conversation_id, history_list
                        )

                        logger.debug(f"✅ [官方保存] {m} 成功（列表）")

                        # 验证是否真的保存成功
                        try:
                            verification = await cm.get_conversation(
                                unified_msg_origin, conversation_id
                            )
                            if verification:
                                if DEBUG_MODE:
                                    logger.debug(
                                        f"✅ [官方保存] 验证成功：对话存在，ID={conversation_id}"
                                    )
                            else:
                                logger.warning(
                                    "[官方保存] 验证失败：无法获取刚保存的对话"
                                )
                        except Exception as ve:
                            logger.warning(f"[官方保存] 验证检查失败: {ve}")

                        return True
                    except TypeError as te:
                        # 参数类型不匹配，尝试字符串格式
                        if DEBUG_MODE:
                            logger.debug(f"[官方保存] {m} 列表参数类型不匹配: {te}")
                    except Exception as e:
                        logger.warning(f"[官方保存] {m}（列表）失败: {e}")

                    # 尝试字符串格式
                    try:
                        history_str = json.dumps(history_list, ensure_ascii=False)
                        if DEBUG_MODE:
                            logger.debug(
                                f"[官方保存] >>> 尝试 {m} 使用字符串参数，长度={len(history_str)}"
                            )
                        await getattr(cm, m)(
                            unified_msg_origin, conversation_id, history_str
                        )

                        logger.debug(f"✅ [官方保存] {m} 成功（字符串）")
                        return True
                    except Exception as e2:
                        logger.warning(f"[官方保存] {m}（字符串）失败: {e2}")

            logger.error("❌ [官方保存] 所有保存方法均失败！消息可能未保存到官方系统！")
            return False

        except Exception as e:
            logger.error(f"[官方保存] 尝试官方持久化时发生严重异常: {e}", exc_info=True)
            return False

    @staticmethod
    async def flush_cached_messages_by_params(
        platform_name: str,
        is_private: bool,
        chat_id: str,
        unified_msg_origin: str,
        cached_messages: list,
        context: "Context",
        self_id: str = None,
        platform_id: str = None,
    ) -> bool:
        try:
            from .message_cleaner import MessageCleaner

            if not cached_messages:
                return True
            if not context:
                logger.warning("[冷群转正] Context 为空，无法保存缓存消息")
                return False
            if not chat_id or not unified_msg_origin:
                logger.warning(
                    "[冷群转正] chat_id 或 unified_msg_origin 为空，跳过保存"
                )
                return False

            normalized_cached_messages = []
            for msg in cached_messages:
                if not isinstance(msg, dict) or "content" not in msg:
                    continue
                cleaned_content = msg["content"]
                if isinstance(cleaned_content, str):
                    cleaned_content = (
                        MessageCleaner.clean_message(cleaned_content) or cleaned_content
                    )
                normalized_msg = dict(msg)
                normalized_msg["content"] = cleaned_content
                normalized_cached_messages.append(normalized_msg)

            if not normalized_cached_messages:
                return True

            # 按消息原始时间戳升序排列，保证写入历史的顺序正确
            normalized_cached_messages.sort(
                key=lambda m: m.get("message_timestamp") or m.get("timestamp", 0)
            )

            # ========== 保存到官方 platform_message_history ==========
            # 冷群转正也需要写入此表，确保 Web Chat UI 可展示这些消息
            # 与 save_user_message (line 1901) 保持一致的参数模式
            if context and platform_id:
                for cached_msg in normalized_cached_messages:
                    try:
                        msg_content = cached_msg.get("content", "")
                        if not msg_content:
                            continue
                        message_chain_dict = [
                            {"type": "text", "data": {"text": msg_content}}
                        ]
                        await context.message_history_manager.insert(
                            platform_id=platform_id,
                            user_id=unified_msg_origin,
                            content=message_chain_dict,
                            sender_id=cached_msg.get("sender_id", "unknown"),
                            sender_name=cached_msg.get("sender_name", "未知用户"),
                        )
                        if DEBUG_MODE:
                            logger.debug(
                                f"[冷群转正] platform_message_history 写入成功: "
                                f"sender={cached_msg.get('sender_name')}, "
                                f"content_preview={msg_content[:80]}..."
                            )
                    except Exception as e:
                        logger.warning(
                            f"[冷群转正] platform_message_history 写入失败 "
                            f"(sender={cached_msg.get('sender_name')}): {e}"
                        )

            cm = context.conversation_manager
            curr_cid = await cm.get_curr_conversation_id(unified_msg_origin)
            if not curr_cid:
                title = f"群聊 {chat_id}" if not is_private else f"私聊 {chat_id}"
                curr_cid = await cm.new_conversation(
                    unified_msg_origin=unified_msg_origin,
                    platform_id=platform_id,
                    title=title,
                    content=[],
                )

            if not curr_cid:
                logger.warning(f"[冷群转正] 无法获取或创建对话ID: {unified_msg_origin}")
                return False

            conversation = await cm.get_conversation(
                unified_msg_origin=unified_msg_origin, conversation_id=curr_cid
            )
            if conversation and conversation.history:
                try:
                    history_list = json.loads(conversation.history)
                except (json.JSONDecodeError, TypeError):
                    history_list = []
            else:
                history_list = []

            added_count = 0
            for cached_msg in normalized_cached_messages:
                content = cached_msg.get("content", "")
                if not content:
                    continue
                image_urls = cached_msg.get("image_urls") or []
                if image_urls:
                    multimodal_content = [{"type": "text", "text": content}]
                    multimodal_content.extend(
                        {"type": "image_url", "image_url": {"url": url}}
                        for url in image_urls
                        if url
                    )
                    history_list.append({"role": "user", "content": multimodal_content})
                else:
                    history_list.append({"role": "user", "content": content})
                added_count += 1

            MAX_HISTORY_LENGTH = 150
            if len(history_list) > MAX_HISTORY_LENGTH:
                history_list = history_list[-MAX_HISTORY_LENGTH:]

            success = await ContextManager._try_official_save(
                cm, unified_msg_origin, curr_cid, history_list
            )
            if success:
                parts = ["platform_message_history", "conversations"]
                dest = "+".join(parts)
                logger.debug(f"✅ [冷群转正] 已保存 {added_count} 条缓存消息到 {dest}")
                return True

            logger.warning("[冷群转正] 保存到官方历史失败")
            return False
        except Exception as e:
            logger.error(f"[冷群转正] 保存缓存消息失败: {e}", exc_info=True)
            return False

    @staticmethod
    async def clear_official_history_for_event(context, event) -> bool:
        """彻底清空该会话的官方历史（platform_message_history + conversations.content），供 reset 使用。

        Args:
            context: AstrBot Context 对象
            event: 消息事件

        Returns:
            是否执行了清理
        """
        try:
            platform_id = event.get_platform_id()
            is_private = event.is_private_chat()
            chat_id = event.get_group_id() if not is_private else event.get_sender_id()
            did = False
            if hasattr(context, "message_history_manager"):
                try:
                    # 该 API 语义为"删除 created_at>=now-offset 的记录"（删较新的），
                    # 用超大 offset 使 cutoff 足够早，从而删除该会话全部历史记录。
                    await context.message_history_manager.delete(
                        platform_id=platform_id,
                        user_id=event.unified_msg_origin,
                        offset_sec=10 * 365 * 24 * 3600,
                    )
                    logger.info(
                        f"[reset] ✅ 已清空官方消息历史(platform_message_history: {platform_id}/{chat_id})"
                    )
                    did = True
                except Exception as e:
                    logger.warning(f"[reset] 清空官方消息历史失败: {e}")
            try:
                cm = context.conversation_manager
                umo = event.unified_msg_origin
                cid = await cm.get_curr_conversation_id(umo)
                if cid:
                    await cm.update_conversation(umo, cid, [])
                    logger.info(f"[reset] ✅ 已清空官方对话内容(conversations: {cid})")
                    did = True
            except Exception as e:
                logger.warning(f"[reset] 清空官方对话失败: {e}")
            return did
        except Exception as e:
            logger.warning(f"[reset] 清理失败: {e}")
            return False

    @staticmethod
    async def save_to_official_conversation_with_cache(
        event: AstrMessageEvent,
        cached_messages: list,
        user_message: str,
        bot_message: str,
        context: "Context",
        save_kind: str = "normal",
    ) -> bool:
        """Serialize one complete official history save operation."""
        async with ContextManager._get_official_save_lock():
            return (
                await ContextManager._save_to_official_conversation_with_cache_unlocked(
                    event,
                    cached_messages,
                    user_message,
                    bot_message,
                    context,
                    save_kind,
                )
            )

    @staticmethod
    async def _save_to_official_conversation_with_cache_unlocked(
        event: AstrMessageEvent,
        cached_messages: list,
        user_message: str,
        bot_message: str,
        context: "Context",
        save_kind: str = "normal",
    ) -> bool:
        """
        保存到 AstrBot 官方对话系统，支持缓存转正

        统一负责本次对话回合的官方历史写入（缓存消息 → 用户消息 → AI 回复）。

        Args:
            event: 消息事件
            cached_messages: 待转正的缓存消息（已去重）
            user_message: 当前用户消息（原始，不带元数据）
            bot_message: AI回复
            context: Context对象
            save_kind: 保存类型（"normal" 普通 / "poke_event" 戳一戳事件）

        Returns:
            是否成功
        """
        try:
            # 导入 MessageCleaner
            from .message_cleaner import MessageCleaner

            # 清理消息，确保不包含系统提示词
            if user_message:
                user_message = (
                    MessageCleaner.clean_message(user_message) or user_message
                )
            if bot_message is not None:
                cleaned_bot = MessageCleaner.clean_message(bot_message)
                bot_message = cleaned_bot or bot_message

            # 清理缓存消息
            if cached_messages:
                for msg in cached_messages:
                    if isinstance(msg, dict) and "content" in msg:
                        original_content = msg["content"]
                        cleaned_content = original_content
                        if isinstance(original_content, str):
                            cleaned_content = (
                                MessageCleaner.clean_message(original_content)
                                or original_content
                            )
                        if cleaned_content:
                            msg["content"] = cleaned_content

            # 1. 获取unified_msg_origin（会话标识）
            unified_msg_origin = event.unified_msg_origin
            platform_id = event.get_platform_id()
            is_private = event.is_private_chat()
            chat_id = event.get_group_id() if not is_private else event.get_sender_id()
            if DEBUG_MODE:
                log_prefix = (
                    "[官方保存+戳一戳事件]"
                    if save_kind == "poke_event"
                    else "[官方保存+缓存转正]"
                )
                logger.debug(f"========== {log_prefix} 开始保存 ==========")
                logger.debug(f"{log_prefix} unified_msg_origin: {unified_msg_origin}")
                logger.debug(f"{log_prefix} 缓存消息: {len(cached_messages)} 条")
                logger.debug(f"{log_prefix} 用户消息长度: {len(user_message)} 字符")
                if bot_message is not None:
                    logger.debug(f"{log_prefix} AI回复长度: {len(bot_message)} 字符")
                else:
                    logger.debug(f"{log_prefix} 本次不保存AI回复（bot_message为空）")

            # 2. 获取conversation_manager
            cm = context.conversation_manager
            if DEBUG_MODE:
                logger.debug(
                    f"[官方保存+缓存转正] ConversationManager类型: {type(cm).__name__}"
                )

            # 3. 获取当前对话ID，如果没有则创建
            curr_cid = await cm.get_curr_conversation_id(unified_msg_origin)
            if DEBUG_MODE:
                logger.debug(f"[官方保存+缓存转正] 当前对话ID: {curr_cid}")

            if not curr_cid:
                if DEBUG_MODE:
                    logger.debug(
                        f"[官方保存+缓存转正] ❗ 会话 {unified_msg_origin} 没有对话，准备创建新对话"
                    )
                # 获取群名作为标题
                chat_id = (
                    event.get_group_id()
                    if not event.is_private_chat()
                    else event.get_sender_id()
                )
                title = (
                    f"群聊 {chat_id}"
                    if not event.is_private_chat()
                    else f"私聊 {event.get_sender_name()}"
                )
                if DEBUG_MODE:
                    logger.debug(f"[官方保存+缓存转正] 新对话标题: {title}")
                    logger.debug(
                        f"[官方保存+缓存转正] 平台ID: {event.get_platform_id()}"
                    )

                # 使用new_conversation创建
                try:
                    curr_cid = await cm.new_conversation(
                        unified_msg_origin=unified_msg_origin,
                        platform_id=event.get_platform_id(),
                        title=title,
                        content=[],
                    )
                    if DEBUG_MODE:
                        logger.debug(
                            f"✅ [官方保存+缓存转正] 成功创建新对话，ID: {curr_cid}"
                        )
                except Exception as create_err:
                    logger.error(
                        f"❌ [官方保存+缓存转正] 创建对话失败: {create_err}",
                        exc_info=True,
                    )
                    return False

            if not curr_cid:
                logger.error("❌ [官方保存+缓存转正] 无法创建或获取对话ID")
                return False

            # 4. 获取当前对话的历史记录
            if DEBUG_MODE:
                logger.debug("[官方保存+缓存转正] 正在获取对话历史...")
            try:
                conversation = await cm.get_conversation(
                    unified_msg_origin=unified_msg_origin, conversation_id=curr_cid
                )
                if DEBUG_MODE:
                    logger.debug(
                        f"[官方保存+缓存转正] 获取对话对象: {conversation is not None}"
                    )
                if conversation:
                    if DEBUG_MODE:
                        logger.debug(
                            f"[官方保存+缓存转正] 对话对象类型: {type(conversation).__name__}"
                        )
                        logger.debug(
                            f"[官方保存+缓存转正] 对话标题: {getattr(conversation, 'title', 'N/A')}"
                        )
            except Exception as get_err:
                logger.error(
                    f"❌ [官方保存+缓存转正] 获取对话失败: {get_err}", exc_info=True
                )
                conversation = None

            # 5. 构建完整的历史列表
            if conversation and conversation.history:
                # history是JSON字符串，需要解析
                try:
                    history_list = json.loads(conversation.history)
                    if DEBUG_MODE:
                        logger.debug(
                            f"[官方保存+缓存转正] 解析历史记录成功: {len(history_list)} 条"
                        )
                except (json.JSONDecodeError, TypeError) as parse_err:
                    logger.warning(f"[官方保存+缓存转正] 解析历史记录失败: {parse_err}")
                    history_list = []
            else:
                if DEBUG_MODE:
                    logger.debug("[官方保存+缓存转正] 对话历史为空，从头开始")
                history_list = []

            # Add every queued message; sender and message identity matter more than text equality.
            cache_converted = 0
            if cached_messages:
                for cached_msg in cached_messages:
                    if not isinstance(cached_msg, dict) or not cached_msg.get(
                        "content"
                    ):
                        continue
                    content = cached_msg["content"]
                    image_urls = cached_msg.get("image_urls") or []
                    if image_urls:
                        multimodal_content = [{"type": "text", "text": content}]
                        multimodal_content.extend(
                            {"type": "image_url", "image_url": {"url": url}}
                            for url in image_urls
                            if url
                        )
                        history_list.append(
                            {"role": "user", "content": multimodal_content}
                        )
                    else:
                        history_list.append({"role": "user", "content": content})
                    cache_converted += 1
                if DEBUG_MODE:
                    logger.debug(
                        f"[官方保存+缓存转正] 已添加 {cache_converted} 条缓存消息"
                    )
            elif DEBUG_MODE:
                logger.debug("[官方保存+缓存转正] 无缓存消息需要转正")

            # ========== 保存缓存消息到官方 platform_message_history ==========
            # 正常转正路径也需要将缓存消息写入此表，确保 Web Chat UI 可展示
            # 与冷群转正路径 (flush_cached_messages_by_params) 保持一致
            if context and platform_id and chat_id and cached_messages:
                for cached_msg in cached_messages:
                    try:
                        if (
                            not isinstance(cached_msg, dict)
                            or "content" not in cached_msg
                        ):
                            continue
                        msg_content = cached_msg.get("content", "")
                        if not msg_content:
                            continue
                        message_chain_dict = [
                            {"type": "text", "data": {"text": msg_content}}
                        ]
                        await context.message_history_manager.insert(
                            platform_id=platform_id,
                            user_id=unified_msg_origin,
                            content=message_chain_dict,
                            sender_id=cached_msg.get("sender_id", "unknown"),
                            sender_name=cached_msg.get("sender_name", "未知用户"),
                        )
                        if DEBUG_MODE:
                            logger.debug(
                                f"[官方保存+缓存转正] platform_message_history 写入成功: "
                                f"sender={cached_msg.get('sender_name', '未知')}, "
                                f"content_preview={msg_content[:80]}..."
                            )
                    except Exception as e:
                        logger.warning(
                            f"[官方保存+缓存转正] platform_message_history 写入失败 "
                            f"(sender={cached_msg.get('sender_name', '未知')}): {e}"
                        )

            # 7. 添加当前用户消息（如果有）
            if user_message:
                history_list.append({"role": "user", "content": user_message})
                if DEBUG_MODE:
                    logger.debug(
                        f"[官方保存+缓存转正] 添加用户消息: {user_message[:50]}..."
                    )
            elif DEBUG_MODE:
                logger.debug(
                    "[官方保存+缓存转正] user_message为空，本次不添加用户消息到历史"
                )

            # 8. 添加AI回复（可选）
            if bot_message:
                history_list.append({"role": "assistant", "content": bot_message})
                if DEBUG_MODE:
                    logger.debug(
                        f"[官方保存+缓存转正] 添加AI回复: {bot_message[:50]}..."
                    )
            elif DEBUG_MODE:
                logger.debug(
                    "[官方保存+缓存转正] bot_message为空，本次不添加AI回复到历史"
                )

            if DEBUG_MODE:
                logger.debug(
                    f"[官方保存+缓存转正] 准备保存，总消息数: {len(history_list)} 条"
                )

            # 🔧 修复：限制历史长度，避免向量检索token溢出
            # 保留最近150条消息（约75轮对话），防止无限增长
            MAX_HISTORY_LENGTH = 150
            if len(history_list) > MAX_HISTORY_LENGTH:
                original_length = len(history_list)
                history_list = history_list[-MAX_HISTORY_LENGTH:]
                if DEBUG_MODE:
                    logger.debug(
                        f"[官方保存+缓存转正] ⚠️ 历史过长，已截断: {original_length} -> {MAX_HISTORY_LENGTH} 条"
                    )
                else:
                    logger.debug(
                        f"[官方保存+缓存转正] 历史截断: {original_length} -> {MAX_HISTORY_LENGTH} 条（避免向量检索溢出）"
                    )

            if DEBUG_MODE:
                logger.debug(
                    "[官方保存+缓存转正] ========== 调用底层保存方法 =========="
                )

            # 9. 使用官方API保存
            success = await ContextManager._try_official_save(
                cm, unified_msg_origin, curr_cid, history_list
            )

            if success:
                # 计算实际转正的缓存数量
                cache_converted = len(
                    [
                        m
                        for m in cached_messages
                        if isinstance(m, dict) and "content" in m
                    ]
                )

                if save_kind == "poke_event":
                    logger.debug("=" * 60)
                    logger.debug("✅ [官方保存+戳一戳事件] 额外保存成功！")
                    logger.debug(f"  对话ID: {curr_cid}")
                    logger.debug(f"  总消息数: {len(history_list)}")
                    logger.debug(
                        f"  事件类型: AI戳一戳事件（额外保存，未转正缓存{cache_converted}条）"
                    )
                    logger.debug("  新增消息: 用户0条 + AI1条")
                    logger.debug("=" * 60)
                else:
                    logger.debug("=" * 60)
                    logger.debug("✅✅✅ [官方保存+缓存转正] 保存成功！")
                    logger.debug(f"  对话ID: {curr_cid}")
                    logger.debug(f"  总消息数: {len(history_list)}")
                    logger.debug(f"  缓存转正: {cache_converted} 条")
                    added_ai = 1 if bot_message else 0
                    added_user = 1 if user_message else 0
                    logger.debug(f"  新增消息: 用户{added_user}条 + AI{added_ai}条")
                    logger.debug("=" * 60)
                return True
            else:
                logger.error("❌❌❌ [官方保存+缓存转正] 保存失败！所有方法均失败！")
                return False

        except Exception as e:
            logger.error(
                f"❌❌❌ [官方保存+缓存转正] 保存过程发生严重异常: {e}", exc_info=True
            )
            return False
