"""
指令过滤与缓存清理域（CommandMixin）。

从主插件类拆出的独立功能域：指令过滤器和图片缓存清理。
装饰器（@filter.command / @filter.event_message_type）由 AstrBot 以
functools.partial(方法, 实例) 显式绑定，故迁移到 mixin 后注册与分发不受影响。
"""

import time

from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import filter


class CommandMixin:
    async def command_filter_handler(self, event: AstrMessageEvent):
        """
        指令过滤处理器（超高优先级）

        检测到指令消息时标记该消息，让本插件的其他处理器跳过。
        """
        try:
            if self.enable_group_chat and not event.is_private_chat():
                if not self._is_enabled(event):
                    return

                current_time = time.time()
                expired_ids = [
                    mid
                    for mid, timestamp in self.command_messages.items()
                    if current_time - timestamp > 10
                ]
                for mid in expired_ids:
                    del self.command_messages[mid]

                if self._is_command_message(event):
                    msg_id = self._get_processing_id(event)
                    self.command_messages[msg_id] = current_time
                    return
        except Exception as e:
            logger.error(f"[指令过滤] 处理消息时发生错误: {e}", exc_info=True)
            return

    def _is_command_message(self, event: AstrMessageEvent) -> bool:
        """
        检测消息是否为指令消息（根据配置的指令前缀和完整指令列表）

        支持格式：
        1. /command 或 !command 等（直接以前缀开头）
        2. @机器人 /command（@ 机器人后跟指令）
        3. 完整指令字符串检测（全字符串匹配，去除@组件和空格）

        Returns:
            True=是指令消息（应跳过），False=不是指令消息
        """
        enable_filter = self.enable_command_filter
        if not enable_filter:
            return False

        command_prefixes = self.command_prefixes
        enable_full_cmd = self.enable_full_command_detection
        full_command_list = self.full_command_list
        enable_prefix_match = self.enable_command_prefix_match
        prefix_match_list = self.command_prefix_match_list

        has_prefix_filter = bool(command_prefixes)
        has_full_cmd = enable_full_cmd and bool(full_command_list)
        has_prefix_match = enable_prefix_match and bool(prefix_match_list)

        if not has_prefix_filter and not has_full_cmd and not has_prefix_match:
            return False

        try:
            original_messages = event.message_obj.message
            if not original_messages:
                return False

            # 第一步：检查指令前缀
            if command_prefixes:
                for component in original_messages:
                    if isinstance(component, Plain):
                        first_text = self._coerce_component_text(component.text).strip()
                        for prefix in command_prefixes:
                            if prefix and first_text.startswith(prefix):
                                return True
                        break

            # 第二步：检查完整指令字符串
            if enable_full_cmd and full_command_list:
                plain_texts = []
                for component in original_messages:
                    if isinstance(component, Plain):
                        plain_texts.append(self._coerce_component_text(component.text))
                combined_text = "".join(plain_texts)
                cleaned_text = "".join(combined_text.split())
                for cmd in full_command_list:
                    if not cmd:
                        continue
                    cleaned_cmd = "".join(str(cmd).split())
                    if cleaned_text == cleaned_cmd:
                        return True

            # 第三步：检查指令前缀匹配（避免误匹配如 'add' 匹配 'address'）
            if has_prefix_match:
                plain_texts = []
                for component in original_messages:
                    if isinstance(component, Plain):
                        plain_texts.append(self._coerce_component_text(component.text))
                combined_text = "".join(plain_texts)
                stripped_text = combined_text.lstrip()
                for cmd in prefix_match_list:
                    if not cmd:
                        continue
                    cmd_str = str(cmd).strip()
                    if not cmd_str:
                        continue
                    if stripped_text.startswith(cmd_str):
                        remaining = stripped_text[len(cmd_str) :]
                        if not remaining or remaining[0].isspace():
                            return True

            return False
        except Exception as e:
            logger.error(f"[指令检测] 发生错误: {e}", exc_info=True)
            return False

    @filter.command("gcp_clear_image_cache")
    async def gcp_clear_image_cache(self, event: AstrMessageEvent):
        """清除本地图片描述缓存并重启AstrBot。"""
        try:
            if event.is_private_chat():
                return
            if not hasattr(event, "message_obj") or not hasattr(
                event.message_obj, "message"
            ):
                return
            components = event.message_obj.message
            if not components:
                return
            if not all(isinstance(c, (Plain, At, AtAll)) for c in components):
                return
            whitelist = self.gcp_clear_image_cache_allowed_user_ids
            allow_all = not whitelist or len(whitelist) == 0
            sender_id = str(event.get_sender_id())
            allowed = allow_all or (str(sender_id) in {str(x) for x in whitelist})
            if not allowed:
                logger.info(
                    "【图片缓存清除】用户 %s 未在白名单中，指令被忽略", sender_id
                )
                return
            try:
                cleared = False
                if self.image_description_cache:
                    cleared = await self.image_description_cache.clear_async()
                notice = (
                    "【Persona Presence】图片描述缓存清除："
                    + ("成功" if cleared else "完成（缓存未启用或无缓存）")
                    + "\n即将重启 AstrBot..."
                )
                yield event.plain_result(notice)
                try:
                    await self.restart_core()
                except Exception as e:
                    yield event.plain_result(f"重启失败：{e}")
            except Exception:
                try:
                    yield event.plain_result(
                        "【Persona Presence】图片描述缓存清除：失败，请查看日志"
                    )
                except Exception:
                    pass
        except Exception:
            return
